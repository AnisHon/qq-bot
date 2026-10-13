from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from typing import Iterator

from app.recipes.models import (
    DatasetAvailability,
    DatasetState,
    DatasetStatus,
    Ingredient,
    IngredientInterpretation,
    IngredientKind,
    IngredientRole,
    PackVersionKey,
    RawRecipeRecord,
    Recipe,
    RecipeDataset,
    RecipeOutput,
    RecipeStatus,
    SourceLocator,
    SourceMetadata,
    validate_exact_identifier,
)
from app.recipes.source import ImportIssue, ImportIssueSeverity


_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class ActiveRecipeSnapshot:
    """One active dataset and its bounded query rows from a single read snapshot."""

    availability: DatasetAvailability
    dataset: RecipeDataset | None = None
    recipes: tuple[Recipe, ...] = ()
    source_locators: tuple[SourceLocator | None, ...] = ()
    total_matches: int = 0
    quarantined_recipe_count: int = 0


class RecipeRepositoryError(RuntimeError):
    """Base error for the structured recipe repository."""


class RecipeNotFound(RecipeRepositoryError):
    """A requested dataset record is absent."""


class RecipeQuarantined(RecipeRepositoryError):
    """A quarantined dataset cannot be made active."""


class RecipeRepository:
    """Additive SQLite storage for versioned recipe records and exact-ID indexes.

    Constructing this repository creates only recipe-owned tables. It does not
    inspect, import, or synthesize any Minecraft or GTNH data.
    """

    def __init__(self, sqlite_path: Path):
        self.sqlite_path = Path(sqlite_path)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.sqlite_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS recipe_schema_metadata (
                    schema_key TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL CHECK (schema_version > 0)
                );

                CREATE TABLE IF NOT EXISTS recipe_datasets (
                    dataset_id TEXT PRIMARY KEY,
                    pack_id TEXT NOT NULL,
                    pack_version TEXT NOT NULL,
                    game_version TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    exporter_id TEXT NOT NULL,
                    exporter_version TEXT,
                    importer_version TEXT NOT NULL,
                    license_id TEXT,
                    content_hash TEXT NOT NULL,
                    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
                    imported_at TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('staging','active','retired','quarantined'))
                );

                CREATE INDEX IF NOT EXISTS idx_recipe_datasets_pack_key
                    ON recipe_datasets(pack_id, pack_version, game_version, state);

                CREATE TABLE IF NOT EXISTS recipe_active_datasets (
                    pack_id TEXT NOT NULL,
                    pack_version TEXT NOT NULL,
                    game_version TEXT NOT NULL,
                    dataset_id TEXT NOT NULL UNIQUE,
                    PRIMARY KEY (pack_id, pack_version, game_version),
                    FOREIGN KEY (dataset_id) REFERENCES recipe_datasets(dataset_id)
                        ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS recipe_source_records (
                    dataset_id TEXT NOT NULL,
                    source_record_id TEXT NOT NULL,
                    source_uri TEXT NOT NULL,
                    source_line INTEGER,
                    source_json_pointer TEXT,
                    source_record_key TEXT,
                    raw_payload BLOB NOT NULL CHECK (length(raw_payload) <= 4194304),
                    raw_hash TEXT NOT NULL,
                    parsed_fields_json TEXT NOT NULL,
                    unknown_fields_json TEXT NOT NULL,
                    critical_unknown_fields_json TEXT NOT NULL,
                    PRIMARY KEY (dataset_id, source_record_id),
                    FOREIGN KEY (dataset_id) REFERENCES recipe_datasets(dataset_id)
                        ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS recipe_import_issues (
                    dataset_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
                    code TEXT NOT NULL,
                    message TEXT NOT NULL,
                    severity TEXT NOT NULL CHECK (severity IN ('warning','error')),
                    source_record_id TEXT,
                    source_uri TEXT,
                    source_line INTEGER,
                    source_json_pointer TEXT,
                    source_record_key TEXT,
                    PRIMARY KEY (dataset_id, ordinal),
                    FOREIGN KEY (dataset_id) REFERENCES recipe_datasets(dataset_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS recipes (
                    dataset_id TEXT NOT NULL,
                    recipe_id TEXT NOT NULL,
                    source_record_id TEXT NOT NULL,
                    source_uri TEXT,
                    source_line INTEGER,
                    source_json_pointer TEXT,
                    source_record_key TEXT,
                    raw_source_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('accepted','conditional','quarantined')),
                    energy_eut INTEGER CHECK (energy_eut IS NULL OR energy_eut >= 0),
                    duration_ticks INTEGER CHECK (duration_ticks IS NULL OR duration_ticks >= 0),
                    conditions_json TEXT NOT NULL,
                    critical_unknown_fields_json TEXT NOT NULL,
                    quarantine_reasons_json TEXT NOT NULL,
                    PRIMARY KEY (dataset_id, recipe_id),
                    FOREIGN KEY (dataset_id, source_record_id)
                        REFERENCES recipe_source_records(dataset_id, source_record_id)
                        ON DELETE RESTRICT
                );

                CREATE INDEX IF NOT EXISTS idx_recipes_dataset_status
                    ON recipes(dataset_id, status, recipe_id);

                CREATE TABLE IF NOT EXISTS recipe_machines (
                    dataset_id TEXT NOT NULL,
                    recipe_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
                    machine_id TEXT NOT NULL,
                    PRIMARY KEY (dataset_id, recipe_id, ordinal),
                    UNIQUE (dataset_id, recipe_id, machine_id),
                    FOREIGN KEY (dataset_id, recipe_id)
                        REFERENCES recipes(dataset_id, recipe_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_recipe_machines_machine
                    ON recipe_machines(machine_id, dataset_id, recipe_id);

                CREATE TABLE IF NOT EXISTS recipe_ingredients (
                    dataset_id TEXT NOT NULL,
                    recipe_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
                    kind TEXT NOT NULL CHECK (kind IN ('item_exact','fluid_exact','tag','ore_dictionary','predicate','source_extension')),
                    value TEXT,
                    amount_decimal TEXT,
                    unit TEXT,
                    role TEXT NOT NULL CHECK (role IN ('material','tool','catalyst','condition')),
                    consumed INTEGER CHECK (consumed IS NULL OR consumed IN (0,1)),
                    constraints_json TEXT,
                    interpretation TEXT NOT NULL CHECK (interpretation IN ('exact','conditional','unsupported')),
                    source_uri TEXT,
                    source_line INTEGER,
                    source_json_pointer TEXT,
                    source_record_key TEXT,
                    source_fields_json TEXT NOT NULL,
                    PRIMARY KEY (dataset_id, recipe_id, ordinal),
                    FOREIGN KEY (dataset_id, recipe_id)
                        REFERENCES recipes(dataset_id, recipe_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_exact
                    ON recipe_ingredients(kind, value, interpretation, dataset_id, recipe_id);

                CREATE TABLE IF NOT EXISTS recipe_outputs (
                    dataset_id TEXT NOT NULL,
                    recipe_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
                    kind TEXT NOT NULL CHECK (kind IN ('item_exact','fluid_exact','tag','ore_dictionary','predicate','source_extension')),
                    value TEXT,
                    amount_decimal TEXT,
                    unit TEXT,
                    probability_decimal TEXT NOT NULL,
                    constraints_json TEXT,
                    interpretation TEXT NOT NULL CHECK (interpretation IN ('exact','conditional','unsupported')),
                    source_uri TEXT,
                    source_line INTEGER,
                    source_json_pointer TEXT,
                    source_record_key TEXT,
                    source_fields_json TEXT NOT NULL,
                    PRIMARY KEY (dataset_id, recipe_id, ordinal),
                    FOREIGN KEY (dataset_id, recipe_id)
                        REFERENCES recipes(dataset_id, recipe_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_recipe_outputs_exact
                    ON recipe_outputs(kind, value, interpretation, dataset_id, recipe_id);
                """
            )
            row = conn.execute(
                "SELECT schema_version FROM recipe_schema_metadata WHERE schema_key = 'recipes'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO recipe_schema_metadata(schema_key, schema_version) VALUES ('recipes', ?)",
                    (_SCHEMA_VERSION,),
                )
            elif int(row["schema_version"]) > _SCHEMA_VERSION:
                raise RecipeRepositoryError("recipe database schema is newer than this application")
            elif int(row["schema_version"]) < _SCHEMA_VERSION:
                if int(row["schema_version"]) == 1:
                    # P4-02 adds only the import-diagnostics table above; existing
                    # recipe rows and the active dataset pointer remain untouched.
                    conn.execute(
                        "UPDATE recipe_schema_metadata SET schema_version = ? WHERE schema_key = 'recipes'",
                        (_SCHEMA_VERSION,),
                    )
                else:
                    raise RecipeRepositoryError("recipe database schema requires an explicit migration")

    @staticmethod
    def _json(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _locator_values(locator: SourceLocator | None) -> tuple[object, object, object, object]:
        if locator is None:
            return (None, None, None, None)
        return (locator.uri, locator.line, locator.json_pointer, locator.record_key)

    @staticmethod
    def _locator_from_row(row: sqlite3.Row, prefix: str = "source_") -> SourceLocator | None:
        uri = row[f"{prefix}uri"]
        if uri is None:
            return None
        return SourceLocator(
            uri=uri,
            line=row[f"{prefix}line"],
            json_pointer=row[f"{prefix}json_pointer"],
            record_key=row[f"{prefix}record_key"],
        )

    def add_dataset(self, dataset: RecipeDataset) -> None:
        if not isinstance(dataset, RecipeDataset):
            raise TypeError("dataset must be a RecipeDataset")
        if dataset.state == DatasetState.ACTIVE:
            raise ValueError("activate datasets through set_active_dataset")
        source = dataset.source
        with self._connect(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO recipe_datasets (
                    dataset_id, pack_id, pack_version, game_version,
                    source_id, source_revision, exporter_id, exporter_version,
                    importer_version, license_id, content_hash, schema_version,
                    imported_at, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dataset.dataset_id,
                    dataset.key.pack_id,
                    dataset.key.pack_version,
                    dataset.key.game_version,
                    source.source_id,
                    source.source_revision,
                    source.exporter_id,
                    source.exporter_version,
                    source.importer_version,
                    source.license_id,
                    source.content_hash,
                    source.schema_version,
                    source.imported_at,
                    dataset.state.value,
                ),
            )

    def add_raw_record(self, record: RawRecipeRecord) -> None:
        if not isinstance(record, RawRecipeRecord):
            raise TypeError("record must be a RawRecipeRecord")
        with self._connect(immediate=True) as conn:
            self._insert_raw_record(conn, record)

    def _insert_raw_record(self, conn: sqlite3.Connection, record: RawRecipeRecord) -> None:
        conn.execute(
            """
            INSERT INTO recipe_source_records (
                dataset_id, source_record_id, source_uri, source_line,
                source_json_pointer, source_record_key, raw_payload, raw_hash,
                parsed_fields_json, unknown_fields_json, critical_unknown_fields_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.dataset_id,
                record.source_record_id,
                *self._locator_values(record.source_locator),
                sqlite3.Binary(record.raw_payload),
                record.raw_hash,
                self._json(record.parsed_fields),
                self._json(record.unknown_fields),
                self._json(record.critical_unknown_fields),
            ),
        )

    def get_raw_record(self, dataset_id: str, source_record_id: str) -> RawRecipeRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM recipe_source_records WHERE dataset_id = ? AND source_record_id = ?",
                (dataset_id, source_record_id),
            ).fetchone()
        if row is None:
            return None
        return RawRecipeRecord(
            dataset_id=row["dataset_id"],
            source_record_id=row["source_record_id"],
            source_locator=self._locator_from_row(row),
            raw_payload=bytes(row["raw_payload"]),
            raw_hash=row["raw_hash"],
            parsed_fields=json.loads(row["parsed_fields_json"]),
            unknown_fields=json.loads(row["unknown_fields_json"]),
            critical_unknown_fields=tuple(json.loads(row["critical_unknown_fields_json"])),
        )

    def add_recipe(self, recipe: Recipe) -> Recipe:
        if not isinstance(recipe, Recipe):
            raise TypeError("recipe must be a Recipe")
        with self._connect(immediate=True) as conn:
            return self._insert_recipe(conn, recipe)

    def _insert_recipe(self, conn: sqlite3.Connection, recipe: Recipe) -> Recipe:
        raw = conn.execute(
            """
            SELECT raw_hash, critical_unknown_fields_json
            FROM recipe_source_records WHERE dataset_id = ? AND source_record_id = ?
            """,
            (recipe.dataset_id, recipe.source_record_id),
        ).fetchone()
        if raw is None:
            raise RecipeRepositoryError("recipe must reference a stored raw source record")
        if raw["raw_hash"] != recipe.raw_source_hash:
            raise RecipeRepositoryError("recipe raw source hash does not match its source record")
        effective_recipe = self._revalidate_recipe(
            recipe,
            tuple(json.loads(raw["critical_unknown_fields_json"])),
        )
        conn.execute(
            """
            INSERT INTO recipes (
                dataset_id, recipe_id, source_record_id, source_uri, source_line,
                source_json_pointer, source_record_key, raw_source_hash, status,
                energy_eut, duration_ticks, conditions_json,
                critical_unknown_fields_json, quarantine_reasons_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                effective_recipe.dataset_id,
                effective_recipe.recipe_id,
                effective_recipe.source_record_id,
                *self._locator_values(effective_recipe.source_locator),
                effective_recipe.raw_source_hash,
                effective_recipe.status.value,
                effective_recipe.energy_eut,
                effective_recipe.duration_ticks,
                self._json(effective_recipe.conditions),
                self._json(effective_recipe.critical_unknown_fields),
                self._json(effective_recipe.quarantine_reasons),
            ),
        )
        conn.executemany(
            "INSERT INTO recipe_machines(dataset_id, recipe_id, ordinal, machine_id) VALUES (?, ?, ?, ?)",
            (
                (effective_recipe.dataset_id, effective_recipe.recipe_id, index, machine)
                for index, machine in enumerate(effective_recipe.machine_ids)
            ),
        )
        conn.executemany(
            """
            INSERT INTO recipe_ingredients (
                dataset_id, recipe_id, ordinal, kind, value, amount_decimal, unit,
                role, consumed, constraints_json, interpretation, source_uri,
                source_line, source_json_pointer, source_record_key, source_fields_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._ingredient_row(effective_recipe, index, item)
                for index, item in enumerate(effective_recipe.inputs)
            ),
        )
        conn.executemany(
            """
            INSERT INTO recipe_outputs (
                dataset_id, recipe_id, ordinal, kind, value, amount_decimal, unit,
                probability_decimal, constraints_json, interpretation, source_uri,
                source_line, source_json_pointer, source_record_key, source_fields_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._output_row(effective_recipe, index, item)
                for index, item in enumerate(effective_recipe.outputs)
            ),
        )
        return effective_recipe

    @staticmethod
    def _revalidate_recipe(recipe: Recipe, raw_critical_fields: tuple[str, ...]) -> Recipe:
        """Rebuild mutable JSON-backed values and derive status at the storage boundary."""
        inputs = tuple(
            Ingredient(
                kind=item.kind,
                value=item.value,
                amount=item.amount,
                unit=item.unit,
                role=item.role,
                consumed=item.consumed,
                constraints=item.constraints,
                interpretation=item.interpretation,
                source_locator=item.source_locator,
                source_fields=item.source_fields,
            )
            for item in recipe.inputs
        )
        outputs = tuple(
            RecipeOutput(
                kind=item.kind,
                value=item.value,
                amount=item.amount,
                unit=item.unit,
                probability=item.probability,
                constraints=item.constraints,
                interpretation=item.interpretation,
                source_locator=item.source_locator,
                source_fields=item.source_fields,
            )
            for item in recipe.outputs
        )
        critical_fields = tuple(
            dict.fromkeys((*recipe.critical_unknown_fields, *raw_critical_fields))
        )
        return Recipe(
            dataset_id=recipe.dataset_id,
            recipe_id=recipe.recipe_id,
            source_record_id=recipe.source_record_id,
            raw_source_hash=recipe.raw_source_hash,
            machine_ids=recipe.machine_ids,
            inputs=inputs,
            outputs=outputs,
            energy_eut=recipe.energy_eut,
            duration_ticks=recipe.duration_ticks,
            conditions=recipe.conditions,
            critical_unknown_fields=critical_fields,
            status=None,
            quarantine_reasons=recipe.quarantine_reasons,
            source_locator=recipe.source_locator,
        )

    def stage_import_snapshot(
        self,
        dataset: RecipeDataset,
        raw_records: tuple[RawRecipeRecord, ...],
        recipes: tuple[Recipe, ...],
        issues: tuple[ImportIssue, ...],
    ) -> None:
        """Atomically replace one non-active staging snapshot and its diagnostics.

        A retry with the same dataset identity replaces the earlier staging
        attempt in one SQLite transaction. Existing active datasets are never
        modified by this operation.
        """
        if not isinstance(dataset, RecipeDataset):
            raise TypeError("dataset must be a RecipeDataset")
        if any(not isinstance(record, RawRecipeRecord) for record in raw_records):
            raise TypeError("raw_records must contain RawRecipeRecord values")
        if any(not isinstance(recipe, Recipe) for recipe in recipes):
            raise TypeError("recipes must contain Recipe values")
        if any(not isinstance(issue, ImportIssue) for issue in issues):
            raise TypeError("issues must contain ImportIssue values")
        staged_state = (
            DatasetState.QUARANTINED
            if any(issue.severity == ImportIssueSeverity.ERROR for issue in issues)
            else DatasetState.STAGING
        )
        source = dataset.source
        expected_identity = (
            dataset.key.pack_id,
            dataset.key.pack_version,
            dataset.key.game_version,
            source.source_id,
            source.source_revision,
            source.exporter_id,
            source.exporter_version,
            source.importer_version,
            source.license_id,
            source.content_hash,
            source.schema_version,
        )
        with self._connect(immediate=True) as conn:
            existing = conn.execute(
                "SELECT * FROM recipe_datasets WHERE dataset_id = ?", (dataset.dataset_id,)
            ).fetchone()
            if existing is not None:
                existing_identity = tuple(
                    existing[column]
                    for column in (
                        "pack_id", "pack_version", "game_version", "source_id",
                        "source_revision", "exporter_id", "exporter_version",
                        "importer_version", "license_id", "content_hash", "schema_version",
                    )
                )
                active = conn.execute(
                    "SELECT 1 FROM recipe_active_datasets WHERE dataset_id = ?",
                    (dataset.dataset_id,),
                ).fetchone()
                if active is not None or existing["state"] == DatasetState.ACTIVE.value:
                    raise RecipeRepositoryError("an active dataset cannot be replaced by a staging import")
                if existing_identity != expected_identity:
                    raise RecipeRepositoryError("dataset_id already belongs to a different source snapshot")
                conn.execute("DELETE FROM recipes WHERE dataset_id = ?", (dataset.dataset_id,))
                conn.execute("DELETE FROM recipe_source_records WHERE dataset_id = ?", (dataset.dataset_id,))
                conn.execute("DELETE FROM recipe_import_issues WHERE dataset_id = ?", (dataset.dataset_id,))
                conn.execute(
                    "UPDATE recipe_datasets SET state = ? WHERE dataset_id = ?",
                    (staged_state.value, dataset.dataset_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO recipe_datasets (
                        dataset_id, pack_id, pack_version, game_version,
                        source_id, source_revision, exporter_id, exporter_version,
                        importer_version, license_id, content_hash, schema_version,
                        imported_at, state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        dataset.dataset_id,
                        dataset.key.pack_id,
                        dataset.key.pack_version,
                        dataset.key.game_version,
                        source.source_id,
                        source.source_revision,
                        source.exporter_id,
                        source.exporter_version,
                        source.importer_version,
                        source.license_id,
                        source.content_hash,
                        source.schema_version,
                        source.imported_at,
                        staged_state.value,
                    ),
                )
            for record in raw_records:
                if record.dataset_id != dataset.dataset_id:
                    raise RecipeRepositoryError("raw source record does not match the staging dataset")
                self._insert_raw_record(conn, record)
            for recipe in recipes:
                if recipe.dataset_id != dataset.dataset_id:
                    raise RecipeRepositoryError("recipe does not match the staging dataset")
                self._insert_recipe(conn, recipe)
            conn.executemany(
                """
                INSERT INTO recipe_import_issues (
                    dataset_id, ordinal, code, message, severity, source_record_id,
                    source_uri, source_line, source_json_pointer, source_record_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        dataset.dataset_id,
                        index,
                        issue.code,
                        issue.message,
                        issue.severity.value,
                        issue.source_record_id,
                        *self._locator_values(issue.source_locator),
                    )
                    for index, issue in enumerate(issues)
                ),
            )

    def import_issues(self, dataset_id: str) -> tuple[ImportIssue, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM recipe_import_issues WHERE dataset_id = ? ORDER BY ordinal",
                (dataset_id,),
            ).fetchall()
        issues: list[ImportIssue] = []
        for row in rows:
            locator = self._locator_from_row(row)
            issues.append(
                ImportIssue(
                    code=row["code"],
                    message=row["message"],
                    severity=ImportIssueSeverity(row["severity"]),
                    source_record_id=row["source_record_id"],
                    source_locator=locator,
                )
            )
        return tuple(issues)

    def _ingredient_row(self, recipe: Recipe, index: int, item: Ingredient) -> tuple[object, ...]:
        locator = self._locator_values(item.source_locator)
        return (
            recipe.dataset_id,
            recipe.recipe_id,
            index,
            item.kind.value,
            item.value,
            str(item.amount) if item.amount is not None else None,
            item.unit,
            item.role.value,
            None if item.consumed is None else int(item.consumed),
            self._json(item.constraints) if item.constraints is not None else None,
            item.interpretation.value,
            *locator,
            self._json(item.source_fields),
        )

    def _output_row(self, recipe: Recipe, index: int, item: RecipeOutput) -> tuple[object, ...]:
        locator = self._locator_values(item.source_locator)
        return (
            recipe.dataset_id,
            recipe.recipe_id,
            index,
            item.kind.value,
            item.value,
            str(item.amount) if item.amount is not None else None,
            item.unit,
            str(item.probability),
            self._json(item.constraints) if item.constraints is not None else None,
            item.interpretation.value,
            *locator,
            self._json(item.source_fields),
        )

    def set_active_dataset(self, dataset_id: str) -> None:
        """Atomically move the active pointer for one exact pack/version key.

        P4-02's importer will call this only after staging validation succeeds.
        A quarantined dataset is never exposed to normal lookup paths.
        """
        with self._connect(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM recipe_datasets WHERE dataset_id = ?", (dataset_id,)
            ).fetchone()
            if row is None:
                raise RecipeNotFound("dataset does not exist")
            if row["state"] == DatasetState.QUARANTINED.value:
                raise RecipeQuarantined("quarantined dataset cannot be activated")
            old = conn.execute(
                """
                SELECT dataset_id FROM recipe_active_datasets
                WHERE pack_id = ? AND pack_version = ? AND game_version = ?
                """,
                (row["pack_id"], row["pack_version"], row["game_version"]),
            ).fetchone()
            if old is not None and old["dataset_id"] != dataset_id:
                conn.execute(
                    "UPDATE recipe_datasets SET state = ? WHERE dataset_id = ?",
                    (DatasetState.RETIRED.value, old["dataset_id"]),
                )
            conn.execute(
                "UPDATE recipe_datasets SET state = ? WHERE dataset_id = ?",
                (DatasetState.ACTIVE.value, dataset_id),
            )
            conn.execute(
                """
                INSERT INTO recipe_active_datasets(pack_id, pack_version, game_version, dataset_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(pack_id, pack_version, game_version)
                DO UPDATE SET dataset_id = excluded.dataset_id
                """,
                (row["pack_id"], row["pack_version"], row["game_version"], dataset_id),
            )

    @staticmethod
    def _dataset_from_row(row: sqlite3.Row) -> RecipeDataset:
        return RecipeDataset(
            dataset_id=row["dataset_id"],
            key=PackVersionKey(row["pack_id"], row["pack_version"], row["game_version"]),
            source=SourceMetadata(
                source_id=row["source_id"],
                source_revision=row["source_revision"],
                exporter_id=row["exporter_id"],
                exporter_version=row["exporter_version"],
                license_id=row["license_id"],
                content_hash=row["content_hash"],
                schema_version=row["schema_version"],
                importer_version=row["importer_version"],
                imported_at=row["imported_at"],
            ),
            state=DatasetState(row["state"]),
        )

    def active_dataset(self, key: PackVersionKey) -> RecipeDataset | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT d.* FROM recipe_active_datasets a
                JOIN recipe_datasets d ON d.dataset_id = a.dataset_id
                WHERE a.pack_id = ? AND a.pack_version = ? AND a.game_version = ?
                """,
                (key.pack_id, key.pack_version, key.game_version),
            ).fetchone()
        return self._dataset_from_row(row) if row else None

    def active_dataset_keys(self) -> tuple[PackVersionKey, ...]:
        """List only exact pack/version keys that currently have an active dataset."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT pack_id, pack_version, game_version
                FROM recipe_active_datasets
                ORDER BY pack_id, pack_version, game_version
                """
            ).fetchall()
        return tuple(PackVersionKey(row["pack_id"], row["pack_version"], row["game_version"]) for row in rows)

    def dataset_status(self, key: PackVersionKey) -> DatasetStatus:
        with self._connect() as conn:
            active = conn.execute(
                """
                SELECT d.dataset_id,
                       SUM(CASE WHEN r.status IN ('accepted','conditional') THEN 1 ELSE 0 END) AS queryable_count,
                       SUM(CASE WHEN r.status = 'quarantined' THEN 1 ELSE 0 END) AS quarantined_count
                FROM recipe_active_datasets a
                JOIN recipe_datasets d ON d.dataset_id = a.dataset_id
                LEFT JOIN recipes r ON r.dataset_id = d.dataset_id
                WHERE a.pack_id = ? AND a.pack_version = ? AND a.game_version = ?
                GROUP BY d.dataset_id
                """,
                (key.pack_id, key.pack_version, key.game_version),
            ).fetchone()
            if active is not None:
                queryable = int(active["queryable_count"] or 0)
                return DatasetStatus(
                    key=key,
                    availability=DatasetAvailability.AVAILABLE if queryable else DatasetAvailability.EMPTY,
                    dataset_id=active["dataset_id"],
                    queryable_recipe_count=queryable,
                    quarantined_recipe_count=int(active["quarantined_count"] or 0),
                )
            exact_staged = conn.execute(
                """
                SELECT COUNT(*) AS count FROM recipe_datasets
                WHERE pack_id = ? AND pack_version = ? AND game_version = ?
                """,
                (key.pack_id, key.pack_version, key.game_version),
            ).fetchone()["count"]
            same_pack = conn.execute(
                "SELECT COUNT(*) AS count FROM recipe_datasets WHERE pack_id = ?",
                (key.pack_id,),
            ).fetchone()["count"]
        return DatasetStatus(
            key=key,
            availability=(
                DatasetAvailability.DATA_UNAVAILABLE
                if exact_staged or not same_pack
                else DatasetAvailability.VERSION_MISMATCH
            ),
        )

    def get_recipe(
        self,
        dataset_id: str,
        recipe_id: str,
        *,
        include_quarantined: bool = False,
    ) -> Recipe | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM recipes WHERE dataset_id = ? AND recipe_id = ?",
                (dataset_id, recipe_id),
            ).fetchone()
            if row is None or (row["status"] == RecipeStatus.QUARANTINED.value and not include_quarantined):
                return None
            return self._recipe_from_row(conn, row)

    def list_active_recipes(self, key: PackVersionKey) -> tuple[Recipe, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT r.* FROM recipe_active_datasets a
                JOIN recipes r ON r.dataset_id = a.dataset_id
                WHERE a.pack_id = ? AND a.pack_version = ? AND a.game_version = ?
                  AND r.status IN ('accepted','conditional')
                ORDER BY r.recipe_id
                """,
                (key.pack_id, key.pack_version, key.game_version),
            ).fetchall()
            return tuple(self._recipe_from_row(conn, row) for row in rows)

    def find_producing_exact(
        self,
        key: PackVersionKey,
        identifier: str,
        *,
        kind: IngredientKind = IngredientKind.ITEM_EXACT,
    ) -> tuple[Recipe, ...]:
        if kind not in {IngredientKind.ITEM_EXACT, IngredientKind.FLUID_EXACT}:
            raise ValueError("exact production lookup only accepts exact item or fluid kinds")
        return self._find_exact(key, kind, identifier, "recipe_outputs")

    def find_consuming_exact(
        self,
        key: PackVersionKey,
        identifier: str,
        *,
        kind: IngredientKind = IngredientKind.ITEM_EXACT,
    ) -> tuple[Recipe, ...]:
        if kind not in {IngredientKind.ITEM_EXACT, IngredientKind.FLUID_EXACT}:
            raise ValueError("exact consumption lookup only accepts exact item or fluid kinds")
        return self._find_exact(key, kind, identifier, "recipe_ingredients")

    def _find_exact(
        self,
        key: PackVersionKey,
        kind: IngredientKind,
        identifier: str,
        table: str,
    ) -> tuple[Recipe, ...]:
        if kind in {IngredientKind.ITEM_EXACT, IngredientKind.FLUID_EXACT}:
            validate_exact_identifier(kind, identifier)
        if table not in {"recipe_outputs", "recipe_ingredients"}:
            raise ValueError("unsupported exact query table")
        with self._connect() as conn:
            consumed_filter = "AND i.consumed = 1" if table == "recipe_ingredients" else ""
            rows = conn.execute(
                f"""
                SELECT DISTINCT r.* FROM recipe_active_datasets a
                JOIN {table} i ON i.dataset_id = a.dataset_id
                JOIN recipes r ON r.dataset_id = i.dataset_id AND r.recipe_id = i.recipe_id
                WHERE a.pack_id = ? AND a.pack_version = ? AND a.game_version = ?
                  AND i.kind = ? AND i.value = ? AND i.interpretation = 'exact'
                  {consumed_filter}
                  AND r.status IN ('accepted','conditional')
                ORDER BY r.recipe_id
                """,
                (key.pack_id, key.pack_version, key.game_version, kind.value, identifier),
            ).fetchall()
            return tuple(self._recipe_from_row(conn, row) for row in rows)

    def find_by_machine(self, key: PackVersionKey, machine_id: str) -> tuple[Recipe, ...]:
        if not isinstance(machine_id, str) or not machine_id.strip():
            raise ValueError("machine_id must be non-empty")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT r.* FROM recipe_active_datasets a
                JOIN recipe_machines m ON m.dataset_id = a.dataset_id
                JOIN recipes r ON r.dataset_id = m.dataset_id AND r.recipe_id = m.recipe_id
                WHERE a.pack_id = ? AND a.pack_version = ? AND a.game_version = ?
                  AND m.machine_id = ? AND r.status IN ('accepted','conditional')
                ORDER BY r.recipe_id
                """,
                (key.pack_id, key.pack_version, key.game_version, machine_id),
            ).fetchall()
            return tuple(self._recipe_from_row(conn, row) for row in rows)

    def query_active_exact(
        self,
        key: PackVersionKey,
        identifier: str,
        *,
        direction: str,
        kind: IngredientKind,
        limit: int = 10,
        offset: int = 0,
    ) -> ActiveRecipeSnapshot:
        """Query one exact ID against one active revision, with SQL-level bounds."""
        if direction not in {"produces", "consumes"}:
            raise ValueError("direction must be produces or consumes")
        if kind not in {IngredientKind.ITEM_EXACT, IngredientKind.FLUID_EXACT}:
            raise ValueError("exact lookup requires an item or fluid kind")
        validate_exact_identifier(kind, identifier)
        self._validate_query_page(limit, offset)
        table = "recipe_outputs" if direction == "produces" else "recipe_ingredients"
        alias = "o" if direction == "produces" else "i"
        consumed_filter = "AND i.consumed = 1" if direction == "consumes" else ""
        query_filter = f"{alias}.kind = ? AND {alias}.value = ? AND {alias}.interpretation = 'exact' {consumed_filter}"
        return self._query_active_snapshot(
            key,
            count_sql=f"""
                SELECT COUNT(DISTINCT r.recipe_id)
                FROM {table} {alias}
                JOIN recipes r ON r.dataset_id = {alias}.dataset_id AND r.recipe_id = {alias}.recipe_id
                WHERE r.dataset_id = ? AND {query_filter}
                  AND r.status IN ('accepted','conditional')
            """,
            rows_sql=f"""
                SELECT DISTINCT r.*
                FROM {table} {alias}
                JOIN recipes r ON r.dataset_id = {alias}.dataset_id AND r.recipe_id = {alias}.recipe_id
                WHERE r.dataset_id = ? AND {query_filter}
                  AND r.status IN ('accepted','conditional')
                ORDER BY r.recipe_id LIMIT ? OFFSET ?
            """,
            query_parameters=(kind.value, identifier),
            limit=limit,
            offset=offset,
        )

    def query_active_machine(
        self,
        key: PackVersionKey,
        machine_id: str,
        *,
        limit: int = 10,
        offset: int = 0,
    ) -> ActiveRecipeSnapshot:
        if not isinstance(machine_id, str) or not machine_id.strip():
            raise ValueError("machine_id must be non-empty")
        self._validate_query_page(limit, offset)
        return self._query_active_snapshot(
            key,
            count_sql="""
                SELECT COUNT(DISTINCT r.recipe_id)
                FROM recipe_machines m
                JOIN recipes r ON r.dataset_id = m.dataset_id AND r.recipe_id = m.recipe_id
                WHERE r.dataset_id = ? AND m.machine_id = ?
                  AND r.status IN ('accepted','conditional')
            """,
            rows_sql="""
                SELECT DISTINCT r.*
                FROM recipe_machines m
                JOIN recipes r ON r.dataset_id = m.dataset_id AND r.recipe_id = m.recipe_id
                WHERE r.dataset_id = ? AND m.machine_id = ?
                  AND r.status IN ('accepted','conditional')
                ORDER BY r.recipe_id LIMIT ? OFFSET ?
            """,
            query_parameters=(machine_id,),
            limit=limit,
            offset=offset,
        )

    @staticmethod
    def _validate_query_page(limit: int, offset: int) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise ValueError("query limit must be between 1 and 20")
        if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= 100_000:
            raise ValueError("query offset must be between 0 and 100000")

    def _query_active_snapshot(
        self,
        key: PackVersionKey,
        *,
        count_sql: str,
        rows_sql: str,
        query_parameters: tuple[object, ...],
        limit: int,
        offset: int,
    ) -> ActiveRecipeSnapshot:
        # An explicit read transaction keeps the active pointer, metadata,
        # counts, recipe rows and source locators on the same SQLite snapshot.
        with self._connect() as conn:
            conn.execute("BEGIN")
            active = conn.execute(
                """
                SELECT d.*,
                       SUM(CASE WHEN r.status IN ('accepted','conditional') THEN 1 ELSE 0 END) AS queryable_count,
                       SUM(CASE WHEN r.status = 'quarantined' THEN 1 ELSE 0 END) AS quarantined_count
                FROM recipe_active_datasets a
                JOIN recipe_datasets d ON d.dataset_id = a.dataset_id
                LEFT JOIN recipes r ON r.dataset_id = d.dataset_id
                WHERE a.pack_id = ? AND a.pack_version = ? AND a.game_version = ?
                GROUP BY d.dataset_id
                """,
                (key.pack_id, key.pack_version, key.game_version),
            ).fetchone()
            if active is None:
                exact_staged = conn.execute(
                    """
                    SELECT COUNT(*) FROM recipe_datasets
                    WHERE pack_id = ? AND pack_version = ? AND game_version = ?
                    """,
                    (key.pack_id, key.pack_version, key.game_version),
                ).fetchone()[0]
                same_pack = conn.execute(
                    "SELECT COUNT(*) FROM recipe_datasets WHERE pack_id = ?",
                    (key.pack_id,),
                ).fetchone()[0]
                availability = (
                    DatasetAvailability.DATA_UNAVAILABLE
                    if exact_staged or not same_pack
                    else DatasetAvailability.VERSION_MISMATCH
                )
                return ActiveRecipeSnapshot(availability=availability)

            dataset = self._dataset_from_row(active)
            queryable_count = int(active["queryable_count"] or 0)
            quarantined_count = int(active["quarantined_count"] or 0)
            if not queryable_count:
                return ActiveRecipeSnapshot(
                    availability=DatasetAvailability.EMPTY,
                    dataset=dataset,
                    quarantined_recipe_count=quarantined_count,
                )

            parameters = (dataset.dataset_id, *query_parameters)
            total = int(conn.execute(count_sql, parameters).fetchone()[0])
            rows = conn.execute(rows_sql, (*parameters, limit, offset)).fetchall()
            recipes = tuple(self._recipe_from_row(conn, row) for row in rows)
            raw_locators: dict[str, SourceLocator | None] = {}
            if recipes:
                source_record_ids = tuple(dict.fromkeys(recipe.source_record_id for recipe in recipes))
                placeholders = ",".join("?" for _ in source_record_ids)
                locator_rows = conn.execute(
                    f"""
                    SELECT source_record_id, source_uri, source_line,
                           source_json_pointer, source_record_key
                    FROM recipe_source_records
                    WHERE dataset_id = ? AND source_record_id IN ({placeholders})
                    """,
                    (dataset.dataset_id, *source_record_ids),
                ).fetchall()
                raw_locators = {
                    row["source_record_id"]: self._locator_from_row(row)
                    for row in locator_rows
                }
            locators = tuple(
                self._locator_from_row(row) or raw_locators.get(recipe.source_record_id)
                for recipe, row in zip(recipes, rows)
            )
            return ActiveRecipeSnapshot(
                availability=DatasetAvailability.AVAILABLE,
                dataset=dataset,
                recipes=recipes,
                source_locators=locators,
                total_matches=total,
                quarantined_recipe_count=quarantined_count,
            )

    def quarantined_recipes(self, dataset_id: str) -> tuple[Recipe, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM recipes WHERE dataset_id = ? AND status = 'quarantined' ORDER BY recipe_id",
                (dataset_id,),
            ).fetchall()
            return tuple(self._recipe_from_row(conn, row) for row in rows)

    def _recipe_from_row(self, conn: sqlite3.Connection, row: sqlite3.Row) -> Recipe:
        dataset_id, recipe_id = row["dataset_id"], row["recipe_id"]
        machines = conn.execute(
            "SELECT machine_id FROM recipe_machines WHERE dataset_id = ? AND recipe_id = ? ORDER BY ordinal",
            (dataset_id, recipe_id),
        ).fetchall()
        input_rows = conn.execute(
            "SELECT * FROM recipe_ingredients WHERE dataset_id = ? AND recipe_id = ? ORDER BY ordinal",
            (dataset_id, recipe_id),
        ).fetchall()
        output_rows = conn.execute(
            "SELECT * FROM recipe_outputs WHERE dataset_id = ? AND recipe_id = ? ORDER BY ordinal",
            (dataset_id, recipe_id),
        ).fetchall()
        inputs = tuple(
            Ingredient(
                kind=IngredientKind(item["kind"]),
                value=item["value"],
                amount=Decimal(item["amount_decimal"]) if item["amount_decimal"] is not None else None,
                unit=item["unit"],
                role=IngredientRole(item["role"]),
                consumed=None if item["consumed"] is None else bool(item["consumed"]),
                constraints=json.loads(item["constraints_json"]) if item["constraints_json"] is not None else None,
                interpretation=IngredientInterpretation(item["interpretation"]),
                source_locator=self._locator_from_row(item),
                source_fields=json.loads(item["source_fields_json"]),
            )
            for item in input_rows
        )
        outputs = tuple(
            RecipeOutput(
                kind=IngredientKind(item["kind"]),
                value=item["value"],
                amount=Decimal(item["amount_decimal"]) if item["amount_decimal"] is not None else None,
                unit=item["unit"],
                probability=Decimal(item["probability_decimal"]),
                constraints=json.loads(item["constraints_json"]) if item["constraints_json"] is not None else None,
                interpretation=IngredientInterpretation(item["interpretation"]),
                source_locator=self._locator_from_row(item),
                source_fields=json.loads(item["source_fields_json"]),
            )
            for item in output_rows
        )
        return Recipe(
            dataset_id=dataset_id,
            recipe_id=recipe_id,
            source_record_id=row["source_record_id"],
            raw_source_hash=row["raw_source_hash"],
            machine_ids=tuple(item["machine_id"] for item in machines),
            inputs=inputs,
            outputs=outputs,
            energy_eut=row["energy_eut"],
            duration_ticks=row["duration_ticks"],
            conditions=json.loads(row["conditions_json"]),
            critical_unknown_fields=tuple(json.loads(row["critical_unknown_fields_json"])),
            status=RecipeStatus(row["status"]),
            quarantine_reasons=tuple(json.loads(row["quarantine_reasons_json"])),
            source_locator=self._locator_from_row(row),
        )

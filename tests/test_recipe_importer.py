from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.recipes.importer import RecipeImporter
from app.recipes.models import (
    DatasetAvailability,
    DatasetState,
    Ingredient,
    IngredientInterpretation,
    IngredientKind,
    PackVersionKey,
    RawRecipeRecord,
    Recipe,
    RecipeDataset,
    RecipeOutput,
    RecipeStatus,
    SourceLocator,
    SourceMetadata,
)
from app.recipes.repository import RecipeRepository
from app.recipes.source import ImportIssue, RecipeSourceRecord


class FakeRecipeSource:
    """Synthetic source contract fixture; records are not GTNH data."""

    def __init__(self, manifest: RecipeDataset, records):
        self.manifest = manifest
        self._records = tuple(records)

    def iter_records(self):
        yield from self._records


def _dataset(dataset_id: str, key: PackVersionKey, *, schema_version: int = 1) -> RecipeDataset:
    return RecipeDataset(
        dataset_id=dataset_id,
        key=key,
        source=SourceMetadata(
            source_id="synthetic-fixture-source",
            source_revision=f"fixture-{dataset_id}",
            exporter_id="hand-authored-fixture",
            exporter_version="fixture-v1",
            license_id="CC0-fixture",
            content_hash=("c" if dataset_id.startswith("current") else "d") * 64,
            schema_version=schema_version,
        ),
        state=DatasetState.STAGING,
    )


def _source_record(dataset_id: str, row_id: str, *, payload_suffix: str, recipes=(), **raw_fields):
    locator = SourceLocator(
        f"synthetic://recipes/{row_id}.json",
        json_pointer=f"/records/{row_id}",
    )
    raw = RawRecipeRecord(
        dataset_id=dataset_id,
        source_record_id=row_id,
        source_locator=locator,
        raw_payload=(f'{{"fixture":"synthetic; not GTNH data","row":"{payload_suffix}"}}').encode(),
        **raw_fields,
    )
    return RecipeSourceRecord(raw_record=raw, recipes=tuple(recipes))


def _recipe(dataset_id: str, recipe_id: str, raw: RawRecipeRecord, *, inputs=(), output_id="synthetic:fixture_output"):
    return Recipe(
        dataset_id=dataset_id,
        recipe_id=recipe_id,
        source_record_id=raw.source_record_id,
        raw_source_hash=raw.raw_hash,
        machine_ids=("synthetic:assembler",),
        inputs=tuple(inputs),
        outputs=(RecipeOutput(IngredientKind.ITEM_EXACT, output_id, Decimal("1"), "item"),),
        energy_eut=16,
        duration_ticks=12,
        source_locator=raw.source_locator,
    )


class RecipeImporterTest(unittest.TestCase):
    """Synthetic Fixture tests cover importer contracts only, not real pack data."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "recipes.db"
        self.repository = RecipeRepository(self.path)
        self.importer = RecipeImporter(self.repository)
        self.key = PackVersionKey("synthetic-pack", "1.0", "1.7.10")

    def tearDown(self):
        self.tmpdir.cleanup()

    def _one_recipe_source(self, dataset_id="current-dataset", *, schema_version=1, unit="item"):
        dataset = _dataset(dataset_id, self.key, schema_version=schema_version)
        raw = _source_record(dataset_id, "row-one", payload_suffix="one").raw_record
        recipe = _recipe(
            dataset_id,
            "recipe:one",
            raw,
            inputs=(Ingredient(
                IngredientKind.ITEM_EXACT,
                "synthetic:fixture_input",
                Decimal("2"),
                unit,
                consumed=True,
            ),),
        )
        return FakeRecipeSource(dataset, (RecipeSourceRecord(raw, (recipe,)),))

    def test_validate_reports_counts_without_writing_or_activating(self):
        source = self._one_recipe_source()

        report = self.importer.validate(source)

        self.assertTrue(report.valid)
        self.assertEqual(report.record_count, 1)
        self.assertEqual(report.accepted_count, 1)
        self.assertEqual(report.conditional_count, 0)
        self.assertEqual(report.quarantined_count, 0)
        self.assertEqual(report.rejected_count, 0)
        self.assertFalse(report.activated)
        self.assertIsNone(self.repository.active_dataset(self.key))
        self.assertEqual(self.repository.dataset_status(self.key).availability, DatasetAvailability.DATA_UNAVAILABLE)

    def test_import_stages_by_default_and_activation_is_explicit(self):
        source = self._one_recipe_source()

        staged = self.importer.import_dataset(source)

        self.assertTrue(staged.valid)
        self.assertFalse(staged.activated)
        self.assertEqual(self.repository.get_recipe("current-dataset", "recipe:one").status, RecipeStatus.ACCEPTED)
        self.assertIsNone(self.repository.active_dataset(self.key))

        activated = self.importer.import_dataset(source, activate=True)
        self.assertTrue(activated.valid)
        self.assertTrue(activated.activated)
        self.assertEqual(self.repository.active_dataset(self.key).dataset_id, "current-dataset")

    def test_unsupported_schema_and_duplicate_recipe_id_quarantine_snapshot(self):
        dataset = _dataset("invalid-dataset", self.key, schema_version=99)
        raw_one = _source_record("invalid-dataset", "row-one", payload_suffix="one").raw_record
        raw_two = _source_record("invalid-dataset", "row-two", payload_suffix="two").raw_record
        first = _recipe("invalid-dataset", "recipe:duplicate", raw_one)
        second = _recipe("invalid-dataset", "recipe:duplicate", raw_two)
        source = FakeRecipeSource(
            dataset,
            (
                RecipeSourceRecord(raw_one, (first,)),
                RecipeSourceRecord(raw_two, (second,)),
            ),
        )

        report = self.importer.import_dataset(source, activate=True)

        self.assertFalse(report.valid)
        self.assertFalse(report.activated)
        self.assertEqual(report.rejected_count, 2)
        self.assertEqual(
            {issue.code for issue in report.issues},
            {"UNSUPPORTED_SCHEMA_VERSION", "DUPLICATE_RECIPE_ID"},
        )
        self.assertEqual(self.repository.import_issues("invalid-dataset"), report.issues)
        with sqlite3.connect(self.path) as conn:
            state = conn.execute(
                "SELECT state FROM recipe_datasets WHERE dataset_id = 'invalid-dataset'"
            ).fetchone()[0]
        self.assertEqual(state, DatasetState.QUARANTINED.value)
        self.assertIsNone(self.repository.active_dataset(self.key))

    def test_invalid_new_snapshot_never_replaces_previous_active_dataset(self):
        good_source = self._one_recipe_source("current-good")
        self.assertTrue(self.importer.import_dataset(good_source, activate=True).activated)
        invalid_source = self._one_recipe_source("invalid-replacement", schema_version=2)

        report = self.importer.import_dataset(invalid_source, activate=True)

        self.assertFalse(report.activated)
        self.assertEqual(self.repository.active_dataset(self.key).dataset_id, "current-good")

    def test_unknown_unit_quarantines_recipe_and_is_reported_as_warning(self):
        source = self._one_recipe_source("current-unknown-unit", unit="synthetic_unit")

        report = self.importer.import_dataset(source, activate=True)

        self.assertTrue(report.valid)
        self.assertTrue(report.activated)
        self.assertEqual(report.quarantined_count, 1)
        self.assertEqual(report.issues[0].code, "UNSUPPORTED_UNIT")
        self.assertEqual(report.issues[0].severity.value, "warning")
        self.assertEqual(self.repository.list_active_recipes(self.key), ())
        self.assertEqual(
            self.repository.quarantined_recipes("current-unknown-unit")[0].status,
            RecipeStatus.QUARANTINED,
        )

    def test_transaction_interruption_rolls_back_and_same_snapshot_can_retry(self):
        source = self._one_recipe_source("current-retry")
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                CREATE TRIGGER fail_recipe_insert BEFORE INSERT ON recipes
                BEGIN SELECT RAISE(ABORT, 'synthetic interruption'); END
                """
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.importer.import_dataset(source, activate=True)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM recipe_datasets").fetchone()[0],
                0,
            )
            conn.execute("DROP TRIGGER fail_recipe_insert")

        report = self.importer.import_dataset(source, activate=True)
        self.assertTrue(report.activated)
        self.assertEqual(self.repository.active_dataset(self.key).dataset_id, "current-retry")
        self.assertEqual(self.repository.list_active_recipes(self.key)[0].recipe_id, "recipe:one")

    def test_duplicate_raw_hash_is_a_located_error(self):
        dataset = _dataset("current-duplicate-raw", self.key)
        first = _source_record("current-duplicate-raw", "row-one", payload_suffix="same")
        second = _source_record("current-duplicate-raw", "row-two", payload_suffix="same")
        source = FakeRecipeSource(dataset, (first, second))

        report = self.importer.validate(source)

        issue = next(issue for issue in report.issues if issue.code == "DUPLICATE_RAW_HASH")
        self.assertEqual(issue.source_record_id, "row-two")
        self.assertEqual(issue.source_locator.uri, "synthetic://recipes/row-two.json")
        self.assertEqual(report.rejected_count, 1)

    def test_duplicate_source_record_id_is_rejected_with_location(self):
        dataset = _dataset("current-duplicate-record", self.key)
        first = _source_record("current-duplicate-record", "row-same", payload_suffix="first")
        second = _source_record("current-duplicate-record", "row-same", payload_suffix="second")
        report = self.importer.validate(FakeRecipeSource(dataset, (first, second)))

        issue = next(issue for issue in report.issues if issue.code == "DUPLICATE_SOURCE_RECORD_ID")
        self.assertEqual(issue.source_record_id, "row-same")
        self.assertEqual(issue.source_locator.uri, "synthetic://recipes/row-same.json")
        self.assertEqual(report.rejected_count, 1)

    def test_malformed_json_preserves_raw_bytes_and_persists_locator_error(self):
        dataset = _dataset("current-bad-json", self.key)
        locator = SourceLocator("synthetic://recipes/broken.json", line=7)
        raw = RawRecipeRecord(
            dataset_id=dataset.dataset_id,
            source_record_id="row-malformed-json",
            source_locator=locator,
            raw_payload=b'{"recipe":',
        )
        try:
            json.loads(raw.raw_payload)
        except json.JSONDecodeError:
            source_issue = ImportIssue(
                code="INVALID_JSON",
                message="source record is not valid JSON",
                source_record_id=raw.source_record_id,
                source_locator=locator,
            )
        source = FakeRecipeSource(dataset, (RecipeSourceRecord(raw, (), (source_issue,)),))

        report = self.importer.import_dataset(source, activate=True)

        self.assertFalse(report.valid)
        self.assertFalse(report.activated)
        self.assertEqual(report.issues[0].source_locator.line, 7)
        self.assertEqual(
            self.repository.get_raw_record(dataset.dataset_id, raw.source_record_id).raw_payload,
            b'{"recipe":',
        )
        self.assertEqual(self.repository.import_issues(dataset.dataset_id), report.issues)

    def test_schema_v1_database_adds_diagnostics_table_without_losing_active_pointer(self):
        dataset = _dataset("current-schema-upgrade", self.key)
        self.repository.add_dataset(dataset)
        self.repository.set_active_dataset(dataset.dataset_id)
        with sqlite3.connect(self.path) as conn:
            conn.execute("DROP TABLE recipe_import_issues")
            conn.execute(
                "UPDATE recipe_schema_metadata SET schema_version = 1 WHERE schema_key = 'recipes'"
            )

        upgraded = RecipeRepository(self.path)

        self.assertEqual(upgraded.active_dataset(self.key).dataset_id, dataset.dataset_id)
        with sqlite3.connect(self.path) as conn:
            version = conn.execute(
                "SELECT schema_version FROM recipe_schema_metadata WHERE schema_key = 'recipes'"
            ).fetchone()[0]
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        self.assertEqual(version, 2)
        self.assertIn("recipe_import_issues", tables)


if __name__ == "__main__":
    unittest.main()

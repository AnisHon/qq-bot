"""Validation and staging for versioned recipe sources."""

from __future__ import annotations

from dataclasses import dataclass
from app.recipes.models import (
    DatasetState,
    Ingredient,
    IngredientKind,
    PackVersionKey,
    RawRecipeRecord,
    Recipe,
    RecipeDataset,
    RecipeOutput,
    RecipeStatus,
    SourceLocator,
)
from app.recipes.repository import RecipeRepository
from app.recipes.source import (
    ImportIssue,
    ImportIssueSeverity,
    RecipeSource,
    RecipeSourceRecord,
)


SUPPORTED_SOURCE_SCHEMA_VERSION = 1
_ITEM_UNITS = frozenset({"item"})
_FLUID_UNITS = frozenset({"mB"})


class RecipeImportError(RuntimeError):
    """The source does not provide a usable manifest or could not be staged."""


@dataclass(frozen=True, slots=True)
class ImportReport:
    """Counts successful recipes by certainty; rejected_count counts error diagnostics."""

    dataset_id: str
    key: PackVersionKey
    record_count: int
    accepted_count: int
    conditional_count: int
    quarantined_count: int
    rejected_count: int
    issues: tuple[ImportIssue, ...]
    activated: bool = False

    @property
    def valid(self) -> bool:
        return self.rejected_count == 0


@dataclass(frozen=True, slots=True)
class _ImportSnapshot:
    dataset: RecipeDataset
    raw_records: tuple[RawRecipeRecord, ...]
    recipes: tuple[Recipe, ...]
    issues: tuple[ImportIssue, ...]


class RecipeImporter:
    """Source-neutral validator; activation is explicit and only follows clean validation."""

    def __init__(self, repository: RecipeRepository):
        self.repository = repository

    def validate(self, source: RecipeSource) -> ImportReport:
        snapshot = self._collect(source)
        return self._report(snapshot)

    def import_dataset(self, source: RecipeSource, *, activate: bool = False) -> ImportReport:
        snapshot = self._collect(source)
        self.repository.stage_import_snapshot(
            snapshot.dataset,
            snapshot.raw_records,
            snapshot.recipes,
            snapshot.issues,
        )
        activated = False
        if activate and not any(issue.severity == ImportIssueSeverity.ERROR for issue in snapshot.issues):
            self.repository.set_active_dataset(snapshot.dataset.dataset_id)
            activated = True
        return self._report(snapshot, activated=activated)

    def _collect(self, source: RecipeSource) -> _ImportSnapshot:
        try:
            dataset = source.manifest
        except Exception as exc:
            raise RecipeImportError("RecipeSource manifest could not be read") from exc
        if not isinstance(dataset, RecipeDataset):
            raise RecipeImportError("RecipeSource manifest must be a RecipeDataset")

        issues: list[ImportIssue] = []
        if dataset.state == DatasetState.ACTIVE:
            issues.append(self._issue("ACTIVE_SOURCE_STATE", "source manifest cannot start active"))
        if dataset.source.schema_version != SUPPORTED_SOURCE_SCHEMA_VERSION:
            issues.append(
                self._issue(
                    "UNSUPPORTED_SCHEMA_VERSION",
                    "source schema version is not supported by this importer",
                )
            )

        raw_records: list[RawRecipeRecord] = []
        recipes: list[Recipe] = []
        seen_record_ids: set[str] = set()
        seen_raw_hashes: set[str] = set()
        seen_recipe_ids: set[str] = set()
        try:
            records = source.iter_records()
            for source_record in records:
                if not isinstance(source_record, RecipeSourceRecord):
                    issues.append(self._issue("INVALID_SOURCE_RECORD", "source emitted an invalid record"))
                    continue
                raw = source_record.raw_record
                if raw.dataset_id != dataset.dataset_id:
                    issues.append(
                        self._issue(
                            "DATASET_ID_MISMATCH",
                            "raw record belongs to a different dataset",
                            raw.source_record_id,
                            raw.source_locator,
                        )
                    )
                    continue
                if raw.source_record_id in seen_record_ids:
                    issues.append(
                        self._issue(
                            "DUPLICATE_SOURCE_RECORD_ID",
                            "source record ID is duplicated",
                            raw.source_record_id,
                            raw.source_locator,
                        )
                    )
                    continue
                if raw.raw_hash in seen_raw_hashes:
                    issues.append(
                        self._issue(
                            "DUPLICATE_RAW_HASH",
                            "raw source content is duplicated",
                            raw.source_record_id,
                            raw.source_locator,
                        )
                    )
                    continue
                seen_record_ids.add(raw.source_record_id)
                seen_raw_hashes.add(raw.raw_hash)
                raw_records.append(raw)
                issues.extend(source_record.issues)
                for recipe in source_record.recipes:
                    if recipe.dataset_id != dataset.dataset_id:
                        issues.append(
                            self._issue(
                                "DATASET_ID_MISMATCH",
                                "recipe belongs to a different dataset",
                                raw.source_record_id,
                                raw.source_locator,
                            )
                        )
                        continue
                    if recipe.source_record_id != raw.source_record_id or recipe.raw_source_hash != raw.raw_hash:
                        issues.append(
                            self._issue(
                                "SOURCE_RECORD_MISMATCH",
                                "recipe source reference does not match its raw record",
                                raw.source_record_id,
                                raw.source_locator,
                            )
                        )
                        continue
                    if recipe.recipe_id in seen_recipe_ids:
                        issues.append(
                            self._issue(
                                "DUPLICATE_RECIPE_ID",
                                "recipe ID is duplicated in the dataset",
                                raw.source_record_id,
                                raw.source_locator,
                            )
                        )
                        continue
                    try:
                        normalized = self.repository._revalidate_recipe(
                            recipe, raw.critical_unknown_fields
                        )
                    except (TypeError, ValueError):
                        issues.append(
                            self._issue(
                                "INVALID_RECIPE",
                                "recipe fields failed contract validation",
                                raw.source_record_id,
                                raw.source_locator,
                            )
                        )
                        continue
                    unit_issues = self._unit_issues(normalized)
                    if unit_issues:
                        normalized = Recipe(
                            dataset_id=normalized.dataset_id,
                            recipe_id=normalized.recipe_id,
                            source_record_id=normalized.source_record_id,
                            raw_source_hash=normalized.raw_source_hash,
                            machine_ids=normalized.machine_ids,
                            inputs=normalized.inputs,
                            outputs=normalized.outputs,
                            energy_eut=normalized.energy_eut,
                            duration_ticks=normalized.duration_ticks,
                            conditions=normalized.conditions,
                            critical_unknown_fields=normalized.critical_unknown_fields,
                            quarantine_reasons=(
                                *normalized.quarantine_reasons,
                                *(message for _, message in unit_issues),
                            ),
                            source_locator=normalized.source_locator,
                        )
                        issues.extend(
                            ImportIssue(
                                code=code,
                                message=message,
                                severity=ImportIssueSeverity.WARNING,
                                source_record_id=raw.source_record_id,
                                source_locator=raw.source_locator,
                            )
                            for code, message in unit_issues
                        )
                    seen_recipe_ids.add(recipe.recipe_id)
                    recipes.append(normalized)
        except Exception:
            # Keep diagnostics free of source payloads, exception text and credentials.
            issues.append(self._issue("SOURCE_READ_ERROR", "source record iteration failed"))

        return _ImportSnapshot(
            dataset=dataset,
            raw_records=tuple(raw_records),
            recipes=tuple(recipes),
            issues=tuple(issues),
        )

    @classmethod
    def _unit_issues(cls, recipe: Recipe) -> tuple[tuple[str, str], ...]:
        issues: list[tuple[str, str]] = []
        for label, components in (("input", recipe.inputs), ("output", recipe.outputs)):
            for index, component in enumerate(components):
                if component.amount is None:
                    continue
                if component.unit is None:
                    issues.append(("MISSING_UNIT", f"{label} unit is missing at index {index}"))
                    continue
                allowed = cls._allowed_units(component)
                if component.unit not in allowed:
                    issues.append(("UNSUPPORTED_UNIT", f"{label} unit is unsupported at index {index}"))
        return tuple(issues)

    @staticmethod
    def _allowed_units(component: Ingredient | RecipeOutput) -> frozenset[str]:
        if component.kind == IngredientKind.FLUID_EXACT:
            return _FLUID_UNITS
        if component.kind in {IngredientKind.ITEM_EXACT, IngredientKind.ORE_DICTIONARY}:
            return _ITEM_UNITS
        # Tags/predicates may describe either item or fluid ingredients.
        return _ITEM_UNITS | _FLUID_UNITS

    @staticmethod
    def _issue(
        code: str,
        message: str,
        source_record_id: str | None = None,
        source_locator: SourceLocator | None = None,
    ) -> ImportIssue:
        return ImportIssue(
            code=code,
            message=message,
            source_record_id=source_record_id,
            source_locator=source_locator,
        )

    @staticmethod
    def _report(snapshot: _ImportSnapshot, *, activated: bool = False) -> ImportReport:
        accepted = sum(recipe.status == RecipeStatus.ACCEPTED for recipe in snapshot.recipes)
        conditional = sum(recipe.status == RecipeStatus.CONDITIONAL for recipe in snapshot.recipes)
        quarantined = sum(recipe.status == RecipeStatus.QUARANTINED for recipe in snapshot.recipes)
        rejected = sum(issue.severity == ImportIssueSeverity.ERROR for issue in snapshot.issues)
        return ImportReport(
            dataset_id=snapshot.dataset.dataset_id,
            key=snapshot.dataset.key,
            record_count=len(snapshot.raw_records),
            accepted_count=accepted,
            conditional_count=conditional,
            quarantined_count=quarantined,
            rejected_count=rejected,
            issues=snapshot.issues,
            activated=activated,
        )

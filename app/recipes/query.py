from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from app.recipes.models import (
    DatasetAvailability,
    Ingredient,
    IngredientKind,
    PackVersionKey,
    Recipe,
    RecipeOutput,
    RecipeStatus,
    SourceLocator,
    validate_exact_identifier,
)
from app.recipes.repository import ActiveRecipeSnapshot, RecipeRepository


class RecipeQueryState(str, Enum):
    AVAILABLE = "available"
    DATA_UNAVAILABLE = "data_unavailable"
    VERSION_MISMATCH = "version_mismatch"
    EMPTY = "empty"
    NO_RESULTS = "no_results"


class MatchQuality(str, Enum):
    EXACT = "exact"
    CONDITIONAL = "conditional"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class RecipeQueryResult:
    state: RecipeQueryState
    key: PackVersionKey
    dataset_id: str | None = None
    source_id: str | None = None
    source_revision: str | None = None
    recipes: tuple[Recipe, ...] = ()
    source_locators: tuple[SourceLocator | None, ...] = ()
    match_quality: MatchQuality | None = None
    quarantined_recipe_count: int = 0
    total_matches: int = 0
    has_more: bool = False
    next_offset: int | None = None


class RecipeQueryService:
    """Exact, active-dataset-only queries; no implicit version or tag resolution."""

    def __init__(self, repository: RecipeRepository) -> None:
        self.repository = repository

    def available_datasets(self) -> tuple[Any, ...]:
        """Return metadata for exact active datasets; never infer alternate versions."""
        return tuple(
            dataset
            for key in self.repository.active_dataset_keys()
            if (dataset := self.repository.active_dataset(key)) is not None
        )

    def find_by_identifier(
        self,
        key: PackVersionKey,
        identifier: str,
        *,
        direction: str,
        kind: str = "item",
        limit: int = 10,
        offset: int = 0,
    ) -> RecipeQueryResult:
        if direction not in {"produces", "consumes"}:
            raise ValueError("direction must be produces or consumes")
        exact_kind = {"item": IngredientKind.ITEM_EXACT, "fluid": IngredientKind.FLUID_EXACT}.get(kind)
        if exact_kind is None:
            raise ValueError("kind must be item or fluid")
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("identifier must be non-empty")
        validate_exact_identifier(exact_kind, identifier)

        snapshot = self.repository.query_active_exact(
            key,
            identifier,
            direction=direction,
            kind=exact_kind,
            limit=limit,
            offset=offset,
        )
        state = self._from_snapshot(key, snapshot, offset=offset)
        if state.state != RecipeQueryState.AVAILABLE or not state.recipes:
            return (
                RecipeQueryResult(
                    state=RecipeQueryState.NO_RESULTS,
                    key=key,
                    dataset_id=state.dataset_id,
                    source_id=state.source_id,
                    source_revision=state.source_revision,
                    quarantined_recipe_count=state.quarantined_recipe_count,
                    total_matches=state.total_matches,
                )
                if state.state == RecipeQueryState.AVAILABLE
                else state
            )
        quality = (
            MatchQuality.CONDITIONAL
            if any(recipe.status == RecipeStatus.CONDITIONAL for recipe in state.recipes)
            else MatchQuality.EXACT
        )
        return replace(state, match_quality=quality)

    def find_by_machine(
        self,
        key: PackVersionKey,
        machine_id: str,
        *,
        limit: int = 10,
        offset: int = 0,
    ) -> RecipeQueryResult:
        if not isinstance(machine_id, str) or not machine_id.strip():
            raise ValueError("machine_id must be non-empty")
        snapshot = self.repository.query_active_machine(key, machine_id, limit=limit, offset=offset)
        state = self._from_snapshot(key, snapshot, offset=offset)
        if state.state != RecipeQueryState.AVAILABLE or not state.recipes:
            return (
                RecipeQueryResult(
                    state=RecipeQueryState.NO_RESULTS,
                    key=key,
                    dataset_id=state.dataset_id,
                    source_id=state.source_id,
                    source_revision=state.source_revision,
                    quarantined_recipe_count=state.quarantined_recipe_count,
                    total_matches=state.total_matches,
                )
                if state.state == RecipeQueryState.AVAILABLE
                else state
            )
        qualities = {self._recipe_quality(recipe) for recipe in state.recipes}
        quality = (
            MatchQuality.UNSUPPORTED
            if MatchQuality.UNSUPPORTED in qualities
            else MatchQuality.CONDITIONAL
            if MatchQuality.CONDITIONAL in qualities
            else MatchQuality.EXACT
        )
        return replace(state, match_quality=quality)

    @staticmethod
    def _recipe_quality(recipe: Recipe) -> MatchQuality:
        if recipe.status == RecipeStatus.CONDITIONAL:
            return MatchQuality.CONDITIONAL
        if any(
            item.interpretation.value == "unsupported"
            for item in (*recipe.inputs, *recipe.outputs)
        ):
            return MatchQuality.UNSUPPORTED
        return MatchQuality.EXACT

    @staticmethod
    def _from_snapshot(
        key: PackVersionKey,
        snapshot: ActiveRecipeSnapshot,
        *,
        offset: int,
    ) -> RecipeQueryResult:
        states = {
            DatasetAvailability.DATA_UNAVAILABLE: RecipeQueryState.DATA_UNAVAILABLE,
            DatasetAvailability.VERSION_MISMATCH: RecipeQueryState.VERSION_MISMATCH,
            DatasetAvailability.EMPTY: RecipeQueryState.EMPTY,
            DatasetAvailability.AVAILABLE: RecipeQueryState.AVAILABLE,
        }
        dataset = snapshot.dataset
        has_more = snapshot.total_matches > offset + len(snapshot.recipes)
        return RecipeQueryResult(
            state=states[snapshot.availability],
            key=key,
            dataset_id=dataset.dataset_id if dataset else None,
            source_id=dataset.source.source_id if dataset else None,
            source_revision=dataset.source.source_revision if dataset else None,
            recipes=snapshot.recipes,
            source_locators=snapshot.source_locators,
            quarantined_recipe_count=snapshot.quarantined_recipe_count,
            total_matches=snapshot.total_matches,
            has_more=has_more,
            next_offset=(
                offset + len(snapshot.recipes)
                if has_more and offset + len(snapshot.recipes) <= 100_000
                else None
            ),
        )


def ingredient_to_dict(item: Ingredient | RecipeOutput) -> dict[str, Any]:
    row: dict[str, Any] = {
        "kind": item.kind.value,
        "id": item.value,
        "amount": str(item.amount) if item.amount is not None else None,
        "unit": item.unit,
        "interpretation": item.interpretation.value,
    }
    if isinstance(item, Ingredient):
        row.update(
            {
                "role": item.role.value,
                "consumed": item.consumed,
            }
        )
    else:
        row["probability"] = str(item.probability)
    if item.constraints is not None:
        row["constraints"] = item.constraints
    return row


def recipe_to_dict(recipe: Recipe, source_locator: SourceLocator | None) -> dict[str, Any]:
    return {
        "recipe_id": recipe.recipe_id,
        "recipe_status": recipe.status.value,
        "match_quality": RecipeQueryService._recipe_quality(recipe).value,
        "machines": list(recipe.machine_ids),
        "inputs": [ingredient_to_dict(item) for item in recipe.inputs],
        "outputs": [ingredient_to_dict(item) for item in recipe.outputs],
        "energy_eut": recipe.energy_eut,
        "duration_ticks": recipe.duration_ticks,
        "conditions": recipe.conditions,
        "source_locator": locator_to_string(source_locator),
    }


def locator_to_string(locator: SourceLocator | None) -> str | None:
    if locator is None:
        return None
    result = locator.uri
    if locator.line is not None:
        result += f"#L{locator.line}"
    elif locator.json_pointer is not None:
        result += f"#{locator.json_pointer}"
    elif locator.record_key is not None:
        result += f"#record={locator.record_key}"
    return result

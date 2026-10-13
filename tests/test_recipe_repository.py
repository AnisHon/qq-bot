from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.recipes.models import (
    DatasetAvailability,
    DatasetState,
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
)
from app.recipes.repository import (
    RecipeQuarantined,
    RecipeRepository,
    RecipeRepositoryError,
)


_FIXTURE_BYTES = b'{"fixture":"synthetic; not GTNH data"}'
_LOCATOR = SourceLocator("synthetic://recipes/fixture.json", json_pointer="/recipes/0")


def _dataset(dataset_id: str, key: PackVersionKey, *, state=DatasetState.STAGING) -> RecipeDataset:
    digest = ("a" if dataset_id.endswith("one") else "b") * 64
    return RecipeDataset(
        dataset_id=dataset_id,
        key=key,
        source=SourceMetadata(
            source_id="synthetic-fixture",
            source_revision=f"fixture-revision-{dataset_id}",
            exporter_id="hand-authored",
            exporter_version="fixture-1",
            license_id="CC0-fixture",
            content_hash=digest,
        ),
        state=state,
    )


def _raw(dataset_id: str, source_record_id: str = "synthetic-row-1", **kwargs) -> RawRecipeRecord:
    return RawRecipeRecord(
        dataset_id=dataset_id,
        source_record_id=source_record_id,
        source_locator=_LOCATOR,
        raw_payload=_FIXTURE_BYTES,
        **kwargs,
    )


def _recipe(
    dataset_id: str,
    recipe_id: str,
    source_record_id: str,
    *,
    inputs: tuple[Ingredient, ...] = (),
    outputs: tuple[RecipeOutput, ...] | None = None,
    machine_ids: tuple[str, ...] = ("synthetic:assembler",),
    critical_unknown_fields: tuple[str, ...] = (),
    conditions: dict | None = None,
) -> Recipe:
    import hashlib

    return Recipe(
        dataset_id=dataset_id,
        recipe_id=recipe_id,
        source_record_id=source_record_id,
        raw_source_hash=hashlib.sha256(_FIXTURE_BYTES).hexdigest(),
        machine_ids=machine_ids,
        inputs=inputs,
        outputs=outputs
        or (
            RecipeOutput(
                kind=IngredientKind.ITEM_EXACT,
                value="synthetic:product",
                amount=Decimal("1.000"),
                unit="item",
            ),
        ),
        energy_eut=32,
        duration_ticks=20,
        critical_unknown_fields=critical_unknown_fields,
        conditions=conditions or {},
        source_locator=_LOCATOR,
    )


class RecipeModelTest(unittest.TestCase):
    """Hand-authored fixtures validate contracts; none represent GTNH source data."""

    def test_ingredient_kinds_keep_exact_and_conditional_sources_distinct(self):
        entries = (
            Ingredient(IngredientKind.ITEM_EXACT, "synthetic:iron_ingot", Decimal("2"), "item", consumed=True),
            Ingredient(IngredientKind.FLUID_EXACT, "synthetic:molten_iron", Decimal("144"), "mB", consumed=True),
            Ingredient(
                IngredientKind.TAG,
                "forge:ingots/iron",
                Decimal("1"),
                "item",
                interpretation=IngredientInterpretation.CONDITIONAL,
            ),
            Ingredient(
                IngredientKind.ORE_DICTIONARY,
                "ingotIron",
                Decimal("1"),
                "item",
                interpretation=IngredientInterpretation.CONDITIONAL,
            ),
            Ingredient(
                IngredientKind.PREDICATE,
                None,
                None,
                None,
                role=IngredientRole.CONDITION,
                interpretation=IngredientInterpretation.CONDITIONAL,
                constraints={"damage": {"min": 0, "max": 3}},
                source_fields={"sourcePredicate": {"kind": "synthetic"}},
            ),
            Ingredient(
                IngredientKind.ITEM_EXACT,
                "synthetic:charged_tool",
                Decimal("1"),
                "item",
                role=IngredientRole.TOOL,
                consumed=False,
                constraints={"components": {"synthetic:charge": 1}},
                interpretation=IngredientInterpretation.UNSUPPORTED,
            ),
            Ingredient(
                IngredientKind.ITEM_EXACT,
                "synthetic:patterned_tool",
                Decimal("1"),
                "item",
                role=IngredientRole.TOOL,
                consumed=False,
                constraints={"nbt": {"tag": {"synthetic:mode": "charged"}}},
                interpretation=IngredientInterpretation.UNSUPPORTED,
            ),
            Ingredient(
                IngredientKind.ITEM_EXACT,
                "synthetic:reusable_catalyst",
                Decimal("1"),
                "item",
                role=IngredientRole.CATALYST,
                consumed=False,
            ),
            Ingredient(
                IngredientKind.SOURCE_EXTENSION,
                None,
                None,
                None,
                interpretation=IngredientInterpretation.UNSUPPORTED,
                source_fields={"unknownIngredientKind": "future_mod_type", "payload": [1, 2]},
            ),
        )

        self.assertEqual(
            [entry.kind for entry in entries],
            [
                IngredientKind.ITEM_EXACT,
                IngredientKind.FLUID_EXACT,
                IngredientKind.TAG,
                IngredientKind.ORE_DICTIONARY,
                IngredientKind.PREDICATE,
                IngredientKind.ITEM_EXACT,
                IngredientKind.ITEM_EXACT,
                IngredientKind.ITEM_EXACT,
                IngredientKind.SOURCE_EXTENSION,
            ],
        )
        self.assertEqual(entries[0].interpretation, IngredientInterpretation.EXACT)
        self.assertEqual(entries[2].interpretation, IngredientInterpretation.CONDITIONAL)
        self.assertEqual(entries[5].role, IngredientRole.TOOL)
        self.assertFalse(entries[5].consumed)
        self.assertEqual(entries[5].constraints["components"]["synthetic:charge"], 1)
        self.assertEqual(entries[6].constraints["nbt"]["tag"]["synthetic:mode"], "charged")
        self.assertEqual(entries[7].role, IngredientRole.CATALYST)
        self.assertFalse(entries[7].consumed)
        self.assertEqual(entries[8].source_fields["unknownIngredientKind"], "future_mod_type")

    def test_unknown_constraints_ids_units_and_tool_consumption_fail_closed(self):
        with self.assertRaises(ValueError):
            Ingredient(
                IngredientKind.TAG,
                "forge:ingots/iron",
                Decimal("1"),
                "item",
                interpretation=IngredientInterpretation.EXACT,
            )
        with self.assertRaises(ValueError):
            Ingredient(IngredientKind.ITEM_EXACT, "#forge:ingots/iron", Decimal("1"), "item", consumed=True)
        with self.assertRaises(ValueError):
            Ingredient(
                IngredientKind.ITEM_EXACT,
                "synthetic:tool",
                Decimal("1"),
                "item",
                consumed=True,
                role=IngredientRole.TOOL,
            )
        with self.assertRaises(ValueError):
            Ingredient(
                IngredientKind.ITEM_EXACT,
                "synthetic:unknown_unit",
                Decimal("1"),
                None,
                consumed=True,
            )
        with self.assertRaises(ValueError):
            Ingredient(IngredientKind.ITEM_EXACT, "synthetic:unknown_consumption", Decimal("1"), "item")

        unsupported = Ingredient(
            IngredientKind.ITEM_EXACT,
            "synthetic:unknown_unit",
            Decimal("1"),
            None,
            consumed=True,
            interpretation=IngredientInterpretation.UNSUPPORTED,
        )
        quarantined = _recipe("dataset-one", "unknown-unit", "row", inputs=(unsupported,))
        self.assertEqual(quarantined.status, RecipeStatus.QUARANTINED)
        self.assertTrue(any("unit unknown" in reason for reason in quarantined.quarantine_reasons))

    def test_unknown_critical_source_fields_quarantine_recipe(self):
        quarantined = _recipe(
            "dataset-one",
            "unknown-condition",
            "row",
            critical_unknown_fields=("dimension_gate",),
        )
        self.assertEqual(quarantined.status, RecipeStatus.QUARANTINED)
        with self.assertRaises(ValueError):
            Recipe(
                dataset_id="dataset-one",
                recipe_id="invalid",
                source_record_id="row",
                raw_source_hash=quarantined.raw_source_hash,
                machine_ids=(),
                inputs=(),
                outputs=quarantined.outputs,
                critical_unknown_fields=("dimension_gate",),
                status=RecipeStatus.ACCEPTED,
            )


class RecipeRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "recipes.db"
        self.repository = RecipeRepository(self.path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _add_recipe(self, dataset_id: str, recipe: Recipe, *, raw: RawRecipeRecord | None = None) -> None:
        self.repository.add_raw_record(raw or _raw(dataset_id, recipe.source_record_id))
        self.repository.add_recipe(recipe)

    def test_empty_database_reports_unavailable_and_initializes_no_domain_rows(self):
        key = PackVersionKey("synthetic-pack", "1.0", "1.7.10")
        result = self.repository.dataset_status(key)
        self.assertEqual(result.availability, DatasetAvailability.DATA_UNAVAILABLE)
        self.assertIsNone(result.dataset_id)
        with sqlite3.connect(self.path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM recipe_datasets").fetchone()[0]
        self.assertEqual(count, 0)

    def test_active_dataset_without_queryable_rows_reports_empty(self):
        key = PackVersionKey("synthetic-pack", "1.0", "1.7.10")
        self.repository.add_dataset(_dataset("dataset-one", key))
        self.repository.set_active_dataset("dataset-one")
        self.assertEqual(self.repository.dataset_status(key).availability, DatasetAvailability.EMPTY)

    def test_existing_database_tables_and_rows_survive_recipe_schema_initialization(self):
        old_path = self.path.parent / "old-bot.db"
        with sqlite3.connect(old_path) as conn:
            conn.execute("CREATE TABLE existing_bot_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT INTO existing_bot_state VALUES ('fixture', 'preserved')")

        RecipeRepository(old_path)
        with sqlite3.connect(old_path) as conn:
            value = conn.execute("SELECT value FROM existing_bot_state WHERE key = 'fixture'").fetchone()[0]
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(value, "preserved")
        self.assertIn("recipe_datasets", tables)
        self.assertIn("recipe_source_records", tables)

    def test_foreign_key_requires_dataset_and_raw_source_record(self):
        raw = _raw("missing-dataset")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.add_raw_record(raw)

        dataset = _dataset("dataset-one", PackVersionKey("synthetic-pack", "1.0", "1.7.10"))
        self.repository.add_dataset(dataset)
        recipe = _recipe("dataset-one", "recipe:one", "missing-row")
        with self.assertRaises(RecipeRepositoryError):
            self.repository.add_recipe(recipe)
        self.assertIsNone(self.repository.get_recipe("dataset-one", "recipe:one"))

    def test_decimal_amount_probability_and_source_provenance_round_trip(self):
        dataset = _dataset("dataset-one", PackVersionKey("synthetic-pack", "1.0", "1.7.10"))
        self.repository.add_dataset(dataset)
        raw = _raw(
            "dataset-one",
            parsed_fields={"machine": "synthetic:assembler"},
            unknown_fields={"futureField": {"opaque": [1, "two"]}},
        )
        recipe = _recipe(
            "dataset-one",
            "recipe:precise",
            raw.source_record_id,
            inputs=(
                Ingredient(
                    IngredientKind.ITEM_EXACT,
                    "synthetic:plate",
                    Decimal("1.2500"),
                    "item",
                    consumed=True,
                    source_locator=SourceLocator("synthetic://recipes/fixture.json", json_pointer="/recipes/0/inputs/0"),
                ),
            ),
            outputs=(
                RecipeOutput(
                    IngredientKind.ITEM_EXACT,
                    "synthetic:rare_output",
                    Decimal("0.12500"),
                    "item",
                    probability=Decimal("0.37500"),
                ),
                RecipeOutput(IngredientKind.FLUID_EXACT, "synthetic:steam", Decimal("1000"), "mB"),
            ),
            machine_ids=("synthetic:assembler", "synthetic:large_assembler"),
        )
        self._add_recipe("dataset-one", recipe, raw=raw)

        stored_recipe = self.repository.get_recipe("dataset-one", "recipe:precise")
        stored_raw = self.repository.get_raw_record("dataset-one", raw.source_record_id)
        self.assertEqual(stored_recipe, recipe)
        self.assertEqual(stored_recipe.inputs[0].amount, Decimal("1.2500"))
        self.assertEqual(stored_recipe.outputs[0].probability, Decimal("0.37500"))
        self.assertEqual(stored_recipe.machine_ids, ("synthetic:assembler", "synthetic:large_assembler"))
        self.assertEqual(stored_raw.raw_payload, _FIXTURE_BYTES)
        self.assertEqual(stored_raw.raw_hash, raw.raw_hash)
        self.assertEqual(stored_raw.unknown_fields, {"futureField": {"opaque": [1, "two"]}})
        self.repository.set_active_dataset("dataset-one")
        self.assertEqual(
            [recipe.recipe_id for recipe in self.repository.find_producing_exact(
                dataset.key, "synthetic:steam", kind=IngredientKind.FLUID_EXACT
            )],
            ["recipe:precise"],
        )

    def test_exact_pack_version_key_isolated_and_unknown_version_never_falls_back(self):
        key_one = PackVersionKey("synthetic-pack", "1.0", "1.7.10")
        key_two = PackVersionKey("synthetic-pack", "2.0", "1.7.10")
        dataset_one, dataset_two = _dataset("dataset-one", key_one), _dataset("dataset-two", key_two)
        self.repository.add_dataset(dataset_one)
        self.repository.add_dataset(dataset_two)
        input_one = Ingredient(IngredientKind.ITEM_EXACT, "synthetic:iron", Decimal("1"), "item", consumed=True)
        input_two = Ingredient(IngredientKind.ITEM_EXACT, "synthetic:copper", Decimal("2"), "item", consumed=True)
        output = (RecipeOutput(IngredientKind.ITEM_EXACT, "synthetic:gear", Decimal("1"), "item"),)
        recipe_one = _recipe("dataset-one", "gear:iron", "row-one", inputs=(input_one,), outputs=output)
        recipe_two = _recipe("dataset-two", "gear:copper", "row-two", inputs=(input_two,), outputs=output)
        self._add_recipe("dataset-one", recipe_one, raw=_raw("dataset-one", "row-one"))
        self._add_recipe("dataset-two", recipe_two, raw=_raw("dataset-two", "row-two"))
        self.repository.set_active_dataset("dataset-one")
        self.repository.set_active_dataset("dataset-two")

        self.assertEqual(
            [recipe.recipe_id for recipe in self.repository.find_producing_exact(key_one, "synthetic:gear")],
            ["gear:iron"],
        )
        self.assertEqual(
            [recipe.recipe_id for recipe in self.repository.find_producing_exact(key_two, "synthetic:gear")],
            ["gear:copper"],
        )
        self.assertEqual(
            self.repository.dataset_status(PackVersionKey("synthetic-pack", "3.0", "1.7.10")).availability,
            DatasetAvailability.VERSION_MISMATCH,
        )
        self.assertEqual(self.repository.dataset_status(key_one).dataset_id, "dataset-one")
        self.assertEqual(self.repository.dataset_status(key_two).dataset_id, "dataset-two")

    def test_exact_lookup_does_not_treat_tag_or_predicate_as_item_id(self):
        key = PackVersionKey("synthetic-pack", "1.0", "1.7.10")
        dataset = _dataset("dataset-one", key)
        self.repository.add_dataset(dataset)
        recipe = _recipe(
            "dataset-one",
            "recipe:tagged",
            "row-tag",
            inputs=(
                Ingredient(
                    IngredientKind.TAG,
                    "forge:ingots/iron",
                    Decimal("1"),
                    "item",
                    interpretation=IngredientInterpretation.CONDITIONAL,
                ),
            ),
            outputs=(RecipeOutput(IngredientKind.ITEM_EXACT, "synthetic:plate", Decimal("1"), "item"),),
        )
        self._add_recipe("dataset-one", recipe, raw=_raw("dataset-one", "row-tag"))
        self.repository.set_active_dataset("dataset-one")

        self.assertEqual(self.repository.find_consuming_exact(key, "forge:ingots/iron"), ())
        self.assertEqual(self.repository.find_consuming_exact(key, "synthetic:plate"), ())

    def test_unrecognized_critical_field_is_preserved_but_quarantined_from_queries(self):
        key = PackVersionKey("synthetic-pack", "1.0", "1.7.10")
        self.repository.add_dataset(_dataset("dataset-one", key))
        raw = _raw(
            "dataset-one",
            "row-critical",
            unknown_fields={"dimension_gate": {"value": "synthetic:moon"}},
            critical_unknown_fields=("dimension_gate",),
        )
        recipe = _recipe(
            "dataset-one",
            "recipe:unknown-gate",
            "row-critical",
            critical_unknown_fields=raw.critical_unknown_fields,
            outputs=(RecipeOutput(IngredientKind.ITEM_EXACT, "synthetic:moon_dust", Decimal("1"), "item"),),
        )
        self._add_recipe("dataset-one", recipe, raw=raw)
        self.repository.set_active_dataset("dataset-one")

        self.assertEqual(recipe.status, RecipeStatus.QUARANTINED)
        self.assertEqual(self.repository.find_producing_exact(key, "synthetic:moon_dust"), ())
        self.assertEqual(self.repository.quarantined_recipes("dataset-one"), (recipe,))
        self.assertEqual(
            self.repository.get_raw_record("dataset-one", "row-critical").unknown_fields,
            {"dimension_gate": {"value": "synthetic:moon"}},
        )

    def test_raw_critical_field_forces_quarantine_when_recipe_omits_it(self):
        key = PackVersionKey("synthetic-pack", "1.0", "1.7.10")
        self.repository.add_dataset(_dataset("dataset-one", key))
        raw = _raw(
            "dataset-one",
            "row-critical-unmarked",
            unknown_fields={"dimension_gate": {"value": "synthetic:moon"}},
            critical_unknown_fields=("dimension_gate",),
        )
        recipe = _recipe(
            "dataset-one",
            "recipe:unknown-gate-unmarked",
            raw.source_record_id,
            outputs=(RecipeOutput(IngredientKind.ITEM_EXACT, "synthetic:moon_dust", Decimal("1"), "item"),),
        )
        self.repository.add_raw_record(raw)
        stored = self.repository.add_recipe(recipe)
        self.repository.set_active_dataset("dataset-one")

        self.assertEqual(recipe.status, RecipeStatus.ACCEPTED)
        self.assertEqual(stored.status, RecipeStatus.QUARANTINED)
        self.assertEqual(stored.critical_unknown_fields, ("dimension_gate",))
        self.assertEqual(self.repository.find_producing_exact(key, "synthetic:moon_dust"), ())
        self.assertEqual(self.repository.quarantined_recipes("dataset-one"), (stored,))

    def test_exact_consumption_lookup_excludes_non_consumable_tool_and_catalyst(self):
        key = PackVersionKey("synthetic-pack", "1.0", "1.7.10")
        self.repository.add_dataset(_dataset("dataset-one", key))
        recipe = _recipe(
            "dataset-one",
            "recipe:uses-tool-and-catalyst",
            "row-tool-catalyst",
            inputs=(
                Ingredient(
                    IngredientKind.ITEM_EXACT,
                    "synthetic:calibrated_wrench",
                    Decimal("1"),
                    "item",
                    role=IngredientRole.TOOL,
                    consumed=False,
                ),
                Ingredient(
                    IngredientKind.ITEM_EXACT,
                    "synthetic:binding_catalyst",
                    Decimal("1"),
                    "item",
                    role=IngredientRole.CATALYST,
                    consumed=False,
                ),
            ),
        )
        self._add_recipe("dataset-one", recipe)
        self.repository.set_active_dataset("dataset-one")

        self.assertEqual(
            self.repository.find_consuming_exact(key, "synthetic:calibrated_wrench"), ()
        )
        self.assertEqual(
            self.repository.find_consuming_exact(key, "synthetic:binding_catalyst"), ()
        )

    def test_recipe_write_revalidates_mutable_conditions_and_constraints(self):
        key = PackVersionKey("synthetic-pack", "1.0", "1.7.10")
        self.repository.add_dataset(_dataset("dataset-one", key))
        recipe_with_condition = _recipe("dataset-one", "recipe:late-condition", "row-late-condition")
        recipe_with_condition.conditions["dimension"] = "synthetic:moon"
        self._add_recipe("dataset-one", recipe_with_condition)
        stored = self.repository.get_recipe("dataset-one", "recipe:late-condition")
        self.assertEqual(stored.status, RecipeStatus.CONDITIONAL)
        self.assertEqual(stored.conditions, {"dimension": "synthetic:moon"})

        constrained_input = Ingredient(
            IngredientKind.ITEM_EXACT,
            "synthetic:mutable_exact_input",
            Decimal("1"),
            "item",
            consumed=True,
            constraints={},
        )
        recipe_with_constraint = _recipe(
            "dataset-one",
            "recipe:late-constraint",
            "row-late-constraint",
            inputs=(constrained_input,),
        )
        constrained_input.constraints["nbt"] = {"display": {"name": "synthetic"}}
        self.repository.add_raw_record(_raw("dataset-one", "row-late-constraint"))
        with self.assertRaisesRegex(ValueError, "constrained ingredients"):
            self.repository.add_recipe(recipe_with_constraint)
        self.assertIsNone(self.repository.get_recipe("dataset-one", "recipe:late-constraint"))

    def test_dataset_replacement_is_single_key_and_quarantined_dataset_cannot_activate(self):
        key = PackVersionKey("synthetic-pack", "1.0", "1.7.10")
        first = _dataset("dataset-one", key)
        replacement = _dataset("dataset-two", key)
        quarantined = _dataset("dataset-three", PackVersionKey("other-pack", "1.0", "1.7.10"), state=DatasetState.QUARANTINED)
        for dataset in (first, replacement, quarantined):
            self.repository.add_dataset(dataset)
        self.repository.set_active_dataset("dataset-one")
        self.repository.set_active_dataset("dataset-two")

        active = self.repository.active_dataset(key)
        self.assertEqual(active.dataset_id, replacement.dataset_id)
        self.assertEqual(active.state, DatasetState.ACTIVE)
        with sqlite3.connect(self.path) as conn:
            states = dict(conn.execute("SELECT dataset_id, state FROM recipe_datasets"))
        self.assertEqual(states["dataset-one"], DatasetState.RETIRED.value)
        self.assertEqual(states["dataset-two"], DatasetState.ACTIVE.value)
        with self.assertRaises(RecipeQuarantined):
            self.repository.set_active_dataset("dataset-three")


if __name__ == "__main__":
    unittest.main()

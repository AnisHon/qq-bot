from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from app.recipes.importer import RecipeImporter
from app.recipes.models import (
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
from app.recipes.query import RecipeQueryService
from app.recipes.repository import RecipeRepository
from app.recipes.source import RecipeSourceRecord
from app.llm import ChatWithToolsResponse, ToolCallRequest
from app.tools.agent import TOOL_REGISTRY, run_agent_loop
from app.tools.contracts import PolicySnapshot, ToolContext, ToolStatus
from app.storage_executor import StorageExecutor


_RECIPE_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "recipes" / "synthetic_recipe_set.json"


class _SyntheticJsonRecipeSource:
    """Test-only adapter for a hand-authored JSON contract fixture, not an exporter."""

    def __init__(self, dataset_data: dict, fixture_bytes: bytes) -> None:
        self.dataset_data = dataset_data
        self.fixture_hash = hashlib.sha256(fixture_bytes).hexdigest()
        key = dataset_data["pack_key"]
        self._manifest = RecipeDataset(
            dataset_id=dataset_data["dataset_id"],
            key=PackVersionKey(key["pack_id"], key["pack_version"], key["game_version"]),
            source=SourceMetadata(
                source_id="synthetic-json-fixture",
                source_revision=dataset_data["source_revision"],
                exporter_id="hand-authored-fixture",
                exporter_version="fixture-v1",
                license_id="synthetic-fixture-only",
                content_hash=self.fixture_hash,
            ),
            state=DatasetState.STAGING,
        )

    @property
    def manifest(self) -> RecipeDataset:
        return self._manifest

    def iter_records(self):
        dataset_id = self._manifest.dataset_id
        for record in self.dataset_data["records"]:
            locator = SourceLocator(
                "fixture://synthetic_recipe_set.json",
                json_pointer=record["source_pointer"],
            )
            raw_payload = json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            raw = RawRecipeRecord(
                dataset_id=dataset_id,
                source_record_id=record["source_record_id"],
                source_locator=locator,
                raw_payload=raw_payload,
                parsed_fields={"recipe_id": record["recipe"]["recipe_id"]},
                unknown_fields=record.get("unknown_fields", {}),
                critical_unknown_fields=tuple(record.get("critical_unknown_fields", ())),
            )
            raw_recipe = record["recipe"]
            inputs = tuple(
                Ingredient(
                    kind=IngredientKind[item["kind"].upper()],
                    value=item.get("value"),
                    amount=Decimal(str(item["amount"])) if item.get("amount") is not None else None,
                    unit=item.get("unit"),
                    role=IngredientRole[item.get("role", "material").upper()],
                    consumed=item.get("consumed"),
                    constraints=item.get("constraints"),
                    interpretation=IngredientInterpretation[item["interpretation"].upper()],
                    source_locator=locator,
                    source_fields=item.get("source_fields", {}),
                )
                for item in raw_recipe.get("inputs", ())
            )
            outputs = tuple(
                RecipeOutput(
                    kind=IngredientKind[item["kind"].upper()],
                    value=item.get("value"),
                    amount=Decimal(str(item["amount"])) if item.get("amount") is not None else None,
                    unit=item.get("unit"),
                    probability=Decimal(str(item.get("probability", "1"))),
                    constraints=item.get("constraints"),
                    interpretation=IngredientInterpretation[item["interpretation"].upper()],
                    source_locator=locator,
                    source_fields=item.get("source_fields", {}),
                )
                for item in raw_recipe["outputs"]
            )
            recipe = Recipe(
                dataset_id=dataset_id,
                recipe_id=raw_recipe["recipe_id"],
                source_record_id=raw.source_record_id,
                raw_source_hash=raw.raw_hash,
                machine_ids=tuple(raw_recipe.get("machine_ids", ())),
                inputs=inputs,
                outputs=outputs,
                energy_eut=raw_recipe.get("energy_eut"),
                duration_ticks=raw_recipe.get("duration_ticks"),
                conditions=raw_recipe.get("conditions", {}),
                source_locator=locator,
            )
            yield RecipeSourceRecord(raw_record=raw, recipes=(recipe,))


def _seed_dataset(repository: RecipeRepository, key: PackVersionKey, dataset_id: str, rows: list[dict]) -> None:
    repository.add_dataset(
        RecipeDataset(
            dataset_id=dataset_id,
            key=key,
            source=SourceMetadata(
                source_id="synthetic-fixture",
                source_revision=f"synthetic-{dataset_id}",
                exporter_id="hand-authored",
                exporter_version="fixture-1",
                license_id="CC0-fixture",
                content_hash=hashlib.sha256(dataset_id.encode()).hexdigest(),
            ),
        )
    )
    for row in rows:
        recipe_id = row["recipe_id"]
        source_id = f"source-{recipe_id}"
        raw_payload = f'{{"fixture":"synthetic, not GTNH","recipe":"{recipe_id}"}}'.encode()
        locator = SourceLocator("synthetic://fixture/recipes.json", json_pointer=f"/recipes/{recipe_id}")
        raw = RawRecipeRecord(
            dataset_id=dataset_id,
            source_record_id=source_id,
            source_locator=locator,
            raw_payload=raw_payload,
            critical_unknown_fields=row.get("critical_unknown_fields", ()),
        )
        repository.add_raw_record(raw)
        recipe = Recipe(
            dataset_id=dataset_id,
            recipe_id=recipe_id,
            source_record_id=source_id,
            raw_source_hash=raw.raw_hash or hashlib.sha256(raw_payload).hexdigest(),
            machine_ids=tuple(row.get("machine_ids", ("fixture:assembler",))),
            inputs=tuple(row.get("inputs", ())),
            outputs=tuple(row["outputs"]),
            energy_eut=row.get("energy_eut", 32),
            duration_ticks=row.get("duration_ticks", 20),
            conditions=row.get("conditions", {}),
            source_locator=locator if row.get("include_recipe_locator", True) else None,
        )
        repository.add_recipe(recipe)
    repository.set_active_dataset(dataset_id)


class RecipeQueryServiceTest(unittest.TestCase):
    """Synthetic fixtures exercise the contract; they are not GTNH recipes."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.repository = RecipeRepository(Path(self.tempdir.name) / "recipes.db")
        self.service = RecipeQueryService(self.repository)
        self.key_v1 = PackVersionKey("fixture-pack", "v1", "game-a")
        self.key_v2 = PackVersionKey("fixture-pack", "v2", "game-a")
        exact_inputs = (
            Ingredient(
                IngredientKind.ITEM_EXACT,
                "fixture:ore",
                Decimal("2"),
                "item",
                consumed=True,
            ),
            Ingredient(
                IngredientKind.FLUID_EXACT,
                "fixture:water",
                Decimal("250"),
                "mB",
                consumed=True,
            ),
            Ingredient(
                IngredientKind.ITEM_EXACT,
                "fixture:reusable_pattern",
                Decimal("1"),
                "item",
                role=IngredientRole.TOOL,
                consumed=False,
            ),
        )
        _seed_dataset(
            self.repository,
            self.key_v1,
            "fixture-v1",
            [
                {
                    "recipe_id": "exact-v1",
                    "inputs": exact_inputs,
                    "outputs": (
                        RecipeOutput(
                            IngredientKind.ITEM_EXACT,
                            "fixture:product",
                            Decimal("1"),
                            "item",
                            probability=Decimal("0.5"),
                        ),
                        RecipeOutput(
                            IngredientKind.FLUID_EXACT,
                            "fixture:byproduct_fluid",
                            Decimal("50"),
                            "mB",
                        ),
                    ),
                    "energy_eut": 32,
                    "duration_ticks": 20,
                },
                {
                    "recipe_id": "conditional-v1",
                    "inputs": (Ingredient(IngredientKind.ITEM_EXACT, "fixture:ore", Decimal("1"), "item", consumed=True),),
                    "outputs": (RecipeOutput(IngredientKind.ITEM_EXACT, "fixture:product", Decimal("1"), "item"),),
                    "conditions": {"dimension": "fixture:underground"},
                },
                {
                    "recipe_id": "tag-output-v1",
                    "outputs": (
                        RecipeOutput(
                            IngredientKind.TAG,
                            "fixture:outputs/product",
                            Decimal("1"),
                            "item",
                            interpretation=IngredientInterpretation.CONDITIONAL,
                        ),
                    ),
                },
                {
                    "recipe_id": "quarantined-v1",
                    "outputs": (RecipeOutput(IngredientKind.ITEM_EXACT, "fixture:product", Decimal("1"), "item"),),
                    "critical_unknown_fields": ("opaque_recipe_condition",),
                },
            ],
        )
        _seed_dataset(
            self.repository,
            self.key_v2,
            "fixture-v2",
            [
                {
                    "recipe_id": "exact-v2",
                    "outputs": (RecipeOutput(IngredientKind.ITEM_EXACT, "fixture:product", Decimal("4"), "item"),),
                    "energy_eut": 64,
                }
            ],
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_exact_item_query_keeps_all_inputs_output_probability_energy_and_locator(self) -> None:
        result = self.service.find_by_identifier(
            self.key_v1, "fixture:product", direction="produces", kind="item"
        )
        self.assertEqual(result.state.value, "available")
        self.assertEqual(result.match_quality.value, "conditional")
        self.assertEqual({recipe.recipe_id for recipe in result.recipes}, {"exact-v1", "conditional-v1"})
        exact = next(recipe for recipe in result.recipes if recipe.recipe_id == "exact-v1")
        self.assertEqual(exact.inputs[1].kind, IngredientKind.FLUID_EXACT)
        self.assertFalse(exact.inputs[2].consumed)
        self.assertEqual(exact.outputs[0].probability, Decimal("0.5"))
        self.assertEqual(exact.energy_eut, 32)
        self.assertEqual(
            result.source_locators[result.recipes.index(exact)].uri,
            "synthetic://fixture/recipes.json",
        )

    def test_exact_query_never_treats_tag_output_as_an_item_id(self) -> None:
        result = self.service.find_by_identifier(
            self.key_v1, "fixture:outputs/product", direction="produces", kind="item"
        )
        self.assertEqual(result.state.value, "no_results")
        self.assertEqual(result.recipes, ())
        self.assertEqual(result.quarantined_recipe_count, 1)

    def test_query_is_exactly_versioned_and_never_falls_back(self) -> None:
        result = self.service.find_by_identifier(
            PackVersionKey("fixture-pack", "v3", "game-a"),
            "fixture:product",
            direction="produces",
        )
        self.assertEqual(result.state.value, "version_mismatch")
        self.assertEqual(result.recipes, ())

        v2 = self.service.find_by_identifier(
            self.key_v2, "fixture:product", direction="produces"
        )
        self.assertEqual([recipe.recipe_id for recipe in v2.recipes], ["exact-v2"])
        self.assertEqual(v2.recipes[0].energy_eut, 64)

    def test_machine_query_returns_item_fluid_and_conditional_details(self) -> None:
        result = self.service.find_by_machine(self.key_v1, "fixture:assembler")
        self.assertEqual(result.state.value, "available")
        by_id = {recipe.recipe_id: recipe for recipe in result.recipes}
        self.assertNotIn("quarantined-v1", by_id)
        self.assertEqual(by_id["exact-v1"].inputs[1].kind, IngredientKind.FLUID_EXACT)
        self.assertEqual(by_id["conditional-v1"].status.value, "conditional")

    def test_sql_level_page_limit_reports_total_and_next_offset(self) -> None:
        key = PackVersionKey("fixture-large", "v1", "game-a")
        rows = [
            {
                "recipe_id": f"bounded-{index:02d}",
                "outputs": (
                    RecipeOutput(IngredientKind.ITEM_EXACT, "fixture:shared_output", Decimal("1"), "item"),
                ),
            }
            for index in range(12)
        ]
        _seed_dataset(self.repository, key, "fixture-large-v1", rows)

        first = self.service.find_by_identifier(
            key, "fixture:shared_output", direction="produces", limit=3
        )
        second = self.service.find_by_identifier(
            key, "fixture:shared_output", direction="produces", limit=3, offset=3
        )

        self.assertEqual(len(first.recipes), 3)
        self.assertEqual(first.total_matches, 12)
        self.assertEqual(first.next_offset, 3)
        self.assertEqual([r.recipe_id for r in second.recipes], ["bounded-03", "bounded-04", "bounded-05"])
        self.assertEqual(second.next_offset, 6)
        with self.assertRaises(ValueError):
            self.service.find_by_identifier(
                key, "fixture:shared_output", direction="produces", limit=21
            )

    def test_active_snapshot_carries_matching_dataset_metadata_and_recipe_rows(self) -> None:
        snapshot = self.repository.query_active_exact(
            self.key_v1,
            "fixture:product",
            direction="produces",
            kind=IngredientKind.ITEM_EXACT,
            limit=10,
        )
        self.assertEqual(snapshot.dataset.dataset_id, "fixture-v1")
        self.assertEqual(snapshot.dataset.source.source_revision, "synthetic-fixture-v1")
        self.assertEqual({recipe.dataset_id for recipe in snapshot.recipes}, {"fixture-v1"})
        self.assertTrue(all(locator is not None for locator in snapshot.source_locators))


class RecipeContractFixtureIntegrationTest(unittest.TestCase):
    """End-to-end synthetic fixture coverage; this is not GTNH data validation."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.repository = RecipeRepository(Path(self.tempdir.name) / "synthetic-fixtures.db")
        fixture_bytes = _RECIPE_FIXTURE_PATH.read_bytes()
        self.fixture = json.loads(fixture_bytes)
        self.assertIn("not GTNH", self.fixture["fixture_notice"])
        importer = RecipeImporter(self.repository)
        self.reports = []
        for dataset in self.fixture["datasets"]:
            report = importer.import_dataset(
                _SyntheticJsonRecipeSource(dataset, fixture_bytes),
                activate=True,
            )
            self.reports.append(report)
        self.service = RecipeQueryService(self.repository)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_fixture_imports_and_supports_exact_conditional_versioned_queries(self) -> None:
        self.assertEqual(len(self.reports), 2)
        self.assertTrue(all(report.valid and report.activated for report in self.reports))
        self.assertEqual(
            (self.reports[0].record_count, self.reports[0].accepted_count,
             self.reports[0].conditional_count, self.reports[0].quarantined_count),
            (5, 2, 1, 2),
        )
        self.assertEqual(self.reports[1].accepted_count, 1)

        v1 = PackVersionKey("synthetic-pack", "fixture-v1", "fixture-game")
        produced = self.service.find_by_identifier(
            v1, "synthetic:final_component", direction="produces", kind="item"
        )
        by_id = {recipe.recipe_id: recipe for recipe in produced.recipes}
        self.assertEqual(produced.state.value, "available")
        self.assertEqual(produced.match_quality.value, "conditional")
        self.assertEqual(produced.key, v1)
        self.assertEqual(produced.dataset_id, "synthetic-recipes-pack-v1")
        self.assertEqual(len(produced.source_locators), len(produced.recipes))
        self.assertTrue(all(locator is not None for locator in produced.source_locators))
        self.assertEqual(
            {
                recipe.recipe_id: locator.json_pointer
                for recipe, locator in zip(produced.recipes, produced.source_locators)
            },
            {
                "synthetic:final-from-ore": "/datasets/0/records/0",
                "synthetic:final-from-conditional-ore": "/datasets/0/records/1",
                "synthetic:final-with-extra-metadata": "/datasets/0/records/2",
            },
        )
        self.assertEqual(
            set(by_id),
            {
                "synthetic:final-from-ore",
                "synthetic:final-from-conditional-ore",
                "synthetic:final-with-extra-metadata",
            },
        )
        self.assertNotIn("synthetic:critical-unknown-condition", by_id)

        exact = by_id["synthetic:final-from-ore"]
        self.assertEqual(len(exact.inputs), 3)
        self.assertEqual(exact.inputs[0].kind, IngredientKind.ITEM_EXACT)
        self.assertEqual(exact.inputs[1].kind, IngredientKind.FLUID_EXACT)
        self.assertEqual(exact.inputs[1].value, "synthetic:water")
        self.assertFalse(exact.inputs[2].consumed)
        self.assertEqual(exact.inputs[2].role, IngredientRole.TOOL)
        self.assertEqual(exact.outputs[0].probability, Decimal("0.25"))
        self.assertEqual(exact.energy_eut, 32)
        self.assertEqual(exact.duration_ticks, 40)

        conditional = by_id["synthetic:final-from-conditional-ore"]
        self.assertEqual(conditional.status, RecipeStatus.CONDITIONAL)
        self.assertEqual(
            {ingredient.kind for ingredient in conditional.inputs},
            {
                IngredientKind.TAG,
                IngredientKind.ORE_DICTIONARY,
                IngredientKind.PREDICATE,
                IngredientKind.ITEM_EXACT,
            },
        )
        constrained = conditional.inputs[-1]
        self.assertIn("nbt", constrained.constraints)
        self.assertIn("components", constrained.constraints)

        fluid_output = self.service.find_by_identifier(
            v1, "synthetic:clean_water", direction="produces", kind="fluid"
        )
        self.assertEqual(fluid_output.match_quality.value, "exact")
        self.assertEqual(fluid_output.key, v1)
        self.assertEqual([recipe.recipe_id for recipe in fluid_output.recipes], ["synthetic:final-from-ore"])
        self.assertEqual(fluid_output.recipes[0].outputs[1].probability, Decimal("0.8"))
        fluid_input = self.service.find_by_identifier(
            v1, "synthetic:water", direction="consumes", kind="fluid"
        )
        self.assertEqual(fluid_input.match_quality.value, "exact")
        self.assertEqual([recipe.recipe_id for recipe in fluid_input.recipes], ["synthetic:final-from-ore"])

        machine_results = self.service.find_by_machine(v1, "synthetic:assembler")
        self.assertEqual(machine_results.state.value, "available")
        self.assertEqual(machine_results.dataset_id, "synthetic-recipes-pack-v1")
        self.assertEqual(machine_results.match_quality.value, "conditional")
        self.assertEqual(len(machine_results.source_locators), len(machine_results.recipes))
        self.assertNotIn(
            "synthetic:critical-unknown-condition",
            {recipe.recipe_id for recipe in machine_results.recipes},
        )

        # An exact lookup must not reinterpret the tag's text as an exact item ID.
        tag_as_exact_item = self.service.find_by_identifier(
            v1, "forge:ores/copper", direction="consumes", kind="item"
        )
        self.assertEqual(tag_as_exact_item.state.value, "no_results")
        missing_item = self.service.find_by_identifier(
            v1, "synthetic:not_present", direction="produces", kind="item"
        )
        self.assertEqual(missing_item.state.value, "no_results")
        missing_pack = self.service.find_by_identifier(
            PackVersionKey("missing-pack", "fixture-v1", "fixture-game"),
            "synthetic:final_component",
            direction="produces",
        )
        self.assertEqual(missing_pack.state.value, "data_unavailable")
        missing_version = self.service.find_by_identifier(
            PackVersionKey("synthetic-pack", "fixture-v3", "fixture-game"),
            "synthetic:final_component",
            direction="produces",
        )
        self.assertEqual(missing_version.state.value, "version_mismatch")

        v2 = PackVersionKey("synthetic-pack", "fixture-v2", "fixture-game")
        v2_result = self.service.find_by_identifier(
            v2, "synthetic:final_component", direction="produces", kind="item"
        )
        self.assertEqual([recipe.recipe_id for recipe in v2_result.recipes], ["synthetic:final-v2"])
        self.assertEqual(v2_result.recipes[0].outputs[0].amount, Decimal("4"))
        self.assertEqual(v2_result.recipes[0].energy_eut, 128)

        # Unknown non-critical fields and exact source bytes / locators survive import.
        preserved = self.repository.get_raw_record(
            "synthetic-recipes-pack-v1", "row-preserved-unknown"
        )
        fixture_record = self.fixture["datasets"][0]["records"][2]
        self.assertEqual(
            preserved.unknown_fields["future_exporter_metadata"],
            fixture_record["unknown_fields"]["future_exporter_metadata"],
        )
        self.assertEqual(preserved.source_locator.json_pointer, "/datasets/0/records/2")
        self.assertEqual(json.loads(preserved.raw_payload), fixture_record)

        # Unknown critical and source-extension conditions are quarantined and excluded.
        quarantined = self.repository.get_recipe(
            "synthetic-recipes-pack-v1",
            "synthetic:critical-unknown-condition",
            include_quarantined=True,
        )
        self.assertEqual(quarantined.status, RecipeStatus.QUARANTINED)
        self.assertEqual(quarantined.inputs[0].kind, IngredientKind.ITEM_EXACT)
        raw_critical = self.repository.get_raw_record(
            "synthetic-recipes-pack-v1", "row-unsupported-critical"
        )
        self.assertEqual(raw_critical.critical_unknown_fields, ("opaque_critical_condition",))
        self.assertEqual(raw_critical.source_locator.json_pointer, "/datasets/0/records/3")
        unsupported = self.repository.get_recipe(
            "synthetic-recipes-pack-v1",
            "synthetic:unsupported-source-extension",
            include_quarantined=True,
        )
        self.assertEqual(unsupported.status, RecipeStatus.QUARANTINED)
        self.assertEqual(unsupported.inputs[0].kind, IngredientKind.SOURCE_EXTENSION)
        self.assertEqual(unsupported.inputs[0].interpretation, IngredientInterpretation.UNSUPPORTED)


class RecipeLookupToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.repository = RecipeRepository(Path(self.tempdir.name) / "recipes.db")
        self.service = RecipeQueryService(self.repository)
        self.storage_executor = StorageExecutor(sqlite_workers=1, vector_workers=1, queue_capacity=2)
        self.key = PackVersionKey("fixture-pack", "v1", "game-a")
        _seed_dataset(
            self.repository,
            self.key,
            "tool-fixture-v1",
            [
                {
                    "recipe_id": "tool-recipe",
                    "include_recipe_locator": False,
                    "outputs": (RecipeOutput(IngredientKind.ITEM_EXACT, "fixture:product", Decimal("1"), "item"),),
                }
            ],
        )
        self.raw_lookup_calls = 0

        def unexpected_per_recipe_lookup(*args, **kwargs):
            self.raw_lookup_calls += 1
            raise AssertionError("tool serialization must use source locators from the offloaded query snapshot")

        self.repository.get_raw_record = unexpected_per_recipe_lookup
        self.context = ToolContext(
            request_id="fixture-request",
            policy_snapshot=PolicySnapshot(roles=frozenset({"group_member"})),
            result_max_chars=4000,
            extensions={
                "recipe_query_service": self.service,
                "recipe_query_runner": self.run_sqlite,
            },
        )

    async def run_sqlite(self, function, /, *args, **kwargs):
        return await self.storage_executor.run_sqlite(function, *args, **kwargs)

    async def asyncTearDown(self) -> None:
        await self.storage_executor.aclose()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_registry_schema_and_tool_return_structured_exact_evidence(self) -> None:
        self.assertIn("recipe_lookup", TOOL_REGISTRY.names())
        self.assertIn("recipe_machine_lookup", TOOL_REGISTRY.names())
        result = await TOOL_REGISTRY.dispatch(
            "recipe_lookup",
            {
                "pack_id": "fixture-pack",
                "pack_version": "v1",
                "game_version": "game-a",
                "identifier": "fixture:product",
                "kind": "item",
                "direction": "produces",
            },
            self.context,
        )
        self.assertEqual(result.status, ToolStatus.OK)
        self.assertEqual(result.content["match_quality"], "exact")
        self.assertEqual(result.content["recipes"][0]["recipe_id"], "tool-recipe")
        self.assertEqual(result.evidence[0].kind, "recipe")
        self.assertIn("synthetic://fixture/recipes.json", result.evidence[0].locator)
        self.assertEqual(result.content["pack_version"]["pack_version"], "v1")

        too_many = await TOOL_REGISTRY.dispatch(
            "recipe_lookup",
            {
                "pack_id": "fixture-pack",
                "pack_version": "v1",
                "game_version": "game-a",
                "identifier": "fixture:product",
                "kind": "item",
                "direction": "produces",
                "limit": 21,
            },
            self.context,
        )
        self.assertEqual(too_many.status, ToolStatus.INVALID_ARGUMENT)

    async def test_missing_version_is_rejected_and_wrong_version_is_not_fallback(self) -> None:
        missing = await TOOL_REGISTRY.dispatch(
            "recipe_lookup",
            {"pack_id": "fixture-pack", "identifier": "fixture:product", "kind": "item", "direction": "produces"},
            self.context,
        )
        self.assertEqual(missing.status, ToolStatus.INVALID_ARGUMENT)

        wrong_version = await TOOL_REGISTRY.dispatch(
            "recipe_lookup",
            {
                "pack_id": "fixture-pack",
                "pack_version": "v2",
                "game_version": "game-a",
                "identifier": "fixture:product",
                "kind": "item",
                "direction": "produces",
            },
            self.context,
        )
        self.assertEqual(wrong_version.status, ToolStatus.VERSION_MISMATCH)
        self.assertEqual(wrong_version.content["recipes"], [])

    async def test_machine_tool_is_read_only_and_returns_evidence(self) -> None:
        result = await TOOL_REGISTRY.dispatch(
            "recipe_machine_lookup",
            {
                "pack_id": "fixture-pack",
                "pack_version": "v1",
                "game_version": "game-a",
                "machine_id": "fixture:assembler",
            },
            self.context,
        )
        self.assertEqual(result.status, ToolStatus.OK)
        self.assertEqual(result.content["recipes"][0]["energy_eut"], 32)
        self.assertTrue(result.evidence)
        self.assertEqual(
            result.content["recipes"][0]["source_locator"],
            "synthetic://fixture/recipes.json#/recipes/tool-recipe",
        )
        self.assertEqual(self.raw_lookup_calls, 0)

    async def test_agent_appends_recipe_source_reference(self) -> None:
        class FakeLLM:
            def __init__(self):
                self.round = 0

            async def chat_with_tools(self, messages, tools, **kwargs):
                self.round += 1
                if self.round == 1:
                    return ChatWithToolsResponse(
                        text=None,
                        tool_calls=[
                            ToolCallRequest(
                                "recipe-call",
                                "recipe_lookup",
                                '{"pack_id":"fixture-pack","pack_version":"v1",'
                                '"game_version":"game-a","identifier":"fixture:product",'
                                '"kind":"item","direction":"produces"}',
                            )
                        ],
                    )
                return ChatWithToolsResponse(text="该配方产出 fixture:product。")

            async def chat(self, messages, **kwargs):
                return "最终回答"

        answer = await run_agent_loop(
            FakeLLM(),
            [],
            max_rounds=2,
            max_calls_per_round=2,
            max_total_calls=2,
            total_timeout=3,
            tool_timeout=1,
            results_max_chars=4000,
            tool_extensions={
                "recipe_query_service": self.service,
                "recipe_query_runner": self.run_sqlite,
            },
        )
        self.assertIn("参考：", answer)
        self.assertIn("synthetic://fixture/recipes.json", answer)


if __name__ == "__main__":
    unittest.main()

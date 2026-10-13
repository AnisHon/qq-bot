from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from app.knowledge.documents import DocumentSource
from app.knowledge.ingestion import MARKDOWN_CHUNKER_VERSION, chunk_markdown_document, parse_markdown_document
from app.knowledge.retrieval import DocumentKnowledgeService
from app.models import GroupMessageEvent
from app.recipes.importer import RecipeImporter
from app.recipes.models import PackVersionKey
from app.recipes.query import RecipeQueryService
from app.recipes.repository import RecipeRepository
from app.rag import RagStore
from app.sfm.contracts import SFMTargetVersion
from app.sfm.fake_validator import FakeSFMValidator
from app.sfm.knowledge import SFMSampleSourceManifest, SFMKnowledgeService
from app.sfm.validator import SFMValidationService
from app.sfm.workflow import SFMGenerationWorkflow
from app.storage_executor import StorageExecutor
from app.task_runtime import MessageTaskRuntime
from app.tools.agent import run_agent_loop
from app.tools.contracts import ToolStatus

from tests.test_recipe_query import _SyntheticJsonRecipeSource


_ROOT = Path(__file__).parent
_SFM_RETRIEVAL_FIXTURE = _ROOT / "fixtures" / "sfm" / "synthetic_sfm_retrieval.md"
_SFM_PROGRAM_FIXTURE = _ROOT / "fixtures" / "sfm" / "synthetic_program.sfm"
_RECIPE_FIXTURE = _ROOT / "fixtures" / "recipes" / "synthetic_recipe_set.json"
_SFM_VERSION = "sfm-fixture-a"
_MC_VERSION = "mc-fixture-a"
_RECIPE_KEY = PackVersionKey("synthetic-pack", "fixture-v1", "fixture-game")


class AgentFixtureIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """End-to-end registry checks over temporary SQLite and synthetic data only."""

    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="agent-fixture-integration-")
        root = Path(self.tempdir.name)
        self.storage = StorageExecutor(sqlite_workers=1, vector_workers=1, queue_capacity=12)
        self.rag = RagStore(root / "documents.db")
        self.documents = DocumentKnowledgeService(self.rag, None, self.storage)
        self.sfm = SFMKnowledgeService(self.documents)
        self.recipe_repository = RecipeRepository(root / "recipes.db")
        self.recipe_service = RecipeQueryService(self.recipe_repository)
        await self._seed_fixtures()

    async def asyncTearDown(self) -> None:
        await self.storage.aclose(timeout=2)
        self.tempdir.cleanup()

    async def _seed_fixtures(self) -> None:
        sfm_manifest = SFMSampleSourceManifest(
            source_id="synthetic-sfm-agent-integration",
            source_uri="fixture://sfm/sfm-fixture-a/mc-fixture-a/synthetic_sfm_retrieval.md",
            sfm_version=_SFM_VERSION,
            minecraft_version=_MC_VERSION,
            source_revision="synthetic-agent-r1",
            license_id="fixture-only",
        )
        source: DocumentSource = sfm_manifest.to_document_source()
        parsed = parse_markdown_document(
            _SFM_RETRIEVAL_FIXTURE.read_bytes(), source, document_id="synthetic-agent-sfm-doc"
        )
        chunks = chunk_markdown_document(parsed, max_chars=1200, overlap_chars=0)
        self.rag.replace_document(
            parsed.document_id,
            source.source_uri,
            parsed.content_hash,
            chunks,
            document_source=source,
            parser_version=parsed.parser_version,
            chunker_version=MARKDOWN_CHUNKER_VERSION,
        )

        fixture_bytes = _RECIPE_FIXTURE.read_bytes()
        fixture = json.loads(fixture_bytes)
        importer = RecipeImporter(self.recipe_repository)
        for dataset in fixture["datasets"]:
            report = importer.import_dataset(
                _SyntheticJsonRecipeSource(dataset, fixture_bytes), activate=True
            )
            self.assertTrue(report.valid)
            self.assertTrue(report.activated)

    async def test_agent_runs_document_recipe_and_sfm_fake_tools_with_isolated_versions(self) -> None:
        source_code = _SFM_PROGRAM_FIXTURE.read_text(encoding="utf-8").strip()
        generator = _FakeGenerator(source_code)
        planner = _FixtureToolPlanner(
            [
                _call(
                    "sfm-exact",
                    "sfm_examples",
                    {
                        "query": "synthetic router routing path",
                        "sfm_version": _SFM_VERSION,
                        "minecraft_version": _MC_VERSION,
                    },
                ),
                _call(
                    "sfm-mismatch",
                    "sfm_examples",
                    {
                        "query": "synthetic router routing path",
                        "sfm_version": "sfm-not-indexed",
                        "minecraft_version": _MC_VERSION,
                    },
                ),
                _call("recipe-v1", "recipe_lookup", _recipe_args("fixture-v1")),
                _call("recipe-mismatch", "recipe_lookup", _recipe_args("fixture-v3")),
                _call("recipe-v2", "recipe_lookup", _recipe_args("fixture-v2")),
            ],
            second_round_call=_call(
                "sfm-generate",
                "sfm_generate_validate",
                {
                    "description": "return the fixed synthetic sample",
                    "sfm_version": _SFM_VERSION,
                    "minecraft_version": _MC_VERSION,
                },
            ),
        )
        workflow = SFMGenerationWorkflow(
            generator,
            self.sfm,
            SFMValidationService(FakeSFMValidator(valid_samples={source_code})),
        )

        reply = await run_agent_loop(
            planner,
            [{"role": "user", "content": "check the local synthetic recipe and SFM example"}],
            max_rounds=4,
            max_calls_per_round=5,
            max_total_calls=8,
            total_timeout=5,
            tool_timeout=3,
            results_max_chars=12_000,
            tool_extensions={
                "sfm_knowledge_service": self.sfm,
                "sfm_generation_workflow": workflow,
                "recipe_query_service": self.recipe_service,
                "recipe_query_runner": self.storage.run_sqlite,
                "use_tool_cache": False,
            },
        )

        self.assertIn("fixture://sfm/sfm-fixture-a/mc-fixture-a/", reply)
        self.assertIn("fixture://synthetic_recipe_set.json", reply)
        tool_results = planner.tool_results
        self.assertEqual(
            set(tool_results),
            {"sfm-exact", "sfm-mismatch", "recipe-v1", "recipe-mismatch", "recipe-v2", "sfm-generate"},
        )

        # Assert each call's own response so another tool/version cannot mask a
        # cross-version leak in the integration path.
        self.assertIn("fixture://sfm/sfm-fixture-a/mc-fixture-a/", tool_results["sfm-exact"])
        self.assertTrue(tool_results["sfm-mismatch"].startswith("[version_mismatch]"))
        self.assertIn("本地资料存在", tool_results["sfm-mismatch"])
        self.assertTrue(tool_results["recipe-mismatch"].startswith("[version_mismatch]"))
        self.assertIn("已知该整合包的其他版本", tool_results["recipe-mismatch"])

        recipe_v1 = tool_results["recipe-v1"]
        recipe_v2 = tool_results["recipe-v2"]
        self.assertIn('"pack_version": "fixture-v1"', recipe_v1)
        self.assertIn('"pack_version": "fixture-v2"', recipe_v2)
        self.assertIn("synthetic:final-from-ore", recipe_v1)
        self.assertNotIn("synthetic:final-v2", recipe_v1)
        self.assertNotIn('"energy_eut": 128', recipe_v1)
        self.assertIn("synthetic:final-v2", recipe_v2)
        self.assertNotIn("synthetic:final-from-ore", recipe_v2)
        self.assertIn('"energy_eut": 128', recipe_v2)

        self.assertIn("simulated_valid", tool_results["sfm-generate"])
        self.assertIn('"is_real_validation": false', tool_results["sfm-generate"])
        self.assertEqual(len(planner.tool_ids), len(set(planner.tool_ids)))
        self.assertCountEqual(
            planner.tool_ids,
            ["sfm-exact", "sfm-mismatch", "recipe-v1", "recipe-mismatch", "recipe-v2", "sfm-generate"],
        )
        self.assertEqual(generator.calls, 1)

    async def test_empty_document_and_recipe_stores_report_explicit_unavailable_states(self) -> None:
        empty_rag = RagStore(Path(self.tempdir.name) / "empty-documents.db")
        empty_sfm = SFMKnowledgeService(DocumentKnowledgeService(empty_rag, None, self.storage))
        empty_recipe = RecipeQueryService(
            RecipeRepository(Path(self.tempdir.name) / "empty-recipes.db")
        )
        context = _FixtureToolPlanner(
            [
                _call(
                    "empty-sfm",
                    "sfm_examples",
                    {
                        "query": "anything",
                        "sfm_version": "missing-sfm",
                        "minecraft_version": "missing-mc",
                    },
                ),
                _call("empty-recipe", "recipe_lookup", _recipe_args("missing-pack-version", pack_id="missing-pack")),
            ]
        )
        await run_agent_loop(
            context,
            [{"role": "user", "content": "query missing local data"}],
            max_rounds=2,
            max_calls_per_round=2,
            max_total_calls=2,
            total_timeout=3,
            tool_timeout=2,
            results_max_chars=4000,
            tool_extensions={
                "sfm_knowledge_service": empty_sfm,
                "recipe_query_service": empty_recipe,
                "recipe_query_runner": self.storage.run_sqlite,
                "use_tool_cache": False,
            },
        )
        self.assertIn(ToolStatus.NOT_INDEXED.value, context.tool_results["empty-sfm"])
        self.assertIn(ToolStatus.DATA_UNAVAILABLE.value, context.tool_results["empty-recipe"])

    async def test_group_runtime_bounds_concurrent_fixture_queries_over_temp_databases(self) -> None:
        active = 0
        peak = 0
        both_started = asyncio.Event()
        release = asyncio.Event()
        completed: list[str] = []

        async def handle(event: GroupMessageEvent) -> None:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                both_started.set()
            try:
                await release.wait()
                document_result, recipe_result = await asyncio.gather(
                    self.sfm.search_examples(
                        "synthetic router routing path",
                        sfm_version=_SFM_VERSION,
                        minecraft_version=_MC_VERSION,
                    ),
                    self.storage.run_sqlite(
                        self.recipe_service.find_by_identifier,
                        _RECIPE_KEY,
                        "synthetic:final_component",
                        direction="produces",
                    ),
                )
                self.assertTrue(document_result.evidence)
                self.assertEqual(recipe_result.state.value, "available")
                completed.append(event.group_id)
            finally:
                active -= 1

        runtime = MessageTaskRuntime(handle, worker_count=2, queue_capacity=3)
        runtime.start()
        self.assertTrue(runtime.try_submit(_event("m1", "group-a")))
        self.assertTrue(runtime.try_submit(_event("m2", "group-b")))
        self.assertTrue(runtime.try_submit(_event("m3", "group-c")))
        await asyncio.wait_for(both_started.wait(), timeout=1)
        self.assertEqual(peak, 2)
        release.set()
        await asyncio.wait_for(runtime._queue.join(), timeout=3)
        await runtime.aclose(drain_timeout=1)

        self.assertCountEqual(completed, ["group-a", "group-b", "group-c"])
        self.assertLessEqual(peak, runtime.worker_count)


class _FixtureToolPlanner:
    def __init__(self, calls, *, second_round_call=None) -> None:
        self.calls = calls
        self.second_round_call = second_round_call
        self.round = 0
        self.tool_ids: list[str] = []
        self.tool_contents: list[str] = []
        self.tool_results: dict[str, str] = {}

    async def chat_with_tools(self, messages, *, tools, **kwargs):
        del kwargs
        self.round += 1
        if self.round == 1:
            self.available_tool_names = {item["function"]["name"] for item in tools}
            return _response(self.calls)
        if self.round == 2 and self.second_round_call is not None:
            self._capture(messages)
            return _response([self.second_round_call])
        self._capture(messages)
        from app.llm import ChatWithToolsResponse

        return ChatWithToolsResponse(text="Synthetic fixture observations were checked.")

    async def chat(self, messages, **kwargs):
        del messages, kwargs
        return "Synthetic fixture observations were checked."

    def _capture(self, messages) -> None:
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        self.tool_ids = [str(message["tool_call_id"]) for message in tool_messages]
        self.tool_contents = [str(message.get("content") or "") for message in tool_messages]
        self.tool_results = {
            str(message["tool_call_id"]): str(message.get("content") or "")
            for message in tool_messages
        }


class _FakeGenerator:
    def __init__(self, source: str) -> None:
        self.source = source
        self.calls = 0

    async def chat(self, messages, **kwargs) -> str:
        del messages, kwargs
        self.calls += 1
        return self.source


def _call(call_id: str, name: str, arguments: dict[str, object]):
    from app.llm import ToolCallRequest

    return ToolCallRequest(call_id, name, json.dumps(arguments, ensure_ascii=False))


def _response(calls):
    from app.llm import ChatWithToolsResponse

    return ChatWithToolsResponse(text=None, tool_calls=calls)


def _recipe_args(version: str, *, pack_id: str = "synthetic-pack") -> dict[str, str]:
    return {
        "pack_id": pack_id,
        "pack_version": version,
        "game_version": "fixture-game",
        "identifier": "synthetic:final_component",
        "kind": "item",
        "direction": "produces",
    }


def _event(message_id: str, group_id: str) -> GroupMessageEvent:
    return GroupMessageEvent(message_id, group_id, f"user-{message_id}", "Fixture", "query", False, 0, {})


if __name__ == "__main__":
    unittest.main()

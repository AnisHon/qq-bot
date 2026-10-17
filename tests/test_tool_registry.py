from __future__ import annotations

import asyncio
import hashlib
import json
from unittest import IsolatedAsyncioTestCase, TestCase

from app.models import SearchResult
from app.tools.agent import TOOL_REGISTRY
from app.tools.contracts import (
    Evidence,
    PolicySnapshot,
    ToolAccess,
    ToolContext,
    ToolPolicy,
    ToolResult,
    ToolSpec,
    ToolStatus,
    search_results_to_tool_result,
    tool_result_to_search_results,
)
from app.tools.registry import ToolRegistry
from app.usage_budget import BudgetScope, ToolUsage


GROUP_POLICY = ToolPolicy(allowed_roles=frozenset({"group_member"}))


def group_context(request_id: str, **kwargs) -> ToolContext:
    return ToolContext(
        request_id=request_id,
        policy_snapshot=PolicySnapshot(roles=frozenset({"group_member"})),
        **kwargs,
    )


class ToolRegistrySchemaTest(TestCase):
    def test_legacy_openai_schema_snapshot_is_unchanged(self) -> None:
        schemas = TOOL_REGISTRY.openai_schemas()
        old_schemas = schemas[:10]
        serialized = json.dumps(old_schemas, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertEqual(len(schemas), 17)
        self.assertEqual(
            {schema["function"]["name"] for schema in schemas[10:]},
            {
                "repository_search",
                "repository_read",
                "recipe_lookup",
                "recipe_machine_lookup",
                "sfm_examples",
                "sfm_generate_validate",
                "task_evidence_read",
            },
        )
        self.assertEqual(
            hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            "bcd6ed210d8615e1f0e91323c4eaefbb58085d3588d1ea2afd2465c1d47676ea",
        )

    def test_duplicate_names_and_frozen_registry_are_rejected(self) -> None:
        async def handler(context, arguments):
            return ToolResult(status=ToolStatus.OK, content="ok")

        spec = ToolSpec("sample", "sample tool", {"type": "object"}, handler, policy=GROUP_POLICY)
        registry = ToolRegistry([spec])
        with self.assertRaisesRegex(ValueError, "duplicate tool name"):
            registry.register(spec)
        registry.freeze()
        with self.assertRaisesRegex(RuntimeError, "frozen"):
            registry.register(ToolSpec("other", "other", {"type": "object"}, handler, policy=GROUP_POLICY))

    def test_tool_spec_requires_an_explicit_policy_audience(self) -> None:
        async def handler(context, arguments):
            return ToolResult(status=ToolStatus.OK, content="ok")

        with self.assertRaisesRegex(ValueError, "must declare an allowed role"):
            ToolSpec("implicit", "implicit", {"type": "object"}, handler, policy=ToolPolicy())

    def test_search_result_adapter_preserves_content_and_provenance(self) -> None:
        source = [SearchResult("manual", "section text", "https://example.test/doc#section", "网页读取")]
        contract = search_results_to_tool_result(source)
        self.assertEqual(contract.status, ToolStatus.OK)
        self.assertEqual(contract.evidence[0].kind, "web_page")
        self.assertEqual(contract.evidence[0].locator, source[0].url)
        self.assertEqual(tool_result_to_search_results(contract), source)


class ToolRegistryDispatchTest(IsolatedAsyncioTestCase):
    @staticmethod
    def schema() -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 20},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 2,
                },
            },
            "required": ["query"],
        }

    async def test_dispatch_validates_arguments_and_injects_budget_context(self) -> None:
        seen: list[object] = []

        async def handler(context, arguments):
            seen.append((context.budget_scope, context.budget_controller, arguments))
            return ToolResult(
                status=ToolStatus.OK,
                content={"query": arguments["query"]},
                usage=ToolUsage(tool_call_count=1),
            )

        registry = ToolRegistry([ToolSpec("lookup", "lookup", self.schema(), handler, policy=GROUP_POLICY)])
        budget_scope = BudgetScope(group_id="g1", user_id="u1", request_id="r1")
        controller = object()
        context = ToolContext(
            request_id="r1",
            group_id="g1",
            user_id="u1",
            policy_snapshot=PolicySnapshot(roles=frozenset({"group_member"})),
            budget_scope=budget_scope,
            budget_controller=controller,
        )

        result = await registry.dispatch("lookup", '{"query":"find","tags":["a"]}', context)
        self.assertEqual(result.status, ToolStatus.OK)
        self.assertEqual(result.content, {"query": "find"})
        self.assertEqual(seen, [(budget_scope, controller, {"query": "find", "tags": ["a"]})])
        self.assertEqual(registry.openai_schemas()[0]["function"]["name"], "lookup")

    async def test_argument_validator_can_normalize_legacy_alias_before_schema_check(self) -> None:
        received: list[dict] = []

        async def handler(context, arguments):
            received.append(arguments)
            return ToolResult(status=ToolStatus.OK, content="ok")

        def normalize(arguments):
            normalized = dict(arguments)
            if "location" not in normalized and "query" in normalized:
                normalized["location"] = normalized.pop("query")
            return normalized

        schema = {
            "type": "object",
            "properties": {"location": {"type": "string"}},
            "required": ["location"],
        }
        registry = ToolRegistry(
            [ToolSpec("weather", "weather", schema, handler, policy=GROUP_POLICY, argument_validator=normalize)]
        )
        result = await registry.dispatch("weather", '{"query":"Shanghai"}', group_context("r"))
        self.assertEqual(result.status, ToolStatus.OK)
        self.assertEqual(received, [{"location": "Shanghai"}])

    async def test_malformed_and_out_of_schema_arguments_are_rejected_before_handler(self) -> None:
        calls = 0

        async def handler(context, arguments):
            nonlocal calls
            calls += 1
            return ToolResult(status=ToolStatus.OK, content="unexpected")

        registry = ToolRegistry([ToolSpec("lookup", "lookup", self.schema(), handler, policy=GROUP_POLICY)])
        cases = (
            "{bad json",
            "[]",
            '{"unknown":"x"}',
            '{"query":3}',
            '{"query":"x","tags":[]}',
            '{"query":"x","tags":["a","b","c"]}',
            json.dumps({"query": "x" * 21}),
        )
        for raw in cases:
            with self.subTest(raw=raw[:24]):
                result = await registry.dispatch("lookup", raw, group_context("r"))
                self.assertEqual(result.status, ToolStatus.INVALID_ARGUMENT)
                self.assertEqual(result.error.code, "invalid_arguments")
        self.assertEqual(calls, 0)

    async def test_policy_denial_filters_schema_and_rejects_dispatch(self) -> None:
        async def handler(context, arguments):
            return ToolResult(status=ToolStatus.OK, content="secret operation")

        spec = ToolSpec(
            "restricted",
            "restricted",
            {"type": "object", "properties": {}, "required": []},
            handler,
            policy=ToolPolicy(access=ToolAccess.LOCAL_READ, required_permissions=frozenset({"repository:read"})),
        )
        registry = ToolRegistry([spec], frozen=True)
        denied_context = ToolContext(request_id="r", policy_snapshot=PolicySnapshot())
        self.assertEqual(registry.openai_schemas(denied_context.policy_snapshot), [])
        self.assertEqual(registry.names(denied_context.policy_snapshot), frozenset())
        denied = await registry.dispatch("restricted", "{}", denied_context)
        self.assertEqual(denied.status, ToolStatus.DENIED)
        self.assertNotIn("secret operation", str(denied.content))

        allowed = ToolContext(
            request_id="r",
            policy_snapshot=PolicySnapshot(permissions=frozenset({"repository:read"})),
        )
        self.assertEqual(len(registry.openai_schemas(allowed.policy_snapshot)), 1)
        self.assertEqual((await registry.dispatch("restricted", "{}", allowed)).status, ToolStatus.OK)

    async def test_handler_failure_is_mapped_without_exposing_exception(self) -> None:
        async def handler(context, arguments):
            raise RuntimeError("provider secret or private response body")

        registry = ToolRegistry(
            [ToolSpec("lookup", "lookup", {"type": "object"}, handler, policy=GROUP_POLICY)],
            frozen=True,
        )
        result = await registry.dispatch("lookup", "{}", group_context("r"))
        self.assertEqual(result.status, ToolStatus.ERROR)
        self.assertEqual(result.error.code, "tool_execution_error")
        self.assertNotIn("provider secret", str(result.content))

    async def test_timeout_and_cancellation_release_concurrency_slot(self) -> None:
        cancelled = asyncio.Event()

        async def blocking(context, arguments):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        registry = ToolRegistry(
            [ToolSpec("slow", "slow", {"type": "object"}, blocking, policy=GROUP_POLICY, timeout_seconds=0.02)],
        )
        timed_out = await registry.dispatch("slow", "{}", group_context("r"))
        self.assertEqual(timed_out.status, ToolStatus.TIMED_OUT)
        self.assertTrue(cancelled.is_set())

        task = asyncio.create_task(registry.dispatch("slow", "{}", group_context("r2")))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())

    async def test_tool_timeout_remains_a_tool_result_when_request_deadline_is_longer(self) -> None:
        async def blocking(context, arguments):
            await asyncio.Event().wait()

        registry = ToolRegistry(
            [ToolSpec("slow", "slow", {"type": "object"}, blocking, policy=GROUP_POLICY, timeout_seconds=1)],
        )
        context = ToolContext(
            request_id="r",
            policy_snapshot=PolicySnapshot(roles=frozenset({"group_member"})),
            deadline_monotonic=asyncio.get_running_loop().time() + 1,
            tool_timeout_seconds=0.01,
        )
        result = await registry.dispatch("slow", "{}", context)
        self.assertEqual(result.status, ToolStatus.TIMED_OUT)

    async def test_per_tool_concurrency_is_bounded(self) -> None:
        active = 0
        peak = 0

        async def handler(context, arguments):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return ToolResult(status=ToolStatus.OK, content="ok")

        registry = ToolRegistry(
            [ToolSpec("limited", "limited", {"type": "object"}, handler, policy=GROUP_POLICY, max_concurrency=1)],
        )
        await asyncio.gather(
            *(registry.dispatch("limited", "{}", group_context(str(i))) for i in range(5))
        )
        self.assertEqual(peak, 1)

    async def test_evidence_and_structured_result_are_bounded(self) -> None:
        async def handler(context, arguments):
            return ToolResult(
                status=ToolStatus.OK,
                content={"payload": "x" * 100},
                evidence=(Evidence("document", "d1", "doc", "d.md#L1"), Evidence("document", "d2", "doc2", "d2.md#L2")),
            )

        registry = ToolRegistry(
            [ToolSpec("lookup", "lookup", {"type": "object"}, handler, policy=GROUP_POLICY, max_result_chars=16)],
        )
        result = await registry.dispatch(
            "lookup",
            "{}",
            group_context("r", evidence_limit=1),
        )
        self.assertEqual(result.status, ToolStatus.PARTIAL)
        self.assertEqual(len(result.evidence), 1)
        self.assertIsInstance(result.content, str)
        self.assertIn("truncated", result.content)

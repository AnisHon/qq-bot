from __future__ import annotations

import asyncio
from dataclasses import replace
import time
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from app.llm import ChatWithToolsResponse, ToolCallRequest
from app.context.contracts import ContextEpochChanged
from app.models import SearchResult
from app.tools.agent import TOOL_REGISTRY
from app.tools.contracts import search_results_to_tool_result
from app.progress.events import AgentEventKind, RequestAgentEventSink, TaskIdentity
from app.tools.agent import _parse_dsml_tool_calls, _tool_call_message, _tool_signature, _validate_research_answer, run_agent_loop


class _FakeLLM:
    def __init__(self) -> None:
        self.round = 0

    async def chat_with_tools(self, messages, tools, temperature=0.25):
        self.round += 1
        if self.round == 1:
            return ChatWithToolsResponse(
                text=None,
                tool_calls=[
                    ToolCallRequest(f"call-{idx}", "weather", f'{{"location":"城市{idx}"}}')
                    for idx in range(3)
                ],
            )
        return ChatWithToolsResponse(text="最终回答")

    async def chat(self, messages, temperature=0.7, max_tokens=None):
        return "降级回答"


class _ToolRequestLLM:
    def __init__(self, tool_count: int = 2) -> None:
        self.tool_count = tool_count
        self.chat_called = False

    async def chat_with_tools(self, messages, tools, temperature=0.25, **kwargs):
        return ChatWithToolsResponse(
            text=None,
            tool_calls=[
                ToolCallRequest(f"call-{index}", "weather", f'{{"location":"城市{index}"}}')
                for index in range(self.tool_count)
            ],
        )

    async def chat(self, messages, temperature=0.7, max_tokens=None, **kwargs):
        self.chat_called = True
        return "final"


class AgentLoopTest(IsolatedAsyncioTestCase):
    async def test_context_guard_prevents_stale_model_dispatch(self) -> None:
        llm = _FakeLLM()

        async def stale_context() -> bool:
            return False

        with self.assertRaises(ContextEpochChanged):
            await run_agent_loop(
                llm,
                [{"role": "user", "content": "request"}],
                max_rounds=2,
                max_calls_per_round=1,
                max_total_calls=1,
                total_timeout=5,
                tool_timeout=1,
                results_max_chars=1000,
                context_guard=stale_context,
            )

        self.assertEqual(llm.round, 0)

    def test_reversed_ftb_task_relation_is_rejected(self) -> None:
        messages = [
            {
                "role": "tool",
                "content": "关系检测：目标作为任务要求出现 1 次；这不是配方输出。 allthetweaks:atm_star",
            }
        ]
        answer = "完成FTB任务后会奖励 allthetweaks:atm_star。"
        corrected = _validate_research_answer(answer, messages)
        self.assertNotIn("会奖励", corrected)
        self.assertIn("JEI/EMI", corrected)

    def test_denial_of_evidence_confirmed_item_is_rejected(self) -> None:
        messages = [{"role": "tool", "content": "任务目标 item: allthetweaks:atm_star"}]
        corrected = _validate_research_answer("ATM10里没有叫ATM之星的物品。", messages)
        self.assertNotIn("没有叫", corrected)
        self.assertIn("allthetweaks:atm_star", corrected)

    def test_dsml_content_is_recovered_as_tool_call(self) -> None:
        text = '''<｜｜DSML｜｜tool_calls>
<｜｜DSML｜｜invoke name="github_repo_inspect">
<｜｜DSML｜｜parameter name="repository" string="true">AllTheMods/ATM-10</｜｜DSML｜｜parameter>
<｜｜DSML｜｜parameter name="query" string="true">ATM Star recipe</｜｜DSML｜｜parameter>
</｜｜DSML｜｜invoke>
</｜｜DSML｜｜tool_calls>'''
        calls = _parse_dsml_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "github_repo_inspect")
        self.assertIn("AllTheMods/ATM-10", calls[0].arguments)

    def test_equivalent_tool_calls_have_same_signature(self) -> None:
        first = ToolCallRequest("1", "web_search", '{"query":" ATM9   Recipe "}')
        second = ToolCallRequest("2", "web_search", '{"query": "atm9 recipe"}')
        self.assertEqual(_tool_signature(first), _tool_signature(second))

    def test_tool_call_extension_is_preserved(self) -> None:
        call = ToolCallRequest(
            "1",
            "weather",
            '{"location":"北京"}',
            {"google": {"thought_signature": "opaque"}},
        )
        self.assertEqual(_tool_call_message(call)["extra_content"]["google"]["thought_signature"], "opaque")

    async def test_every_tool_call_gets_a_response_when_round_is_capped(self) -> None:
        messages = [{"role": "user", "content": "查询多个城市天气"}]
        result = [SearchResult("天气", "晴", "", "天气")]
        weather_spec = TOOL_REGISTRY.get("weather")

        async def fake_weather(context, arguments):
            return search_results_to_tool_result(result)

        with patch.dict(TOOL_REGISTRY._specs, {"weather": replace(weather_spec, handler=fake_weather)}):
            answer = await run_agent_loop(
                _FakeLLM(),
                messages,
                max_rounds=2,
                max_calls_per_round=2,
                max_total_calls=2,
                total_timeout=10,
                tool_timeout=2,
                results_max_chars=2000,
            )

        self.assertEqual(answer, "最终回答")
        tool_ids = [message["tool_call_id"] for message in messages if message["role"] == "tool"]
        self.assertEqual(tool_ids, ["call-0", "call-1", "call-2"])

    async def test_total_deadline_cancels_all_parallel_tools_and_caps_tool_timeout(self) -> None:
        llm = _ToolRequestLLM()
        started = 0
        all_started = asyncio.Event()
        cancelled: set[str] = set()
        passed_timeouts: list[float] = []

        async def blocking_tool(context, arguments):
            nonlocal started
            started += 1
            passed_timeouts.append(
                min(context.tool_timeout_seconds, context.deadline_monotonic - asyncio.get_running_loop().time())
            )
            if started == 2:
                all_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.add(context.request_id)
                raise

        started_at = time.monotonic()
        weather_spec = TOOL_REGISTRY.get("weather")
        with patch.dict(TOOL_REGISTRY._specs, {"weather": replace(weather_spec, handler=blocking_tool)}):
            with self.assertRaises(TimeoutError):
                await run_agent_loop(
                    llm,
                    [{"role": "user", "content": "test"}],
                    max_rounds=2,
                    max_calls_per_round=2,
                    max_total_calls=2,
                    total_timeout=0.08,
                    tool_timeout=10,
                    results_max_chars=2000,
                )

        self.assertTrue(all_started.is_set())
        self.assertEqual(cancelled, {"agent-call-0", "agent-call-1"})
        self.assertTrue(all(0 < timeout < 10 for timeout in passed_timeouts))
        self.assertLess(time.monotonic() - started_at, 0.5)

    async def test_registry_lifecycle_events_do_not_await_progress_delivery(self) -> None:
        llm = _ToolRequestLLM(tool_count=1)
        tool_started = asyncio.Event()
        tool_cancelled = asyncio.Event()
        events = []

        async def blocked_tool(_context, _arguments):
            tool_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                tool_cancelled.set()
                raise

        weather_spec = TOOL_REGISTRY.get("weather")
        identity = TaskIdentity.create(
            request_id="progress-test", group_id="g1", user_id="u1", epoch=0, origin="user_message"
        )
        sink = RequestAgentEventSink(identity, events.append)
        with patch.dict(TOOL_REGISTRY._specs, {"weather": replace(weather_spec, handler=blocked_tool)}):
            with self.assertRaises(TimeoutError):
                await run_agent_loop(
                    llm,
                    [{"role": "user", "content": "test"}],
                    max_rounds=1,
                    max_calls_per_round=1,
                    max_total_calls=1,
                    total_timeout=0.02,
                    tool_timeout=1,
                    results_max_chars=2000,
                    task_identity=identity,
                    event_sink=sink,
                )

        self.assertTrue(tool_started.is_set())
        self.assertTrue(tool_cancelled.is_set())
        self.assertEqual(
            [event.kind for event in events],
            [
                AgentEventKind.WORKING_STATE_NOT_CONFIGURED,
                AgentEventKind.MODEL_REQUEST_STARTED,
                AgentEventKind.MODEL_REQUEST_COMPLETED,
                AgentEventKind.TOOL_STARTED,
                AgentEventKind.TOOL_FAILED,
            ],
        )

    async def test_total_deadline_cancels_model_call_and_skips_final_call(self) -> None:
        class BlockingModel(_ToolRequestLLM):
            def __init__(self) -> None:
                super().__init__()
                self.started = asyncio.Event()
                self.cancelled = asyncio.Event()

            async def chat_with_tools(self, messages, tools, temperature=0.25, **kwargs):
                self.started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    raise

        llm = BlockingModel()
        with self.assertLogs("app.tools.agent", level="WARNING"):
            result = await run_agent_loop(
                llm,
                [{"role": "user", "content": "test"}],
                max_rounds=1,
                max_calls_per_round=1,
                max_total_calls=1,
                total_timeout=0.02,
                tool_timeout=1,
                results_max_chars=2000,
            )
        self.assertTrue(llm.started.is_set())
        self.assertTrue(llm.cancelled.is_set())
        self.assertFalse(llm.chat_called)
        self.assertIn("Agent deadline expired", result)

    async def test_external_cancellation_cleans_up_parallel_tools(self) -> None:
        llm = _ToolRequestLLM()
        all_started = asyncio.Event()
        started = 0
        cancelled: set[str] = set()

        async def blocking_tool(context, arguments):
            nonlocal started
            started += 1
            if started == 2:
                all_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.add(context.request_id)
                raise

        weather_spec = TOOL_REGISTRY.get("weather")
        with patch.dict(TOOL_REGISTRY._specs, {"weather": replace(weather_spec, handler=blocking_tool)}):
            task = asyncio.create_task(
                run_agent_loop(
                    llm,
                    [{"role": "user", "content": "test"}],
                    max_rounds=2,
                    max_calls_per_round=2,
                    max_total_calls=2,
                    total_timeout=10,
                    tool_timeout=10,
                    results_max_chars=2000,
                )
            )
            await all_started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(cancelled, {"agent-call-0", "agent-call-1"})

    async def test_expired_deadline_does_not_start_a_three_second_final_call(self) -> None:
        llm = _ToolRequestLLM(tool_count=0)
        result = await run_agent_loop(
            llm,
            [{"role": "user", "content": "test"}],
            max_rounds=0,
            max_calls_per_round=1,
            max_total_calls=1,
            total_timeout=10,
            tool_timeout=1,
            results_max_chars=2000,
            deadline=asyncio.get_running_loop().time() - 1,
        )
        self.assertFalse(llm.chat_called)
        self.assertIn("Agent deadline expired", result)

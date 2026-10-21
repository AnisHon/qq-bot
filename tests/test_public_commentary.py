from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from unittest.mock import patch

from app.llm import ChatWithToolsResponse, ToolCallRequest
from app.progress.commentary import PUBLIC_COMMENTARY_TOOL, public_commentary_spec
from app.progress.coordinator import ProgressCoordinator
from app.progress.events import (
    AgentEventKind,
    AgentEventPayload,
    CommentaryKind,
    PublicCommentary,
    RequestAgentEventSink,
    TaskIdentity,
    ToolCategory,
    ToolOutcome,
)
from app.progress.reporter import FinalDeliveryStatus, QQProgressReporter
from app.qq_dispatcher import QQMessageDispatcher
from app.tools.agent import TOOL_REGISTRY, agent_tool_schemas, run_agent_loop
from app.tools.contracts import PolicySnapshot, ToolContext, ToolResult, ToolStatus
from tests.test_progress_coordinator import FakeClock


class _FakeLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.schemas: list[list[dict]] = []
        self.messages: list[list[dict]] = []

    async def chat_with_tools(self, messages, tools, **_kwargs):
        self.calls += 1
        self.schemas.append(tools)
        self.messages.append([dict(item) for item in messages])
        if self.calls == 1:
            return ChatWithToolsResponse(
                text=None,
                tool_calls=[
                    ToolCallRequest(
                        "plan-1", PUBLIC_COMMENTARY_TOOL,
                        '{"kind":"plan","text":"我会先查询北京昨日天气，再计算目标数字，检索并核实相关新闻，最后依据真实新闻开头创作故事。"}',
                    ),
                    ToolCallRequest("weather-1", "weather", '{"location":"北京"}'),
                ],
            )
        if self.calls == 2:
            return ChatWithToolsResponse(
                text=None,
                tool_calls=[
                    ToolCallRequest(
                        "update-1", PUBLIC_COMMENTARY_TOOL,
                        '{"kind":"update","text":"天气查询已完成，我会继续检索相关报道。","related_tool_call_ids":["weather-1"]}',
                    ),
                    ToolCallRequest("news-1", "web_search", '{"query":"fixture-only news query"}'),
                ],
            )
        return ChatWithToolsResponse(
            text="没有检索到可核实的新闻来源；模拟天气数据不能代表北京真实气温，所以不能据此编造基于真实报道的故事。"
        )


class _FakeQQAdapter:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.sent_event = asyncio.Event()

    async def send_group_message(self, _group_id, text, reply_to="") -> None:
        self.sent.append(text)
        self.sent_event.set()


class PublicCommentaryContractTests(unittest.TestCase):
    def test_commentary_normalizes_text_and_escapes_cq_markup(self) -> None:
        row = PublicCommentary(
            CommentaryKind.PLAN, "  我会先查资料。\n [CQ:at,qq=123]  ", "task-1", 1,
        )
        self.assertEqual(row.text, "我会先查资料。 ［CQ:at,qq=123]")

    def test_commentary_rejects_secrets_urls_and_oversized_text(self) -> None:
        for text in (
            "打开 https://example.invalid/page",
            "API_KEY=not-a-public-value",
            "x" * 241,
        ):
            with self.subTest(text=text[:24]), self.assertRaises(ValueError):
                PublicCommentary(CommentaryKind.PLAN, text, "task-1", 1)

    def test_public_tool_schema_is_hidden_without_live_progress_sink(self) -> None:
        policy = PolicySnapshot(roles=frozenset({"group_member"}))
        identity = TaskIdentity.create(
            request_id="r1", group_id="g1", user_id="u1", epoch=0, origin="user_message",
        )
        without_sink = agent_tool_schemas(policy, task_identity=identity, event_sink=RequestAgentEventSink(identity))
        self.assertNotIn(PUBLIC_COMMENTARY_TOOL, {row["function"]["name"] for row in without_sink})

        with_sink = agent_tool_schemas(
            policy, task_identity=identity, event_sink=RequestAgentEventSink(identity, lambda _event: True),
        )
        self.assertIn(PUBLIC_COMMENTARY_TOOL, {row["function"]["name"] for row in with_sink})

        quiet_coordinator = ProgressCoordinator(clock=FakeClock())
        quiet_sink = quiet_coordinator.register(identity, "quiet")
        quiet = agent_tool_schemas(
            policy, task_identity=identity, event_sink=quiet_sink,
        )
        self.assertNotIn(PUBLIC_COMMENTARY_TOOL, {row["function"]["name"] for row in quiet})


class PublicCommentaryAgentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_plan_is_rejected_after_application_plan_was_announced(self) -> None:
        identity = TaskIdentity.create(
            request_id="announced-plan", group_id="g1", user_id="u1", epoch=1,
            origin="user_message",
        )
        coordinator = ProgressCoordinator(clock=FakeClock())
        sink = coordinator.register(identity)
        plan = PublicCommentary(CommentaryKind.PLAN, "我先查资料，再整理答案。", identity.task_id, 1)
        self.assertTrue(sink.emit(AgentEventKind.PUBLIC_COMMENTARY, round_index=1, commentary=plan))
        context = ToolContext(
            request_id=identity.request_id, group_id=identity.group_id, user_id=identity.user_id,
            task_identity=identity, event_sink=sink, tool_call_id="duplicate-plan",
            agent_round=1, extensions={"agent_plan_publicly_announced": True},
        )
        result = await public_commentary_spec().handler(context, {
            "kind": "plan", "text": "我会按步骤处理：查资料；再整理答案。",
        })
        self.assertEqual(result.status, ToolStatus.INVALID_ARGUMENT)
        self.assertEqual(coordinator.metrics["commentary_accepted"], 1)
        self.assertEqual(coordinator.metrics["commentary_rejected"], 0)
        await coordinator.aclose()

    async def test_fast_plan_and_tool_completion_are_suppressed_before_three_seconds(self) -> None:
        clock = FakeClock()
        coordinator = ProgressCoordinator(clock=clock)
        coordinator.start()
        identity = TaskIdentity.create(
            request_id="fast-plan", group_id="g1", user_id="u1", epoch=0,
            origin="user_message",
        )
        sink = coordinator.register(identity)
        sink.emit(AgentEventKind.TASK_STARTED)
        sink.emit(AgentEventKind.MODEL_REQUEST_STARTED, round_index=1)
        plan = PublicCommentary(
            CommentaryKind.PLAN, "我会先查资料再整理答案。", identity.task_id, 1,
        )
        self.assertTrue(sink.emit(
            AgentEventKind.PUBLIC_COMMENTARY, round_index=1, commentary=plan,
        ))
        sink.emit(AgentEventKind.TOOL_STARTED,
                  AgentEventPayload(category=ToolCategory.WEB_SEARCH), tool_call_id="search")
        await clock.advance(2)
        sink.emit(AgentEventKind.TOOL_COMPLETED,
                  AgentEventPayload(category=ToolCategory.WEB_SEARCH, outcome=ToolOutcome.SUCCESS),
                  tool_call_id="search")
        sink.emit(AgentEventKind.TASK_COMPLETED)
        await clock.advance(5)
        self.assertEqual(coordinator.metrics["candidates_offered"], 0)
        self.assertEqual(coordinator.mailbox.size, 0)
        await coordinator.aclose()

    async def test_multistep_fixture_publishes_plan_in_normal_tool_round_and_preserves_protocol(self) -> None:
        clock = FakeClock()
        coordinator = ProgressCoordinator(clock=clock)
        identity = TaskIdentity.create(
            request_id="multi-step-fixture", group_id="g1", user_id="u1", epoch=3,
            origin="user_message",
        )
        coordinator.start()
        sink = coordinator.register(identity)
        sink.emit(AgentEventKind.TASK_STARTED)
        adapter = _FakeQQAdapter()
        dispatcher = QQMessageDispatcher(adapter=adapter, max_chars=500, part_delay=0, status_window=0)
        reporter = QQProgressReporter(
            coordinator,
            dispatcher,
            read_epoch=lambda _group: asyncio.sleep(0, result=3),
            workers=1,
        )
        reporter.start()
        weather_started = asyncio.Event()
        release_weather = asyncio.Event()
        llm = _FakeLLM()

        async def fake_weather(_context, _arguments):
            weather_started.set()
            await release_weather.wait()
            # Fixture intentionally contains no asserted real-world temperature.
            return ToolResult(ToolStatus.OK, {"fixture": "temperature omitted"})

        async def fake_web_search(_context, _arguments):
            return ToolResult(ToolStatus.NO_RESULTS, {"results": []})

        original_weather = TOOL_REGISTRY.get("weather")
        original_search = TOOL_REGISTRY.get("web_search")
        try:
            with patch.dict(TOOL_REGISTRY._specs, {
                "weather": replace(original_weather, handler=fake_weather),
                "web_search": replace(original_search, handler=fake_web_search),
            }):
                task = asyncio.create_task(run_agent_loop(
                    llm,
                    [{"role": "user", "content": "用北京昨天平均气温加114514查新闻并据报道开头写故事"}],
                    max_rounds=5,
                    max_calls_per_round=4,
                    max_total_calls=12,
                    total_timeout=10,
                    tool_timeout=3,
                    results_max_chars=4000,
                    policy_snapshot=PolicySnapshot(roles=frozenset({"group_member"})),
                    task_identity=identity,
                    event_sink=sink,
                ))
                await asyncio.wait_for(weather_started.wait(), timeout=1)
                plan = (
                    "我会先查询北京昨日天气，再计算目标数字，检索并核实相关新闻，最后依据真实新闻开头创作故事。"
                )
                for term in ("天气", "计算", "新闻", "核实", "故事"):
                    self.assertIn(term, plan)

                # The plan is emitted from the same native Function Calling turn;
                # the active weather tool keeps the task alive until the 3s gate.
                self.assertEqual(llm.calls, 1)
                await clock.advance(3)
                await asyncio.wait_for(adapter.sent_event.wait(), timeout=0.5)
                self.assertEqual(adapter.sent[0], plan)

                release_weather.set()
                answer = await asyncio.wait_for(task, timeout=3)
                self.assertIn("没有检索到可核实的新闻", answer)
                self.assertEqual(llm.calls, 3)  # no extra commentary-only model request
                self.assertTrue(all(
                    PUBLIC_COMMENTARY_TOOL in {row["function"]["name"] for row in schemas}
                    for schemas in llm.schemas
                ))
                self.assertEqual(coordinator.metrics["commentary_accepted"], 2)
                self.assertEqual(coordinator.metrics["commentary_rejected"], 0)

                # Every native tool_call, including the control function, has a paired result.
                for messages in llm.messages:
                    for index, message in enumerate(messages):
                        if message.get("role") != "assistant" or not message.get("tool_calls"):
                            continue
                        expected = [call["id"] for call in message["tool_calls"]]
                        actual = [
                            row.get("tool_call_id") for row in messages[index + 1:index + 1 + len(expected)]
                        ]
                        self.assertEqual(actual, expected)
                sink.emit(AgentEventKind.TASK_COMPLETED)
                delivery = await reporter.send_final(
                    "g1", answer, task_id=identity.task_id, epoch=identity.epoch,
                )
                self.assertEqual(delivery, FinalDeliveryStatus.ACCEPTED_BY_ADAPTER)
                self.assertEqual(adapter.sent[-1], answer)
                self.assertEqual(len(adapter.sent), 2)
        finally:
            release_weather.set()
            await reporter.aclose(timeout=1)
            await coordinator.aclose()

    async def test_update_cannot_claim_success_from_no_results(self) -> None:
        coordinator = ProgressCoordinator(clock=FakeClock())
        identity = TaskIdentity.create(
            request_id="mismatch", group_id="g1", user_id="u1", epoch=0, origin="user_message",
        )
        sink = coordinator.register(identity)
        sink.emit(AgentEventKind.TOOL_STARTED,
                  AgentEventPayload(category=ToolCategory.WEB_SEARCH), tool_call_id="search-1")
        sink.emit(AgentEventKind.TOOL_COMPLETED,
                  AgentEventPayload(category=ToolCategory.WEB_SEARCH, outcome=ToolOutcome.NO_RESULTS),
                  tool_call_id="search-1")
        comment = PublicCommentary(
            CommentaryKind.UPDATE, "已经成功找到相关新闻。", identity.task_id, 2, ("search-1",),
        )
        self.assertFalse(sink.emit(
            AgentEventKind.PUBLIC_COMMENTARY, round_index=2, commentary=comment,
        ))
        revision = PublicCommentary(
            CommentaryKind.REVISION, "目前没有检索到结果，我会避免编造来源。",
            identity.task_id, 2, ("search-1",),
        )
        self.assertTrue(sink.emit(
            AgentEventKind.PUBLIC_COMMENTARY, round_index=2, commentary=revision,
        ))
        self.assertEqual(coordinator.metrics["commentary_rejected"], 1)
        self.assertEqual(coordinator.metrics["commentary_accepted"], 1)
        await coordinator.aclose()


if __name__ == "__main__":
    unittest.main()

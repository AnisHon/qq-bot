from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from unittest.mock import patch

from app.llm import ChatWithToolsResponse, ToolCallRequest
from app.progress.coordinator import ProgressCoordinator
from app.progress.events import AgentEventKind, RequestAgentEventSink, TaskIdentity
from app.progress.reporter import FinalDeliveryStatus, QQProgressReporter
from app.qq_dispatcher import QQMessageDispatcher
from app.tools.agent import TOOL_REGISTRY, run_agent_loop
from app.tools.contracts import PolicySnapshot, ToolResult, ToolStatus
from tests.test_progress_coordinator import FakeClock


class _FakeAgentLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_tools(self, _messages, tools, **_kwargs):
        del tools
        self.calls += 1
        if self.calls == 1:
            return ChatWithToolsResponse(
                text=None,
                tool_calls=[ToolCallRequest("weather-call", "weather", '{"location":"hidden query"}')],
            )
        return ChatWithToolsResponse(text="final answer")


class _FakeQQAdapter:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.sent_event = asyncio.Event()

    async def send_group_message(self, _group_id, text, reply_to="") -> None:
        self.sent.append(text)
        self.sent_event.set()


class AgentProgressIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_registry_events_reach_reporter_then_final_in_order(self) -> None:
        clock = FakeClock()
        coordinator = ProgressCoordinator(clock=clock)
        adapter = _FakeQQAdapter()
        dispatcher = QQMessageDispatcher(adapter=adapter, max_chars=500, part_delay=0, status_window=0)
        reporter = QQProgressReporter(
            coordinator,
            dispatcher,
            read_epoch=lambda _group: asyncio.sleep(0, result=4),
            workers=1,
        )
        identity = TaskIdentity.create(
            request_id="onebot-message",
            group_id="group-1",
            user_id="user-1",
            epoch=4,
            origin="user_message",
        )
        sink = coordinator.register(identity)
        sink.emit(AgentEventKind.TASK_STARTED)
        handler_started = asyncio.Event()
        release_handler = asyncio.Event()

        async def slow_weather(_context, _arguments):
            handler_started.set()
            await release_handler.wait()
            return ToolResult(ToolStatus.OK, "private tool content")

        llm = _FakeAgentLLM()
        coordinator.start()
        reporter.start()
        weather = TOOL_REGISTRY.get("weather")
        try:
            with patch.dict(TOOL_REGISTRY._specs, {"weather": replace(weather, handler=slow_weather)}):
                agent = asyncio.create_task(run_agent_loop(
                    llm,
                    [{"role": "user", "content": "request"}],
                    max_rounds=2,
                    max_calls_per_round=1,
                    max_total_calls=1,
                    total_timeout=10,
                    tool_timeout=5,
                    results_max_chars=1000,
                    policy_snapshot=PolicySnapshot(roles=frozenset({"group_member"})),
                    task_identity=identity,
                    event_sink=sink,
                ))
                await asyncio.wait_for(handler_started.wait(), timeout=1)
                # Advance the coordinator's monotonic clock to its active wake deadline.
                await asyncio.sleep(0)
                await clock.advance(3)
                await asyncio.wait_for(adapter.sent_event.wait(), timeout=1)
                self.assertIn("正在", adapter.sent[0])
                self.assertNotIn("hidden query", adapter.sent[0])
                self.assertNotIn("private tool content", adapter.sent[0])

                release_handler.set()
                answer = await asyncio.wait_for(agent, timeout=2)
                self.assertEqual(answer, "final answer")
                self.assertEqual(llm.calls, 2)  # planning + final; no progress-only LLM call
                sink.emit(AgentEventKind.TASK_COMPLETED)
                result = await asyncio.wait_for(reporter.send_final(
                    identity.group_id,
                    answer,
                    task_id=identity.task_id,
                    epoch=identity.epoch,
                ), timeout=1)
                self.assertEqual(result, FinalDeliveryStatus.ACCEPTED_BY_ADAPTER)
                self.assertEqual(adapter.sent[-1], "final answer")
                self.assertEqual(len(adapter.sent), 2)
        finally:
            release_handler.set()
            if 'agent' in locals() and not agent.done():
                agent.cancel()
                await asyncio.gather(agent, return_exceptions=True)
            await reporter.aclose(timeout=1)
            await coordinator.aclose(timeout=1)


if __name__ == "__main__":
    unittest.main()

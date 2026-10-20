from __future__ import annotations

import asyncio
import unittest

from app.progress.coordinator import ProgressCandidate, ProgressCoordinator, ProgressMode
from app.progress.events import AgentEventKind, AgentEventPayload, TaskIdentity, ToolCategory, ToolOutcome
from app.progress.presentation import render_progress
from app.progress.reporter import FinalDeliveryStatus, QQProgressReporter
from app.qq_dispatcher import QQMessageDispatcher


class _FakeAdapter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.fail_status = False
        self.fail_final = False

    async def send_group_message(self, group_id, text, reply_to="") -> None:
        if self.fail_status and text.startswith("正在"):
            raise RuntimeError("fake transport failure")
        if self.fail_final and text == "final":
            raise RuntimeError("fake final transport failure")
        self.sent.append((str(group_id), text, reply_to))


class ProgressReporterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.coordinator = ProgressCoordinator()
        self.adapter = _FakeAdapter()
        self.dispatcher = QQMessageDispatcher(
            adapter=self.adapter, max_chars=500, part_delay=0, status_window=0,
        )  # type: ignore[arg-type]
        self.epoch = 4
        self.reporter = QQProgressReporter(
            self.coordinator, self.dispatcher,
            read_epoch=self._read_epoch,
            workers=1,
        )

    async def _read_epoch(self, group_id: str) -> int:
        return self.epoch

    def identity(
        self, *, group: str = "g1", epoch: int = 4, request_id: str = "message-1"
    ) -> TaskIdentity:
        return TaskIdentity.create(
            request_id=request_id, group_id=group, user_id="u1", epoch=epoch,
            origin="user_message",
        )

    def candidate(self, identity: TaskIdentity) -> ProgressCandidate:
        return ProgressCandidate(
            identity=identity, mode=ProgressMode.VERBOSE, sequence=2,
            categories=(ToolCategory.WEB_SEARCH,),
            outcomes=((ToolOutcome.NO_RESULTS, 1),),
            active_tools=1, active_models=0,
            created_monotonic=self.coordinator.clock.monotonic(),
            expires_monotonic=self.coordinator.clock.monotonic() + 100,
            notice_number=1,
        )

    def register_active_tool(self, identity: TaskIdentity) -> None:
        sink = self.coordinator.register(identity)
        sink.emit(AgentEventKind.TASK_STARTED)
        sink.emit(AgentEventKind.TOOL_STARTED,
                  AgentEventPayload(category=ToolCategory.WEB_SEARCH), tool_call_id="call-1")

    async def test_final_closes_pending_progress_before_group_lock_is_released(self) -> None:
        identity = self.identity()
        self.register_active_tool(identity)
        self.coordinator.mailbox.offer(self.candidate(identity))
        self.reporter.start()

        async with self.dispatcher.delivery_barrier(identity.group_id):
            await asyncio.sleep(0)
            final = asyncio.create_task(self.reporter.send_final(
                identity.group_id, "最终答案", task_id=identity.task_id, epoch=identity.epoch,
            ))
            await asyncio.sleep(0)
        result = await asyncio.wait_for(final, timeout=0.2)
        await asyncio.sleep(0)

        self.assertEqual(result, FinalDeliveryStatus.ACCEPTED_BY_ADAPTER)
        self.assertEqual([item[1] for item in self.adapter.sent], ["最终答案"])
        self.assertEqual(self.coordinator.metrics["progress_stale_or_terminal_dropped"], 1)

    async def test_old_epoch_candidate_is_rejected_at_adapter_boundary(self) -> None:
        identity = self.identity(epoch=3)
        self.register_active_tool(identity)
        self.epoch = 4
        await self.reporter._send_candidate(self.candidate(identity))
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(self.coordinator.metrics["progress_stale_epoch_dropped"], 1)

    async def test_final_reply_is_epoch_checked_under_barrier_and_delivery_state_is_recorded(self) -> None:
        identity = self.identity(epoch=3)
        self.register_active_tool(identity)
        self.epoch = 4
        status = await self.reporter.send_final(
            identity.group_id, "stale final", task_id=identity.task_id, epoch=identity.epoch,
        )
        self.assertEqual(status, FinalDeliveryStatus.STALE_EPOCH)
        self.assertEqual(self.adapter.sent, [])
        diagnostic = self.coordinator.task_diagnostic(identity.task_id)
        self.assertEqual(diagnostic["execution_status"], "completed")
        self.assertEqual(diagnostic["qq_final_delivery"], "stale_epoch")

    async def test_reset_epoch_fence_wins_while_reporter_waits_for_group_barrier(self) -> None:
        identity = self.identity()
        self.register_active_tool(identity)
        candidate = self.candidate(identity)
        async with self.dispatcher.delivery_barrier(identity.group_id):
            sending = asyncio.create_task(self.reporter._send_candidate(candidate))
            await asyncio.sleep(0)
            self.coordinator.begin_group_epoch_fence(identity.group_id)
            self.epoch = 5
            self.coordinator.invalidate_group_epoch(identity.group_id, 5)
        await asyncio.wait_for(sending, timeout=0.2)
        self.assertEqual(self.adapter.sent, [])

    async def test_dispatcher_suppression_is_observable_and_does_not_block_final(self) -> None:
        identity = self.identity()
        self.register_active_tool(identity)
        self.dispatcher._last_status[identity.group_id] = asyncio.get_running_loop().time() + 10
        await self.reporter._send_candidate(self.candidate(identity))
        self.assertEqual(self.coordinator.metrics["progress_suppressed"], 1)
        final = await self.reporter.send_final(identity.group_id, "final", epoch=identity.epoch)
        self.assertEqual(final, FinalDeliveryStatus.ACCEPTED_BY_ADAPTER)

    async def test_shutdown_cancels_consumer_and_counts_pending_candidates(self) -> None:
        identity = self.identity()
        self.register_active_tool(identity)
        self.reporter.start()
        async with self.dispatcher.delivery_barrier(identity.group_id):
            self.coordinator.mailbox.offer(self.candidate(identity))
            await asyncio.sleep(0)
            await self.reporter.aclose(timeout=0.2)
        await self.coordinator.aclose(timeout=0.2)
        self.assertTrue(all(task.done() for task in self.reporter._workers))
        self.assertGreaterEqual(self.coordinator.metrics["progress_shutdown_inflight_unknown"], 1)

    async def test_progress_failure_isolated_and_final_delivery_state_separate(self) -> None:
        identity = self.identity()
        self.register_active_tool(identity)
        self.adapter.fail_status = True
        self.reporter.start()
        self.coordinator.mailbox.offer(self.candidate(identity))
        for _ in range(20):
            if self.coordinator.metrics["progress_send_failed"]:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(self.coordinator.metrics["progress_send_failed"], 1)
        # The worker owns failure isolation; direct candidate dispatch may raise.
        self.coordinator.close_task(identity.task_id)
        status = await self.reporter.send_final(
            identity.group_id, "final", task_id=identity.task_id, epoch=identity.epoch,
        )
        self.assertEqual(status, FinalDeliveryStatus.ACCEPTED_BY_ADAPTER)
        self.assertEqual(self.coordinator.metrics["terminal_completed"], 1)
        self.assertEqual(self.coordinator.metrics["final_adapter_accepted"], 1)

    async def test_final_adapter_failure_does_not_rewrite_agent_execution_terminal(self) -> None:
        identity = self.identity()
        self.register_active_tool(identity)
        self.coordinator.close_task(identity.task_id)
        self.adapter.fail_final = True
        status = await self.reporter.send_final(
            identity.group_id, "final", task_id=identity.task_id, epoch=identity.epoch,
        )
        diagnostic = self.coordinator.task_diagnostic(identity.task_id)
        self.assertEqual(status, FinalDeliveryStatus.FAILED)
        self.assertEqual(diagnostic["execution_status"], "completed")
        self.assertEqual(diagnostic["qq_final_delivery"], "failed")

    async def test_renderer_uses_only_bounded_static_event_projection(self) -> None:
        text = render_progress(self.candidate(self.identity()))
        self.assertIn("网页搜索", text)
        self.assertIn("无结果", text)
        self.assertNotIn("message-1", text)
        self.assertLessEqual(len(text), 240)

    async def test_model_only_progress_does_not_claim_tool_work_is_complete(self) -> None:
        candidate = self.candidate(self.identity())
        candidate = ProgressCandidate(
            identity=candidate.identity,
            mode=candidate.mode,
            sequence=candidate.sequence,
            categories=(),
            outcomes=(),
            active_tools=0,
            active_models=1,
            created_monotonic=candidate.created_monotonic,
            expires_monotonic=candidate.expires_monotonic,
            notice_number=candidate.notice_number,
        )
        self.assertEqual(render_progress(candidate), "正在生成回复……")

    async def test_untrusted_category_text_cannot_be_rendered_as_progress(self) -> None:
        base = self.candidate(self.identity())
        hostile = "https://user:secret@example.test/?token=hidden [CQ:at,qq=1]"
        candidate = ProgressCandidate(
            identity=base.identity,
            mode=base.mode,
            sequence=base.sequence,
            categories=(hostile,),  # type: ignore[arg-type]
            outcomes=(),
            active_tools=1,
            active_models=0,
            created_monotonic=base.created_monotonic,
            expires_monotonic=base.expires_monotonic,
            notice_number=base.notice_number,
        )
        text = render_progress(candidate)
        self.assertNotIn("secret", text)
        self.assertNotIn("hidden", text)
        self.assertNotIn("[CQ:", text)
        self.assertIn("相关工具", text)

    async def test_global_progress_attempt_rate_is_bounded_without_limiting_final(self) -> None:
        for index in range(31):
            identity = self.identity(group=f"g{index}")
            self.register_active_tool(identity)
            await self.reporter._send_candidate(self.candidate(identity))
        self.assertEqual(len(self.adapter.sent), 30)
        self.assertEqual(self.coordinator.metrics["progress_global_rate_limited"], 1)
        final = await self.reporter.send_final("g30", "final", epoch=4)
        self.assertEqual(final, FinalDeliveryStatus.ACCEPTED_BY_ADAPTER)
        self.assertEqual(len(self.adapter.sent), 31)

    async def test_group_progress_rate_is_shared_across_tasks(self) -> None:
        first = self.identity()
        second = self.identity(request_id="message-2")
        self.register_active_tool(first)
        self.register_active_tool(second)
        await self.reporter._send_candidate(self.candidate(first))
        await self.reporter._send_candidate(self.candidate(second))
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertEqual(self.coordinator.metrics["progress_group_rate_limited"], 1)

    async def asyncTearDown(self) -> None:
        await self.reporter.aclose()
        await self.coordinator.aclose()


if __name__ == "__main__":
    unittest.main()

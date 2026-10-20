from __future__ import annotations

import asyncio
import unittest

from app.progress.coordinator import (
    BoundedProgressMailbox,
    ProgressCandidate,
    ProgressCoordinator,
    ProgressMode,
)
from app.progress.events import (
    AgentEventKind,
    AgentEventPayload,
    ExecutionStatus,
    TaskIdentity,
    ToolCategory,
    ToolOutcome,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def monotonic(self) -> float:
        return self.now

    async def wait_for_event(self, event: asyncio.Event, timeout: float | None) -> None:
        if timeout is None:
            await event.wait()
            return
        if timeout <= 0:
            return
        future = asyncio.get_running_loop().create_future()
        row = (self.now + timeout, future)
        self._sleepers.append(row)
        event_task = asyncio.create_task(event.wait())
        try:
            await asyncio.wait((event_task, future), return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not event_task.done():
                event_task.cancel()
                await asyncio.gather(event_task, return_exceptions=True)
            if not future.done():
                future.cancel()
            if row in self._sleepers:
                self._sleepers.remove(row)

    async def advance(self, seconds: float) -> None:
        self.now += seconds
        for deadline, future in tuple(self._sleepers):
            if deadline <= self.now and not future.done():
                future.set_result(None)
        await asyncio.sleep(0)
        await asyncio.sleep(0)


class ProgressCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def make_identity(self, request_id: str = "r1", *, group: str = "g1", user: str = "u1") -> TaskIdentity:
        return TaskIdentity.create(
            request_id=request_id,
            group_id=group,
            user_id=user,
            epoch=2,
            origin="user_message",
        )

    async def asyncSetUp(self) -> None:
        self.clock = FakeClock()
        self.coordinator = ProgressCoordinator(clock=self.clock)
        self.coordinator.start()

    async def asyncTearDown(self) -> None:
        await self.coordinator.aclose()

    async def test_first_progress_is_actively_emitted_at_virtual_three_second_deadline(self) -> None:
        identity = self.make_identity()
        sink = self.coordinator.register(identity)
        sink.emit(AgentEventKind.TASK_STARTED)
        await self.clock.advance(1)
        sink.emit(AgentEventKind.TOOL_STARTED, AgentEventPayload(
            category=ToolCategory.WEB_SEARCH,
        ), round_index=1, tool_call_id="call-1")
        await self.clock.advance(2)

        candidate = await asyncio.wait_for(self.coordinator.mailbox.get(), timeout=0.2)
        self.assertEqual(candidate.identity, identity)
        self.assertEqual(candidate.categories, (ToolCategory.WEB_SEARCH,))
        self.assertEqual(candidate.active_tools, 1)
        self.assertEqual(candidate.notice_number, 1)

    async def test_thirty_second_multiphase_task_is_bounded_to_three_progress_notices(self) -> None:
        identity = self.make_identity()
        sink = self.coordinator.register(identity, ProgressMode.VERBOSE)
        sink.emit(AgentEventKind.TASK_STARTED)
        await self.clock.advance(1)
        sink.emit(AgentEventKind.TOOL_STARTED,
                  AgentEventPayload(category=ToolCategory.WEB_SEARCH), tool_call_id="search")
        await self.clock.advance(2)
        first = await self.coordinator.mailbox.get()
        self.assertEqual(first.notice_number, 1)

        await self.clock.advance(1)
        sink.emit(AgentEventKind.TOOL_COMPLETED, AgentEventPayload(
            category=ToolCategory.WEB_SEARCH, outcome=ToolOutcome.SUCCESS,
        ), tool_call_id="search")
        sink.emit(AgentEventKind.TOOL_STARTED,
                  AgentEventPayload(category=ToolCategory.WEB_READ), tool_call_id="read")
        await self.clock.advance(7)
        second = await self.coordinator.mailbox.get()
        self.assertEqual(second.notice_number, 2)
        self.assertEqual(second.categories, (ToolCategory.WEB_READ,))

        await self.clock.advance(1)
        sink.emit(AgentEventKind.TOOL_COMPLETED, AgentEventPayload(
            category=ToolCategory.WEB_READ, outcome=ToolOutcome.PARTIAL,
        ), tool_call_id="read")
        sink.emit(AgentEventKind.MODEL_REQUEST_STARTED, round_index=2)
        await self.clock.advance(8)
        third = await self.coordinator.mailbox.get()
        self.assertEqual(third.notice_number, 3)

        await self.clock.advance(11)
        sink.emit(AgentEventKind.MODEL_REQUEST_COMPLETED, round_index=2)
        sink.emit(AgentEventKind.TASK_COMPLETED)
        await self.clock.advance(5)
        self.assertEqual(self.coordinator.mailbox.size, 0)
        self.assertEqual(self.coordinator.metrics["candidates_offered"], 3)

    async def test_task_completed_before_threshold_never_emits_late_progress(self) -> None:
        identity = self.make_identity()
        sink = self.coordinator.register(identity)
        sink.emit(AgentEventKind.TASK_STARTED)
        sink.emit(AgentEventKind.MODEL_REQUEST_STARTED, round_index=1)
        sink.emit(AgentEventKind.TASK_COMPLETED, AgentEventPayload(
            execution_status=ExecutionStatus.COMPLETED,
        ))
        await self.clock.advance(30)
        self.assertEqual(self.coordinator.mailbox.size, 0)
        self.assertEqual(self.coordinator.metrics["candidates_offered"], 0)

    async def test_two_second_tool_finishes_without_any_progress_candidate(self) -> None:
        identity = self.make_identity()
        sink = self.coordinator.register(identity)
        sink.emit(AgentEventKind.TASK_STARTED)
        sink.emit(AgentEventKind.TOOL_STARTED, AgentEventPayload(category=ToolCategory.WEB_SEARCH),
                  tool_call_id="search")
        await self.clock.advance(2)
        sink.emit(AgentEventKind.TOOL_COMPLETED, AgentEventPayload(
            category=ToolCategory.WEB_SEARCH, outcome=ToolOutcome.SUCCESS,
        ), tool_call_id="search")
        sink.emit(AgentEventKind.TASK_COMPLETED)
        await self.clock.advance(30)
        self.assertEqual(self.coordinator.mailbox.size, 0)
        self.assertEqual(self.coordinator.metrics["candidates_offered"], 0)

    async def test_ten_second_single_tool_task_gets_only_one_intermediate_notice(self) -> None:
        identity = self.make_identity()
        sink = self.coordinator.register(identity)
        sink.emit(AgentEventKind.TASK_STARTED)
        sink.emit(AgentEventKind.TOOL_STARTED,
                  AgentEventPayload(category=ToolCategory.WEB_SEARCH), tool_call_id="search")
        await self.clock.advance(3)
        first = await self.coordinator.mailbox.get()
        self.assertEqual(first.notice_number, 1)

        await self.clock.advance(7)
        sink.emit(AgentEventKind.TOOL_COMPLETED, AgentEventPayload(
            category=ToolCategory.WEB_SEARCH, outcome=ToolOutcome.SUCCESS,
        ), tool_call_id="search")
        sink.emit(AgentEventKind.TASK_COMPLETED)
        await self.clock.advance(1)
        self.assertEqual(self.coordinator.metrics["candidates_offered"], 1)
        self.assertEqual(self.coordinator.mailbox.size, 0)

    async def test_parallel_tools_aggregate_real_outcomes_and_live_snapshot_is_scoped(self) -> None:
        identity = self.make_identity()
        sink = self.coordinator.register(identity, ProgressMode.VERBOSE)
        sink.emit(AgentEventKind.TASK_STARTED)
        sink.emit(AgentEventKind.MODEL_REQUEST_STARTED, round_index=1)
        sink.emit(AgentEventKind.TOOL_STARTED, AgentEventPayload(category=ToolCategory.WEB_SEARCH),
                  round_index=1, tool_call_id="search")
        sink.emit(AgentEventKind.TOOL_STARTED, AgentEventPayload(category=ToolCategory.REPOSITORY),
                  round_index=1, tool_call_id="repo")
        sink.emit(AgentEventKind.TOOL_COMPLETED, AgentEventPayload(
            category=ToolCategory.WEB_SEARCH, outcome=ToolOutcome.SUCCESS, evidence_count=4,
        ), round_index=1, tool_call_id="search")
        sink.emit(AgentEventKind.TOOL_COMPLETED, AgentEventPayload(
            category=ToolCategory.REPOSITORY, outcome=ToolOutcome.NO_RESULTS,
        ), round_index=1, tool_call_id="repo")
        self.assertEqual(self.coordinator.snapshot("g1", "u2"), ())
        rows = self.coordinator.snapshot("g1", "u1")
        self.assertEqual(rows[0]["active_models"], 1)
        self.assertEqual(dict(rows[0]["outcomes"]), {
            ToolOutcome.SUCCESS.value: 1, ToolOutcome.NO_RESULTS.value: 1,
        })

    async def test_concurrent_groups_keep_independent_task_and_tool_state(self) -> None:
        first_identity = self.make_identity("g1-request", group="g1", user="u1")
        second_identity = self.make_identity("g2-request", group="g2", user="u2")
        first = self.coordinator.register(first_identity)
        second = self.coordinator.register(second_identity)
        first.emit(AgentEventKind.TASK_STARTED)
        second.emit(AgentEventKind.TASK_STARTED)
        first.emit(AgentEventKind.TOOL_STARTED,
                   AgentEventPayload(category=ToolCategory.WEB_SEARCH), tool_call_id="same-call-id")
        second.emit(AgentEventKind.TOOL_STARTED,
                    AgentEventPayload(category=ToolCategory.REPOSITORY), tool_call_id="same-call-id")

        self.assertEqual(self.coordinator.snapshot("g1", "u1")[0]["active_tools"], 1)
        self.assertEqual(self.coordinator.snapshot("g2", "u2")[0]["active_tools"], 1)
        self.assertEqual(self.coordinator.snapshot("g1", "u2"), ())

    async def test_five_parallel_tools_can_complete_out_of_order_without_losing_statuses(self) -> None:
        identity = self.make_identity()
        sink = self.coordinator.register(identity, ProgressMode.VERBOSE)
        sink.emit(AgentEventKind.TASK_STARTED)
        categories = (ToolCategory.WEB_SEARCH, ToolCategory.WEB_READ, ToolCategory.WIKI,
                      ToolCategory.REPOSITORY, ToolCategory.RECIPE)
        for index, category in enumerate(categories):
            sink.emit(AgentEventKind.TOOL_STARTED, AgentEventPayload(category=category),
                      tool_call_id=f"call-{index}")
        for index, outcome in ((3, ToolOutcome.NO_RESULTS), (1, ToolOutcome.SUCCESS),
                               (4, ToolOutcome.UNAVAILABLE), (0, ToolOutcome.PARTIAL),
                               (2, ToolOutcome.DENIED)):
            sink.emit(AgentEventKind.TOOL_COMPLETED, AgentEventPayload(
                category=categories[index], outcome=outcome,
            ), tool_call_id=f"call-{index}")
        rows = self.coordinator.snapshot("g1", "u1")
        self.assertEqual(rows[0]["active_tools"], 0)
        self.assertEqual(dict(rows[0]["outcomes"]), {
            "success": 1, "partial": 1, "no_results": 1, "unavailable": 1, "denied": 1,
        })

    async def test_mailbox_coalesces_per_task_and_drops_oldest_when_full(self) -> None:
        mailbox = BoundedProgressMailbox(capacity=2, clock=self.clock)

        def candidate(index: int) -> ProgressCandidate:
            return ProgressCandidate(
                identity=self.make_identity(str(index)), mode=ProgressMode.BALANCED,
                sequence=index + 1, categories=(), outcomes=(), active_tools=1,
                active_models=0, created_monotonic=0, expires_monotonic=20,
                notice_number=1,
            )

        mailbox.offer(candidate(1))
        mailbox.offer(candidate(2))
        third = candidate(3)
        mailbox.offer(third)
        self.assertEqual(mailbox.size, 2)
        self.assertEqual(mailbox.dropped, 1)
        mailbox.offer(third)
        self.assertEqual(mailbox.size, 2)
        self.assertEqual(mailbox.merged, 1)

    async def test_terminal_and_epoch_fence_remove_pending_candidate(self) -> None:
        identity = self.make_identity()
        sink = self.coordinator.register(identity)
        sink.emit(AgentEventKind.TASK_STARTED)
        sink.emit(AgentEventKind.TOOL_STARTED, AgentEventPayload(category=ToolCategory.WEB_READ),
                  tool_call_id="read")
        await self.clock.advance(3)
        self.assertEqual(self.coordinator.mailbox.size, 1)
        self.coordinator.invalidate_group_epoch("g1", 3)
        self.assertEqual(self.coordinator.mailbox.size, 0)
        self.assertFalse(self.coordinator.delivery_is_current(identity, 2))
        self.assertEqual(self.coordinator.snapshot("g1", "u1"), ())

    async def test_quiet_mode_never_offers_progress(self) -> None:
        identity = self.make_identity()
        sink = self.coordinator.register(identity, ProgressMode.QUIET)
        sink.emit(AgentEventKind.TASK_STARTED)
        sink.emit(AgentEventKind.TOOL_STARTED, AgentEventPayload(category=ToolCategory.WIKI),
                  tool_call_id="wiki")
        await self.clock.advance(10)
        self.assertEqual(self.coordinator.mailbox.size, 0)

    async def test_execution_checkpoint_and_qq_delivery_states_are_separate(self) -> None:
        identity = self.make_identity()
        sink = self.coordinator.register(identity)
        sink.emit(AgentEventKind.TASK_STARTED)
        sink.emit(AgentEventKind.CHECKPOINT_SAVED, AgentEventPayload(revision=7))
        sink.emit(AgentEventKind.TASK_COMPLETED, AgentEventPayload(
            execution_status=ExecutionStatus.COMPLETED,
        ))
        before_delivery = self.coordinator.task_diagnostic(identity.task_id)
        self.assertEqual(before_delivery["execution_status"], "completed")
        self.assertEqual(before_delivery["working_state_status"], "saved")
        self.assertEqual(before_delivery["checkpoint_revision"], 7)
        self.assertEqual(before_delivery["qq_final_delivery"], "unknown")
        self.coordinator.record_final_delivery(identity.task_id, "failed")
        after_delivery = self.coordinator.task_diagnostic(identity.task_id)
        self.assertEqual(after_delivery["execution_status"], "completed")
        self.assertEqual(after_delivery["working_state_status"], "saved")
        self.assertEqual(after_delivery["qq_final_delivery"], "failed")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from enum import StrEnum
import logging
import time
from typing import Protocol

from app.progress.events import (
    AgentEvent,
    AgentEventKind,
    AgentEventPayload,
    ExecutionStatus,
    RequestAgentEventSink,
    TaskIdentity,
    ToolCategory,
    ToolOutcome,
    WorkingStateStatus,
)


LOGGER = logging.getLogger(__name__)
FIRST_NOTICE_DELAY_SECONDS = 3.0
MIN_NOTICE_INTERVAL_SECONDS = 8.0
MAX_INTERMEDIATE_NOTICES_PER_TASK = 3
MAX_ACTIVE_TASKS = 32
MAX_TERMINAL_TASKS = 256
TERMINAL_TTL_SECONDS = 900.0
MAX_MAILBOX_TASKS = 32
NOTICE_TTL_SECONDS = 2.0
MAX_TRACKED_GROUP_EPOCHS = 512

_TERMINAL_KINDS = {
    AgentEventKind.TASK_COMPLETED,
    AgentEventKind.TASK_FAILED,
    AgentEventKind.TASK_CANCELLED,
    AgentEventKind.TASK_TIMED_OUT,
    AgentEventKind.TASK_STALE,
}
_OUTCOMES = tuple(ToolOutcome)


class ProgressMode(StrEnum):
    QUIET = "quiet"
    BALANCED = "balanced"
    VERBOSE = "verbose"


class Clock(Protocol):
    def monotonic(self) -> float: ...
    async def wait_for_event(self, event: asyncio.Event, timeout: float | None) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    async def wait_for_event(self, event: asyncio.Event, timeout: float | None) -> None:
        if timeout is None:
            await event.wait()
            return
        try:
            await asyncio.wait_for(event.wait(), timeout=max(0.0, timeout))
        except TimeoutError:
            return


@dataclass(frozen=True, slots=True)
class ProgressCandidate:
    identity: TaskIdentity
    mode: ProgressMode
    sequence: int
    categories: tuple[ToolCategory, ...]
    outcomes: tuple[tuple[ToolOutcome, int], ...]
    active_tools: int
    active_models: int
    created_monotonic: float
    expires_monotonic: float
    notice_number: int


class BoundedProgressMailbox:
    """Coalesces pending status by task; never blocks event producers."""

    def __init__(self, *, capacity: int = MAX_MAILBOX_TASKS, clock: Clock | None = None) -> None:
        if capacity < 1:
            raise ValueError("mailbox capacity must be positive")
        self.capacity = capacity
        self.clock = clock or SystemClock()
        self._items: OrderedDict[str, ProgressCandidate] = OrderedDict()
        self._ready = asyncio.Event()
        self._closed = False
        self.dropped = 0
        self.merged = 0
        self.expired = 0

    def offer(self, candidate: ProgressCandidate) -> bool:
        if self._closed:
            self.dropped += 1
            return False
        task_id = candidate.identity.task_id
        if task_id in self._items:
            self._items.pop(task_id)
            self.merged += 1
        elif len(self._items) >= self.capacity:
            _oldest_id, _oldest = self._items.popitem(last=False)
            self.dropped += 1
        self._items[task_id] = candidate
        self._ready.set()
        return True

    def remove(self, task_id: str) -> None:
        self._items.pop(task_id, None)
        if not self._items:
            self._ready.clear()

    async def get(self) -> ProgressCandidate | None:
        while not self._closed:
            if self._items:
                _task_id, candidate = self._items.popitem(last=False)
                if candidate.expires_monotonic <= self.clock.monotonic():
                    self.expired += 1
                    continue
                if not self._items:
                    self._ready.clear()
                return candidate
            self._ready.clear()
            if self._items:
                self._ready.set()
                continue
            await self._ready.wait()
        return None

    def close(self) -> int:
        self._closed = True
        discarded = len(self._items)
        self._items.clear()
        self._ready.set()
        return discarded

    @property
    def size(self) -> int:
        return len(self._items)


@dataclass(slots=True)
class _ToolState:
    call_id: str
    category: ToolCategory
    outcome: ToolOutcome | None = None
    active: bool = False


@dataclass(slots=True)
class _TaskState:
    identity: TaskIdentity
    mode: ProgressMode
    created: float
    last_sequence: int = 0
    event_count: int = 0
    tools: dict[str, _ToolState] = field(default_factory=dict)
    active_models: set[int] = field(default_factory=set)
    terminal: ExecutionStatus | None = None
    last_notice: float | None = None
    notice_count: int = 0
    next_notice_at: float | None = None
    last_projection: tuple[object, ...] | None = None
    working_state_status: WorkingStateStatus = WorkingStateStatus.UNKNOWN
    checkpoint_revision: int | None = None
    final_delivery_status: str | None = None

    @property
    def active(self) -> bool:
        return self.terminal is None


class ProgressCoordinator:
    """Request-scoped bounded reducer and one active timer scheduler."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        self.clock = clock or SystemClock()
        self.mailbox = BoundedProgressMailbox(clock=self.clock)
        self._tasks: OrderedDict[str, _TaskState] = OrderedDict()
        self._group_epochs: OrderedDict[str, int] = OrderedDict()
        self._fenced_groups: set[str] = set()
        self._wake = asyncio.Event()
        self._scheduler: asyncio.Task[None] | None = None
        self._closing = False
        self.metrics: Counter[str] = Counter()

    def start(self) -> None:
        if self._closing:
            raise RuntimeError("progress coordinator is closed")
        if self._scheduler is None:
            self._scheduler = asyncio.create_task(self._run_scheduler(), name="agent-progress-scheduler")

    def register(self, identity: TaskIdentity, mode: ProgressMode | str = ProgressMode.BALANCED) -> RequestAgentEventSink:
        self._cleanup()
        try:
            selected_mode = ProgressMode(mode)
        except ValueError:
            selected_mode = ProgressMode.BALANCED
        current_epoch = self._group_epochs.get(identity.group_id)
        if identity.group_id in self._fenced_groups or (
            current_epoch is not None and identity.epoch < current_epoch
        ):
            self.metrics["stale_registration"] += 1
            return RequestAgentEventSink(identity)
        self._remember_group_epoch(identity.group_id, max(identity.epoch, current_epoch or 0))
        if len([item for item in self._tasks.values() if item.active]) >= MAX_ACTIVE_TASKS:
            self.metrics["task_capacity_dropped"] += 1
            return RequestAgentEventSink(identity)
        self._tasks[identity.task_id] = _TaskState(identity, selected_mode, self.clock.monotonic())
        self._tasks.move_to_end(identity.task_id)
        self._wake.set()
        return RequestAgentEventSink(identity, self.publish)

    def publish(self, event: AgentEvent) -> bool:
        state = self._tasks.get(event.identity.task_id)
        if state is None or not state.active:
            self.metrics["late_event_dropped"] += 1
            return False
        if event.identity != state.identity or event.sequence <= state.last_sequence:
            self.metrics["invalid_or_duplicate_event_dropped"] += 1
            return False
        if event.identity.group_id in self._fenced_groups:
            self.metrics["epoch_fence_event_dropped"] += 1
            return False
        if self._group_epochs.get(event.identity.group_id, event.identity.epoch) != event.identity.epoch:
            self._terminate(state, ExecutionStatus.STALE)
            self.metrics["stale_event_dropped"] += 1
            return False
        state.last_sequence = event.sequence
        state.event_count += 1
        self.metrics["events_seen"] += 1
        if state.event_count > 192:
            self.metrics["event_limit_reached"] += 1
            if event.kind not in _TERMINAL_KINDS:
                return False

        self._reduce(state, event)
        if event.kind in _TERMINAL_KINDS:
            status = event.payload.execution_status or {
                AgentEventKind.TASK_COMPLETED: ExecutionStatus.COMPLETED,
                AgentEventKind.TASK_FAILED: ExecutionStatus.FAILED,
                AgentEventKind.TASK_CANCELLED: ExecutionStatus.CANCELLED,
                AgentEventKind.TASK_TIMED_OUT: ExecutionStatus.TIMED_OUT,
                AgentEventKind.TASK_STALE: ExecutionStatus.STALE,
            }[event.kind]
            self._terminate(state, status)
        else:
            self._maybe_offer(state, now=self.clock.monotonic())
        self._wake.set()
        return True

    def _reduce(self, state: _TaskState, event: AgentEvent) -> None:
        payload = event.payload
        if event.kind == AgentEventKind.CHECKPOINT_SAVED:
            state.working_state_status = WorkingStateStatus.SAVED
            state.checkpoint_revision = payload.revision
        elif event.kind == AgentEventKind.WORKING_STATE_NOT_CONFIGURED:
            state.working_state_status = WorkingStateStatus.NOT_CONFIGURED
        elif event.kind == AgentEventKind.WORKING_STATE_SAVE_FAILED:
            state.working_state_status = WorkingStateStatus.FAILED
        elif event.kind == AgentEventKind.MODEL_REQUEST_STARTED:
            state.active_models.add(event.round_index or payload.attempt or 1)
        elif event.kind == AgentEventKind.MODEL_REQUEST_COMPLETED:
            state.active_models.discard(event.round_index or payload.attempt or 1)
        elif event.kind in {
            AgentEventKind.TOOL_STARTED,
            AgentEventKind.TOOL_COMPLETED,
            AgentEventKind.TOOL_FAILED,
            AgentEventKind.TOOL_CANCELLED,
            AgentEventKind.TOOL_REJECTED,
        }:
            call_id = event.tool_call_id or f"unkeyed:{event.sequence}"
            category = payload.category or ToolCategory.OTHER
            tool = state.tools.setdefault(call_id, _ToolState(call_id, category))
            tool.category = category
            if event.kind == AgentEventKind.TOOL_STARTED:
                tool.active = True
                tool.outcome = None
            elif event.kind == AgentEventKind.TOOL_REJECTED:
                tool.active = False
                tool.outcome = payload.outcome or ToolOutcome.REJECTED
            else:
                tool.active = False
                tool.outcome = payload.outcome or ToolOutcome.UNKNOWN

    def _projection(self, state: _TaskState) -> tuple[object, ...]:
        active = tuple(sorted({tool.category for tool in state.tools.values() if tool.active}, key=str))
        categories = tuple(sorted({tool.category for tool in state.tools.values()}, key=str))
        outcomes = Counter(tool.outcome for tool in state.tools.values() if tool.outcome is not None)
        outcome_rows = tuple((outcome, outcomes[outcome]) for outcome in _OUTCOMES if outcomes[outcome])
        return active, categories, outcome_rows, len(state.active_models), sum(tool.active for tool in state.tools.values())

    def _maybe_offer(self, state: _TaskState, *, now: float) -> None:
        if state.mode == ProgressMode.QUIET or not state.active:
            return
        active_models = len(state.active_models)
        active_tools = sum(tool.active for tool in state.tools.values())
        if not active_models and not active_tools:
            return
        if now - state.created < FIRST_NOTICE_DELAY_SECONDS:
            state.next_notice_at = state.created + FIRST_NOTICE_DELAY_SECONDS
            return
        if state.notice_count >= MAX_INTERMEDIATE_NOTICES_PER_TASK:
            return
        projection = self._projection(state)
        if projection == state.last_projection:
            return
        if state.last_notice is not None and now - state.last_notice < MIN_NOTICE_INTERVAL_SECONDS:
            state.next_notice_at = state.last_notice + MIN_NOTICE_INTERVAL_SECONDS
            return
        candidate = ProgressCandidate(
            identity=state.identity,
            mode=state.mode,
            sequence=state.last_sequence,
            categories=projection[0] or projection[1],
            outcomes=projection[2],
            active_tools=int(projection[4]),
            active_models=int(projection[3]),
            created_monotonic=now,
            expires_monotonic=now + NOTICE_TTL_SECONDS,
            notice_number=state.notice_count + 1,
        )
        if self.mailbox.offer(candidate):
            state.notice_count += 1
            state.last_notice = now
            state.last_projection = projection
            state.next_notice_at = None
            self.metrics["candidates_offered"] += 1

    def _process_due(self, now: float) -> None:
        for state in tuple(self._tasks.values()):
            if not state.active or state.next_notice_at is None or now < state.next_notice_at:
                continue
            state.next_notice_at = None
            self._maybe_offer(state, now=now)
        self._cleanup(now=now)

    def _next_deadline(self) -> float | None:
        deadlines = [
            state.next_notice_at for state in self._tasks.values()
            if state.active and state.next_notice_at is not None
        ]
        return min(deadlines) if deadlines else None

    async def _run_scheduler(self) -> None:
        while not self._closing:
            self._wake.clear()
            now = self.clock.monotonic()
            self._process_due(now)
            if self._closing:
                break
            deadline = self._next_deadline()
            timeout = None if deadline is None else max(0.0, deadline - self.clock.monotonic())
            try:
                await self.clock.wait_for_event(self._wake, timeout)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                LOGGER.exception("progress scheduler iteration failed")
                await asyncio.sleep(0)

    def _terminate(self, state: _TaskState, status: ExecutionStatus) -> None:
        if not state.active:
            return
        state.terminal = status
        state.next_notice_at = None
        for tool in state.tools.values():
            tool.active = False
        self.mailbox.remove(state.identity.task_id)
        self.metrics[f"terminal_{status.value}"] += 1
        self._cleanup()

    def close_task(self, task_id: str, status: ExecutionStatus = ExecutionStatus.COMPLETED) -> bool:
        state = self._tasks.get(task_id)
        if state is None or not state.active:
            return False
        self._terminate(state, status)
        self._wake.set()
        return True

    def invalidate_group_epoch(self, group_id: str, epoch: int) -> None:
        group_key = str(group_id)
        current = self._group_epochs.get(group_key, -1)
        self._remember_group_epoch(group_key, max(current, int(epoch)))
        self._fenced_groups.discard(group_key)
        for state in tuple(self._tasks.values()):
            if state.identity.group_id == group_key and state.identity.epoch < epoch and state.active:
                self._terminate(state, ExecutionStatus.STALE)
                self.metrics["epoch_fenced"] += 1
        self._wake.set()

    def begin_group_epoch_fence(self, group_id: str) -> None:
        self._fenced_groups.add(str(group_id))
        self._wake.set()

    def abort_group_epoch_fence(self, group_id: str) -> None:
        self._fenced_groups.discard(str(group_id))
        self._wake.set()

    def _remember_group_epoch(self, group_id: str, epoch: int) -> None:
        self._group_epochs[group_id] = int(epoch)
        self._group_epochs.move_to_end(group_id)
        while len(self._group_epochs) > MAX_TRACKED_GROUP_EPOCHS:
            active_groups = {
                state.identity.group_id for state in self._tasks.values() if state.active
            }
            removable = next(
                (key for key in self._group_epochs if key not in active_groups and key != group_id),
                None,
            )
            if removable is None:
                break
            self._group_epochs.pop(removable, None)

    def snapshot(
        self, group_id: str, user_id: str, *, is_admin: bool = False,
        exclude_task_id: str | None = None,
    ) -> tuple[dict[str, object], ...]:
        self._cleanup()
        rows = []
        for state in self._tasks.values():
            if not state.active or state.identity.group_id != str(group_id):
                continue
            if exclude_task_id is not None and state.identity.task_id == exclude_task_id:
                continue
            if not is_admin and state.identity.user_id != str(user_id):
                continue
            projection = self._projection(state)
            rows.append({
                "task_id": state.identity.task_id,
                "epoch": state.identity.epoch,
                "mode": state.mode.value,
                "active_tools": int(projection[4]),
                "active_models": int(projection[3]),
                "categories": tuple(item.value for item in projection[1]),
                "outcomes": tuple((key.value, value) for key, value in projection[2]),
                "started_monotonic": state.created,
            })
        return tuple(rows)

    def delivery_is_current(self, identity: TaskIdentity, sequence: int) -> bool:
        state = self._tasks.get(identity.task_id)
        return bool(
            state is not None
            and state.active
            and state.identity == identity
            and state.last_sequence >= sequence
            and identity.group_id not in self._fenced_groups
            and self._group_epochs.get(identity.group_id, identity.epoch) == identity.epoch
        )

    def active_task_count(self, group_id: str | None = None) -> int:
        self._cleanup()
        return sum(
            state.active and (group_id is None or state.identity.group_id == str(group_id))
            for state in self._tasks.values()
        )

    def record_final_delivery(self, task_id: str | None, status: str) -> None:
        state = self._tasks.get(task_id) if task_id else None
        if state is not None:
            state.final_delivery_status = str(status)
        self.metrics[f"final_delivery_{status}"] += 1

    def task_diagnostic(self, task_id: str) -> dict[str, object] | None:
        state = self._tasks.get(task_id)
        if state is None:
            return None
        return {
            "task_id": state.identity.task_id,
            "execution_status": state.terminal.value if state.terminal else ExecutionStatus.RUNNING.value,
            "working_state_status": state.working_state_status.value,
            "checkpoint_revision": state.checkpoint_revision,
            "qq_final_delivery": state.final_delivery_status or "unknown",
        }

    def _cleanup(self, *, now: float | None = None) -> None:
        current = self.clock.monotonic() if now is None else now
        for task_id, state in tuple(self._tasks.items()):
            if state.active or state.terminal is None or current - state.created <= TERMINAL_TTL_SECONDS:
                continue
            self._tasks.pop(task_id, None)
        while len(self._tasks) > MAX_TERMINAL_TASKS:
            task_id, state = next(iter(self._tasks.items()))
            if state.active:
                removable = next((key for key, value in self._tasks.items() if not value.active), None)
                if removable is None:
                    break
                self._tasks.pop(removable, None)
            else:
                self._tasks.pop(task_id, None)

    async def aclose(self, timeout: float = 2.0) -> None:
        self._closing = True
        discarded = self.mailbox.close()
        self.metrics["progress_shutdown_dropped"] += discarded
        self._wake.set()
        scheduler = self._scheduler
        self._scheduler = None
        if scheduler is not None:
            scheduler.cancel()
            try:
                await asyncio.wait_for(scheduler, timeout=max(0.0, timeout))
            except (asyncio.CancelledError, TimeoutError):
                pass

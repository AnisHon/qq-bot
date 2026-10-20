from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
import json
import uuid
from typing import Any, Callable, Protocol


MAX_EVENTS_PER_TASK = 192
MAX_EVENT_PAYLOAD_BYTES = 4096
MAX_ID_CHARS = 160
_TERMINAL_KINDS = frozenset({
    "task_completed", "task_failed", "task_cancelled", "task_timed_out", "task_stale",
})


class AgentEventKind(StrEnum):
    TASK_STARTED = "task_started"
    MODEL_REQUEST_STARTED = "model_request_started"
    MODEL_REQUEST_COMPLETED = "model_request_completed"
    TOOL_REJECTED = "tool_rejected"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    TOOL_CANCELLED = "tool_cancelled"
    COMPACTION_STARTED = "compaction_started"
    COMPACTION_COMPLETED = "compaction_completed"
    CHECKPOINT_SAVED = "checkpoint_saved"
    WORKING_STATE_NOT_CONFIGURED = "working_state_not_configured"
    WORKING_STATE_SAVE_FAILED = "working_state_save_failed"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    TASK_TIMED_OUT = "task_timed_out"
    TASK_STALE = "task_stale"


class ToolOutcome(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    NO_RESULTS = "no_results"
    UNAVAILABLE = "unavailable"
    DENIED = "denied"
    TIMEOUT = "timeout"
    ERROR = "error"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class ToolCategory(StrEnum):
    WEB_SEARCH = "web_search"
    WEB_READ = "web_read"
    NEWS_SEARCH = "news_search"
    WIKI = "wiki"
    RECIPE = "recipe"
    MACHINE = "machine"
    REPOSITORY = "repository"
    SFM_VALIDATION = "sfm_validation"
    WEATHER = "weather"
    OTHER = "other"


class ExecutionStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    STALE = "stale"


class WorkingStateStatus(StrEnum):
    UNKNOWN = "unknown"
    NOT_CONFIGURED = "not_configured"
    SAVED = "saved"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AgentEventPayload:
    """Bounded public-safe event facts; never carries arguments or result text."""

    category: ToolCategory | None = None
    outcome: ToolOutcome | None = None
    execution_status: ExecutionStatus | None = None
    execution_started: bool | None = None
    evidence_count: int | None = None
    completeness: str | None = None
    completed_count: int | None = None
    total_count: int | None = None
    revision: int | None = None
    attempt: int | None = None
    elapsed_bucket: str | None = None

    def __post_init__(self) -> None:
        for name in ("evidence_count", "completed_count", "total_count", "revision", "attempt"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or value < 0):
                raise ValueError(f"{name} must be a non-negative integer")
        if self.completeness not in {None, "complete", "partial", "reference_only", "unavailable"}:
            raise ValueError("invalid evidence completeness")
        if self.elapsed_bucket not in {None, "<3s", "3-8s", "8-30s", "30-60s", "60s+"}:
            raise ValueError("invalid elapsed time bucket")


@dataclass(frozen=True, slots=True)
class TaskIdentity:
    task_id: str
    request_id: str
    group_id: str
    user_id: str
    epoch: int
    origin: str

    def __post_init__(self) -> None:
        for name in ("task_id", "request_id", "group_id", "user_id", "origin"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > MAX_ID_CHARS:
                raise ValueError(f"{name} must be a non-empty bounded string")
        if self.epoch < 0:
            raise ValueError("epoch cannot be negative")
        if self.origin not in {"user_message", "slash_command", "scheduled_task"}:
            raise ValueError("unsupported task origin")

    @classmethod
    def create(
        cls,
        *,
        request_id: str | None,
        group_id: str,
        user_id: str | None,
        epoch: int,
        origin: str,
    ) -> TaskIdentity:
        return cls(
            task_id=str(uuid.uuid4()),
            request_id=(str(request_id).strip()[:MAX_ID_CHARS] or str(uuid.uuid4()))
            if request_id is not None else str(uuid.uuid4()),
            group_id=str(group_id),
            user_id=str(user_id or "unknown"),
            epoch=int(epoch),
            origin=origin,
        )


@dataclass(frozen=True, slots=True)
class AgentEvent:
    schema_version: int
    identity: TaskIdentity
    sequence: int
    timestamp: datetime
    kind: AgentEventKind
    round_index: int | None = None
    tool_call_id: str | None = None
    payload: AgentEventPayload = AgentEventPayload()

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported AgentEvent schema version")
        if self.sequence < 1:
            raise ValueError("event sequence must be positive")
        if self.timestamp.tzinfo is None:
            raise ValueError("event timestamp must be timezone-aware")
        if self.round_index is not None and self.round_index < 1:
            raise ValueError("round index must be positive")
        if self.tool_call_id is not None and len(self.tool_call_id) > MAX_ID_CHARS:
            raise ValueError("tool_call_id exceeds the allowed length")
        try:
            encoded = json.dumps(asdict(self.payload), ensure_ascii=False, separators=(",", ":"), default=str)
        except (TypeError, ValueError) as exc:
            raise ValueError("event payload must be JSON serializable") from exc
        if len(encoded.encode("utf-8")) > MAX_EVENT_PAYLOAD_BYTES:
            raise ValueError("event payload exceeds the allowed size")


class EventSink(Protocol):
    def publish(self, event: AgentEvent) -> bool: ...
    def emit(
        self,
        kind: AgentEventKind,
        payload: AgentEventPayload | None = None,
        *,
        round_index: int | None = None,
        tool_call_id: str | None = None,
    ) -> bool: ...


class RequestAgentEventSink:
    """Assigns a monotonic per-request sequence and isolates observer failures."""

    def __init__(
        self,
        identity: TaskIdentity,
        consumer: Callable[[AgentEvent], Any] | None = None,
        *,
        max_events: int = MAX_EVENTS_PER_TASK,
    ) -> None:
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self.identity = identity
        self._consumer = consumer
        self._max_events = max_events
        self._sequence = 0
        self._published = 0
        self.dropped_events = 0

    @property
    def sequence(self) -> int:
        return self._sequence

    def emit(
        self,
        kind: AgentEventKind,
        payload: AgentEventPayload | None = None,
        *,
        round_index: int | None = None,
        tool_call_id: str | None = None,
    ) -> bool:
        if self._published >= self._max_events and kind.value not in _TERMINAL_KINDS:
            self.dropped_events += 1
            return False
        self._sequence += 1
        try:
            event = AgentEvent(
                schema_version=1,
                identity=self.identity,
                sequence=self._sequence,
                timestamp=datetime.now(UTC),
                kind=kind,
                round_index=round_index,
                tool_call_id=tool_call_id,
                payload=payload or AgentEventPayload(),
            )
        except (TypeError, ValueError):
            self.dropped_events += 1
            return False
        self._published += 1
        return self.publish(event)

    def publish(self, event: AgentEvent) -> bool:
        if event.identity != self.identity or event.sequence <= 0:
            self.dropped_events += 1
            return False
        if self._consumer is None:
            return True
        try:
            result = self._consumer(event)
            return result is not False
        except Exception:  # noqa: BLE001 - progress must not break Agent execution
            self.dropped_events += 1
            return False

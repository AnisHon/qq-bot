from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
import json
import re
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
    PUBLIC_COMMENTARY = "public_commentary"
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
    COMMENTARY = "commentary"
    OTHER = "other"


class CommentaryKind(StrEnum):
    PLAN = "plan"
    UPDATE = "update"
    REVISION = "revision"
    BLOCKED = "blocked"


MAX_PUBLIC_COMMENTARY_CHARS = 240
MAX_COMMENTARY_TOOL_REFS = 6
_SENSITIVE_COMMENTARY_RE = re.compile(
    r"(?:https?://|www\.|\bbearer\s+\S+|(?:api[_ -]?key|access[_ -]?token|password|secret)\s*[:=]|"
    r"\b(?:sk|tvly|ghp|github_pat|xox[baprs]|AIza)[-_A-Za-z0-9]{12,})",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PublicCommentary:
    """Validated public task narration, separate from execution facts."""

    kind: CommentaryKind
    text: str
    task_id: str
    round_index: int
    related_tool_call_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CommentaryKind):
            raise ValueError("invalid public commentary kind")
        if not isinstance(self.text, str):
            raise ValueError("commentary text must be a string")
        if not isinstance(self.task_id, str) or not self.task_id or len(self.task_id) > MAX_ID_CHARS:
            raise ValueError("commentary task id is invalid")
        if isinstance(self.round_index, bool) or self.round_index < 1:
            raise ValueError("commentary round index must be positive")
        if not isinstance(self.related_tool_call_ids, tuple):
            raise ValueError("commentary tool references must be a tuple")
        if len(self.related_tool_call_ids) > MAX_COMMENTARY_TOOL_REFS:
            raise ValueError("too many commentary tool references")
        if any(not isinstance(item, str) or not item or len(item) > MAX_ID_CHARS
               for item in self.related_tool_call_ids):
            raise ValueError("invalid commentary tool reference")
        if len(set(self.related_tool_call_ids)) != len(self.related_tool_call_ids):
            raise ValueError("duplicate commentary tool reference")
        # Drop control characters and collapse whitespace before checking and publishing.
        normalized = re.sub(r"[\x00-\x1f\x7f]+", " ", self.text)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if not normalized or len(normalized) > MAX_PUBLIC_COMMENTARY_CHARS:
            raise ValueError("commentary text is empty or exceeds the allowed length")
        if _SENSITIVE_COMMENTARY_RE.search(normalized) or re.search(r"\bQQ\s*\d{5,}\b", normalized, re.IGNORECASE):
            raise ValueError("commentary contains a disallowed secret or private identifier")
        # CQ-looking markup is rendered as plain text and cannot become a QQ segment.
        normalized = normalized.replace("[CQ:", "［CQ:").replace("[cq:", "［cq:")
        object.__setattr__(self, "text", normalized)

    @property
    def fingerprint(self) -> str:
        return f"{self.kind.value}:{self.text.casefold()}"


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
    commentary: PublicCommentary | None = None

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
        if self.kind == AgentEventKind.PUBLIC_COMMENTARY:
            if self.commentary is None or self.commentary.task_id != self.identity.task_id:
                raise ValueError("public commentary must match the event task identity")
            if self.round_index != self.commentary.round_index:
                raise ValueError("public commentary round must match the event round")
        elif self.commentary is not None:
            raise ValueError("commentary is only valid on public_commentary events")
        try:
            encoded = json.dumps(
                {"payload": asdict(self.payload), "commentary": asdict(self.commentary) if self.commentary else None},
                ensure_ascii=False, separators=(",", ":"), default=str,
            )
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
        commentary: PublicCommentary | None = None,
    ) -> bool: ...


class RequestAgentEventSink:
    """Assigns a monotonic per-request sequence and isolates observer failures."""

    def __init__(
        self,
        identity: TaskIdentity,
        consumer: Callable[[AgentEvent], Any] | None = None,
        *,
        max_events: int = MAX_EVENTS_PER_TASK,
        public_commentary_enabled: bool | None = None,
    ) -> None:
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self.identity = identity
        self._consumer = consumer
        self._public_commentary_enabled = (
            consumer is not None if public_commentary_enabled is None else bool(public_commentary_enabled)
        )
        self._max_events = max_events
        self._sequence = 0
        self._published = 0
        self.dropped_events = 0

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def has_consumer(self) -> bool:
        return self._consumer is not None

    @property
    def public_commentary_enabled(self) -> bool:
        return self.has_consumer and self._public_commentary_enabled

    def emit(
        self,
        kind: AgentEventKind,
        payload: AgentEventPayload | None = None,
        *,
        round_index: int | None = None,
        tool_call_id: str | None = None,
        commentary: PublicCommentary | None = None,
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
                commentary=commentary,
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

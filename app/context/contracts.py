from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.context.budget import ContextBudgetDecision


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    """Immutable boundary for one group's request-time history view."""

    group_id: str
    user_id: str
    request_id: str
    epoch: int
    current_row_id: int | None
    summary_revision: int
    persisted: bool = True
    persistence_status: Literal["persisted", "capacity_degraded"] = "persisted"

    def __post_init__(self) -> None:
        if not self.group_id:
            raise ValueError("context snapshot requires a group id")
        if self.epoch < 0 or self.summary_revision < 0:
            raise ValueError("context snapshot revisions cannot be negative")
        if self.persisted and (self.current_row_id is None or self.current_row_id < 1):
            raise ValueError("persisted snapshots require a positive message row id")
        if not self.persisted and self.current_row_id is not None:
            raise ValueError("non-persisted snapshots cannot claim a message row id")
        if self.persisted != (self.persistence_status == "persisted"):
            raise ValueError("snapshot persistence status must match its persisted flag")


@dataclass(frozen=True, slots=True)
class SummarySnapshot:
    """CAS input boundary for one summary update, copied before the model call."""

    group_id: str
    epoch: int
    revision: int
    previously_covered_message_id: int
    through_message_id: int
    previous_summary: str
    rows: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ContextItem:
    key: str
    kind: str
    content: str
    source_refs: tuple[str, ...]
    trust: str = "untrusted"
    estimated_tokens: int = 0
    required: bool = False
    priority: int = 0
    completeness: Literal["complete", "partial", "reference_only", "unavailable"] = "complete"


@dataclass(frozen=True, slots=True)
class ContextMessagePlan:
    message_index: int
    item_key: str
    priority: int
    required: bool
    source_refs: tuple[str, ...] = ()
    completeness: Literal["complete", "partial", "reference_only", "unavailable"] = "complete"


@dataclass(frozen=True, slots=True)
class WorkingState:
    """Request-scoped task seed; it is data, never an authority or permission source."""

    task_id: str
    group_id: str
    user_id: str
    request_id: str
    epoch: int
    goal: str
    constraints: tuple[str, ...] = ()
    completed_steps: tuple[str, ...] = ()
    pending_steps: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    status: Literal["running", "completed", "failed", "cancelled", "interrupted"] = "running"
    checkpoint_revision: int = 0

    def __post_init__(self) -> None:
        if not self.task_id or not self.group_id or not self.user_id or not self.request_id:
            raise ValueError("working state requires task, group, user, and request identifiers")
        if self.epoch < 0 or self.checkpoint_revision < 0:
            raise ValueError("working state revisions cannot be negative")


@dataclass(frozen=True, slots=True)
class PreparedContext:
    snapshot: ContextSnapshot
    messages: tuple[dict[str, Any], ...]
    items: tuple[ContextItem, ...]
    current_request: str
    persisted: bool
    estimated_input_tokens: int | None
    output_tokens: int | None
    dropped_item_keys: tuple[str, ...] = ()
    budget_decision: ContextBudgetDecision | None = None
    working_state: WorkingState | None = None
    context_plan: tuple[ContextMessagePlan, ...] = ()


class ContextEpochChanged(RuntimeError):
    """The request context was reset while work was in flight."""


class ContextPersistenceUnavailable(RuntimeError):
    """A required SQLite operation failed and cannot safely degrade."""


class ContextHistoryCapacityReached(RuntimeError):
    """A configured history capacity was reached while SQLite remains healthy."""

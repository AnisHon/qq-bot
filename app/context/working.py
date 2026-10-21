from __future__ import annotations

from dataclasses import asdict, replace
import json
from typing import Any, Callable

from app.context.contracts import WorkingState
from app.memory import MemoryStore
from app.planning.contracts import TaskPlan


class WorkingStateConflict(RuntimeError):
    """The scoped working task changed or expired before a checkpoint commit."""


class WorkingMemoryService:
    """Persists a bounded request-scoped Agent checkpoint; it never resumes execution."""

    def __init__(self, store: MemoryStore, sqlite_call: Callable[..., Any]) -> None:
        self.store = store
        self.sqlite_call = sqlite_call

    async def start(self, state: WorkingState, *, ttl_seconds: int = 86_400) -> WorkingState:
        normalized = self._bounded(state)
        created = await self.sqlite_call(
            self.store.create_agent_task,
            task_id=normalized.task_id,
            request_id=normalized.request_id,
            group_id=normalized.group_id,
            user_id=normalized.user_id,
            epoch=normalized.epoch,
            goal=normalized.goal,
            state_json=self._encode(normalized),
            ttl_seconds=ttl_seconds,
        )
        if not created:
            existing = await self.load(
                normalized.task_id,
                group_id=normalized.group_id,
                user_id=normalized.user_id,
                epoch=normalized.epoch,
            )
            if existing is not None and existing.request_id == normalized.request_id and existing.status == "running":
                return existing
            raise WorkingStateConflict("working task identifier already exists or is no longer active")
        return normalized

    async def checkpoint(
        self,
        state: WorkingState,
        *,
        status: str | None = None,
        **changes: Any,
    ) -> WorkingState:
        candidate = self._bounded(replace(state, status=status or state.status, **changes))
        next_status = candidate.status
        revision = await self.sqlite_call(
            self.store.save_agent_checkpoint,
            task_id=candidate.task_id,
            group_id=candidate.group_id,
            user_id=candidate.user_id,
            epoch=candidate.epoch,
            expected_revision=candidate.checkpoint_revision,
            state_json=self._encode(candidate),
            status=next_status,
        )
        if revision is None:
            raise WorkingStateConflict("working task checkpoint lost its scope, epoch, or revision")
        return replace(candidate, status=next_status, checkpoint_revision=revision)

    async def load(self, task_id: str, *, group_id: str, user_id: str, epoch: int) -> WorkingState | None:
        row = await self.sqlite_call(
            self.store.get_agent_task,
            task_id,
            group_id=group_id,
            user_id=user_id,
            epoch=epoch,
        )
        if row is None:
            return None
        try:
            payload = json.loads(str(row["state_json"]))
            if not isinstance(payload, dict):
                return None
            payload["status"] = str(row["status"])
            payload["checkpoint_revision"] = int(row["revision"])
            payload["constraints"] = tuple(payload.get("constraints", ()))
            payload["completed_steps"] = tuple(payload.get("completed_steps", ()))
            payload["pending_steps"] = tuple(payload.get("pending_steps", ()))
            payload["evidence_refs"] = tuple(payload.get("evidence_refs", ()))
            payload["assumptions"] = tuple(payload.get("assumptions", ()))
            payload["open_questions"] = tuple(payload.get("open_questions", ()))
            stored_plan = payload.get("task_plan")
            parsed_plan = TaskPlan.from_dict(stored_plan)
            # Older checkpoints have no task_plan field. A present but invalid
            # plan must not be silently discarded, since that would turn an
            # incomplete planned task into an apparently unplanned one.
            if stored_plan is not None and parsed_plan is None:
                return None
            payload["task_plan"] = parsed_plan
            return self._bounded(WorkingState(**payload))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _encode(state: WorkingState) -> str:
        return json.dumps(asdict(state), ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _bounded(state: WorkingState) -> WorkingState:
        if len(state.goal.encode("utf-8")) > 8192:
            raise ValueError("working goal is too large")
        for value in (*state.constraints, *state.completed_steps, *state.pending_steps,
                      *state.assumptions, *state.open_questions):
            if len(value.encode("utf-8")) > 2048:
                raise ValueError("working state item is too large")
        if max(len(state.constraints), len(state.completed_steps), len(state.pending_steps),
               len(state.evidence_refs), len(state.assumptions), len(state.open_questions)) > 64:
            raise ValueError("working state item count exceeds the limit")
        if state.task_plan is not None and state.task_plan.task_id != state.task_id:
            raise ValueError("working state task plan scope is invalid")
        if len(WorkingMemoryService._encode(state).encode("utf-8")) > 32_768:
            raise ValueError("working state is too large")
        return state

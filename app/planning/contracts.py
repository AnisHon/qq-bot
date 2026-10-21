from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import re
from typing import Any


MAX_PLAN_STEPS = 8
MAX_PLAN_TEXT_CHARS = 500
MAX_PLAN_CONSTRAINTS = 32
MAX_PLAN_EVIDENCE_REFS = 64
MAX_PLAN_TOOL_NAMES = 8
_STEP_ID = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,31}$")
_TOOL_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")
_EVIDENCE_KINDS = frozenset({
    "tool_result", "source", "full_source", "recipe", "version", "weather",
    "validation", "model_assessment",
})


class TaskComplexity(StrEnum):
    SIMPLE = "simple"
    MULTI_STEP = "multi_step"
    RESEARCH = "research"


class StepStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanAssessment(StrEnum):
    SATISFIED = "satisfied"
    UNSATISFIED = "unsatisfied"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PlanStep:
    step_id: str
    description: str
    dependencies: tuple[str, ...]
    completion_criteria: str
    required_evidence: tuple[str, ...]
    tool_names: tuple[str, ...] = ()
    status: StepStatus = StepStatus.PENDING
    assessment: PlanAssessment = PlanAssessment.UNKNOWN
    evidence_refs: tuple[str, ...] = ()
    tool_call_ids: tuple[str, ...] = ()
    verification: str = "none"

    def __post_init__(self) -> None:
        if not _STEP_ID.fullmatch(self.step_id):
            raise ValueError("plan step id is invalid")
        for name in ("description", "completion_criteria"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > MAX_PLAN_TEXT_CHARS:
                raise ValueError(f"plan step {name} is empty or too long")
        if len(self.dependencies) > MAX_PLAN_STEPS or len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("plan step dependencies are invalid")
        if len(self.tool_names) > MAX_PLAN_TOOL_NAMES or len(set(self.tool_names)) != len(self.tool_names):
            raise ValueError("plan step tool names are invalid")
        if any(not _TOOL_NAME.fullmatch(value) for value in self.tool_names):
            raise ValueError("plan step contains an invalid tool name")
        if not 1 <= len(self.required_evidence) <= 8 or len(set(self.required_evidence)) != len(self.required_evidence):
            raise ValueError("plan evidence requirements are invalid")
        if any(value not in _EVIDENCE_KINDS for value in self.required_evidence):
            raise ValueError("plan step contains an unsupported evidence requirement")
        if len(self.evidence_refs) > MAX_PLAN_EVIDENCE_REFS or len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("plan step evidence references are invalid")
        if len(self.tool_call_ids) > 32 or len(set(self.tool_call_ids)) != len(self.tool_call_ids):
            raise ValueError("plan step tool call references are invalid")
        if self.verification not in {"none", "evidence_checked", "model_assessed"}:
            raise ValueError("plan step verification state is invalid")


@dataclass(frozen=True, slots=True)
class TaskPlan:
    task_id: str
    goal: str
    constraints: tuple[str, ...]
    steps: tuple[PlanStep, ...]
    revision: int = 1
    replan_count: int = 0

    def __post_init__(self) -> None:
        if not self.task_id or len(self.task_id) > 160:
            raise ValueError("task plan identity is invalid")
        if not isinstance(self.goal, str) or not self.goal.strip() or len(self.goal.encode("utf-8")) > 8192:
            raise ValueError("task plan goal is empty or too large")
        if len(self.constraints) > MAX_PLAN_CONSTRAINTS or any(
            not isinstance(item, str) or len(item.encode("utf-8")) > MAX_PLAN_TEXT_CHARS
            for item in self.constraints
        ):
            raise ValueError("task plan constraints are invalid")
        if not 1 <= len(self.steps) <= MAX_PLAN_STEPS:
            raise ValueError("task plan must contain between 1 and 8 steps")
        if self.revision < 1 or not 0 <= self.replan_count <= 2:
            raise ValueError("task plan revision or replan count is invalid")
        identifiers = [step.step_id for step in self.steps]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("task plan step ids must be unique")
        available = set(identifiers)
        if any(step.step_id in step.dependencies or not set(step.dependencies) <= available for step in self.steps):
            raise ValueError("task plan contains an unknown or self dependency")
        self._validate_acyclic()

    def _validate_acyclic(self) -> None:
        by_id = {step.step_id: step for step in self.steps}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise ValueError("task plan dependencies contain a cycle")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in by_id[step_id].dependencies:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in by_id:
            visit(step_id)

    @classmethod
    def from_draft(
        cls,
        *,
        task_id: str,
        goal: str,
        constraints: tuple[str, ...],
        draft: Any,
        allowed_tools: frozenset[str],
        revision: int = 1,
        replan_count: int = 0,
        completed_steps: tuple[PlanStep, ...] = (),
        previous_steps: tuple[PlanStep, ...] = (),
    ) -> TaskPlan:
        if not isinstance(draft, dict) or set(draft) != {"steps"} or not isinstance(draft["steps"], list):
            raise ValueError("planner output must contain only a steps array")
        rows = draft["steps"]
        if not 1 <= len(rows) <= MAX_PLAN_STEPS:
            raise ValueError("planner returned an invalid number of steps")
        parsed: list[PlanStep] = []
        for row in rows:
            expected = {"step_id", "description", "dependencies", "completion_criteria", "required_evidence", "tool_names"}
            if not isinstance(row, dict) or set(row) != expected:
                raise ValueError("planner step fields do not match the contract")
            for key in ("dependencies", "required_evidence", "tool_names"):
                if not isinstance(row[key], list) or any(not isinstance(value, str) for value in row[key]):
                    raise ValueError(f"planner step {key} must be a string array")
            tool_names = tuple(row["tool_names"])
            if not set(tool_names) <= set(allowed_tools):
                raise ValueError("planner referenced a tool that is not enabled for this request")
            parsed.append(PlanStep(
                step_id=row["step_id"],
                description=row["description"],
                dependencies=tuple(row["dependencies"]),
                completion_criteria=row["completion_criteria"],
                required_evidence=tuple(row["required_evidence"]),
                tool_names=tool_names,
            ))

        # The caller supplies goal and constraints; planner output cannot rewrite them.
        completed = {step.step_id: step for step in completed_steps if step.status == StepStatus.COMPLETED}
        if completed:
            proposed = {step.step_id: step for step in parsed}
            for step_id, old_step in completed.items():
                if proposed.get(step_id) != replace(old_step, status=StepStatus.PENDING,
                                                    assessment=PlanAssessment.UNKNOWN,
                                                    evidence_refs=(), tool_call_ids=(), verification="none"):
                    raise ValueError("replan must preserve completed steps exactly")
                parsed = [old_step if step.step_id == step_id else step for step in parsed]
        prior_steps = previous_steps or completed_steps
        if prior_steps:
            proposed = {step.step_id: step for step in parsed}
            for old_step in prior_steps:
                if old_step.status in {StepStatus.COMPLETED, StepStatus.SKIPPED}:
                    continue
                replacement = proposed.get(old_step.step_id)
                if replacement is None:
                    raise ValueError("replan cannot silently remove an unresolved requirement")
                if replacement.completion_criteria != old_step.completion_criteria:
                    raise ValueError("replan cannot weaken or replace unresolved completion criteria")
                if not set(old_step.required_evidence) <= set(replacement.required_evidence):
                    raise ValueError("replan cannot weaken unresolved evidence requirements")
                if old_step.tool_names and not replacement.tool_names:
                    raise ValueError("replan cannot remove all tools required by an unresolved step")
        present = {step.step_id for step in parsed}
        normalized = tuple(
            step if step.status == StepStatus.COMPLETED else replace(
                step,
                status=(StepStatus.READY if not step.dependencies else StepStatus.PENDING),
            )
            for step in parsed
        )
        if any(not set(step.dependencies) <= present for step in normalized):
            raise ValueError("replan removed a step still required by a dependency")
        return cls(task_id, goal, constraints, normalized, revision, replan_count)

    @classmethod
    def from_dict(cls, value: Any) -> TaskPlan | None:
        if not isinstance(value, dict):
            return None
        try:
            steps = tuple(PlanStep(
                step_id=row["step_id"], description=row["description"],
                dependencies=tuple(row.get("dependencies", ())),
                completion_criteria=row["completion_criteria"],
                required_evidence=tuple(row.get("required_evidence", ())),
                tool_names=tuple(row.get("tool_names", ())),
                status=StepStatus(row.get("status", "pending")),
                assessment=PlanAssessment(row.get("assessment", "unknown")),
                evidence_refs=tuple(row.get("evidence_refs", ())),
                tool_call_ids=tuple(row.get("tool_call_ids", ())),
                verification=str(row.get("verification", "none")),
            ) for row in value.get("steps", ()))
            return cls(
                task_id=str(value["task_id"]), goal=str(value["goal"]),
                constraints=tuple(value.get("constraints", ())), steps=steps,
                revision=int(value.get("revision", 1)),
                replan_count=int(value.get("replan_count", 0)),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def as_prompt_data(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "revision": self.revision,
            "goal": self.goal,
            "constraints": list(self.constraints),
            "replan_count": self.replan_count,
            "steps": [{
                "step_id": step.step_id,
                "description": step.description,
                "dependencies": list(step.dependencies),
                "completion_criteria": step.completion_criteria,
                "required_evidence": list(step.required_evidence),
                "tool_names": list(step.tool_names),
                "status": step.status.value,
                "assessment": step.assessment.value,
                "evidence_refs": list(step.evidence_refs),
                "tool_call_ids": list(step.tool_call_ids),
                "verification": step.verification,
            } for step in self.steps],
        }

    def with_steps(self, steps: tuple[PlanStep, ...], *, replan: bool = False) -> TaskPlan:
        return TaskPlan(
            self.task_id, self.goal, self.constraints, steps,
            revision=self.revision + 1,
            replan_count=self.replan_count + (1 if replan else 0),
        )

    def public_summary(self) -> str:
        descriptions = [step.description.strip() for step in self.steps[:4]]
        suffix = "；后续步骤将根据结果调整。" if len(self.steps) > 4 else "。"
        return ("我会按计划处理：" + "；".join(descriptions) + suffix)[:240]

    @property
    def is_complete(self) -> bool:
        return all(step.status == StepStatus.COMPLETED for step in self.steps)

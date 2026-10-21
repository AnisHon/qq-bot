from __future__ import annotations

from dataclasses import replace
from typing import Any, Awaitable, Callable

from app.planning.contracts import PlanAssessment, PlanStep, StepStatus, TaskPlan
from app.progress.events import ToolCategory
from app.tools.contracts import (
    ToolAccess, ToolContext, ToolPolicy, ToolResult, ToolSpec, ToolStatus,
)


PersistPlan = Callable[[TaskPlan], Awaitable[None]]
EmitPlanEvent = Callable[[str, TaskPlan, str | None, int], None]


class PlanExecutionController:
    """Request-local plan reducer; evidence and authorization stay outside the plan."""

    def __init__(
        self,
        plan: TaskPlan,
        *,
        allowed_tools: frozenset[str],
        persist: PersistPlan,
        emit: EmitPlanEvent,
        evidence_store: Any,
        request_id: str,
        group_id: str,
        user_id: str,
        epoch: int,
    ) -> None:
        self.plan = plan
        self.allowed_tools = allowed_tools
        self.persist = persist
        self.emit = emit
        self.evidence_store = evidence_store
        self.scope = dict(request_id=request_id, group_id=group_id, user_id=user_id, epoch=epoch)
        self.tool_results: dict[str, tuple[str, str, tuple[str, ...]]] = {}
        self._current_round: int | None = None
        self._round_base_revision = plan.revision
        self._last_mutation_round: int | None = None
        self._assessed_steps_this_round: set[str] = set()

    def begin_round(self, round_index: int) -> None:
        """Freeze the plan revision visible to one model response.

        Several assessment calls can be returned together. They all reference
        the same revision the model saw, so later calls in that response may
        apply against that same base after earlier assessments have committed.
        Calls from later rounds must use the new revision.
        """
        if round_index < 1:
            raise ValueError("plan round index must be positive")
        self._current_round = round_index
        self._round_base_revision = self.plan.revision
        self._assessed_steps_this_round.clear()

    def prompt_data(self) -> dict[str, Any]:
        return self.plan.as_prompt_data()

    async def observe_tool_result(
        self,
        call_id: str,
        tool_name: str,
        result: ToolResult | BaseException,
        evidence_refs: tuple[str, ...],
        *,
        round_index: int,
    ) -> None:
        status = "error" if isinstance(result, BaseException) else result.status.value
        self.tool_results[call_id] = (tool_name, status, evidence_refs)
        steps = list(self.plan.steps)
        selected_index = self._step_for_tool(tool_name, steps)
        if selected_index is None:
            return
        step = steps[selected_index]
        next_status = (
            StepStatus.IN_PROGRESS if status in {"ok", "partial"}
            else StepStatus.BLOCKED
        )
        next_assessment = (
            PlanAssessment.UNKNOWN if next_status == StepStatus.IN_PROGRESS
            else PlanAssessment.UNSATISFIED
        )
        steps[selected_index] = replace(
            step,
            status=next_status,
            assessment=next_assessment,
            evidence_refs=tuple(dict.fromkeys((*step.evidence_refs, *evidence_refs)))[:64],
            tool_call_ids=tuple(dict.fromkeys((*step.tool_call_ids, call_id)))[:32],
            verification="none",
        )
        await self._commit(tuple(steps), event="step_updated", step_id=step.step_id,
                           round_index=round_index)

    async def assess_step(
        self,
        *,
        expected_revision: int,
        step_id: str,
        assessment: str,
        related_tool_call_ids: tuple[str, ...],
        evidence_refs: tuple[str, ...],
        round_index: int,
    ) -> ToolResult:
        if not self._revision_is_current_or_batched(expected_revision, round_index):
            return _failure("plan_revision_conflict", "The plan changed; use its current revision.")
        try:
            selected = PlanAssessment(assessment)
        except ValueError:
            return _failure("invalid_assessment", "Assessment must be satisfied, unsatisfied, or unknown.")
        step = next((item for item in self.plan.steps if item.step_id == step_id), None)
        if step is None or step.status in {StepStatus.COMPLETED, StepStatus.SKIPPED}:
            return _failure("plan_step_unavailable", "The plan step is unknown or already terminal.")
        if round_index == self._current_round and step_id in self._assessed_steps_this_round:
            return _failure("duplicate_step_assessment", "This step was already assessed in the current model response.")
        if not related_tool_call_ids or len(related_tool_call_ids) > 6:
            return _failure("plan_evidence_required", "Assessment must cite completed tool results.")
        if any(call_id not in self.tool_results for call_id in related_tool_call_ids):
            return _failure("plan_tool_scope", "A cited tool call is not a completed call in this request.")
        if not evidence_refs or len(evidence_refs) > 12:
            return _failure("plan_evidence_required", "Assessment must cite available evidence references.")
        allowed_refs = {
            ref for call_id in related_tool_call_ids
            for ref in self.tool_results[call_id][2]
        }
        if not set(evidence_refs) <= allowed_refs:
            return _failure("plan_evidence_scope", "Evidence references must belong to the cited tool calls.")
        if any(self.evidence_store.metadata_for(ref, **self.scope) is None for ref in evidence_refs):
            return _failure("plan_evidence_unavailable", "A cited evidence reference is unavailable in this request.")
        if selected == PlanAssessment.SATISFIED:
            incomplete_deps = [dep for dep in step.dependencies if not self._is_completed(dep)]
            if incomplete_deps:
                return _failure("plan_dependencies_incomplete", "Step dependencies are not complete.")
            reason = self._evidence_failure(step, related_tool_call_ids, evidence_refs)
            if reason:
                return _failure("plan_evidence_mismatch", reason)

        status = (
            StepStatus.COMPLETED if selected == PlanAssessment.SATISFIED
            else StepStatus.BLOCKED if selected == PlanAssessment.UNSATISFIED
            else StepStatus.IN_PROGRESS
        )
        # The application verifies scope, status, dependencies, evidence kind
        # and completeness. Natural-language completion criteria are still
        # judged by the model, so never label this as objective verification.
        verification = "model_assessed" if selected == PlanAssessment.SATISFIED else "none"
        steps = list(self.plan.steps)
        index = next(i for i, item in enumerate(steps) if item.step_id == step_id)
        steps[index] = replace(
            step,
            status=status,
            assessment=selected,
            evidence_refs=tuple(dict.fromkeys((*step.evidence_refs, *evidence_refs)))[:64],
            tool_call_ids=tuple(dict.fromkeys((*step.tool_call_ids, *related_tool_call_ids)))[:32],
            verification=verification,
        )
        await self._commit(tuple(self._unlock_ready(steps)), event="step_updated", step_id=step_id,
                           round_index=round_index)
        if round_index == self._current_round:
            self._assessed_steps_this_round.add(step_id)
        return ToolResult(ToolStatus.OK, {
            "step_id": step_id,
            "assessment": selected.value,
            "status": status.value,
            "verification": verification,
            "plan_revision": self.plan.revision,
        })

    async def replan(self, *, expected_revision: int, draft: Any, round_index: int) -> ToolResult:
        if not self._revision_is_current_or_batched(expected_revision, round_index):
            return _failure("plan_revision_conflict", "The plan changed; use its current revision.")
        if self.plan.replan_count >= 2:
            return _failure("replan_limit_reached", "The task has reached its two-replan limit.")
        try:
            revised = TaskPlan.from_draft(
                task_id=self.plan.task_id,
                goal=self.plan.goal,
                constraints=self.plan.constraints,
                draft=draft,
                allowed_tools=self.allowed_tools,
                revision=self.plan.revision + 1,
                replan_count=self.plan.replan_count + 1,
                completed_steps=tuple(item for item in self.plan.steps if item.status == StepStatus.COMPLETED),
                previous_steps=self.plan.steps,
            )
        except (TypeError, ValueError):
            return _failure("invalid_replan", "The revised plan failed validation and was not applied.")
        await self._commit(revised.steps, event="plan_revised", step_id=None,
                           round_index=round_index, plan=revised)
        return ToolResult(ToolStatus.OK, {
            "revision": self.plan.revision,
            "replan_count": self.plan.replan_count,
            "pending_steps": [step.step_id for step in self.plan.steps if step.status != StepStatus.COMPLETED],
        })

    def completion_snapshot(self) -> dict[str, Any]:
        incomplete = [
            {"step_id": step.step_id, "description": step.description,
             "status": step.status.value, "assessment": step.assessment.value}
            for step in self.plan.steps if step.status != StepStatus.COMPLETED
        ]
        verification_failures = []
        completed_ids = {step.step_id for step in self.plan.steps if step.status == StepStatus.COMPLETED}
        for step in self.plan.steps:
            if step.status != StepStatus.COMPLETED:
                continue
            invalid_reason = None
            if step.assessment != PlanAssessment.SATISFIED or step.verification != "model_assessed":
                invalid_reason = "completion was not model-assessed"
            elif not set(step.dependencies) <= completed_ids:
                invalid_reason = "a prerequisite is incomplete"
            else:
                invalid_reason = self._evidence_failure(
                    step, tuple(step.tool_call_ids), tuple(step.evidence_refs),
                )
            if invalid_reason:
                verification_failures.append({
                    "step_id": step.step_id,
                    "description": step.description,
                    "status": step.status.value,
                    "assessment": step.assessment.value,
                    "verification_error": invalid_reason,
                })
        return {
            "complete": not incomplete and not verification_failures,
            "revision": self.plan.revision,
            "replan_count": self.plan.replan_count,
            "completed_count": len(completed_ids) - len(verification_failures),
            "total_count": len(self.plan.steps),
            "incomplete": [*incomplete, *verification_failures],
            "verification": sorted({
                step.verification for step in self.plan.steps if step.status == StepStatus.COMPLETED
            }),
        }

    async def _commit(
        self,
        steps: tuple[PlanStep, ...],
        *,
        event: str,
        step_id: str | None,
        round_index: int,
        plan: TaskPlan | None = None,
    ) -> None:
        candidate = plan or self.plan.with_steps(self._unlock_ready(list(steps)))
        await self.persist(candidate)
        self.plan = candidate
        self._last_mutation_round = round_index
        try:
            self.emit(event, candidate, step_id, round_index)
        except Exception:  # noqa: BLE001 - observer failures never alter execution
            pass

    def _step_for_tool(self, tool_name: str, steps: list[PlanStep]) -> int | None:
        completed = {step.step_id for step in steps if step.status == StepStatus.COMPLETED}
        candidates = []
        for index, step in enumerate(steps):
            if step.status not in {StepStatus.PENDING, StepStatus.READY, StepStatus.IN_PROGRESS, StepStatus.BLOCKED}:
                continue
            if not set(step.dependencies) <= completed:
                continue
            if tool_name in step.tool_names:
                candidates.append(index)
        # A tool name alone cannot distinguish two plan steps that use the
        # same service. Leave the result unassigned; the model must associate
        # its explicit assessment with a call id and evidence refs.
        return candidates[0] if len(candidates) == 1 else None

    def _evidence_failure(
        self,
        step: PlanStep,
        call_ids: tuple[str, ...],
        evidence_refs: tuple[str, ...],
    ) -> str | None:
        outcomes = [self.tool_results[call_id][1] for call_id in call_ids]
        if any(status not in {"ok", "partial"} for status in outcomes):
            return "The cited tool result is not successful or partial."
        expected_tools = set(step.tool_names)
        observed_tools = {self.tool_results[call_id][0] for call_id in call_ids}
        if expected_tools and not expected_tools.intersection(observed_tools):
            return "The cited tool calls do not match the tools required by this step."
        metadata = [self.evidence_store.metadata_for(ref, **self.scope) for ref in evidence_refs]
        rows = [item for item in metadata if item is not None]
        checks = {
            "tool_result": any(status == "ok" for status in outcomes),
            "source": any(row.get("source_id") and row.get("kind") != "tool_output" for row in rows),
            "full_source": any(row.get("completeness") == "complete" for row in rows),
            "recipe": any(row.get("kind") == "recipe" and row.get("completeness") == "complete" for row in rows),
            "version": any(row.get("version") or row.get("pack_version") for row in rows),
            "weather": any(row.get("kind") == "weather" and row.get("completeness") == "complete" for row in rows),
            "validation": any(row.get("kind") == "sfm_validation" and row.get("completeness") == "complete" for row in rows),
            # This explicitly records a model assessment; it is never called objective verification.
            "model_assessment": bool(rows) and bool(step.dependencies or call_ids),
        }
        missing = [kind for kind in step.required_evidence if not checks[kind]]
        return f"Required evidence is missing or incomplete: {', '.join(missing)}." if missing else None

    def _unlock_ready(self, steps: list[PlanStep]) -> list[PlanStep]:
        completed = {step.step_id for step in steps if step.status == StepStatus.COMPLETED}
        return [
            replace(step, status=StepStatus.READY)
            if step.status == StepStatus.PENDING and set(step.dependencies) <= completed
            else step
            for step in steps
        ]

    def _is_completed(self, step_id: str) -> bool:
        return any(step.step_id == step_id and step.status == StepStatus.COMPLETED for step in self.plan.steps)

    def _revision_is_current_or_batched(self, expected_revision: int, round_index: int) -> bool:
        if expected_revision == self.plan.revision:
            return True
        return (
            expected_revision == self._round_base_revision
            and round_index == self._current_round
            and self._last_mutation_round == round_index
        )


def plan_tool_specs() -> tuple[ToolSpec, ToolSpec]:
    async def assess(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        controller = context.extensions.get("agent_plan_controller")
        if not isinstance(controller, PlanExecutionController):
            return _failure("planning_unavailable", "This request has no active task plan.")
        try:
            related = tuple(arguments.get("related_tool_call_ids", ()))
            refs = tuple(arguments.get("evidence_refs", ()))
            if any(not isinstance(item, str) for item in (*related, *refs)):
                raise ValueError
            return await controller.assess_step(
                expected_revision=int(arguments["expected_revision"]),
                step_id=str(arguments["step_id"]), assessment=str(arguments["assessment"]),
                related_tool_call_ids=related, evidence_refs=refs,
                round_index=context.agent_round or 1,
            )
        except (KeyError, TypeError, ValueError):
            return _failure("invalid_plan_assessment", "The step assessment arguments are invalid.")

    async def replan(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        controller = context.extensions.get("agent_plan_controller")
        if not isinstance(controller, PlanExecutionController):
            return _failure("planning_unavailable", "This request has no active task plan.")
        try:
            return await controller.replan(
                expected_revision=int(arguments["expected_revision"]),
                draft={"steps": arguments["steps"]},
                round_index=context.agent_round or 1,
            )
        except (KeyError, TypeError, ValueError):
            return _failure("invalid_replan", "The revised plan arguments are invalid.")

    policy = ToolPolicy(access=ToolAccess.READ_ONLY, allowed_roles=frozenset({"group_member"}))
    common = dict(policy=policy, timeout_seconds=3.0, max_concurrency=1,
                  max_result_chars=2000, progress_category=ToolCategory.COMMENTARY,
                  expose_by_default=False)
    assess_spec = ToolSpec(
        name="plan_step_assess",
        description=("Submit an assessment for an active plan step after inspecting its tool result. "
                     "A satisfied assessment must cite same-request tool_call IDs and EvidenceStore references; "
                     "the application independently checks scope, dependencies, statuses and evidence kinds. "
                     "Model assessment is not objective proof."),
        parameters={
            "type": "object", "properties": {
                "expected_revision": {"type": "integer", "minimum": 1},
                "step_id": {"type": "string", "maxLength": 32},
                "assessment": {"type": "string", "enum": [item.value for item in PlanAssessment]},
                "related_tool_call_ids": {"type": "array", "items": {"type": "string", "maxLength": 160}, "minItems": 1, "maxItems": 6},
                "evidence_refs": {"type": "array", "items": {"type": "string", "maxLength": 256}, "minItems": 1, "maxItems": 12},
            }, "required": ["expected_revision", "step_id", "assessment", "related_tool_call_ids", "evidence_refs"],
            "additionalProperties": False,
        }, handler=assess, **common,
    )
    step_schema = {
        "type": "object", "properties": {
            "step_id": {"type": "string", "maxLength": 32},
            "description": {"type": "string", "minLength": 1, "maxLength": 500},
            "dependencies": {"type": "array", "items": {"type": "string", "maxLength": 32}, "maxItems": 8},
            "completion_criteria": {"type": "string", "minLength": 1, "maxLength": 500},
            "required_evidence": {"type": "array", "items": {"type": "string", "enum": [
                "tool_result", "source", "full_source", "recipe", "version", "weather", "validation", "model_assessment"
            ]}, "minItems": 1, "maxItems": 8},
            "tool_names": {"type": "array", "items": {"type": "string", "maxLength": 64}, "maxItems": 8},
        }, "required": ["step_id", "description", "dependencies", "completion_criteria", "required_evidence", "tool_names"],
        "additionalProperties": False,
    }
    replan_spec = ToolSpec(
        name="replan_task",
        description=("Replace or extend only unresolved steps after a concrete failed, partial, or contradictory observation. "
                     "Preserve completed steps exactly, the original goal and constraints, and use only enabled tools. "
                     "At most two replans are accepted for one task."),
        parameters={
            "type": "object", "properties": {
                "expected_revision": {"type": "integer", "minimum": 1},
                "steps": {"type": "array", "items": step_schema, "minItems": 1, "maxItems": 8},
            }, "required": ["expected_revision", "steps"], "additionalProperties": False,
        }, handler=replan, **common,
    )
    return assess_spec, replan_spec


def _failure(code: str, message: str) -> ToolResult:
    from app.tools.contracts import ToolError
    return ToolResult(ToolStatus.INVALID_ARGUMENT, message, error=ToolError(code, message))

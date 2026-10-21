from __future__ import annotations

from typing import Any

from app.progress.events import (
    AgentEventKind,
    CommentaryKind,
    PublicCommentary,
    TaskIdentity,
    ToolCategory,
)
from app.prompts import PUBLIC_COMMENTARY_TOOL_RULES
from app.tools.contracts import (
    ToolAccess,
    ToolContext,
    ToolError,
    ToolPolicy,
    ToolResult,
    ToolSpec,
    ToolStatus,
)


PUBLIC_COMMENTARY_TOOL = "publish_public_commentary"
PUBLIC_COMMENTARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": [kind.value for kind in CommentaryKind]},
        "text": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240,
            "description": "A concise public status sentence. Do not include raw queries, URLs, results, or internal reasoning.",
        },
        "related_tool_call_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 160},
            "maxItems": 6,
        },
    },
    "required": ["kind", "text"],
    "additionalProperties": False,
}

_UPDATE_OUTCOMES = frozenset({"ok", "partial"})
_REVISION_OUTCOMES = frozenset({
    "partial", "no_results", "data_unavailable", "not_indexed", "version_mismatch",
    "denied", "timed_out", "error", "cancelled",
})
_BLOCKED_OUTCOMES = frozenset({
    "data_unavailable", "not_indexed", "version_mismatch", "denied", "timed_out", "error",
})


def commentary_delivery_available(event_sink: object | None, identity: TaskIdentity | None) -> bool:
    """Only expose the public-control function when a live request observer exists."""
    enabled = getattr(
        event_sink,
        "public_commentary_enabled",
        getattr(event_sink, "has_consumer", False),
    )
    return bool(
        identity is not None
        and event_sink is not None
        and enabled
    )


def public_commentary_spec() -> ToolSpec:
    async def publish(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        identity = context.task_identity
        sink = context.event_sink
        if identity is None or sink is None or not commentary_delivery_available(sink, identity):
            return ToolResult(
                ToolStatus.DATA_UNAVAILABLE,
                "Public progress delivery is unavailable for this request.",
                error=ToolError("commentary_unavailable", "Public progress delivery is unavailable."),
            )

        try:
            kind = CommentaryKind(arguments["kind"])
            refs = tuple(arguments.get("related_tool_call_ids", ()))
            commentary = PublicCommentary(
                kind=kind,
                text=arguments["text"],
                task_id=identity.task_id,
                round_index=context.agent_round or 1,
                related_tool_call_ids=refs,
            )
        except (KeyError, TypeError, ValueError):
            return ToolResult(
                ToolStatus.INVALID_ARGUMENT,
                "The public commentary was rejected by validation.",
                error=ToolError("invalid_public_commentary", "Public commentary failed validation."),
            )

        outcomes = context.extensions.get("agent_tool_outcomes", {})
        if not isinstance(outcomes, dict):
            outcomes = {}
        if kind == CommentaryKind.PLAN and refs:
            return _invalid("A plan must not claim completed tool results.")
        if kind == CommentaryKind.PLAN and context.extensions.get("agent_plan_publicly_announced"):
            return _invalid("The application has already published this task plan.")
        if kind == CommentaryKind.PLAN and (context.agent_round != 1 or outcomes):
            return _invalid("A public plan is only allowed before this task executes domain tools.")
        if kind != CommentaryKind.PLAN:
            if not refs or any(call_id not in outcomes for call_id in refs):
                return _invalid("An update must reference completed tool calls from this task.")
            referenced = {str(outcomes[call_id]) for call_id in refs}
            allowed = {
                CommentaryKind.UPDATE: _UPDATE_OUTCOMES,
                CommentaryKind.REVISION: _REVISION_OUTCOMES,
                CommentaryKind.BLOCKED: _BLOCKED_OUTCOMES,
            }[kind]
            if not referenced.intersection(allowed):
                return _invalid("The referenced tool outcomes do not support this commentary kind.")

        try:
            accepted = sink.emit(
                AgentEventKind.PUBLIC_COMMENTARY,
                round_index=commentary.round_index,
                tool_call_id=context.tool_call_id,
                commentary=commentary,
            )
        except Exception:  # noqa: BLE001 - commentary is optional instrumentation
            accepted = False
        if not accepted:
            return ToolResult(
                ToolStatus.PARTIAL,
                "Public commentary was not accepted; continue the task without it.",
            )
        return ToolResult(
            ToolStatus.OK,
            {"accepted": True, "note": "This is public narration of intent or status, not a verified task result."},
        )

    return ToolSpec(
        name=PUBLIC_COMMENTARY_TOOL,
        description=PUBLIC_COMMENTARY_TOOL_RULES,
        parameters=PUBLIC_COMMENTARY_SCHEMA,
        handler=publish,
        policy=ToolPolicy(access=ToolAccess.READ_ONLY, allowed_roles=frozenset({"group_member"})),
        timeout_seconds=2.0,
        max_concurrency=1,
        max_result_chars=800,
        progress_category=ToolCategory.COMMENTARY,
        expose_by_default=False,
    )


def _invalid(message: str) -> ToolResult:
    return ToolResult(
        ToolStatus.INVALID_ARGUMENT,
        "The public commentary was rejected because it did not match verified execution state.",
        error=ToolError("commentary_state_mismatch", message),
    )

from __future__ import annotations

import asyncio
from dataclasses import replace
from copy import deepcopy
import json
import logging
from typing import Any, Iterable

from app.tools.contracts import (
    PolicySnapshot,
    ToolContext,
    ToolError,
    ToolResult,
    ToolSpec,
    ToolStatus,
    ToolUsage,
)
from app.progress.events import AgentEventKind, AgentEventPayload, ToolOutcome


LOGGER = logging.getLogger(__name__)
MAX_ARGUMENT_BYTES = 64 * 1024
MAX_STRING_CHARS = 8 * 1024
MAX_ARRAY_ITEMS = 256


class ToolArgumentError(ValueError):
    def __init__(self, message: str, field: str | None = None) -> None:
        self.field = field
        super().__init__(message)


class ToolInvocationError(Exception):
    def __init__(self, error: ToolError, status: ToolStatus = ToolStatus.ERROR) -> None:
        self.error = error
        self.status = status
        super().__init__(error.message)


def _json_type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _validate_value(value: Any, schema: dict[str, Any], field_name: str) -> Any:
    expected = schema.get("type")
    if isinstance(expected, str) and not _json_type_matches(value, expected):
        raise ToolArgumentError(f"expected {expected}", field_name or None)
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        raise ToolArgumentError("value is not an allowed option", field_name or None)

    if isinstance(value, str):
        max_length = schema.get("maxLength", MAX_STRING_CHARS)
        min_length = schema.get("minLength", 0)
        if len(value) < min_length:
            raise ToolArgumentError("string is shorter than allowed", field_name or None)
        if len(value) > min(MAX_STRING_CHARS, max_length):
            raise ToolArgumentError("string exceeds the allowed length", field_name or None)

    if isinstance(value, list):
        max_items = schema.get("maxItems", MAX_ARRAY_ITEMS)
        min_items = schema.get("minItems", 0)
        if len(value) < min_items:
            raise ToolArgumentError("array has fewer items than required", field_name or None)
        if len(value) > min(MAX_ARRAY_ITEMS, max_items):
            raise ToolArgumentError("array exceeds the allowed item count", field_name or None)
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_value(item, item_schema, f"{field_name}[{index}]")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise ToolArgumentError("invalid tool schema")
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(key, str) for key in required):
            raise ToolArgumentError("invalid tool schema")
        for key in required:
            if key not in value:
                raise ToolArgumentError("required field is missing", f"{field_name}.{key}".strip("."))
        allow_extra = schema.get("additionalProperties", False)
        for key, item in value.items():
            key_path = f"{field_name}.{key}".strip(".")
            if key not in properties:
                if allow_extra is True:
                    continue
                if isinstance(allow_extra, dict):
                    _validate_value(item, allow_extra, key_path)
                    continue
                raise ToolArgumentError("unknown field", key_path)
            child_schema = properties[key]
            if not isinstance(child_schema, dict):
                raise ToolArgumentError("invalid tool schema", key_path)
            _validate_value(item, child_schema, key_path)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise ToolArgumentError("number is below the allowed minimum", field_name or None)
        if isinstance(maximum, (int, float)) and value > maximum:
            raise ToolArgumentError("number exceeds the allowed maximum", field_name or None)
    return value


def _decode_arguments(raw_arguments: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw_arguments, str):
        try:
            raw_size = len(raw_arguments.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ToolArgumentError("arguments contain invalid text encoding") from exc
        if raw_size > MAX_ARGUMENT_BYTES:
            raise ToolArgumentError("arguments exceed the allowed size")

        def reject_constant(token: str) -> None:
            raise ValueError(f"invalid JSON constant: {token}")

        try:
            value = json.loads(raw_arguments, parse_constant=reject_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ToolArgumentError("arguments are not valid JSON") from exc
    else:
        value = raw_arguments
        try:
            encoded_size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise ToolArgumentError("arguments are not valid JSON values") from exc
        if encoded_size > MAX_ARGUMENT_BYTES:
            raise ToolArgumentError("arguments exceed the allowed size")
    if not isinstance(value, dict):
        raise ToolArgumentError("arguments must be a JSON object")
    return dict(value)


def validate_arguments(parameters: dict[str, Any], raw_arguments: str | dict[str, Any]) -> dict[str, Any]:
    """Validate the deliberately small JSON Schema subset used by Bot tools."""
    value = _decode_arguments(raw_arguments)
    validated = _validate_value(value, parameters, "")
    return dict(validated)


class ToolRegistry:
    """Startup-assembled tool schema and dispatch registry."""

    def __init__(self, specs: Iterable[ToolSpec] = (), *, frozen: bool = False) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._frozen = False
        for spec in specs:
            self.register(spec)
        if frozen:
            self.freeze()

    def register(self, spec: ToolSpec) -> None:
        if self._frozen:
            raise RuntimeError("tool registry is frozen")
        if spec.name in self._specs:
            raise ValueError(f"duplicate tool name: {spec.name}")
        self._specs[spec.name] = spec

    def freeze(self) -> None:
        self._frozen = True

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(
        self,
        policy_snapshot: PolicySnapshot | None = None,
        *,
        group_id: str = "",
        user_id: str = "",
        include_internal: bool = False,
    ) -> frozenset[str]:
        visible_specs = {
            name: spec for name, spec in self._specs.items()
            if include_internal or spec.expose_by_default
        }
        if policy_snapshot is None:
            return frozenset(visible_specs)
        context = ToolContext(
            request_id="",
            group_id=group_id,
            user_id=user_id,
            policy_snapshot=policy_snapshot,
        )
        return frozenset(
            name for name, spec in visible_specs.items()
            if spec.policy.denial_reason(name, context) is None
        )

    def openai_schemas(
        self,
        policy_snapshot: PolicySnapshot | None = None,
        *,
        group_id: str = "",
        user_id: str = "",
        enabled_names: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        allowed = self.names(
            policy_snapshot, group_id=group_id, user_id=user_id,
            include_internal=enabled_names is not None,
        )
        if enabled_names is not None:
            allowed = allowed.intersection(str(name) for name in enabled_names)
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": deepcopy(spec.parameters),
                },
            }
            for name, spec in self._specs.items()
            if name in allowed
        ]

    async def dispatch(self, name: str, raw_arguments: str | dict[str, Any], context: ToolContext) -> ToolResult:
        spec = self._specs.get(name)
        if spec is None:
            self._emit(context, AgentEventKind.TOOL_REJECTED, AgentEventPayload(
                outcome=ToolOutcome.REJECTED, execution_started=False,
            ))
            return self._failure(ToolStatus.INVALID_ARGUMENT, "unknown_tool", "Unknown tool.")

        denial = spec.policy.denial_reason(name, context)
        if denial:
            self._emit(context, AgentEventKind.TOOL_REJECTED, AgentEventPayload(
                category=spec.progress_category, outcome=ToolOutcome.DENIED, execution_started=False,
            ))
            return self._failure(ToolStatus.DENIED, "tool_denied", "This tool is not permitted.")
        try:
            arguments = _decode_arguments(raw_arguments)
            if spec.argument_validator is not None:
                arguments = spec.argument_validator(arguments)
                if not isinstance(arguments, dict):
                    raise ToolArgumentError("validator must return an object")
            arguments = validate_arguments(spec.parameters, arguments)
        except ToolArgumentError as exc:
            self._emit(context, AgentEventKind.TOOL_REJECTED, AgentEventPayload(
                category=spec.progress_category, outcome=ToolOutcome.REJECTED, execution_started=False,
            ))
            return self._failure(
                ToolStatus.INVALID_ARGUMENT,
                "invalid_arguments",
                str(exc),
                field=exc.field,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("tool %s argument validator failed (%s)", name, type(exc).__name__)
            self._emit(context, AgentEventKind.TOOL_REJECTED, AgentEventPayload(
                category=spec.progress_category, outcome=ToolOutcome.ERROR, execution_started=False,
            ))
            return self._failure(ToolStatus.ERROR, "argument_validation_error", "Tool argument validation failed.")

        loop = asyncio.get_running_loop()
        remaining = None
        if context.deadline_monotonic is not None:
            remaining = context.deadline_monotonic - loop.time()
        timeout = spec.timeout_seconds
        if context.tool_timeout_seconds is not None:
            timeout = min(timeout, context.tool_timeout_seconds)
        deadline_limited = False
        if remaining is not None:
            if remaining <= 0:
                self._emit(context, AgentEventKind.TOOL_REJECTED, AgentEventPayload(
                    category=spec.progress_category, outcome=ToolOutcome.TIMEOUT, execution_started=False,
                ))
                return self._failure(ToolStatus.TIMED_OUT, "deadline_expired", "Request deadline expired.")
            deadline_limited = (
                remaining <= spec.timeout_seconds
                and (context.tool_timeout_seconds is None or remaining <= context.tool_timeout_seconds)
            )
            timeout = min(timeout, remaining)

        semaphore = self._semaphores.setdefault(name, asyncio.Semaphore(spec.max_concurrency))
        started = False
        try:
            async with asyncio.timeout(timeout):
                await semaphore.acquire()
                try:
                    started = True
                    self._emit(context, AgentEventKind.TOOL_STARTED, AgentEventPayload(
                        category=spec.progress_category, execution_started=True,
                    ))
                    result = await spec.handler(context, arguments)
                finally:
                    semaphore.release()
        except asyncio.CancelledError:
            self._emit(context, AgentEventKind.TOOL_CANCELLED, AgentEventPayload(
                category=spec.progress_category,
                outcome=ToolOutcome.CANCELLED,
                execution_started=started,
            ))
            raise
        except TimeoutError:
            self._emit(context, AgentEventKind.TOOL_FAILED if started else AgentEventKind.TOOL_REJECTED,
                       AgentEventPayload(
                           category=spec.progress_category,
                           outcome=ToolOutcome.TIMEOUT,
                           execution_started=started,
                       ))
            if deadline_limited:
                # Let the Agent's request-level deadline cancel sibling calls
                # and run its existing cancellation cleanup path.
                raise
            return self._failure(ToolStatus.TIMED_OUT, "tool_timeout", "Tool execution timed out.", retryable=True)
        except ToolInvocationError as exc:
            result = ToolResult(status=exc.status, content=exc.error.message, error=exc.error)
            self._emit_result(context, spec.progress_category, result)
            return result
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("tool %s handler failed (%s)", name, type(exc).__name__)
            result = self._failure(ToolStatus.ERROR, "tool_execution_error", "Tool execution failed.", retryable=True)
            self._emit(context, AgentEventKind.TOOL_FAILED, AgentEventPayload(
                category=spec.progress_category, outcome=ToolOutcome.ERROR, execution_started=True,
            ))
            return result

        if not isinstance(result, ToolResult):
            invalid = self._failure(ToolStatus.ERROR, "invalid_tool_result", "Tool returned an invalid result.")
            self._emit(context, AgentEventKind.TOOL_FAILED, AgentEventPayload(
                category=spec.progress_category, outcome=ToolOutcome.ERROR, execution_started=True,
            ))
            return invalid
        result_limit = spec.max_result_chars
        if context.result_max_chars is not None:
            result_limit = min(result_limit, max(1, context.result_max_chars))
        limited = self._limit_result(result, result_limit, context.evidence_limit)
        self._emit_result(context, spec.progress_category, limited)
        return limited

    @staticmethod
    def _emit(context: ToolContext, kind: AgentEventKind, payload: AgentEventPayload) -> None:
        if context.event_sink is None or context.task_identity is None:
            return
        try:
            context.event_sink.emit(
                kind,
                payload,
                round_index=context.agent_round,
                tool_call_id=context.tool_call_id,
            )
        except Exception:  # noqa: BLE001 - instrumentation is isolated from tool outcomes
            LOGGER.debug("tool lifecycle event was dropped", exc_info=True)

    @classmethod
    def _emit_result(cls, context: ToolContext, category, result: ToolResult) -> None:
        if result.status == ToolStatus.CANCELLED:
            event_kind = AgentEventKind.TOOL_CANCELLED
        elif result.status in {
            ToolStatus.ERROR, ToolStatus.INVALID_ARGUMENT, ToolStatus.DENIED, ToolStatus.TIMED_OUT,
        }:
            event_kind = AgentEventKind.TOOL_FAILED
        else:
            event_kind = AgentEventKind.TOOL_COMPLETED
        outcome = {
            ToolStatus.OK: ToolOutcome.SUCCESS,
            ToolStatus.PARTIAL: ToolOutcome.PARTIAL,
            ToolStatus.NO_RESULTS: ToolOutcome.NO_RESULTS,
            ToolStatus.DATA_UNAVAILABLE: ToolOutcome.UNAVAILABLE,
            ToolStatus.NOT_INDEXED: ToolOutcome.UNAVAILABLE,
            ToolStatus.VERSION_MISMATCH: ToolOutcome.UNAVAILABLE,
            ToolStatus.INVALID_ARGUMENT: ToolOutcome.REJECTED,
            ToolStatus.DENIED: ToolOutcome.DENIED,
            ToolStatus.TIMED_OUT: ToolOutcome.TIMEOUT,
            ToolStatus.CANCELLED: ToolOutcome.CANCELLED,
            ToolStatus.ERROR: ToolOutcome.ERROR,
        }.get(result.status, ToolOutcome.UNKNOWN)
        cls._emit(context, event_kind, AgentEventPayload(
            category=category,
            outcome=outcome,
            execution_started=True,
            evidence_count=len(result.evidence),
            completeness=result.completeness,
        ))

    @staticmethod
    def _failure(
        status: ToolStatus,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        field: str | None = None,
    ) -> ToolResult:
        error = ToolError(code=code, message=message, retryable=retryable, field=field)
        return ToolResult(status=status, content=message, error=error)

    @staticmethod
    def _limit_result(result: ToolResult, max_chars: int, evidence_limit: int) -> ToolResult:
        content = result.content
        serialized = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
        content_truncated = len(serialized) > max_chars
        if content_truncated:
            content = serialized[:max_chars].rstrip() + "…[truncated]"
        limit = max(0, evidence_limit)
        evidence = result.evidence[:limit]
        if (content_truncated or len(evidence) != len(result.evidence)) and result.status == ToolStatus.OK:
            status = ToolStatus.PARTIAL
        else:
            status = result.status
        completeness = result.completeness
        if result.status == ToolStatus.PARTIAL or content_truncated or len(evidence) != len(result.evidence):
            completeness = "partial"
            evidence = tuple(
                replace(item, completeness="partial") if item.completeness == "complete" else item
                for item in evidence
            )
        return ToolResult(
            status=status, content=content, evidence=evidence, usage=result.usage,
            error=result.error, completeness=completeness,
        )

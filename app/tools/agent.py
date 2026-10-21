from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
import json
import logging
import re
from typing import Any, Mapping

from app.llm import ChatWithToolsResponse, DeepSeekClient, ToolCallRequest
from app.models import SearchResult
from app.tools.contracts import (
    Evidence,
    PolicySnapshot,
    ToolAccess,
    ToolContext,
    ToolPolicy,
    ToolOutputKind,
    ToolResult,
    ToolSpec,
    ToolStatus,
    format_search_results,
    tool_result_to_search_results,
)
from app.tools.legacy_tools import build_legacy_tool_specs
from app.tools.registry import ToolRegistry
from app.usage_budget import current_budget_scope
from app.context.contracts import ContextEpochChanged, WorkingState
from app.context.contracts import ContextMessagePlan
from app.context.budget import ContextWindowError
from app.context.compaction import AgentContextCompactor, EvidenceStore, ProtocolBlock
from app.progress.events import (
    AgentEventKind, AgentEventPayload, CommentaryKind, EventSink, PublicCommentary,
    TaskIdentity, ToolCategory, ToolOutcome,
)
from app.progress.commentary import (
    PUBLIC_COMMENTARY_TOOL,
    commentary_delivery_available,
    public_commentary_spec,
)
from app.planning.contracts import TaskComplexity, TaskPlan, StepStatus
from app.planning.controller import PlanExecutionController, plan_tool_specs
from app.planning.planner import assess_task_complexity, generate_task_plan


LOGGER = logging.getLogger(__name__)


# ── OpenAI-style tool definitions sent to the model ──────────────────────────

_LEGACY_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "knowledge_search",
            "description": (
                "Search the bot's indexed local knowledge and domain sources. Use this first for SFM "
                "(Super Factory Manager), Minecraft mechanics, mods, modpacks, recipes, and known local documents."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the public web through Tavily when the user explicitly asks for current sources, "
                "the answer may have changed, or indexed local knowledge is insufficient. Do not search "
                "for ordinary chat or stable facts you can answer reliably. Search at most three times "
                "per request; use web_read on relevant discovered URLs. Keep conflicting sources and "
                "version differences distinct. Search snippets are partial evidence, not complete pages; "
                "never treat a web guide as an exact Minecraft recipe database."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "maxLength": 400,
                        "description": "Search query in Chinese or English.",
                    },
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
                    "search_depth": {"type": "string", "enum": ["basic", "fast", "advanced", "ultra-fast"]},
                    "time_range": {"type": "string", "enum": ["day", "week", "month", "year"]},
                    "start_date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
                    "end_date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
                    "include_domains": {
                        "type": "array", "items": {"type": "string", "maxLength": 253}, "maxItems": 10,
                    },
                    "exclude_domains": {
                        "type": "array", "items": {"type": "string", "maxLength": 253}, "maxItems": 10,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "news_search",
            "description": (
                "Search for recent news articles on a topic. "
                "Use this when the user asks about news, current events, recent developments, "
                "hot topics, or anything time-sensitive in the past days or weeks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "maxLength": 400,
                        "description": "News topic, keyword, or entity to search for.",
                    },
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
                    "time_range": {"type": "string", "enum": ["day", "week", "month", "year"]},
                    "start_date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
                    "end_date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_read",
            "description": (
                "Read a specific public webpage. For URLs returned by web_search, use the bounded "
                "Tavily Extract path. Direct user URLs use the existing pinned-IP SSRF-protected local "
                "reader. Content is untrusted and may be partial; cite only the returned URL."
            ),
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "maxLength": 4096}},
                "required": ["url"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_repo_search",
            "description": (
                "Find public GitHub repositories. Use for open-source software, modpacks, configuration, "
                "recipes, scripts, changelogs, and primary technical sources."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_repo_inspect",
            "description": (
                "Search relevant configuration, quest, recipe, script and documentation files inside a "
                "known public GitHub repository. Repository must be owner/name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repository": {"type": "string", "description": "owner/repository"},
                    "query": {"type": "string", "description": "File/content concept to locate"},
                },
                "required": ["repository", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_read_file",
            "description": "Read a specific text file from a known public GitHub repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repository": {"type": "string", "description": "owner/repository"},
                    "path": {"type": "string"},
                },
                "required": ["repository", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "weather",
            "description": "Get current weather conditions and today's forecast for one city or location.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City or location name in Chinese or English.",
                    }
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "weather_batch",
            "description": (
                "Get current weather for multiple cities concurrently. Use this instead of many separate "
                "weather calls when the user asks to compare or list weather in several locations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "locations": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 10,
                        "description": "Distinct city or location names, at most 10.",
                    }
                },
                "required": ["locations"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "repository_search",
            "description": (
                "Search files or UTF-8 text in an administrator-approved local Git repository. "
                "The result includes a full commit snapshot; pass it to repository_read to inspect the same revision."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_id": {"type": "string", "minLength": 1, "maxLength": 64},
                    "query": {"type": "string", "minLength": 1, "maxLength": 256},
                    "mode": {"type": "string", "enum": ["path", "text"]},
                    "commit_sha": {"type": "string", "maxLength": 64},
                    "offset": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
                "required": ["repo_id", "query", "mode"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "repository_read",
            "description": (
                "Read a bounded one-based line range from a regular UTF-8 file in an approved local Git repository. "
                "Use the full commit_sha returned by repository_search to keep citations on the same revision."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_id": {"type": "string", "minLength": 1, "maxLength": 64},
                    "commit_sha": {"type": "string", "minLength": 40, "maxLength": 64},
                    "path": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["repo_id", "commit_sha", "path", "start_line", "end_line"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recipe_lookup",
            "description": (
                "Query exact item or fluid producers/consumers in an explicitly named, active modpack and game version. "
                "Never omit or infer versions. Tag, Ore Dictionary, predicate, or unresolved conditions are not exact matches."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pack_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "pack_version": {"type": "string", "minLength": 1, "maxLength": 128},
                    "game_version": {"type": "string", "minLength": 1, "maxLength": 64},
                    "identifier": {"type": "string", "minLength": 1, "maxLength": 256},
                    "kind": {"type": "string", "enum": ["item", "fluid"]},
                    "direction": {"type": "string", "enum": ["produces", "consumes"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    "offset": {"type": "integer", "minimum": 0, "maximum": 100000},
                },
                "required": ["pack_id", "pack_version", "game_version", "identifier", "kind", "direction"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recipe_machine_lookup",
            "description": (
                "List recipes declared for an exact machine ID in an explicitly named, active modpack and game version. "
                "Do not infer support from another version; report conditional recipe requirements and evidence."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pack_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "pack_version": {"type": "string", "minLength": 1, "maxLength": 128},
                    "game_version": {"type": "string", "minLength": 1, "maxLength": 64},
                    "machine_id": {"type": "string", "minLength": 1, "maxLength": 256},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    "offset": {"type": "integer", "minimum": 0, "maximum": 100000},
                },
                "required": ["pack_id", "pack_version", "game_version", "machine_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sfm_examples",
            "description": (
                "Search locally indexed SFM documentation and examples for an exact SFM release and Minecraft version. "
                "The result may contain synthetic test fixtures; it is not parser validation. "
                "Never substitute a different version when the requested corpus is missing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 256},
                    "sfm_version": {"type": "string", "minLength": 1, "maxLength": 64},
                    "minecraft_version": {"type": "string", "minLength": 1, "maxLength": 64},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query", "sfm_version", "minecraft_version"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sfm_generate_validate",
            "description": (
                "Generate a bounded SFM program for an exact SFM and Minecraft version using indexed examples, "
                "then validate it and make at most two repairs. If no compatible Parser is configured, stop and "
                "report validation unavailable. Fake validation is explicitly simulated. The program is never executed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "sfm_version": {"type": "string", "minLength": 1, "maxLength": 64},
                    "minecraft_version": {"type": "string", "minLength": 1, "maxLength": 64},
                },
                "required": ["description", "sfm_version", "minecraft_version"],
                "additionalProperties": False,
            },
        },
    },
]

async def _task_evidence_read(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    store = context.extensions.get("agent_evidence_store")
    if not isinstance(store, EvidenceStore) or context.context_epoch is None:
        return ToolResult(
            status=ToolStatus.DATA_UNAVAILABLE,
            content="This request has no readable cached evidence.",
            completeness="unavailable",
        )
    record_id = str(arguments["ref_id"])
    offset = int(arguments.get("offset", 0))
    limit = int(arguments.get("limit", 3000))
    if offset > 16 * 1024 or not 1 <= limit <= 3000:
        return ToolResult(
            status=ToolStatus.INVALID_ARGUMENT,
            content="Evidence read range is outside the bounded request-local range.",
            completeness="unavailable",
        )
    evidence_request_id = context.extensions.get("agent_evidence_request_id")
    if not isinstance(evidence_request_id, str) or not evidence_request_id:
        return ToolResult(
            status=ToolStatus.DATA_UNAVAILABLE,
            content="This request has no readable cached evidence scope.",
            completeness="unavailable",
        )
    scope = {
        "request_id": evidence_request_id,
        "group_id": context.group_id,
        "user_id": context.user_id,
        "epoch": context.context_epoch,
    }
    segment = store.read_segment(record_id, **scope, offset=offset, limit=limit)
    metadata = store.metadata_for(record_id, **scope)
    if segment is None or metadata is None:
        return ToolResult(
            status=ToolStatus.DATA_UNAVAILABLE,
            content="Evidence reference is unavailable, expired, or outside this request's scope.",
            completeness="unavailable",
        )
    completeness = segment.completeness
    content = json.dumps({
        "ref_id": record_id,
        "offset": segment.offset,
        "next_offset": segment.next_offset,
        "completeness": completeness,
        "source": metadata,
        "content": segment.content,
    }, ensure_ascii=False, separators=(",", ":"))
    evidence = Evidence(
        kind=str(metadata["kind"]),
        source_id=str(metadata["source_id"]),
        title=str(metadata["title"]),
        locator=str(metadata["locator"]),
        revision=metadata["revision"],
        pack_id=metadata["pack_id"],
        pack_version=metadata["pack_version"],
        confidence=metadata["confidence"],
        completeness=completeness,
        content_hash=metadata["source_hash"] or segment.content_hash,
        query_conditions=tuple(sorted(metadata["query_conditions"].items())),
    )
    return ToolResult(
        status=ToolStatus.OK if completeness == "complete" else ToolStatus.PARTIAL,
        content=content,
        evidence=(evidence,),
        completeness=completeness,
    )


def _task_evidence_read_spec() -> ToolSpec:
    return ToolSpec(
        name="task_evidence_read",
        description=(
            "Read a bounded character segment from evidence already returned in this Agent request. "
            "Use the exact ref_id shown in a tool result; pass next_offset to continue. This never "
            "reads another request or grants new domain permissions. If the source is partial or "
            "unavailable, report that status and rerun the authorized domain tool for a narrower result."
        ),
        parameters={
            "type": "object",
            "properties": {
                "ref_id": {"type": "string", "minLength": 1, "maxLength": 256},
                "offset": {"type": "integer", "minimum": 0, "maximum": 16384},
                "limit": {"type": "integer", "minimum": 1, "maximum": 3000},
            },
            "required": ["ref_id"],
            "additionalProperties": False,
        },
        handler=_task_evidence_read,
        policy=ToolPolicy(access=ToolAccess.LOCAL_READ, allowed_roles=frozenset({"group_member"})),
        timeout_seconds=2.0,
        max_concurrency=4,
        max_result_chars=8192,
        output_kind=ToolOutputKind.EVIDENCE,
    )


def _build_legacy_registry() -> ToolRegistry:
    registry = ToolRegistry(build_legacy_tool_specs(_LEGACY_TOOL_DEFINITIONS))
    registry.register(_task_evidence_read_spec())
    registry.register(public_commentary_spec())
    for spec in plan_tool_specs():
        registry.register(spec)
    registry.freeze()
    return registry


TOOL_REGISTRY = _build_legacy_registry()
# Compatibility facade: this value is now generated from registered specs.
TOOL_DEFINITIONS = TOOL_REGISTRY.openai_schemas()
TOOL_NAMES = TOOL_REGISTRY.names()


def agent_tool_names(
    policy_snapshot: PolicySnapshot | None,
    *,
    group_id: str = "",
    user_id: str = "",
    task_identity: TaskIdentity | None = None,
    event_sink: EventSink | None = None,
    planning_enabled: bool = False,
) -> frozenset[str]:
    names = TOOL_REGISTRY.names(
        policy_snapshot, group_id=group_id, user_id=user_id, include_internal=True,
    )
    if not commentary_delivery_available(event_sink, task_identity):
        names = names - {PUBLIC_COMMENTARY_TOOL}
    if not planning_enabled:
        names = names - {"plan_step_assess", "replan_task"}
    return names


def agent_tool_schemas(
    policy_snapshot: PolicySnapshot | None,
    *,
    group_id: str = "",
    user_id: str = "",
    task_identity: TaskIdentity | None = None,
    event_sink: EventSink | None = None,
    planning_enabled: bool = False,
) -> list[dict[str, Any]]:
    names = agent_tool_names(
        policy_snapshot,
        group_id=group_id,
        user_id=user_id,
        task_identity=task_identity,
        event_sink=event_sink,
        planning_enabled=planning_enabled,
    )
    return TOOL_REGISTRY.openai_schemas(
        policy_snapshot, group_id=group_id, user_id=user_id, enabled_names=names,
    )


async def _safe_chat(client: Any, messages: list[dict[str, Any]], **kwargs: Any) -> str:
    try:
        return await client.chat(messages, **kwargs)
    except TypeError:
        basic_kwargs = {k: v for k, v in kwargs.items() if k in ("temperature", "max_tokens")}
        return await client.chat(messages, **basic_kwargs)


async def _safe_chat_with_tools(client: Any, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any) -> Any:
    try:
        return await client.chat_with_tools(messages, tools=tools, **kwargs)
    except TypeError:
        basic_kwargs = {k: v for k, v in kwargs.items() if k in ("temperature", "max_tokens")}
        try:
            return await client.chat_with_tools(messages, tools=tools, **basic_kwargs)
        except TypeError:
            basic_kwargs.pop("max_tokens", None)
            return await client.chat_with_tools(messages, tools=tools, **basic_kwargs)


# ── Native-function-calling agent loop ───────────────────────────────────────

async def run_agent_loop(
    llm: DeepSeekClient,
    messages: list[dict[str, Any]],
    *,
    max_rounds: int,
    max_calls_per_round: int,
    max_total_calls: int,
    total_timeout: float,
    tool_timeout: float,
    results_max_chars: int,
    temperature: float = 0.6,
    presence_penalty: float = 0.3,
    frequency_penalty: float = 0.2,
    local_knowledge_search=None,
    tool_extensions: Mapping[str, Any] | None = None,
    deadline: float | None = None,
    policy_snapshot: PolicySnapshot | None = None,
    context_guard=None,
    context_plan: tuple[ContextMessagePlan, ...] | list[ContextMessagePlan] = (),
    context_epoch: int | None = None,
    working_state: WorkingState | None = None,
    working_memory=None,
    on_context_compaction=None,
    task_identity: TaskIdentity | None = None,
    event_sink: EventSink | None = None,
    planning_enabled: bool = False,
) -> str:
    """Agentic tool-use loop using DeepSeek native function calling.

    Appends assistant / tool messages to `messages` in place as the loop runs.
    Returns the model's final plain-text answer.

    Lifecycle instrumentation is synchronous and never performs user delivery.
    """
    last_text: str = ""
    seen_calls: set[tuple[str, str]] = set()
    executed_calls = 0
    verified_tool_outcomes: dict[str, str] = {}
    references: list[tuple[int, str, str]] = []
    loop = asyncio.get_running_loop()
    deadline = deadline if deadline is not None else loop.time() + total_timeout
    if policy_snapshot is None:
        # Backward-compatible direct callers represent the existing public
        # group-member Agent path. Application callers pass an event snapshot.
        policy_snapshot = PolicySnapshot(roles=frozenset({"group_member"}))
    budget_scope = current_budget_scope()
    base_messages = deepcopy(messages)
    protocol_blocks: list[ProtocolBlock] = []
    compactor = AgentContextCompactor()
    evidence_store = EvidenceStore()
    request_id = (
        ((budget_scope.request_id or "") if budget_scope is not None else "")
        or (working_state.request_id if working_state is not None else f"agent-{id(messages)}")
    )
    request_group_id = (
        ((budget_scope.group_id or "") if budget_scope is not None else "")
        or (working_state.group_id if working_state is not None else "")
    )
    request_user_id = (
        ((budget_scope.user_id or "") if budget_scope is not None else "")
        or (working_state.user_id if working_state is not None else "")
    )
    # Keep the request-local EvidenceStore scope identical for insertion,
    # metadata checks and reads. Direct Agent callers may provide a
    # WorkingState without the optional context_epoch argument.
    evidence_epoch = (
        context_epoch if context_epoch is not None
        else working_state.epoch if working_state is not None else 0
    )
    context_budget = getattr(llm, "context_budget", None)
    profile_key = getattr(llm, "context_profile_key", None)
    counted_removed_context: set[str] = set()
    counted_archived_rounds: set[int] = set()
    plan_completion_retry_used = False

    def emit(kind: AgentEventKind, payload: AgentEventPayload | None = None, *,
             round_index: int | None = None, tool_call_id: str | None = None) -> None:
        if event_sink is None or task_identity is None:
            return
        try:
            event_sink.emit(kind, payload or AgentEventPayload(),
                            round_index=round_index, tool_call_id=tool_call_id)
        except Exception:  # noqa: BLE001
            LOGGER.debug("Agent lifecycle event was dropped", exc_info=True)

    async def record_compaction(fitted) -> None:
        new_removed = set(fitted.removed_context_keys) - counted_removed_context
        new_archived = set(fitted.archived_rounds) - counted_archived_rounds
        if not new_removed and not new_archived:
            return
        counted_removed_context.update(new_removed)
        counted_archived_rounds.update(new_archived)
        if on_context_compaction is not None:
            try:
                await on_context_compaction(len(new_archived), len(new_removed))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("could not record context compaction metrics (%s)", type(exc).__name__)
        emit(AgentEventKind.COMPACTION_COMPLETED, AgentEventPayload(
            completed_count=len(new_archived), total_count=len(new_removed),
        ))

    def fit_context(*args, **kwargs):
        started = False

        def on_compaction_start() -> None:
            nonlocal started
            if started:
                return
            started = True
            emit(AgentEventKind.COMPACTION_STARTED)

        try:
            fitted = compactor.fit(
                *args,
                **kwargs,
                on_compaction_start=on_compaction_start,
            )
        except Exception:
            if started:
                emit(AgentEventKind.COMPACTION_COMPLETED, AgentEventPayload(
                    outcome=ToolOutcome.ERROR,
                ))
            raise
        return fitted

    runtime_tool_extensions = dict(tool_extensions or {})
    # This request-local reader is injected after caller extensions so it cannot
    # be replaced by arbitrary extension data.
    runtime_tool_extensions["agent_evidence_store"] = evidence_store

    async def save_working_state(*, status: str | None = None, **changes) -> None:
        nonlocal working_state
        if working_state is None:
            return
        if working_memory is None:
            working_state = replace(working_state, status=status or working_state.status, **changes)
            return
        try:
            working_state = await working_memory.checkpoint(working_state, status=status, **changes)
        except asyncio.CancelledError:
            raise
        except Exception:
            emit(AgentEventKind.WORKING_STATE_SAVE_FAILED)
            raise
        emit(AgentEventKind.CHECKPOINT_SAVED, AgentEventPayload(
            revision=working_state.checkpoint_revision,
        ))

    if working_state is not None and working_memory is not None:
        try:
            working_state = await working_memory.start(working_state)
        except asyncio.CancelledError:
            raise
        except Exception:
            emit(AgentEventKind.WORKING_STATE_SAVE_FAILED)
            raise
    else:
        emit(AgentEventKind.WORKING_STATE_NOT_CONFIGURED)

    plan_controller: PlanExecutionController | None = None
    planning_scope_valid = (
        working_state is not None
        and (task_identity is None or (
            task_identity.task_id == working_state.task_id
            and task_identity.request_id == working_state.request_id
            and task_identity.group_id == working_state.group_id
            and task_identity.user_id == working_state.user_id
            and task_identity.epoch == working_state.epoch
        ))
    )
    if planning_enabled and working_state is not None and not planning_scope_valid:
        LOGGER.warning("task planning skipped because WorkingState and TaskIdentity scopes differ")
    complexity = (
        assess_task_complexity(working_state.goal)
        if planning_enabled and planning_scope_valid and working_state is not None
        else TaskComplexity.SIMPLE
    )
    if (
        planning_enabled
        and working_state is not None
        and complexity != TaskComplexity.SIMPLE
        and callable(getattr(llm, "chat", None))
        and deadline - loop.time() >= 4.0
    ):
        enabled_plan_tools = TOOL_REGISTRY.names(
            policy_snapshot,
            group_id=working_state.group_id,
            user_id=working_state.user_id,
        )
        if context_guard is not None and not await context_guard():
            raise ContextEpochChanged("context epoch changed before planning model dispatch")
        emit(AgentEventKind.MODEL_REQUEST_STARTED, AgentEventPayload(attempt=1), round_index=1)
        try:
            initial_plan = await generate_task_plan(
                llm,
                task_id=working_state.task_id,
                goal=working_state.goal,
                constraints=working_state.constraints,
                enabled_tools=enabled_plan_tools,
                deadline=deadline,
                loop=loop,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - planning failure falls back to the original loop
            emit(AgentEventKind.MODEL_REQUEST_COMPLETED, AgentEventPayload(
                outcome=ToolOutcome.ERROR, attempt=1,
            ), round_index=1)
            LOGGER.info("task planning was unavailable (%s); using the normal Agent path", type(exc).__name__)
        else:
            emit(AgentEventKind.MODEL_REQUEST_COMPLETED, AgentEventPayload(
                outcome=ToolOutcome.SUCCESS, attempt=1,
            ), round_index=1)
            if context_guard is not None and not await context_guard():
                raise ContextEpochChanged("context epoch changed before plan checkpoint")

            async def persist_plan(plan: TaskPlan) -> None:
                completed = tuple(
                    f"{step.step_id}: {step.description}" for step in plan.steps
                    if step.status == StepStatus.COMPLETED
                )
                pending = tuple(
                    f"{step.step_id}: {step.description} [{step.status.value}]"
                    for step in plan.steps if step.status != StepStatus.COMPLETED
                )
                await save_working_state(task_plan=plan, completed_steps=completed, pending_steps=pending)

            def emit_plan_event(
                event_name: str, plan: TaskPlan, step_id: str | None, round_index: int,
            ) -> None:
                if event_sink is None or task_identity is None:
                    return
                kind = {
                    "step_updated": AgentEventKind.PLAN_STEP_UPDATED,
                    "plan_revised": AgentEventKind.PLAN_REVISED,
                }.get(event_name, AgentEventKind.PLAN_CREATED)
                step = next((row for row in plan.steps if row.step_id == step_id), None)
                try:
                    event_sink.emit(kind, AgentEventPayload(
                        revision=plan.revision,
                        completed_count=sum(row.status == StepStatus.COMPLETED for row in plan.steps),
                        total_count=len(plan.steps),
                        plan_step_id=step_id,
                        plan_step_status=step.status.value if step is not None else None,
                    ), round_index=max(1, round_index))
                    if (
                        step is not None and step.status == StepStatus.COMPLETED
                        and step.tool_call_ids
                    ):
                        public = PublicCommentary(
                            kind=CommentaryKind.UPDATE,
                            text=(f"计划状态：{sum(row.status == StepStatus.COMPLETED for row in plan.steps)} / "
                                  f"{len(plan.steps)} 项已标记完成。"),
                            task_id=task_identity.task_id,
                            round_index=max(1, round_index),
                            related_tool_call_ids=tuple(step.tool_call_ids[-6:]),
                        )
                        event_sink.emit(
                            AgentEventKind.PUBLIC_COMMENTARY,
                            round_index=max(1, round_index),
                            commentary=public,
                        )
                except Exception:  # noqa: BLE001 - progress is optional
                    LOGGER.debug("task plan event was dropped", exc_info=True)

            plan_controller = PlanExecutionController(
                initial_plan,
                allowed_tools=enabled_plan_tools,
                persist=persist_plan,
                emit=emit_plan_event,
                evidence_store=evidence_store,
                request_id=request_id,
                group_id=request_group_id or working_state.group_id,
                user_id=request_user_id or working_state.user_id,
                epoch=evidence_epoch,
            )
            runtime_tool_extensions["agent_plan_controller"] = plan_controller
            await persist_plan(initial_plan)
            emit_plan_event("plan_created", initial_plan, None, 1)
            if commentary_delivery_available(event_sink, task_identity):
                try:
                    public_plan = PublicCommentary(
                        kind=CommentaryKind.PLAN,
                        text=initial_plan.public_summary(),
                        task_id=task_identity.task_id,
                        round_index=1,
                    )
                    event_sink.emit(
                        AgentEventKind.PUBLIC_COMMENTARY,
                        round_index=1,
                        commentary=public_plan,
                    )
                except (TypeError, ValueError):
                    LOGGER.debug("task plan was not safe to publish as progress")
            insert_at = next(
                (index for index, message in enumerate(base_messages) if message.get("role") == "user"),
                len(base_messages),
            )
            base_messages.insert(insert_at, {"role": "system", "content": (
                "An application-managed task plan is included in request-scoped working state. "
                "Treat it as a bounded checklist, not as evidence or permission. Execute only through the available tools. "
                "After inspecting results, use plan_step_assess with same-request evidence references to assess a step. "
                "A successful tool call alone does not complete a step. For a blocked or invalid path, use replan_task "
                "with the current revision; preserve the original goal and constraints. Never claim a pending step is done. "
                "At most two replans are accepted. If requirements remain unresolved, explain that in the final answer."
            )})
            context_plan = tuple(
                replace(item, message_index=item.message_index + (1 if item.message_index >= insert_at else 0))
                for item in context_plan
            )

    allowed_names = agent_tool_names(
        policy_snapshot,
        group_id=budget_scope.group_id or "" if budget_scope is not None else "",
        user_id=budget_scope.user_id or "" if budget_scope is not None else "",
        task_identity=task_identity,
        event_sink=event_sink,
        planning_enabled=plan_controller is not None,
    )

    for _round in range(max_rounds):
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        if plan_controller is not None:
            plan_controller.begin_round(_round + 1)
        if context_guard is not None and not await context_guard():
            raise ContextEpochChanged("context epoch changed before Agent model dispatch")
        schemas = agent_tool_schemas(
            policy_snapshot,
            group_id=budget_scope.group_id or "" if budget_scope is not None else "",
            user_id=budget_scope.user_id or "" if budget_scope is not None else "",
            task_identity=task_identity,
            event_sink=event_sink,
            planning_enabled=plan_controller is not None,
        )
        model_started = False
        model_completed = False
        try:
            fitted = fit_context(
                base_messages, context_plan, protocol_blocks,
                model=str(getattr(llm, "model", "unknown")),
                tool_schemas=schemas,
                budget=context_budget,
                profile_key=profile_key,
                working_state=_working_state_prompt_data(working_state) if working_state is not None else None,
            )
            await record_compaction(fitted)
            messages[:] = [dict(message) for message in fitted.messages]
            emit(AgentEventKind.MODEL_REQUEST_STARTED, AgentEventPayload(
                attempt=_round + 1,
            ), round_index=_round + 1)
            model_started = True
            response = await asyncio.wait_for(
                _safe_chat_with_tools(
                    llm,
                    messages,
                    tools=schemas,
                    temperature=min(temperature, 0.5),
                    max_tokens=fitted.max_tokens,
                    presence_penalty=presence_penalty,
                    frequency_penalty=frequency_penalty,
                ),
                timeout=remaining,
            )
            emit(AgentEventKind.MODEL_REQUEST_COMPLETED, AgentEventPayload(
                outcome=ToolOutcome.SUCCESS, attempt=_round + 1,
            ), round_index=_round + 1)
            model_completed = True
        except ContextWindowError:
            if working_state is not None and working_memory is not None and working_state.status == "running":
                await save_working_state(status="failed")
            raise
        except Exception as exc:  # noqa: BLE001
            if model_started and not model_completed:
                emit(AgentEventKind.MODEL_REQUEST_COMPLETED, AgentEventPayload(
                    outcome=ToolOutcome.ERROR, attempt=_round + 1,
                ), round_index=_round + 1)
            LOGGER.warning("agent planning round failed: %s", exc)
            break

        if response.text and not response.wants_tools:
            dsml_calls = _parse_dsml_tool_calls(response.text)
            if dsml_calls:
                response = ChatWithToolsResponse(text=None, tool_calls=dsml_calls)

        if response.text:
            last_text = response.text

        if not response.wants_tools:
            # A model final is not task completion while a structured plan remains open.
            if plan_controller is not None:
                completion = plan_controller.completion_snapshot()
                if not completion["complete"]:
                    if not plan_completion_retry_used and _round + 1 < max_rounds and deadline - loop.time() > 1.0:
                        plan_completion_retry_used = True
                        base_messages.append({
                            "role": "user",
                            "content": (
                                "Application-managed plan completion check (state data, not permission): "
                                + json.dumps(completion, ensure_ascii=False, separators=(",", ":"))
                                + " The plan is not complete. Continue any feasible remaining steps using the enabled tools; "
                                "if an attempt is blocked, call replan_task within its limit. If it remains impossible, "
                                "give an explicitly partial answer and do not claim the original task is complete."
                            ),
                        })
                        continue
                    last_text = _annotate_incomplete_plan(last_text, completion)
            if executed_calls:
                # This response was generated with the tool observations in the
                # native conversation, so it is already the agent's synthesis.
                # Re-prompting here adds latency and can make a second answer
                # ignore evidence that the first answer used correctly.
                await save_working_state(status="completed")
                return _append_references(_validate_research_answer(last_text, messages), references)
            await save_working_state(status="completed")
            return last_text

        if context_guard is not None and not await context_guard():
            raise ContextEpochChanged("context epoch changed before Agent tool execution")

        LOGGER.info("agent round %d requested tools: %s", _round + 1, [tc.name for tc in response.tool_calls])

        # ── Build one indivisible assistant-call / tool-result block ─────
        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": response.text,  # may be None — that's fine per spec
            "tool_calls": [_tool_call_message(tc) for tc in response.tool_calls],
        }
        # Preserve provider-specific tool-call extensions when present.
        if response.extra_content:
            assistant_message["extra_content"] = response.extra_content

        # ── Execute all requested tools (concurrently) ───────────────────
        valid_calls = []
        skipped: dict[str, str] = {}
        commentary_seen_this_round = False
        for tc in response.tool_calls:
            signature = _tool_signature(tc)
            if tc.name not in allowed_names:
                skipped[tc.id] = f"未知工具：{tc.name}。"
            elif tc.name == PUBLIC_COMMENTARY_TOOL and commentary_seen_this_round:
                skipped[tc.id] = "本轮公开说明数量已达到上限，请继续执行实际任务。"
            elif signature in seen_calls:
                skipped[tc.id] = "该工具调用与之前重复，已跳过。请使用已有结果。"
            elif len(valid_calls) >= max_calls_per_round:
                skipped[tc.id] = "本轮工具并发数量已达到上限，已跳过。"
            elif executed_calls + len(valid_calls) >= max_total_calls:
                skipped[tc.id] = "本次任务的工具调用总预算已用完，已跳过。"
            else:
                seen_calls.add(signature)
                valid_calls.append(tc)
                if tc.name == PUBLIC_COMMENTARY_TOOL:
                    commentary_seen_this_round = True
        for tc in response.tool_calls:
            if tc.id not in skipped:
                continue
            spec = TOOL_REGISTRY.get(tc.name)
            category = spec.progress_category if spec is not None else ToolCategory.OTHER
            emit(AgentEventKind.TOOL_REJECTED, AgentEventPayload(
                category=category, outcome=ToolOutcome.REJECTED, execution_started=False,
            ), round_index=_round + 1, tool_call_id=tc.id)
        # This workflow returns source code and performs several bounded model
        # calls. Run it by itself so the response cap preserves its full source;
        # skipped calls still receive one result per tool_call_id and can be
        # retried by the Agent in a later round.
        generation_call = next(
            (call for call in valid_calls if call.name == "sfm_generate_validate"),
            None,
        )
        if generation_call is not None:
            for call in valid_calls:
                if call.id != generation_call.id:
                    skipped[call.id] = (
                        "SFM 生成与验证工作流单独运行；请在下一轮提出其他工具调用。"
                    )
                    # The call was deferred, not executed or deduplicated. Let
                    # the model retry it after receiving the SFM result.
                    seen_calls.discard(_tool_signature(call))
            valid_calls = [generation_call]
        remaining = deadline - loop.time()
        if valid_calls and remaining <= 0:
            raise TimeoutError("Agent deadline expired before tool execution")
        per_result_chars = (
            max(400, results_max_chars)
            if generation_call is not None
            else max(400, results_max_chars // max(len(valid_calls), 1))
        )
        control_names = {PUBLIC_COMMENTARY_TOOL, "plan_step_assess", "replan_task"}
        control_calls = [call for call in valid_calls if call.name in control_names]
        business_calls = [call for call in valid_calls if call.name not in control_names]
        tool_results_by_id: dict[str, ToolResult | BaseException] = {}
        # Publish the public plan before starting same-round work. This preserves
        # the model's tool_call protocol order later while keeping actual domain
        # tools concurrent with one another.
        for call in control_calls:
            try:
                tool_results_by_id[call.id] = await _dispatch_registered_tool_call(
                    llm, call, deadline=deadline, tool_timeout=tool_timeout,
                    result_max_chars=per_result_chars, policy_snapshot=policy_snapshot,
                    local_knowledge_search=local_knowledge_search,
                    tool_extensions=runtime_tool_extensions, context_epoch=evidence_epoch,
                    request_id=request_id, task_identity=task_identity, event_sink=event_sink,
                    round_index=_round + 1, tool_call_id=call.id,
                    verified_tool_outcomes=verified_tool_outcomes,
                )
            except asyncio.CancelledError:
                if working_state is not None and working_memory is not None and working_state.status == "running":
                    try:
                        await asyncio.shield(save_working_state(status="cancelled"))
                    except Exception:  # noqa: BLE001
                        LOGGER.warning("could not persist cancellation checkpoint")
                raise
            except Exception as exc:  # noqa: BLE001 - a commentary failure must not block work
                tool_results_by_id[call.id] = exc

        tool_tasks = [
            asyncio.create_task(
                _dispatch_registered_tool_call(
                    llm,
                    tc,
                    deadline=deadline,
                    tool_timeout=tool_timeout,
                    result_max_chars=per_result_chars,
                    policy_snapshot=policy_snapshot,
                    local_knowledge_search=local_knowledge_search,
                    tool_extensions=runtime_tool_extensions,
                    context_epoch=evidence_epoch,
                    request_id=request_id,
                    task_identity=task_identity,
                    event_sink=event_sink,
                    round_index=_round + 1,
                    tool_call_id=tc.id,
                    verified_tool_outcomes=verified_tool_outcomes,
                ),
                name=f"agent-tool-{index}",
            )
            for index, tc in enumerate(business_calls)
        ]
        business_results = []
        try:
            if tool_tasks:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError("Agent deadline expired while scheduling tools")
                _done, pending = await asyncio.wait(tool_tasks, timeout=remaining)
                if pending:
                    raise TimeoutError("Agent deadline expired while tools were running")
                business_results = await asyncio.gather(*tool_tasks, return_exceptions=True)
                if any(isinstance(result, TimeoutError) for result in business_results):
                    raise TimeoutError("Agent deadline expired while tools were running")
                tool_results_by_id.update(
                    (call.id, result) for call, result in zip(business_calls, business_results)
                )
        except (asyncio.CancelledError, TimeoutError):
            for task in tool_tasks:
                if not task.done():
                    task.cancel()
            if tool_tasks:
                await asyncio.gather(*tool_tasks, return_exceptions=True)
            if working_state is not None and working_memory is not None and working_state.status == "running":
                try:
                    await asyncio.shield(save_working_state(status="cancelled"))
                except Exception:  # noqa: BLE001
                    LOGGER.warning("could not persist cancellation checkpoint")
            raise
        executed_calls += len(valid_calls)

        observations: dict[str, str] = {}
        evidence_refs: list[str] = []
        evidence_by_call: dict[str, tuple[str, ...]] = {}
        evidence_metadata: list[dict[str, Any]] = []
        result_statuses: dict[str, str] = {}
        results = [
            tool_results_by_id.get(
                call.id,
                ToolResult(ToolStatus.ERROR, "Tool result unavailable."),
            )
            for call in valid_calls
        ]
        for tc, result in zip(valid_calls, results):
            result_refs: tuple[str, ...] = ()
            if isinstance(result, BaseException):
                content = f"工具 {tc.name} 执行失败。"
                result_statuses[tc.id] = "error"
                LOGGER.warning("tool %s failed (%s)", tc.name, type(result).__name__)
            else:
                content = _format_tool_observation(result, per_result_chars)
                result_statuses[tc.id] = result.status.value
                spec = TOOL_REGISTRY.get(tc.name)
                if spec is not None and spec.progress_category != ToolCategory.COMMENTARY:
                    verified_tool_outcomes[tc.id] = result.status.value
                result_refs = evidence_store.add_result(
                    result,
                    request_id=request_id,
                    group_id=request_group_id,
                    user_id=request_user_id,
                    epoch=evidence_epoch,
                    tool_call_id=tc.id,
                )
                evidence_by_call[tc.id] = result_refs
                evidence_refs.extend(result_refs)
                for ref in result_refs:
                    metadata = evidence_store.metadata_for(
                        ref, request_id=request_id, group_id=request_group_id,
                        user_id=request_user_id, epoch=evidence_epoch,
                    )
                    if metadata is not None:
                        evidence_metadata.append(metadata)
                content = _append_evidence_refs(content, result_refs, per_result_chars)
                for item in result.evidence:
                    is_document = item.kind == "document" and bool(item.locator)
                    is_web_source = item.locator.startswith(("http://", "https://"))
                    is_recipe_source = item.kind == "recipe" and bool(item.locator)
                    if is_document or is_web_source or (
                        item.kind == "repository_file" and item.locator.startswith("git://")
                    ) or is_recipe_source:
                        if tc.name == "sfm_generate_validate" and is_document:
                            # Preserve every retrieved SFM source in the final
                            # citations even when earlier tools already added
                            # references. SFM retrieval is bounded to 8 items.
                            priority = -1
                        elif is_document or item.kind in {"repository_file", "web_page"}:
                            priority = 0
                        elif is_recipe_source:
                            priority = 0
                        elif item.kind == "repository":
                            priority = 1
                        else:
                            priority = 2
                        reference = (priority, item.title, item.locator)
                        if reference not in references:
                            references.append(reference)
            spec = TOOL_REGISTRY.get(tc.name)
            if (
                plan_controller is not None
                and spec is not None
                and spec.progress_category != ToolCategory.COMMENTARY
            ):
                await plan_controller.observe_tool_result(
                    tc.id, tc.name, result, result_refs,
                    round_index=_round + 1,
                )
            observations[tc.id] = content

        # DeepSeek requires exactly one tool response for every tool_call id.
        # This includes unknown, duplicate and budget-skipped calls.
        for tc in response.tool_calls:
            if tc.id in skipped:
                observations[tc.id] = skipped[tc.id]
                result_statuses[tc.id] = "skipped"

        block = ProtocolBlock(
            round_index=_round + 1,
            messages=(
                assistant_message,
                *(
                    {"role": "tool", "tool_call_id": tc.id,
                     "content": observations.get(tc.id, "Tool result unavailable.")}
                    for tc in response.tool_calls
                ),
            ),
            evidence_refs=tuple(dict.fromkeys(evidence_refs)),
            result_statuses=tuple(result_statuses.items()),
            evidence_by_call=tuple((call_id, refs) for call_id, refs in evidence_by_call.items()),
            evidence_metadata=tuple(evidence_metadata),
        )
        compactor.validate_protocol_block(block)
        protocol_blocks.append(block)

        if working_state is not None:
            step_updates = tuple(
                f"{tc.name}: {result_statuses.get(tc.id, 'unknown')}"
                for tc in response.tool_calls
                if (TOOL_REGISTRY.get(tc.name) is None or
                    TOOL_REGISTRY.get(tc.name).progress_category != ToolCategory.COMMENTARY)
            )
            failures = tuple(
                f"{tc.name}: {result_statuses.get(tc.id, 'unknown')}"
                for tc in response.tool_calls
                if result_statuses.get(tc.id) not in {"ok", "no_results"}
                and (TOOL_REGISTRY.get(tc.name) is None or
                     TOOL_REGISTRY.get(tc.name).progress_category != ToolCategory.COMMENTARY)
            )
            if plan_controller is None:
                completed = tuple((*working_state.completed_steps, *step_updates)[-32:])
                pending = working_state.pending_steps
            else:
                completed = tuple(
                    f"{step.step_id}: {step.description}" for step in plan_controller.plan.steps
                    if step.status == StepStatus.COMPLETED
                )
                pending = tuple(
                    f"{step.step_id}: {step.description} [{step.status.value}]"
                    for step in plan_controller.plan.steps if step.status != StepStatus.COMPLETED
                )
            await save_working_state(
                completed_steps=completed,
                pending_steps=pending,
                open_questions=tuple((*working_state.open_questions, *failures)[-32:]),
                evidence_refs=tuple(dict.fromkeys((*working_state.evidence_refs, *evidence_refs)))[-64:],
            )

        if not valid_calls:
            break

    # Max rounds reached — ask for a final answer without offering tools
    LOGGER.debug("agent_loop hit max_rounds=%d, requesting final answer", max_rounds)
    if context_guard is not None and not await context_guard():
        raise ContextEpochChanged("context epoch changed before Agent final synthesis")
    model_started = False
    model_completed = False
    try:
        final_fit = fit_context(
            base_messages, context_plan, protocol_blocks,
            model=str(getattr(llm, "model", "unknown")),
            tool_schemas=None,
            budget=context_budget,
            profile_key=profile_key,
            working_state=_working_state_prompt_data(working_state) if working_state is not None else None,
        )
        await record_compaction(final_fit)
        messages[:] = [dict(message) for message in final_fit.messages]
        completion = plan_controller.completion_snapshot() if plan_controller is not None else None
        completion_note = (
            "Application completion check: the structured task plan is not complete. "
            "Clearly state that the answer is partial and do not claim unresolved requirements were satisfied."
            if completion is not None and not completion["complete"] else None
        )
        final_messages = _build_final_answer_messages(
            messages, results_max_chars * 2, completion_note=completion_note,
        )
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError("Agent deadline expired before final response")
        final_timeout = min(15.0, remaining)
        emit(AgentEventKind.MODEL_REQUEST_STARTED, AgentEventPayload(
            attempt=max_rounds + 1,
        ), round_index=max_rounds + 1)
        model_started = True
        final = await asyncio.wait_for(
            _safe_chat(
                llm,
                final_messages,
                temperature=temperature,
                max_tokens=final_fit.max_tokens,
                presence_penalty=presence_penalty,
                frequency_penalty=frequency_penalty,
            ),
            timeout=final_timeout,
        )
        emit(AgentEventKind.MODEL_REQUEST_COMPLETED, AgentEventPayload(
            outcome=ToolOutcome.SUCCESS, attempt=max_rounds + 1,
        ), round_index=max_rounds + 1)
        model_completed = True
        candidate = str(final).strip()
        if completion is not None and not completion["complete"]:
            candidate = _annotate_incomplete_plan(candidate, completion)
        await save_working_state(status="completed")
        if executed_calls:
            return _append_references(_validate_research_answer(candidate, messages), references)
        return candidate
    except ContextWindowError:
        if working_state is not None and working_memory is not None and working_state.status == "running":
            await save_working_state(status="failed")
        raise
    except Exception as exc:  # noqa: BLE001
        if model_started and not model_completed:
            emit(AgentEventKind.MODEL_REQUEST_COMPLETED, AgentEventPayload(
                outcome=ToolOutcome.ERROR, attempt=max_rounds + 1,
            ), round_index=max_rounds + 1)
        if working_state is not None and working_memory is not None and working_state.status == "running":
            try:
                await save_working_state(status="failed")
            except Exception:  # noqa: BLE001
                LOGGER.warning("could not persist failed task checkpoint")
        return last_text or f"（工具执行完毕，但生成最终回复失败: {exc}）"


async def _dispatch_registered_tool_call(
    llm: Any,
    call: ToolCallRequest,
    *,
    deadline: float,
    tool_timeout: float,
    result_max_chars: int,
    policy_snapshot: PolicySnapshot | None = None,
    local_knowledge_search=None,
    tool_extensions: Mapping[str, Any] | None = None,
    context_epoch: int | None = None,
    request_id: str | None = None,
    task_identity: TaskIdentity | None = None,
    event_sink: EventSink | None = None,
    round_index: int | None = None,
    tool_call_id: str | None = None,
    verified_tool_outcomes: Mapping[str, str] | None = None,
) -> ToolResult:
    budget_scope = current_budget_scope()
    tool_request_id = budget_scope.request_id if budget_scope is not None else f"agent-{call.id}"
    context = ToolContext(
        request_id=tool_request_id,
        group_id=budget_scope.group_id or "" if budget_scope is not None else "",
        user_id=budget_scope.user_id or "" if budget_scope is not None else "",
        context_epoch=context_epoch,
        deadline_monotonic=deadline,
        cancel_scope=asyncio.current_task(),
        policy_snapshot=policy_snapshot or PolicySnapshot(roles=frozenset({"group_member"})),
        budget_scope=budget_scope,
        budget_controller=getattr(llm, "budget_controller", None),
        evidence_limit=8,
        tool_timeout_seconds=tool_timeout,
        result_max_chars=result_max_chars,
        task_identity=task_identity,
        event_sink=event_sink,
        tool_call_id=tool_call_id or call.id,
        agent_round=round_index,
        extensions={
            "local_knowledge_search": local_knowledge_search,
            **(dict(tool_extensions) if tool_extensions is not None else {}),
            "agent_evidence_request_id": request_id,
            "agent_tool_outcomes": dict(verified_tool_outcomes or {}),
        },
    )
    return await TOOL_REGISTRY.dispatch(call.name, call.arguments, context)


def _tool_signature(tc: ToolCallRequest) -> tuple[str, str]:
    """Collapse cosmetically different calls so the model cannot loop on them."""
    try:
        payload = json.loads(tc.arguments)
    except json.JSONDecodeError:
        payload = tc.arguments

    def normalize(value: Any) -> Any:
        if isinstance(value, str):
            return re.sub(r"\s+", " ", value).strip().casefold()
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {key: normalize(value[key]) for key in sorted(value)}
        return value

    return tc.name, json.dumps(normalize(payload), ensure_ascii=False, sort_keys=True)


def _working_state_prompt_data(state: WorkingState) -> dict[str, Any]:
    """Project only task continuity fields; storage identifiers are not prompt data."""
    return {
        "goal": state.goal,
        "constraints": list(state.constraints),
        "completed_steps": list(state.completed_steps),
        "pending_steps": list(state.pending_steps),
        "evidence_refs": list(state.evidence_refs),
        "assumptions": list(state.assumptions),
        "open_questions": list(state.open_questions),
        "status": state.status,
        "checkpoint_revision": state.checkpoint_revision,
        "task_plan": state.task_plan.as_prompt_data() if state.task_plan is not None else None,
    }


def _tool_call_message(tc: ToolCallRequest) -> dict[str, Any]:
    message = {
        "id": tc.id,
        "type": "function",
        "function": {"name": tc.name, "arguments": tc.arguments},
    }
    if tc.extra_content:
        message["extra_content"] = tc.extra_content
    return message


def _format_tool_result(results: list[SearchResult], max_chars: int) -> str:
    """Format a list of SearchResult into a compact string for the tool message."""
    return format_search_results(results, max_chars)


def _format_tool_observation(result: ToolResult, max_chars: int) -> str:
    """Format status and content without discarding useful partial evidence."""
    legacy_results = tool_result_to_search_results(result)
    if isinstance(result.content, dict) and isinstance(result.content.get("text"), str):
        detail = result.content["text"].strip()
    elif legacy_results or result.status == ToolStatus.NO_RESULTS:
        detail = _format_tool_result(legacy_results, max_chars=max_chars)
    else:
        detail = result.content if isinstance(result.content, str) else json.dumps(
            result.content, ensure_ascii=False
        )

    if result.error is not None:
        status = f"[{result.status.value}] {result.error.message}"
        detail = f"{status}\n{detail}" if detail and detail != result.error.message else status
    if len(detail) > max_chars:
        return detail[:max_chars].rstrip() + "..."
    return detail


def _append_evidence_refs(content: str, refs: tuple[str, ...], max_chars: int) -> str:
    if not refs or max_chars < 160:
        return content[:max_chars]
    header = "Evidence ref IDs (untrusted; page with task_evidence_read):\n"
    marker = "\n[… output shortened; request a narrower authorized result]"
    selected: list[str] = []
    for ref in refs[:8]:
        candidate = header + "\n".join((*selected, ref))
        if len(candidate) + len(marker) + 2 > max_chars:
            break
        selected.append(ref)
    if not selected:
        return content[:max_chars]
    notice = header + "\n".join(selected)
    if len(content) + 1 + len(notice) <= max_chars:
        return content + "\n" + notice
    content_limit = max(0, max_chars - len(marker) - 1 - len(notice))
    return content[:content_limit].rstrip() + marker + "\n" + notice


def _append_references(answer: str, references: list[tuple[int, str, str]], limit: int = 4) -> str:
    sfm_unique: list[tuple[str, str]] = []
    regular_unique: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    for priority, title, url in sorted(references, key=lambda item: item[0]):
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        item = (re.sub(r"\s+", " ", title).strip(), url)
        if priority < 0:
            sfm_unique.append(item)
        elif len(regular_unique) < limit:
            regular_unique.append(item)
    # SFM source citations are part of the generated program's provenance.
    # Keep all bounded (max 8) SFM references and use the normal cap for other
    # sources only when room remains.
    unique = sfm_unique + regular_unique[: max(0, limit - len(sfm_unique))]
    if not unique:
        return answer
    lines = [answer.rstrip(), "", "参考："]
    lines.extend(f"{idx}. {title} {url}" for idx, (title, url) in enumerate(unique, start=1))
    return "\n".join(lines)


def _parse_dsml_tool_calls(text: str) -> list[ToolCallRequest]:
    """Recover tool calls that DeepSeek occasionally emits as DSML content."""
    if "DSML" not in text or "invoke" not in text:
        return []
    calls: list[ToolCallRequest] = []
    invoke_pattern = re.compile(
        r'<[^>]*invoke\s+name="(?P<name>[^"]+)"[^>]*>(?P<body>.*?)</[^>]*invoke>',
        re.DOTALL,
    )
    parameter_pattern = re.compile(
        r'<[^>]*parameter\s+name="(?P<name>[^"]+)"[^>]*>(?P<value>.*?)</[^>]*parameter>',
        re.DOTALL,
    )
    for idx, invoke in enumerate(invoke_pattern.finditer(text)):
        args = {
            match.group("name"): re.sub(r"<[^>]+>", "", match.group("value")).strip()
            for match in parameter_pattern.finditer(invoke.group("body"))
        }
        # Recover arrays encoded as JSON strings when possible.
        for key, value in list(args.items()):
            if value.startswith("[") or value.startswith("{"):
                try:
                    args[key] = json.loads(value)
                except json.JSONDecodeError:
                    pass
        calls.append(
            ToolCallRequest(
                id=f"dsml-{idx}-{abs(hash(invoke.group(0))) % 1_000_000}",
                name=invoke.group("name"),
                arguments=json.dumps(args, ensure_ascii=False),
            )
        )
    return calls


def _build_final_answer_messages(
    messages: list[dict[str, Any]],
    max_evidence_chars: int,
    *,
    completion_note: str | None = None,
) -> list[dict[str, str]]:
    """Flatten research into a clean prompt so the final call cannot continue the tool protocol."""
    system_parts = [
        str(message.get("content") or "")
        for message in messages
        if message.get("role") == "system"
    ]
    user_parts = [
        str(message.get("content") or "")
        for message in messages
        if message.get("role") == "user"
    ]
    evidence_parts = [
        str(message.get("content") or "")
        for message in messages
        if message.get("role") == "tool"
    ]
    evidence = "\n\n".join(evidence_parts)
    if len(evidence) > max_evidence_chars:
        evidence = evidence[-max_evidence_chars:]
    return [
        {
            "role": "system",
            "content": (
                "\n".join(system_parts[:3])
                + "\nResearch is finished. Give the final plain-text answer now. "
                "Do not call, request, simulate, or mention tools. "
                "Use evidence when available; otherwise give a useful best-effort answer and mark uncertainty. "
                "For recipes, verify the exact target output/item id and ingredient relationship. Never convert a similarly named item, input, quest mention, reward, dependency, or unrelated pattern into the requested recipe. "
                "Copy identifiers exactly. In FTB data, tasks are requirements and rewards are received only after completion. "
                + (completion_note or "")
            ),
        },
        {
            "role": "user",
            "content": (
                f"Original request:\n{user_parts[-1] if user_parts else ''}\n\n"
                f"Collected research evidence:\n{evidence}\n\n"
                "Answer the original request directly. Do not output a progress update or promise more research."
            ),
        },
    ]


def _annotate_incomplete_plan(answer: str, completion: Mapping[str, Any]) -> str:
    total = int(completion.get("total_count", 0))
    complete = int(completion.get("completed_count", 0))
    remaining = max(0, total - complete)
    note = (
        f"任务未完全完成：计划中 {complete}/{total} 项步骤有完成记录，"
        f"另有 {remaining} 项未完成或未验证。以下仅为当前部分结果。"
    )
    cleaned = str(answer or "").strip()
    if cleaned.startswith("任务未完全完成："):
        return cleaned
    return f"{note}\n\n{cleaned}" if cleaned else note


async def _generate_final_answer(
    llm: DeepSeekClient,
    messages: list[dict[str, Any]],
    max_evidence_chars: int,
    deadline: float,
    temperature: float = 0.6,
    presence_penalty: float = 0.3,
    frequency_penalty: float = 0.2,
) -> str:
    final_messages = _build_final_answer_messages(messages, max_evidence_chars)
    timeout = max(3.0, min(18.0, deadline - asyncio.get_running_loop().time()))
    try:
        return str(
            await asyncio.wait_for(
                _safe_chat(
                    llm,
                    final_messages,
                    temperature=temperature,
                    presence_penalty=presence_penalty,
                    frequency_penalty=frequency_penalty,
                ),
                timeout=timeout,
            )
        ).strip()
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("final research answer generation failed: %s", exc)
        return "现有检索已结束，但没有生成完整答案。请稍后重试。"


async def _verify_research_answer(
    llm: DeepSeekClient,
    messages: list[dict[str, Any]],
    candidate: str,
    max_evidence_chars: int,
    deadline: float,
) -> str:
    """Evidence-check a researched answer in a separate, tool-free model call."""
    tool_evidence = "\n\n".join(
        str(message.get("content") or "")
        for message in messages
        if message.get("role") == "tool"
    )
    if len(tool_evidence) > max_evidence_chars:
        tool_evidence = tool_evidence[-max_evidence_chars:]
    original = next(
        (
            str(message.get("content") or "")
            for message in reversed(messages)
            if message.get("role") == "user"
        ),
        "",
    )
    verify_messages = [
        {
            "role": "system",
            "content": (
                "You are the final evidence editor. Return only a corrected, useful, plain Chinese answer. "
                "Check every exact recipe, quantity, identifier, date, causal direction and source claim against the evidence. "
                "Remove or explicitly qualify anything not directly supported. Do not replace unsupported details with new guesses. "
                "Preserve identifiers character-for-character. For FTB data: tasks are requirements, rewards come afterward, dependencies are prerequisites. "
                "A similarly named item or a target used as an ingredient is not the requested output recipe. "
                "When exact details remain unverified, provide the verified progression and practical in-game verification steps instead of refusing the whole answer."
                " This is the last call: never output a plan, progress update, future action, 'let me check', 'I will read/search', or ask the user to wait. "
                "The answer must be complete enough to send immediately."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Original request:\n{original}\n\n"
                f"Evidence:\n{tool_evidence}\n\n"
                f"Candidate answer to audit:\n{candidate}"
            ),
        },
    ]
    timeout = max(3.0, min(18.0, deadline - asyncio.get_running_loop().time()))
    try:
        return str(
            await asyncio.wait_for(
                llm.chat(verify_messages, temperature=0.05),
                timeout=timeout,
            )
        ).strip()
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("research answer verification failed: %s", exc)
        return candidate


def _validate_research_answer(answer: str, messages: list[dict[str, Any]]) -> str:
    incomplete_patterns = (
        r"让我(?:再|重新|继续)?(?:查看|读取|搜索|查询|检查)",
        r"我(?:再|将|会|需要)(?:查看|读取|搜索|查询|检查)",
        r"请稍等|稍等一下|正在(?:搜索|查询|读取|检查)",
        r"内容被截断.*(?:读取|查看)",
    )
    incomplete = any(re.search(pattern, answer, re.IGNORECASE) for pattern in incomplete_patterns)
    evidence = "\n".join(
        str(message.get("content") or "")
        for message in messages
        if message.get("role") == "tool"
    ).lower()
    answer_ids = set(re.findall(r"\b[a-z0-9_]+:[a-z0-9_./-]+\b", answer.lower()))
    invented_ids = {item_id for item_id in answer_ids if item_id not in evidence}
    reversed_task_relation = (
        "关系检测：目标作为任务要求" in evidence
        and bool(
            re.search(
                r"(?:完成|做完|通过).{0,30}任务.{0,30}(?:奖励|获得|给你).{0,50}(?:atm.?星|allthetweaks:atm_star)",
                answer,
                re.IGNORECASE | re.DOTALL,
            )
        )
    )
    denies_known_item = (
        "allthetweaks:atm_star" in evidence
        and bool(re.search(r"没有.{0,20}(?:ATM.?星|ATM之星).{0,20}物品|不存在.{0,20}(?:ATM.?星|ATM之星)", answer, re.IGNORECASE))
    )
    if not incomplete and not invented_ids and not reversed_task_relation and not denies_known_item:
        return answer
    return (
        "这轮检索没有拿到足以核实精确配方的完整证据，所以我不直接列材料，避免把相似物品或任务奖励当成配方。\n\n"
        "可以先在 JEI/EMI 搜索物品 ID allthetweaks:atm_star，悬停后按 R 查看当前整合包版本的制作方式；"
        "再打开 FTB 任务书的 ATM Star 章节，按前置任务准备各路线材料。ATM10 更新可能调整配方，游戏内 JEI/EMI 和任务书比旧攻略可靠。\n\n"
        "如果把 JEI 配方页截图或具体 ATM10 版本号发出来，我可以继续拆解每项材料的获取路线。"
    )

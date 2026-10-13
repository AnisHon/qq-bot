from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import re
from typing import Any, Mapping

from app.llm import ChatWithToolsResponse, DeepSeekClient, ToolCallRequest
from app.models import SearchResult
from app.tools.contracts import (
    PolicySnapshot,
    ToolContext,
    ToolResult,
    ToolStatus,
    format_search_results,
    tool_result_to_search_results,
)
from app.tools.legacy_tools import build_legacy_tool_specs
from app.tools.registry import ToolRegistry
from app.usage_budget import current_budget_scope


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
                "Search the public web for current information, facts, recent events, or any topic. "
                "Use this for general questions about the world, technology, people, or anything that "
                "may have changed after the model's training cutoff."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query in Chinese or English.",
                    }
                },
                "required": ["query"],
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
                        "description": "News topic, keyword, or entity to search for.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "multi_search",
            "description": (
                "Run up to six differently phrased web searches concurrently. Use when one search is weak, "
                "for niche topics, translated names, aliases, technical guides, recipes, or research tasks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 6,
                    }
                },
                "required": ["queries"],
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
            "name": "fetch_url",
            "description": (
                "Read the text content of a specific webpage given its URL. "
                "Use this to get full details from a URL found in search results, "
                "or when the user shares or asks about a specific URL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full http or https URL to fetch.",
                    }
                },
                "required": ["url"],
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

def _build_legacy_registry() -> ToolRegistry:
    return ToolRegistry(build_legacy_tool_specs(_LEGACY_TOOL_DEFINITIONS), frozen=True)


TOOL_REGISTRY = _build_legacy_registry()
# Compatibility facade: this value is now generated from registered specs.
TOOL_DEFINITIONS = TOOL_REGISTRY.openai_schemas()
TOOL_NAMES = TOOL_REGISTRY.names()


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
    on_tool_call=None,
    local_knowledge_search=None,
    tool_extensions: Mapping[str, Any] | None = None,
    deadline: float | None = None,
    policy_snapshot: PolicySnapshot | None = None,
) -> str:
    """Agentic tool-use loop using DeepSeek native function calling.

    Appends assistant / tool messages to `messages` in place as the loop runs.
    Returns the model's final plain-text answer.

    on_tool_call: optional async callable() fired once when the model first
    requests a tool (caller can send a "querying…" notice before executing).
    """
    last_text: str = ""
    seen_calls: set[tuple[str, str]] = set()
    executed_calls = 0
    references: list[tuple[int, str, str]] = []
    progress_stage = ""
    progress_count = 0
    loop = asyncio.get_running_loop()
    deadline = deadline if deadline is not None else loop.time() + total_timeout
    if policy_snapshot is None:
        # Backward-compatible direct callers represent the existing public
        # group-member Agent path. Application callers pass an event snapshot.
        policy_snapshot = PolicySnapshot(roles=frozenset({"group_member"}))
    budget_scope = current_budget_scope()
    allowed_names = TOOL_REGISTRY.names(
        policy_snapshot,
        group_id=budget_scope.group_id or "" if budget_scope is not None else "",
        user_id=budget_scope.user_id or "" if budget_scope is not None else "",
    )

    for _round in range(max_rounds):
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            response = await asyncio.wait_for(
                _safe_chat_with_tools(
                    llm,
                    messages,
                    tools=TOOL_REGISTRY.openai_schemas(
                        policy_snapshot,
                        group_id=budget_scope.group_id or "" if budget_scope is not None else "",
                        user_id=budget_scope.user_id or "" if budget_scope is not None else "",
                    ),
                    temperature=min(temperature, 0.5),
                    presence_penalty=presence_penalty,
                    frequency_penalty=frequency_penalty,
                ),
                timeout=remaining,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("agent planning round failed: %s", exc)
            break

        if response.text and not response.wants_tools:
            dsml_calls = _parse_dsml_tool_calls(response.text)
            if dsml_calls:
                response = ChatWithToolsResponse(text=None, tool_calls=dsml_calls)

        if response.text:
            last_text = response.text

        if not response.wants_tools:
            # Model produced a final answer — done.
            if executed_calls:
                # This response was generated with the tool observations in the
                # native conversation, so it is already the agent's synthesis.
                # Re-prompting here adds latency and can make a second answer
                # ignore evidence that the first answer used correctly.
                return _append_references(_validate_research_answer(last_text, messages), references)
            return last_text

        LOGGER.info("agent round %d requested tools: %s", _round + 1, [tc.name for tc in response.tool_calls])

        # ── Send concise operational progress when the research stage changes ──
        stage, progress_text = _tool_progress(response.tool_calls)
        if on_tool_call is not None and stage != progress_stage and progress_count < 4:
            progress_stage = stage
            progress_count += 1
            try:
                callback = on_tool_call(progress_text)
            except TypeError:
                callback = on_tool_call()
            try:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError("Agent deadline expired before progress notification")
                await asyncio.wait_for(callback, timeout=remaining)
            except TimeoutError:
                raise
            except Exception:  # noqa: BLE001
                pass

        # ── Append assistant message with pending tool calls ─────────────
        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": response.text,  # may be None — that's fine per spec
            "tool_calls": [_tool_call_message(tc) for tc in response.tool_calls],
        }
        # Preserve provider-specific tool-call extensions when present.
        if response.extra_content:
            assistant_message["extra_content"] = response.extra_content
        messages.append(assistant_message)

        # ── Execute all requested tools (concurrently) ───────────────────
        valid_calls = []
        skipped: dict[str, str] = {}
        for tc in response.tool_calls:
            signature = _tool_signature(tc)
            if tc.name not in allowed_names:
                skipped[tc.id] = f"未知工具：{tc.name}。"
            elif signature in seen_calls:
                skipped[tc.id] = "该工具调用与之前重复，已跳过。请使用已有结果。"
            elif len(valid_calls) >= max_calls_per_round:
                skipped[tc.id] = "本轮工具并发数量已达到上限，已跳过。"
            elif executed_calls + len(valid_calls) >= max_total_calls:
                skipped[tc.id] = "本次任务的工具调用总预算已用完，已跳过。"
            else:
                seen_calls.add(signature)
                valid_calls.append(tc)
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
                    tool_extensions=tool_extensions,
                ),
                name=f"agent-tool-{index}",
            )
            for index, tc in enumerate(valid_calls)
        ]
        try:
            if tool_tasks:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError("Agent deadline expired while scheduling tools")
                _done, pending = await asyncio.wait(tool_tasks, timeout=remaining)
                if pending:
                    raise TimeoutError("Agent deadline expired while tools were running")
                results = await asyncio.gather(*tool_tasks, return_exceptions=True)
                if any(isinstance(result, TimeoutError) for result in results):
                    raise TimeoutError("Agent deadline expired while tools were running")
            else:
                results = []
        except (asyncio.CancelledError, TimeoutError):
            for task in tool_tasks:
                if not task.done():
                    task.cancel()
            if tool_tasks:
                await asyncio.gather(*tool_tasks, return_exceptions=True)
            raise
        executed_calls += len(valid_calls)

        for tc, result in zip(valid_calls, results):
            if isinstance(result, BaseException):
                content = f"工具 {tc.name} 执行出错: {result}"
                LOGGER.warning("tool %s failed: %s", tc.name, result)
            else:
                content = _format_tool_observation(result, per_result_chars)
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
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": content,
                }
            )

        # DeepSeek requires exactly one tool response for every tool_call id.
        # This includes unknown, duplicate and budget-skipped calls.
        for tc in response.tool_calls:
            if tc.id in skipped:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": skipped[tc.id],
                    }
                )

        if not valid_calls:
            break

    # Max rounds reached — ask for a final answer without offering tools
    LOGGER.debug("agent_loop hit max_rounds=%d, requesting final answer", max_rounds)
    try:
        final_messages = _build_final_answer_messages(messages, results_max_chars * 2)
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError("Agent deadline expired before final response")
        final_timeout = min(15.0, remaining)
        final = await asyncio.wait_for(
            _safe_chat(
                llm,
                final_messages,
                temperature=temperature,
                presence_penalty=presence_penalty,
                frequency_penalty=frequency_penalty,
            ),
            timeout=final_timeout,
        )
        candidate = str(final).strip()
        if executed_calls:
            return _append_references(_validate_research_answer(candidate, messages), references)
        return candidate
    except Exception as exc:  # noqa: BLE001
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
) -> ToolResult:
    budget_scope = current_budget_scope()
    request_id = budget_scope.request_id if budget_scope is not None else f"agent-{call.id}"
    context = ToolContext(
        request_id=request_id,
        group_id=budget_scope.group_id or "" if budget_scope is not None else "",
        user_id=budget_scope.user_id or "" if budget_scope is not None else "",
        deadline_monotonic=deadline,
        cancel_scope=asyncio.current_task(),
        policy_snapshot=policy_snapshot or PolicySnapshot(roles=frozenset({"group_member"})),
        budget_scope=budget_scope,
        budget_controller=getattr(llm, "budget_controller", None),
        evidence_limit=8,
        tool_timeout_seconds=tool_timeout,
        result_max_chars=result_max_chars,
        extensions={
            "local_knowledge_search": local_knowledge_search,
            **(dict(tool_extensions) if tool_extensions is not None else {}),
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


def _tool_progress(calls: list[ToolCallRequest]) -> tuple[str, str]:
    names = {call.name for call in calls}
    if "sfm_generate_validate" in names:
        return "sfm_generation", "正在检索指定版本的 SFM 示例并执行有界生成与验证。"
    if names & {"repository_search", "repository_read"}:
        return "repository", "正在只读检索已批准的本地 Git 仓库。"
    if "knowledge_search" in names:
        return "knowledge", "正在检索本地知识库和领域资料。"
    if names & {"github_repo_inspect", "github_read_file"}:
        return "github_files", "已定位相关项目，正在核对官方仓库里的配置和原始文件。"
    if "github_repo_search" in names:
        return "github_repo", "网页结果不够明确，正在查找相关项目的官方仓库。"
    if "fetch_url" in names:
        return "fetch", "已找到一些候选页面，正在读取正文并核对细节。"
    if names & {"weather", "weather_batch"}:
        return "weather", "正在查询实时天气数据。"
    if names & {"web_search", "news_search", "multi_search"}:
        queries: list[str] = []
        for call in calls:
            try:
                args = json.loads(call.arguments)
            except json.JSONDecodeError:
                continue
            values = args.get("queries") or [args.get("query")]
            if not isinstance(values, list):
                values = [values]
            queries.extend(str(value).strip() for value in values if value)
        topic = "；".join(queries[:2])
        if len(topic) > 90:
            topic = topic[:90].rstrip() + "..."
        return "search", f"正在并行搜索：{topic}" if topic else "正在并行搜索相关资料。"
    return "tools", "正在调用外部资料进行核对。"


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


def _build_final_answer_messages(messages: list[dict[str, Any]], max_evidence_chars: int) -> list[dict[str, str]]:
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
                "Copy identifiers exactly. In FTB data, tasks are requirements and rewards are received only after completion."
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

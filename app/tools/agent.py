from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import re
from typing import Any, Mapping

from app.llm import ChatWithToolsResponse, DeepSeekClient, ToolCallRequest
from app.models import SearchResult
from app.tools.cache import get_cache
from app.tools.external import (
    fetch_web_page,
    get_weather,
    inspect_github_repository,
    multi_search,
    read_github_file,
    search_github_repositories,
    search_news,
    search_web,
)


LOGGER = logging.getLogger(__name__)


# ── OpenAI-style tool definitions sent to the model ──────────────────────────

TOOL_DEFINITIONS: list[dict[str, Any]] = [
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
]

TOOL_NAMES = {t["function"]["name"] for t in TOOL_DEFINITIONS}


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
    deadline = asyncio.get_running_loop().time() + total_timeout

    for _round in range(max_rounds):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 1:
            break
        try:
            response = await asyncio.wait_for(
                _safe_chat_with_tools(
                    llm,
                    messages,
                    tools=TOOL_DEFINITIONS,
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
                await on_tool_call(progress_text)
            except TypeError:
                await on_tool_call()
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
            if tc.name not in TOOL_NAMES:
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
        results = await asyncio.gather(
            *[
                _execute_native_tool_call(
                    tc,
                    timeout=tool_timeout,
                    local_knowledge_search=local_knowledge_search,
                )
                for tc in valid_calls
            ],
            return_exceptions=True,
        )
        executed_calls += len(valid_calls)

        per_result_chars = max(400, results_max_chars // max(len(valid_calls), 1))
        for tc, result in zip(valid_calls, results):
            if isinstance(result, Exception):
                content = f"工具 {tc.name} 执行出错: {result}"
                LOGGER.warning("tool %s failed: %s", tc.name, result)
            else:
                content = _format_tool_result(result, max_chars=per_result_chars)
                for item in result:
                    if item.url and not item.title.endswith("失败"):
                        if item.source in {"GitHub仓库文件", "GitHub文件", "网页读取"}:
                            priority = 0
                        elif item.source == "GitHub仓库":
                            priority = 1
                        else:
                            priority = 2
                        reference = (priority, item.title, item.url)
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
        final_timeout = max(3.0, min(15.0, deadline - asyncio.get_running_loop().time()))
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


async def _execute_native_tool_call(
    tc: ToolCallRequest,
    timeout: float,
    local_knowledge_search=None,
) -> list[SearchResult]:
    """Parse the tool call's JSON arguments and dispatch to the right function."""
    try:
        args: dict[str, Any] = json.loads(tc.arguments)
    except json.JSONDecodeError:
        args = {}

    name = tc.name
    cache = get_cache()
    _do_cache = name not in {"knowledge_search", "fetch_url", "github_read_file"}
    if _do_cache:
        _cached = cache.get(name, args)
        if _cached is not None:
            return _cached

    if name == "knowledge_search":
        if local_knowledge_search is None:
            return [SearchResult("本地知识库", "当前没有可用的本地知识源。", "", "本地知识库")]
        return await local_knowledge_search(str(args.get("query") or ""))
    if name == "weather":
        result = await get_weather(str(args.get("location") or args.get("query") or ""), timeout=timeout)
        cache.set(name, args, result)
        return result
    if name == "weather_batch":
        raw_locations = args.get("locations") or []
        if not isinstance(raw_locations, list):
            raw_locations = [raw_locations]
        locations = list(dict.fromkeys(str(item).strip() for item in raw_locations if str(item).strip()))[:10]
        if not locations:
            return [SearchResult(title="批量天气失败", summary="没有提供有效城市。", url="", source="天气")]
        batches = await asyncio.gather(
            *[get_weather(location, timeout=timeout) for location in locations],
            return_exceptions=True,
        )
        merged: list[SearchResult] = []
        for location, batch in zip(locations, batches):
            if isinstance(batch, Exception):
                merged.append(SearchResult(title=f"{location}天气失败", summary=str(batch), url="", source="天气"))
            else:
                merged.extend(batch)
        cache.set(name, args, merged)
        return merged
    if name == "news_search":
        result = await search_news(str(args.get("query") or ""), timeout=timeout)
        cache.set(name, args, result)
        return result
    if name == "multi_search":
        queries = args.get("queries") or []
        if not isinstance(queries, list):
            queries = [queries]
        result = await multi_search([str(item) for item in queries], timeout=timeout)
        cache.set(name, args, result)
        return result
    if name == "github_repo_search":
        result = await search_github_repositories(str(args.get("query") or ""), timeout=timeout)
        cache.set(name, args, result)
        return result
    if name == "github_repo_inspect":
        result = await inspect_github_repository(
            str(args.get("repository") or ""),
            str(args.get("query") or ""),
            timeout=timeout,
        )
        cache.set(name, args, result)
        return result
    if name == "github_read_file":
        return await read_github_file(
            str(args.get("repository") or ""),
            str(args.get("path") or ""),
            timeout=timeout,
        )
    if name == "fetch_url":
        return await fetch_web_page(str(args.get("url") or ""), timeout=timeout)
    # web_search (default)
    result = await search_web(str(args.get("query") or ""), timeout=timeout)
    if _do_cache:
        cache.set(name, args, result)
    return result


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
    if not results:
        return "没有找到结果。"
    lines = []
    for idx, r in enumerate(results[:6], start=1):
        summary = re.sub(r"\s+", " ", r.summary).strip()
        if len(summary) > 2200:
            summary = summary[:2200].rstrip() + "..."
        url_part = f"\n   URL: {r.url}" if r.url else ""
        lines.append(f"{idx}. {r.title}\n   {summary}{url_part}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "..."
    return text


def _tool_progress(calls: list[ToolCallRequest]) -> tuple[str, str]:
    names = {call.name for call in calls}
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
    unique: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    for _, title, url in sorted(references, key=lambda item: item[0]):
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        unique.append((re.sub(r"\s+", " ", title).strip(), url))
        if len(unique) >= limit:
            break
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


# ── Legacy helpers kept for backward compatibility ────────────────────────────
# (used by forced_tool path in bot_service.py and existing tests)

@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    args: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ToolObservation:
    call: ToolCall
    results: tuple[SearchResult, ...]


async def execute_tool_call(call: ToolCall, timeout: float) -> ToolObservation:
    if call.name == "weather":
        results = await get_weather(_tool_arg_value(call.name, call.args), timeout=timeout)
    elif call.name == "news_search":
        results = await search_news(_tool_arg_value(call.name, call.args), timeout=timeout)
    elif call.name == "fetch_url":
        results = await fetch_web_page(_tool_arg_value(call.name, call.args), timeout=timeout)
    else:
        results = await search_web(_tool_arg_value(call.name, call.args), timeout=timeout)
    return ToolObservation(call=call, results=tuple(results))


def forced_tool_call(action: str, payload: str) -> ToolCall | None:
    payload = payload.strip()
    if action == "weather":
        return ToolCall("weather", {"location": payload})
    if action == "news":
        return ToolCall("news_search", {"query": payload})
    if action == "web":
        if payload.startswith(("http://", "https://")):
            return ToolCall("fetch_url", {"url": payload})
        return ToolCall("web_search", {"query": payload})
    return None


def format_tool_observations(observations: list[ToolObservation], max_chars: int) -> str:
    if not observations:
        return ""
    lines = [
        "Tool results. Use these as evidence. If results are failed, sparse, or irrelevant, say so.",
    ]
    for obs_idx, observation in enumerate(observations, start=1):
        arg_text = ", ".join(f"{k}={v}" for k, v in observation.call.args.items())
        lines.append(f"{obs_idx}. Tool {observation.call.name}({arg_text})")
        for result_idx, result in enumerate(observation.results[:5], start=1):
            summary = re.sub(r"\s+", " ", result.summary).strip()
            if len(summary) > 420:
                summary = summary[:420].rstrip() + "..."
            url = f" URL: {result.url}" if result.url else ""
            lines.append(f"   {result_idx}) [{result.source}] {result.title} - {summary}{url}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "..."
    return text


def _tool_arg_value(name: str, args: Mapping[str, str]) -> str:
    if name == "weather":
        return str(args.get("location") or args.get("query") or "").strip()
    if name == "fetch_url":
        return str(args.get("url") or "").strip()
    return str(args.get("query") or args.get("keyword") or "").strip()

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from app.models import SearchResult
from app.tools.cache import get_cache
from app.tools.contracts import (
    Evidence,
    ToolAccess,
    ToolContext,
    ToolError,
    ToolPolicy,
    ToolResult,
    ToolSpec,
    ToolStatus,
    ToolOutputKind,
    format_search_results,
    search_results_to_tool_result,
)
from app.progress.events import ToolCategory
from app.tools.external import (
    get_weather,
    inspect_github_repository,
    read_github_file,
    search_github_repositories,
)
from app.repositories.local_git import (
    LocalGitRepositoryService,
    RepositoryDataUnavailable,
    RepositoryError,
    RepositoryExecutionError,
    RepositoryFileUnavailable,
    RepositoryInvalidRange,
    RepositoryPathDenied,
    RepositoryTimedOut,
    SnapshotRef,
)
from app.recipes.models import PackVersionKey
from app.recipes.query import (
    RecipeQueryService,
    RecipeQueryState,
    locator_to_string,
    recipe_to_dict,
)
from app.sfm.contracts import SFMTargetVersion
from app.sfm.workflow import SFMWorkflowStatus


LOGGER = logging.getLogger(__name__)
LegacyOperation = Callable[[], Awaitable[list[SearchResult]]]
ToolHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]]


def normalize_legacy_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Preserve the legacy aliases before validating the public tool schema."""
    normalized = dict(arguments)
    if name == "weather" and "location" not in normalized and "query" in normalized:
        normalized["location"] = normalized.pop("query")
    if name == "weather_batch":
        key = "locations"
        if isinstance(normalized.get(key), str):
            normalized[key] = [normalized[key]]
    return normalized


def _as_tool_result(results: list[SearchResult], context: ToolContext) -> ToolResult:
    base = search_results_to_tool_result(results)
    requested_limit = context.result_max_chars or 16_000
    # Leave room for the JSON object wrapper added by the registry output cap.
    visible_text = format_search_results(results, max(1, requested_limit - 16))
    content: dict[str, Any] = {"text": visible_text}
    if context.extensions.get("compat_search_results") is True:
        compat_rows: list[dict[str, str]] = []
        for item in results[:5]:
            summary = re.sub(r"\s+", " ", item.summary).strip()
            if len(summary) > 420:
                summary = summary[:420].rstrip() + "..."
            compat_rows.append(
                {
                    "title": item.title,
                    "summary": summary,
                    "url": item.url,
                    "source": item.source,
                }
            )
        content["results"] = compat_rows
    return ToolResult(
        status=base.status,
        content=content,
        evidence=base.evidence,
        usage=base.usage,
    )


def _failure(title: str, message: str, source: str) -> SearchResult:
    return SearchResult(title=title, summary=message, url="", source=source)


def _timeout(context: ToolContext) -> float:
    timeout = context.tool_timeout_seconds or 30.0
    if context.deadline_monotonic is not None:
        import asyncio

        timeout = min(timeout, max(0.0, context.deadline_monotonic - asyncio.get_running_loop().time()))
    return timeout


async def _cached(
    name: str,
    arguments: dict[str, Any],
    context: ToolContext,
    operation: LegacyOperation,
    *,
    cache_enabled: bool = True,
) -> ToolResult:
    cache_enabled = cache_enabled and context.extensions.get("use_tool_cache", True) is not False
    cache = get_cache()
    if cache_enabled:
        cached = cache.get(name, arguments)
        if isinstance(cached, list) and all(isinstance(item, SearchResult) for item in cached):
            return _as_tool_result(cached, context)
        if cached is not None:
            LOGGER.warning("tool %s cache entry had an unexpected value type", name)
    results = await operation()
    if not isinstance(results, list) or any(not isinstance(item, SearchResult) for item in results):
        raise TypeError("legacy tool adapter expected SearchResult rows")
    if cache_enabled:
        cache.set(name, arguments, results)
    return _as_tool_result(results, context)


async def _knowledge_search(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    callback = context.extensions.get("local_knowledge_search")
    if not callable(callback):
        return ToolResult(
            status=ToolStatus.DATA_UNAVAILABLE,
            content="当前没有可用的本地知识源。",
            error=ToolError("knowledge_unavailable", "当前没有可用的本地知识源。"),
        )
    results = await callback(arguments["query"])
    if isinstance(results, ToolResult):
        return results
    if not isinstance(results, list) or any(not isinstance(item, SearchResult) for item in results):
        raise TypeError("knowledge handler returned an invalid result")
    return _as_tool_result(results, context)


async def _sfm_examples(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    service = context.extensions.get("sfm_knowledge_service")
    if service is None:
        return ToolResult(
            status=ToolStatus.DATA_UNAVAILABLE,
            content={"text": "SFM 示例检索服务当前不可用。", "results": []},
            error=ToolError("sfm_knowledge_unavailable", "SFM 示例检索服务当前不可用。"),
        )
    return await service.search_examples(
        arguments["query"],
        sfm_version=arguments["sfm_version"],
        minecraft_version=arguments["minecraft_version"],
        limit=arguments.get("limit", 6),
    )


async def _sfm_generate_validate(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    workflow = context.extensions.get("sfm_generation_workflow")
    if workflow is None:
        return ToolResult(
            status=ToolStatus.DATA_UNAVAILABLE,
            content={"status": "unavailable", "source": None, "message": "SFM 工作流当前不可用。"},
            error=ToolError("sfm_workflow_unavailable", "SFM 工作流当前不可用。"),
        )
    try:
        target = SFMTargetVersion(
            arguments["sfm_version"],
            arguments["minecraft_version"],
        )
        result = await workflow.generate_validate(
            arguments["description"],
            target=target,
            deadline_monotonic=context.deadline_monotonic
            if context.deadline_monotonic is not None
            else asyncio.get_running_loop().time() + (context.tool_timeout_seconds or 15.0),
            max_result_chars=context.result_max_chars or 6000,
        )
    except (KeyError, ValueError):
        return ToolResult(
            status=ToolStatus.INVALID_ARGUMENT,
            content={"status": "invalid_argument", "source": None},
            error=ToolError("invalid_sfm_workflow_arguments", "SFM 工作流参数无效。"),
        )

    states = {
        SFMWorkflowStatus.VALID: ToolStatus.OK,
        SFMWorkflowStatus.INVALID: ToolStatus.PARTIAL,
        SFMWorkflowStatus.SIMULATED_VALID: ToolStatus.PARTIAL,
        SFMWorkflowStatus.SIMULATED_INVALID: ToolStatus.PARTIAL,
        SFMWorkflowStatus.UNAVAILABLE: ToolStatus.DATA_UNAVAILABLE,
        SFMWorkflowStatus.VERSION_UNSUPPORTED: ToolStatus.VERSION_MISMATCH,
        SFMWorkflowStatus.DATA_UNAVAILABLE: ToolStatus.DATA_UNAVAILABLE,
        SFMWorkflowStatus.NOT_INDEXED: ToolStatus.NOT_INDEXED,
        SFMWorkflowStatus.NO_RESULTS: ToolStatus.NO_RESULTS,
        SFMWorkflowStatus.VERSION_MISMATCH: ToolStatus.VERSION_MISMATCH,
        SFMWorkflowStatus.TIMED_OUT: ToolStatus.TIMED_OUT,
        SFMWorkflowStatus.BUDGET_EXCEEDED: ToolStatus.DENIED,
        SFMWorkflowStatus.ERROR: ToolStatus.ERROR,
    }
    status = states[result.status]
    error = None
    if result.status != SFMWorkflowStatus.VALID:
        error = ToolError(
            f"sfm_workflow_{result.status.value}",
            result.message or "SFM 工作流没有完成验证。",
            retryable=result.status in {SFMWorkflowStatus.TIMED_OUT, SFMWorkflowStatus.ERROR},
        )
    content = result.to_dict()
    output_limit = min(context.result_max_chars or 12_000, 12_000)
    if len(json.dumps(content, ensure_ascii=False)) > output_limit:
        # Preserve a complete structured result instead of letting the generic
        # Registry clip the middle of generated source code.
        content["source"] = None
        content["status"] = "source_omitted_output_limit"
        content["source_omitted"] = True
        content["message"] = "完整源码无法在本次结果预算内返回；请缩小程序范围后重试。"
        status = ToolStatus.PARTIAL
        error = ToolError(
            "sfm_source_output_limit",
            "完整源码无法在本次结果预算内返回；请缩小程序范围后重试。",
        )
    return ToolResult(
        status=status,
        content=content,
        evidence=result.evidence,
        error=error,
    )


async def _weather(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    location = arguments["location"]
    return await _cached(
        "weather",
        arguments,
        context,
        lambda: get_weather(location, timeout=_timeout(context)),
    )


async def _weather_batch(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    locations = list(dict.fromkeys(location.strip() for location in arguments["locations"] if location.strip()))
    if not locations:
        return _as_tool_result([_failure("批量天气失败", "没有提供有效城市。", "天气")], context)

    async def operation() -> list[SearchResult]:
        batches = await asyncio.gather(
            *(get_weather(location, timeout=_timeout(context)) for location in locations),
            return_exceptions=True,
        )
        merged: list[SearchResult] = []
        for location, batch in zip(locations, batches):
            if isinstance(batch, BaseException):
                merged.append(
                    _failure(
                        f"{location}天气失败",
                        f"天气服务执行失败（{type(batch).__name__}）。",
                        "天气",
                    )
                )
            else:
                merged.extend(batch)
        return merged

    return await _cached("weather_batch", {"locations": locations}, context, operation)


async def _news_search(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    service = context.extensions.get("web_research_service")
    if service is None:
        return ToolResult(
            ToolStatus.DATA_UNAVAILABLE,
            "Tavily Web Search 未配置（NOT_CONFIGURED）。",
            error=ToolError("not_configured", "Tavily Web Search 未配置。"),
        )
    return await service.news_search(
        arguments["query"], context,
        max_results=arguments.get("max_results"),
        time_range=arguments.get("time_range"),
        start_date=arguments.get("start_date"),
        end_date=arguments.get("end_date"),
    )


async def _github_repo_search(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    return await _cached(
        "github_repo_search",
        arguments,
        context,
        lambda: search_github_repositories(arguments["query"], timeout=_timeout(context)),
    )


async def _github_repo_inspect(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    return await _cached(
        "github_repo_inspect",
        arguments,
        context,
        lambda: inspect_github_repository(
            arguments["repository"], arguments["query"], timeout=_timeout(context)
        ),
    )


async def _github_read_file(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    return await _cached(
        "github_read_file",
        arguments,
        context,
        lambda: read_github_file(
            arguments["repository"], arguments["path"], timeout=_timeout(context)
        ),
        cache_enabled=False,
    )


async def _web_read(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    service = context.extensions.get("web_research_service")
    if service is None:
        return ToolResult(
            ToolStatus.DATA_UNAVAILABLE,
            "安全网页读取服务当前不可用。",
            error=ToolError("web_read_unavailable", "安全网页读取服务当前不可用。"),
        )
    return await service.read(arguments["url"], context)


async def _run_repository_sync(context: ToolContext, function, /, *args, **kwargs):
    """Use the app-owned bounded executor for synchronous local Git operations."""
    runner = context.extensions.get("local_repository_runner")
    if not callable(runner):
        raise RepositoryExecutionError("local repository execution lane is unavailable")
    return await runner(function, *args, **kwargs)


def _repository_evidence(repo_id: str, commit_sha: str, path: str, *, line: int | None = None, end_line: int | None = None):
    locator = f"git://{repo_id}/{commit_sha}/{quote(path, safe='/')}"
    if line is not None:
        locator += f"#L{line}"
        if end_line is not None and end_line != line:
            locator += f"-L{end_line}"
    return locator


async def _repository_search(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    service = context.extensions.get("local_repository_service")
    if not isinstance(service, LocalGitRepositoryService):
        return ToolResult(
            status=ToolStatus.DATA_UNAVAILABLE,
            content="当前没有配置可检索的本地仓库。",
            error=ToolError("repository_unavailable", "当前没有配置可检索的本地仓库。"),
        )
    try:
        page = await _run_repository_sync(
            context,
            service.search,
            arguments["repo_id"],
            arguments["query"],
            mode=arguments["mode"],
            commit_sha=arguments.get("commit_sha"),
            offset=arguments.get("offset", 0),
            limit=arguments.get("limit", 10),
        )
    except RepositoryTimedOut:
        return ToolResult(
            status=ToolStatus.TIMED_OUT,
            content="本地仓库搜索超时。",
            error=ToolError("repository_search_timed_out", "本地仓库搜索超时。", retryable=True),
        )
    except RepositoryDataUnavailable:
        return ToolResult(
            status=ToolStatus.DATA_UNAVAILABLE,
            content="请求的本地仓库或提交当前不可用。",
            error=ToolError("repository_unavailable", "请求的本地仓库或提交当前不可用。"),
        )
    except RepositoryPathDenied:
        return ToolResult(
            status=ToolStatus.INVALID_ARGUMENT,
            content="本地仓库查询参数无效或被安全策略拒绝。",
            error=ToolError("repository_query_denied", "本地仓库查询参数无效或被安全策略拒绝。"),
        )
    except RepositoryError:
        return ToolResult(
            status=ToolStatus.ERROR,
            content="本地仓库搜索失败。",
            error=ToolError("repository_search_failed", "本地仓库搜索失败。", retryable=True),
        )

    evidence = []
    lines = [f"仓库 {page.snapshot.repo_id}，固定提交 {page.snapshot.commit_sha}。"]
    if not page.matches:
        lines.append("没有找到匹配项。")
    for match in page.matches:
        if match.line is None:
            lines.append(f"- {match.path}")
            evidence.append(
                Evidence(
                    kind="repository_file",
                    source_id=page.snapshot.repo_id,
                    title=match.path,
                    locator=_repository_evidence(page.snapshot.repo_id, page.snapshot.commit_sha, match.path),
                    revision=page.snapshot.commit_sha,
                )
            )
        else:
            lines.append(f"- {match.path}:L{match.line}: {match.snippet}")
            evidence.append(
                Evidence(
                    kind="repository_file",
                    source_id=page.snapshot.repo_id,
                    title=match.path,
                    locator=_repository_evidence(
                        page.snapshot.repo_id,
                        page.snapshot.commit_sha,
                        match.path,
                        line=match.line,
                    ),
                    revision=page.snapshot.commit_sha,
                )
            )
    if page.next_offset is not None:
        lines.append(f"还有结果；下一页 offset={page.next_offset}。")
    if page.truncated:
        lines.append("本次搜索达到扫描上限，当前结果可能不完整。")
    status = ToolStatus.PARTIAL if page.truncated else (ToolStatus.OK if page.matches else ToolStatus.NO_RESULTS)
    error = (
        ToolError("repository_search_partial", "搜索达到扫描上限，结果可能不完整。", retryable=True)
        if page.truncated
        else None
    )
    return ToolResult(
        status=status,
        content={"text": "\n".join(lines), "snapshot": page.snapshot.commit_sha, "next_offset": page.next_offset},
        evidence=tuple(evidence),
        error=error,
    )


async def _repository_read(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    service = context.extensions.get("local_repository_service")
    if not isinstance(service, LocalGitRepositoryService):
        return ToolResult(
            status=ToolStatus.DATA_UNAVAILABLE,
            content="当前没有配置可检索的本地仓库。",
            error=ToolError("repository_unavailable", "当前没有配置可检索的本地仓库。"),
        )
    try:
        snapshot = SnapshotRef(arguments["repo_id"], arguments["commit_sha"])
        result = await _run_repository_sync(
            context,
            service.read_lines,
            arguments["repo_id"],
            arguments["path"],
            snapshot,
            start_line=arguments["start_line"],
            end_line=arguments["end_line"],
        )
    except RepositoryTimedOut:
        return ToolResult(
            status=ToolStatus.TIMED_OUT,
            content="本地仓库文件读取超时。",
            error=ToolError("repository_read_timed_out", "本地仓库文件读取超时。", retryable=True),
        )
    except RepositoryDataUnavailable:
        return ToolResult(
            status=ToolStatus.DATA_UNAVAILABLE,
            content="请求的本地仓库或提交当前不可用。",
            error=ToolError("repository_unavailable", "请求的本地仓库或提交当前不可用。"),
        )
    except RepositoryFileUnavailable:
        return ToolResult(status=ToolStatus.NO_RESULTS, content="指定提交中没有该文件。")
    except RepositoryInvalidRange:
        return ToolResult(
            status=ToolStatus.INVALID_ARGUMENT,
            content="文件行范围无效或超出文件内容。",
            error=ToolError("repository_line_range_invalid", "文件行范围无效或超出文件内容。"),
        )
    except RepositoryPathDenied:
        return ToolResult(
            status=ToolStatus.DENIED,
            content="该仓库路径被安全策略拒绝。",
            error=ToolError("repository_path_denied", "该仓库路径被安全策略拒绝。"),
        )
    except RepositoryError:
        return ToolResult(
            status=ToolStatus.ERROR,
            content="本地仓库文件读取失败。",
            error=ToolError("repository_read_failed", "本地仓库文件读取失败。", retryable=True),
        )

    file = result.file
    evidence = Evidence(
        kind="repository_file",
        source_id=file.repo_id,
        title=file.path,
        locator=_repository_evidence(
            file.repo_id,
            file.snapshot.commit_sha,
            file.path,
            line=result.start_line,
            end_line=result.end_line,
        ),
        revision=file.snapshot.commit_sha,
    )
    return ToolResult(
        status=ToolStatus.PARTIAL if result.truncated else ToolStatus.OK,
        content={
            "text": f"{file.repo_id}@{file.snapshot.commit_sha} {file.path} L{result.start_line}-L{result.end_line}\n{result.text}",
            "snapshot": file.snapshot.commit_sha,
            "path": file.path,
            "start_line": result.start_line,
            "end_line": result.end_line,
            "truncated": result.truncated,
        },
        evidence=(evidence,),
        error=(
            ToolError("repository_output_clipped", "文件输出已按字节上限截断。")
            if result.truncated
            else None
        ),
    )


async def _web_search(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    service = context.extensions.get("web_research_service")
    if service is None:
        return ToolResult(
            ToolStatus.DATA_UNAVAILABLE,
            "Tavily Web Search 未配置（NOT_CONFIGURED）。",
            error=ToolError("not_configured", "Tavily Web Search 未配置。"),
        )
    return await service.search(
        arguments["query"], context,
        max_results=arguments.get("max_results"),
        search_depth=arguments.get("search_depth"),
        time_range=arguments.get("time_range"),
        start_date=arguments.get("start_date"),
        end_date=arguments.get("end_date"),
        include_domains=arguments.get("include_domains", ()),
        exclude_domains=arguments.get("exclude_domains", ()),
    )


def _recipe_state_result(query, *, request: dict[str, Any]) -> ToolResult:
    state_to_status = {
        RecipeQueryState.DATA_UNAVAILABLE: ToolStatus.DATA_UNAVAILABLE,
        RecipeQueryState.VERSION_MISMATCH: ToolStatus.VERSION_MISMATCH,
        RecipeQueryState.EMPTY: ToolStatus.NO_RESULTS,
        RecipeQueryState.NO_RESULTS: ToolStatus.NO_RESULTS,
        RecipeQueryState.AVAILABLE: ToolStatus.OK,
    }
    status = ToolStatus.PARTIAL if query.has_more else state_to_status[query.state]
    messages = {
        RecipeQueryState.DATA_UNAVAILABLE: "指定整合包与版本尚无可查询的配方数据；请先导入并激活该精确版本的数据。",
        RecipeQueryState.VERSION_MISMATCH: "已知该整合包的其他版本，但请求版本没有已激活数据；不会回退到其他版本。",
        RecipeQueryState.EMPTY: "指定版本的数据集已激活，但当前没有可查询配方。",
        RecipeQueryState.NO_RESULTS: "指定版本的已激活数据中没有精确匹配；条件配方不会被当成精确命中。",
        RecipeQueryState.AVAILABLE: "",
    }
    recipe_rows = list(zip(query.recipes, query.source_locators))
    recipes = [recipe_to_dict(recipe, locator) for recipe, locator in recipe_rows]
    evidence = tuple(
        Evidence(
            kind="recipe",
            source_id=query.source_id or "recipe-dataset",
            title=recipe.recipe_id,
            locator=locator_text,
            revision=query.source_revision,
            pack_id=query.key.pack_id,
            pack_version=query.key.pack_version,
            confidence=(
                "exact"
                if recipe.status.value == "accepted"
                else "conditional"
                if recipe.status.value == "conditional"
                else "unsupported"
            ),
        )
        for recipe, locator in recipe_rows
        if (locator_text := locator_to_string(locator)) is not None
    )
    content: dict[str, Any] = {
        "state": query.state.value,
        "message": (
            f"本页返回 {len(query.recipes)} / {query.total_matches} 条匹配；还有结果，请用 offset={query.next_offset} 继续查询。"
            if query.next_offset is not None
            else "仍有配方匹配未返回；已达到本地最大分页边界。"
            if query.has_more
            else messages[query.state]
        ),
        "pack_version": {
            "pack_id": query.key.pack_id,
            "pack_version": query.key.pack_version,
            "game_version": query.key.game_version,
        },
        "query": request,
        "dataset_id": query.dataset_id,
        "source_id": query.source_id,
        "source_revision": query.source_revision,
        "match_quality": query.match_quality.value if query.match_quality else None,
        "quarantined_recipe_count": query.quarantined_recipe_count,
        "total_matches": query.total_matches,
        "has_more": query.has_more,
        "next_offset": query.next_offset,
        "recipes": recipes,
    }
    error = None
    if query.state in {RecipeQueryState.DATA_UNAVAILABLE, RecipeQueryState.VERSION_MISMATCH}:
        error = ToolError(query.state.value, messages[query.state])
    return ToolResult(status=status, content=content, evidence=evidence, error=error)


async def _run_recipe_query(context: ToolContext, function, /, *args, **kwargs):
    service = context.extensions.get("recipe_query_service")
    runner = context.extensions.get("recipe_query_runner")
    if not isinstance(service, RecipeQueryService) or not callable(runner):
        return None
    return await runner(function, *args, **kwargs)


async def _recipe_lookup(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    service = context.extensions.get("recipe_query_service")
    if not isinstance(service, RecipeQueryService):
        return ToolResult(
            status=ToolStatus.DATA_UNAVAILABLE,
            content="配方查询服务当前不可用。",
            error=ToolError("recipe_service_unavailable", "配方查询服务当前不可用。"),
        )
    try:
        key = PackVersionKey(
            arguments["pack_id"], arguments["pack_version"], arguments["game_version"]
        )
        result = await _run_recipe_query(
            context,
            service.find_by_identifier,
            key,
            arguments["identifier"],
            direction=arguments["direction"],
            kind=arguments["kind"],
            limit=arguments.get("limit", 10),
            offset=arguments.get("offset", 0),
        )
    except (KeyError, ValueError):
        return ToolResult(
            status=ToolStatus.INVALID_ARGUMENT,
            content="请提供有效的整合包、整合包版本、Minecraft 版本和精确 item/fluid ID。",
            error=ToolError("invalid_recipe_query", "配方查询参数无效。"),
        )
    if result is None:
        return ToolResult(
            status=ToolStatus.DATA_UNAVAILABLE,
            content="配方查询服务当前不可用。",
            error=ToolError("recipe_service_unavailable", "配方查询服务当前不可用。"),
        )
    return _recipe_state_result(
        result,
        request={
            "identifier": arguments["identifier"],
            "kind": arguments["kind"],
            "direction": arguments["direction"],
            "limit": arguments.get("limit", 10),
            "offset": arguments.get("offset", 0),
        },
    )


async def _recipe_machine_lookup(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    service = context.extensions.get("recipe_query_service")
    if not isinstance(service, RecipeQueryService):
        return ToolResult(
            status=ToolStatus.DATA_UNAVAILABLE,
            content="配方查询服务当前不可用。",
            error=ToolError("recipe_service_unavailable", "配方查询服务当前不可用。"),
        )
    try:
        key = PackVersionKey(
            arguments["pack_id"], arguments["pack_version"], arguments["game_version"]
        )
        result = await _run_recipe_query(
            context,
            service.find_by_machine,
            key,
            arguments["machine_id"],
            limit=arguments.get("limit", 10),
            offset=arguments.get("offset", 0),
        )
    except (KeyError, ValueError):
        return ToolResult(
            status=ToolStatus.INVALID_ARGUMENT,
            content="请提供有效的整合包、整合包版本、Minecraft 版本和机器 ID。",
            error=ToolError("invalid_machine_query", "机器配方查询参数无效。"),
        )
    if result is None:
        return ToolResult(
            status=ToolStatus.DATA_UNAVAILABLE,
            content="配方查询服务当前不可用。",
            error=ToolError("recipe_service_unavailable", "配方查询服务当前不可用。"),
        )
    return _recipe_state_result(
        result,
        request={
            "machine_id": arguments["machine_id"],
            "limit": arguments.get("limit", 10),
            "offset": arguments.get("offset", 0),
        },
    )


_HANDLERS: dict[str, tuple[ToolHandler, ToolAccess, tuple[str, ...]]] = {
    "knowledge_search": (_knowledge_search, ToolAccess.LOCAL_READ, ("local_knowledge_index",)),
    "web_search": (_web_search, ToolAccess.NETWORK_READ, ()),
    "news_search": (_news_search, ToolAccess.NETWORK_READ, ()),
    "web_read": (_web_read, ToolAccess.NETWORK_READ, ()),
    "github_repo_search": (_github_repo_search, ToolAccess.NETWORK_READ, ()),
    "github_repo_inspect": (_github_repo_inspect, ToolAccess.NETWORK_READ, ()),
    "github_read_file": (_github_read_file, ToolAccess.NETWORK_READ, ()),
    "weather": (_weather, ToolAccess.NETWORK_READ, ()),
    "weather_batch": (_weather_batch, ToolAccess.NETWORK_READ, ()),
    "repository_search": (_repository_search, ToolAccess.LOCAL_READ, ("local_git_repository",)),
    "repository_read": (_repository_read, ToolAccess.LOCAL_READ, ("local_git_repository",)),
    "recipe_lookup": (_recipe_lookup, ToolAccess.LOCAL_READ, ("versioned_recipe_dataset",)),
    "recipe_machine_lookup": (_recipe_machine_lookup, ToolAccess.LOCAL_READ, ("versioned_recipe_dataset",)),
    "sfm_examples": (_sfm_examples, ToolAccess.LOCAL_READ, ("versioned_sfm_document_corpus",)),
    "sfm_generate_validate": (
        _sfm_generate_validate,
        ToolAccess.VALIDATION_PROCESS,
        ("versioned_sfm_document_corpus", "versioned_sfm_validator"),
    ),
}


def build_legacy_tool_specs(definitions: list[dict[str, Any]]) -> list[ToolSpec]:
    """Build direct ToolSpecs for each existing tool name and public schema."""
    specs: list[ToolSpec] = []
    seen: set[str] = set()
    for definition in definitions:
        function = definition["function"]
        name = str(function["name"])
        if name in seen:
            raise ValueError(f"duplicate legacy schema name: {name}")
        seen.add(name)
        entry = _HANDLERS.get(name)
        if entry is None:
            raise ValueError(f"no legacy handler registered for tool: {name}")
        handler, access, data_requirements = entry
        if name in {"web_search"}:
            progress_category = ToolCategory.WEB_SEARCH
        elif name == "web_read":
            progress_category = ToolCategory.WEB_READ
        elif name == "news_search":
            progress_category = ToolCategory.NEWS_SEARCH
        elif name in {"knowledge_search", "wiki_search"}:
            progress_category = ToolCategory.WIKI
        elif name in {"recipe_lookup"}:
            progress_category = ToolCategory.RECIPE
        elif name == "recipe_machine_lookup":
            progress_category = ToolCategory.MACHINE
        elif name.startswith(("repository_", "github_")):
            progress_category = ToolCategory.REPOSITORY
        elif name in {"sfm_examples", "sfm_generate_validate"}:
            progress_category = ToolCategory.SFM_VALIDATION
        elif name.startswith("weather"):
            progress_category = ToolCategory.WEATHER
        else:
            progress_category = ToolCategory.OTHER
        specs.append(
            ToolSpec(
                name=name,
                description=str(function["description"]),
                parameters=function["parameters"],
                handler=handler,
                argument_validator=lambda arguments, tool_name=name: normalize_legacy_arguments(
                    tool_name, arguments
                ),
                policy=ToolPolicy(access=access, allowed_roles=frozenset({"group_member"})),
                timeout_seconds=15 if name.startswith(("repository_", "recipe_", "sfm_")) else 60,
                max_concurrency=2 if name in {"repository_search", "sfm_generate_validate"} else 4,
                max_result_chars=(
                    8_000
                    if name.startswith("recipe_")
                    else 20_000
                    if name.startswith("repository_")
                    else 12_000
                    if name.startswith("sfm_")
                    else 64_000
                ),
                output_kind=ToolOutputKind.EVIDENCE,
                data_requirements=data_requirements,
                progress_category=progress_category,
            )
        )
    if seen != set(_HANDLERS):
        missing = sorted(set(_HANDLERS) - seen)
        raise ValueError(f"missing legacy schema definitions: {', '.join(missing)}")
    return specs

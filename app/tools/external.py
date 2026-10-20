from __future__ import annotations

import html as htmlmod
import json
import os
from pathlib import Path
import re
import socket
import time
from collections import Counter
from typing import Any, Mapping
from urllib.parse import urlparse

from bs4 import BeautifulSoup
import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.models import SearchResult
from app.tools.url_policy import (
    FetchLimitError,
    SafeHTTPFetcher,
    URLPolicyError,
    normalize_url,
    redact_url,
)


# ── HTTP client helpers ───────────────────────────────────────────────────────

# Proxy is set via environment variable (https_proxy / HTTPS_PROXY).
# httpx.AsyncClient respects these automatically; no explicit config needed.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}

# Remaining general external client for GitHub metadata and weather endpoints.
_GITHUB_API = "https://api.github.com"
_GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "qq-deepseek-bot/0.2"}
_GITHUB_TREE_CACHE: dict[str, tuple[float, str, list[dict[str, Any]]]] = {}
_SAFE_WEB_FETCHER = SafeHTTPFetcher(max_redirects=5, max_response_bytes=1_000_000)


def _external_client(timeout: float) -> httpx.AsyncClient:
    proxy = os.getenv("NETWORK_PROXY_URL", "").strip() or None
    if proxy and not _proxy_is_reachable(proxy):
        proxy = None
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers=_BROWSER_HEADERS,
        proxy=proxy,
    )


def _proxy_is_reachable(proxy_url: str) -> bool:
    parsed = urlparse(proxy_url)
    if not parsed.hostname or not parsed.port:
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=0.15):
            return True
    except OSError:
        return False


@retry(
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=0.2, min=0.2, max=1.0),
    reraise=True,
)
async def _get_with_retry(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    return await client.get(url, **kwargs)


def _github_headers() -> dict[str, str]:
    headers = dict(_GITHUB_HEADERS)
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def looks_like_url(text: str) -> bool:
    try:
        normalize_url(text)
    except URLPolicyError:
        return False
    return True


async def search_github_repositories(query: str, timeout: float = 12.0, max_results: int = 6) -> list[SearchResult]:
    query = query.strip()
    if not query:
        return []
    variants = [query]
    project_query = re.sub(
        r"\b(recipe|recipes|craft|crafting|guide|how|make|star|kubejs|script|ingredients)\b",
        " ",
        query,
        flags=re.IGNORECASE,
    )
    project_query = re.sub(r"\s+", " ", project_query).strip()
    if project_query:
        variants.append(f"{project_query} in:name")
    atm_match = re.search(r"all\s*the\s*mods\s*[- ]?(\d+)", query, re.IGNORECASE)
    if atm_match:
        variants.insert(0, f"AllTheMods ATM-{atm_match.group(1)} in:name")
    variants = list(dict.fromkeys(variants))[:3]
    try:
        async with _external_client(timeout) as client:
            import asyncio
            responses = await asyncio.gather(
                *[
                    client.get(
                        f"{_GITHUB_API}/search/repositories",
                        params={"q": variant, "per_page": max_results},
                        headers=_github_headers(),
                    )
                    for variant in variants
                ]
            )
            items = []
            seen: set[str] = set()
            for resp in responses:
                resp.raise_for_status()
                for item in resp.json().get("items") or []:
                    full_name = str(item.get("full_name") or "")
                    if full_name and full_name not in seen:
                        seen.add(full_name)
                        items.append(item)
    except Exception as exc:  # noqa: BLE001
        atm_match = re.search(r"all\s*the\s*mods\s*[- ]?(\d+)|\batm\s*[- ]?(\d+)\b", query, re.IGNORECASE)
        if atm_match:
            version = next(group for group in atm_match.groups() if group)
            repository = f"AllTheMods/ATM-{version}"
            return [
                SearchResult(
                    title=repository,
                    summary=f"GitHub API受限，使用可验证的官方仓库候选；后续将通过本地浅克隆核对文件。API错误：{exc}",
                    url=f"https://github.com/{repository}",
                    source="GitHub仓库",
                )
            ]
        return [_failed_result("GitHub仓库搜索", str(exc))]
    return [
        SearchResult(
            title=str(item.get("full_name") or "GitHub repository"),
            summary=(
                f"默认分支：{item.get('default_branch') or '未知'}；"
                f"描述：{item.get('description') or '无'}"
            ),
            url=str(item.get("html_url") or ""),
            source="GitHub仓库",
        )
        for item in items[:max_results]
    ]


async def inspect_github_repository(
    repository: str,
    query: str,
    timeout: float = 15.0,
    max_results: int = 6,
) -> list[SearchResult]:
    """Search likely text files in a public GitHub repository without code-search auth."""
    repository = repository.strip().strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        return [_failed_result("GitHub仓库检索", "仓库名必须是 owner/repository 格式。")]
    try:
        branch, tree = await _github_tree(repository, timeout)
    except Exception as exc:  # noqa: BLE001
        return [_failed_result("GitHub仓库检索", str(exc))]

    terms = _github_query_terms(query)
    candidates: list[tuple[int, str]] = []
    allowed = (".md", ".txt", ".json", ".json5", ".js", ".ts", ".snbt", ".toml", ".yaml", ".yml", ".cfg")
    for item in tree:
        path = str(item.get("path") or "")
        if item.get("type") != "blob" or not path.lower().endswith(allowed):
            continue
        if int(item.get("size") or 0) > 1_500_000:
            continue
        lowered = path.lower().replace("-", "_").replace(" ", "_")
        score = sum(3 for term in terms if term in lowered)
        if score:
            score += 2 if any(part in lowered for part in ("recipe", "quest", "kubejs", "config")) else 0
            score += 6 if "/zh_cn/" in lowered else 0
            score += 3 if "/en_us/" in lowered else 0
            score -= 4 if "/lang/" in lowered and "/zh_cn/" not in lowered and "/en_us/" not in lowered else 0
            score += 3 if "chapter_2_the_star" in lowered else 0
            score -= 2 if "creative" in lowered and "creative" not in query.lower() else 0
            candidates.append((score, path))
    candidates.sort(key=lambda item: (-item[0], len(item[1])))
    paths = [path for _, path in candidates[: min(max_results * 2, 10)]]
    if not paths:
        return []

    import asyncio
    bodies = await asyncio.gather(
        *[_github_raw_file(repository, branch, path, timeout) for path in paths],
        return_exceptions=True,
    )
    results: list[SearchResult] = []
    exact_term = terms[0] if terms and "_" in terms[0] else ""
    recipe_query = bool(re.search(r"recipe|craft|制作|合成|配方", query, re.IGNORECASE))
    for path, body in zip(paths, bodies):
        if isinstance(body, Exception) or not body:
            continue
        if exact_term and exact_term not in body.lower():
            # A matching filename alone is not evidence that the file defines the target item.
            continue
        relation = _target_relation_summary(body, exact_term) if exact_term else ""
        recipe_facts = _kubejs_recipe_facts(body, exact_term) if exact_term else []
        if recipe_query and relation.startswith("目标仅作为输入") and not path.lower().endswith(".snbt"):
            continue
        snippet = _relevant_file_snippet(body, terms, 4200)
        results.append(
            SearchResult(
                title=f"{repository}: {path}",
                summary=(
                    "证据提示：文件中出现了目标词，但必须根据 item id 和 recipe/result 字段判断它是输入、输出还是仅被提及；"
                    "不要把相似名称物品的配方当成目标配方。FTB任务文件中 tasks 是完成条件、rewards 是完成后的奖励、"
                    "dependencies 是前置任务，禁止反向推断。物品ID必须逐字引用，不要改写下划线或命名空间。"
                    f"关系检测：{relation or '未能确定目标在文件中的输入/输出关系'}。"
                    + ("精确配方计数：" + "；".join(recipe_facts) + "。" if recipe_facts else "")
                    + "\n" + snippet
                ),
                url=f"https://github.com/{repository}/blob/{branch}/{path}",
                source="GitHub仓库文件",
            )
        )
        if len(results) >= max_results:
            break
    return results


async def read_github_file(repository: str, path: str, timeout: float = 15.0) -> list[SearchResult]:
    repository = repository.strip().strip("/")
    path = path.strip().lstrip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or ".." in path:
        return [_failed_result("GitHub文件读取", "仓库名或路径无效。")]
    try:
        branch, _ = await _github_tree(repository, timeout)
        body = await _github_raw_file(repository, branch, path, timeout)
    except Exception as exc:  # noqa: BLE001
        return [_failed_result("GitHub文件读取", str(exc))]
    text = body[:6000].rstrip()
    return [SearchResult(path, text, f"https://github.com/{repository}/blob/{branch}/{path}", "GitHub文件")]


async def _github_tree(repository: str, timeout: float) -> tuple[str, list[dict[str, Any]]]:
    cached = _GITHUB_TREE_CACHE.get(repository)
    if cached and time.monotonic() - cached[0] < 900:
        return cached[1], cached[2]
    local_dir = _github_local_dir(repository)
    if local_dir.is_dir():
        branch, tree = _local_github_tree(local_dir)
    else:
        try:
            async with _external_client(timeout) as client:
                repo_resp = await client.get(f"{_GITHUB_API}/repos/{repository}", headers=_github_headers())
                repo_resp.raise_for_status()
                branch = str(repo_resp.json().get("default_branch") or "main")
                tree_resp = await client.get(
                    f"{_GITHUB_API}/repos/{repository}/git/trees/{branch}",
                    params={"recursive": "1"},
                    headers=_github_headers(),
                )
                tree_resp.raise_for_status()
                tree = tree_resp.json().get("tree") or []
        except Exception:
            await _clone_github_repository(repository, local_dir, timeout=max(60.0, timeout * 4))
            branch, tree = _local_github_tree(local_dir)
    _GITHUB_TREE_CACHE[repository] = (time.monotonic(), branch, tree)
    return branch, tree


async def _github_raw_file(repository: str, branch: str, path: str, timeout: float) -> str:
    local_file = _github_local_dir(repository) / path
    if local_file.is_file():
        return local_file.read_text(encoding="utf-8", errors="replace")
    async with _external_client(timeout) as client:
        resp = await client.get(f"https://raw.githubusercontent.com/{repository}/{branch}/{path}")
        resp.raise_for_status()
        return resp.text


def _github_local_dir(repository: str) -> Path:
    owner, name = repository.split("/", 1)
    return Path("data/github_repos") / f"{owner}__{name}"


def _local_github_tree(local_dir: Path) -> tuple[str, list[dict[str, Any]]]:
    branch = "main"
    head = local_dir / ".git/HEAD"
    if head.is_file():
        value = head.read_text(encoding="utf-8", errors="replace").strip()
        if value.startswith("ref:"):
            branch = value.rsplit("/", 1)[-1]
    tree: list[dict[str, Any]] = []
    for path in local_dir.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        tree.append(
            {
                "path": path.relative_to(local_dir).as_posix(),
                "type": "blob",
                "size": path.stat().st_size,
            }
        )
    return branch, tree


async def _clone_github_repository(repository: str, local_dir: Path, timeout: float) -> None:
    import asyncio
    local_dir.parent.mkdir(parents=True, exist_ok=True)
    if local_dir.exists():
        raise RuntimeError(f"GitHub缓存目录存在但不可用：{local_dir}")
    process = await asyncio.create_subprocess_exec(
        "git",
        "clone",
        "--depth",
        "1",
        f"https://github.com/{repository}.git",
        str(local_dir),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError(f"浅克隆 {repository} 超时") from None
    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"浅克隆 {repository} 失败：{message}")


def _github_query_terms(query: str) -> list[str]:
    terms = re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", query.lower())
    ignored = {"how", "the", "and", "制作", "怎么", "如何", "配方", "recipe", "guide"}
    normalized = [term.replace(" ", "_") for term in terms if term not in ignored]
    ascii_terms = [term for term in normalized if re.fullmatch(r"[a-z0-9_]+", term)]
    if len(ascii_terms) >= 2:
        normalized.insert(0, "_".join(ascii_terms[:3]))
    return list(dict.fromkeys(normalized)) or ["readme"]


def _relevant_file_snippet(text: str, terms: list[str], limit: int) -> str:
    lowered = text.lower()
    position = -1
    for term in terms:
        occurrences = [match.start() for match in re.finditer(re.escape(term), lowered)]
        if not occurrences:
            continue
        position = max(
            occurrences,
            key=lambda pos: _evidence_context_score(lowered[max(0, pos - 450) : pos + 700]),
        )
        break
    start = max(0, position - 350) if position >= 0 else 0
    if position >= 0:
        recipe_start = max(
            text.rfind("allthemods.custom({", 0, position),
            text.rfind("ServerEvents.recipes", 0, position),
        )
        if recipe_start >= 0 and position - recipe_start <= limit - 500:
            start = recipe_start
    snippet = text[start : start + limit]
    if start:
        snippet = "..." + snippet
    if start + limit < len(text):
        snippet += "..."
    return re.sub(r"\x00", "", snippet).strip()


def _evidence_context_score(context: str) -> int:
    score = sum(
        weight
        for marker, weight in {
            "result": 6,
            "output": 6,
            "ingredients": 4,
            "pattern": 3,
            "item:": 2,
            '"item"': 2,
            "tasks": 1,
            "filename": -8,
        }.items()
        if marker in context
    )
    return score


def _target_relation_summary(text: str, exact_term: str) -> str:
    if not exact_term:
        return ""
    escaped = re.escape(exact_term)
    output_patterns = [
        rf"result\s*[:=]\s*\{{[^}}]{{0,600}}{escaped}",
        rf"result\s*[:=][^\n]{{0,500}}{escaped}",
        rf"(?:shaped|shapeless)\s*\(\s*['\"][^'\"]*{escaped}",
        rf"output\s*[:=][^\n]{{0,300}}{escaped}",
    ]
    output_count = sum(len(re.findall(pattern, text, re.IGNORECASE | re.DOTALL)) for pattern in output_patterns)
    input_count = len(re.findall(rf"(?:ingredient|input)[^\n]{{0,300}}{escaped}", text, re.IGNORECASE))
    task_count = len(re.findall(rf"tasks?[\s\S]{{0,500}}{escaped}", text, re.IGNORECASE))
    if output_count:
        return f"检测到目标作为配方输出 {output_count} 次，作为输入 {input_count} 次"
    if input_count:
        return f"目标仅作为输入出现 {input_count} 次；此文件不能证明目标物品的制作配方"
    if task_count:
        return f"目标作为任务要求出现 {task_count} 次；这不是配方输出"
    return "只检测到文本提及，未检测到目标作为配方输出"


def _kubejs_recipe_facts(text: str, exact_term: str) -> list[str]:
    """Extract deterministic ingredient counts from KubeJS mechanical-crafting blocks."""
    facts: list[str] = []
    blocks = re.findall(r"allthemods\.custom\(\{(.*?)\}\)\.id", text, re.DOTALL)
    for index, block in enumerate(blocks, start=1):
        if not re.search(rf"result\s*:[^\n]{{0,500}}{re.escape(exact_term)}", block, re.IGNORECASE):
            continue
        pattern_match = re.search(r"pattern\s*:\s*\[(.*?)\]", block, re.DOTALL)
        key_match = re.search(r"key\s*:\s*\{(.*?)\}\s*,\s*result", block, re.DOTALL)
        if not pattern_match or not key_match:
            continue
        rows = re.findall(r"['\"]([^'\"]+)['\"]", pattern_match.group(1))
        counts = Counter(char for row in rows for char in row if not char.isspace())
        mappings = {
            match.group(1): match.group(2)
            for match in re.finditer(
                r"\b([A-Z])\s*:\s*[^\n]*?['\"]([a-z0-9_.-]+:[a-z0-9_./-]+)['\"]",
                key_match.group(1),
                re.IGNORECASE,
            )
        }
        ingredients = [
            f"{counts[symbol]}x {mappings.get(symbol, '符号 ' + symbol)}"
            for symbol in sorted(counts)
        ]
        if ingredients:
            facts.append(f"配方{index} " + ", ".join(ingredients))
    return facts


# ── Webpage fetch ─────────────────────────────────────────────────────────────

async def fetch_web_page(url: str, timeout: float = 15.0) -> list[SearchResult]:
    """Fetch an HTTP(S) page only through the pinned, bounded public-web transport."""
    safe_url = ""
    try:
        safe_url = redact_url(normalize_url(url).url)
        response = await _SAFE_WEB_FETCHER.get(url, timeout=timeout)
    except (TimeoutError, httpx.TimeoutException):
        return [_failed_result("网页读取", f"读取超时（>{timeout:.0f}s）。", url=safe_url)]
    except URLPolicyError:
        return [_failed_result("网页读取", "目标 URL 或其 DNS 解析结果不符合安全策略。", url=safe_url)]
    except FetchLimitError:
        return [_failed_result("网页读取", "响应超出安全大小限制或使用了不支持的压缩编码。", url=safe_url)]
    except Exception as exc:  # noqa: BLE001
        return [_failed_result("网页读取", f"网页读取失败（{type(exc).__name__}）。", url=safe_url)]

    final_url = redact_url(response.url)
    content_type = response.headers.get("content-type", "").casefold()
    if response.status_code >= 400:
        return [_failed_result("网页读取", f"目标网页返回 HTTP {response.status_code}。", url=final_url)]
    text = response.text

    if "application/json" in content_type or "application/vnd.github+json" in content_type:
        try:
            summary = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except ValueError:
            summary = text
        if len(summary) > 6000:
            summary = summary[:6000].rstrip() + "..."
        return [
            SearchResult(
                title=f"API数据：{urlparse(response.url).path}",
                summary=summary,
                url=final_url,
                source="GitHub API" if urlparse(response.url).hostname == "api.github.com" else "JSON API",
            )
        ]

    if "text/html" not in content_type and "text/plain" not in content_type:
        return [_failed_result("网页读取", f"内容类型 {content_type or '未知'} 不支持读取。", url=final_url)]

    page_title = _extract_title(text) or final_url
    summary = _extract_page_text(text)
    if len(summary) > 1200:
        summary = summary[:1200].rstrip() + "..."
    return [
        SearchResult(
            title=page_title,
            summary=summary or "网页内容为空或无法提取。",
            url=final_url,
            source="网页读取",
        )
    ]


async def _fetch_via_jina(url: str, timeout: float, reason: str) -> list[SearchResult]:
    """Retained as a compatibility shim; unvalidated URLs are never sent to a Reader."""
    del timeout
    return [
        _failed_result(
            "网页读取",
            f"{reason}；远程 Reader fallback 已禁用，未转交目标 URL。",
            url=redact_url(url),
        )
    ]


# ── Weather ───────────────────────────────────────────────────────────────────

async def get_weather(location: str, timeout: float = 10.0) -> list[SearchResult]:
    """Get current weather using Open-Meteo geocoding and forecast APIs."""
    location = location.strip() or "当前地区"
    try:
        async with _external_client(timeout) as client:
            geo_resp = await _get_with_retry(
                client,
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": location, "count": 1, "language": "zh", "format": "json"},
            )
            geo_resp.raise_for_status()
            geo_data: dict[str, Any] = geo_resp.json()
            places = geo_data.get("results") or []
            if not places:
                return [_failed_result("天气", f"找不到地点：{location}。")]
            place = places[0]
            weather_resp = await _get_with_retry(
                client,
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": place["latitude"],
                    "longitude": place["longitude"],
                    "current": (
                        "temperature_2m,apparent_temperature,relative_humidity_2m,"
                        "weather_code,wind_speed_10m"
                    ),
                    "daily": "temperature_2m_max,temperature_2m_min",
                    "timezone": "auto",
                    "forecast_days": 1,
                },
            )
            weather_resp.raise_for_status()
            data: dict[str, Any] = weather_resp.json()
    except Exception as exc:  # noqa: BLE001
        return await _get_weather_via_wttr(location, timeout, str(exc))

    current = data.get("current") if isinstance(data.get("current"), Mapping) else {}
    daily = data.get("daily") if isinstance(data.get("daily"), Mapping) else {}
    area_name = str(place.get("name") or location)
    admin = str(place.get("admin1") or "")
    country = str(place.get("country") or "")
    display_name = "，".join(dict.fromkeys(item for item in [country, admin, area_name] if item))
    code = int(current.get("weather_code") or 0)
    parts = [
        f"地点：{display_name}",
        f"天气：{_weather_code_text(code)}",
        f"气温：{current.get('temperature_2m', '?')}°C，体感：{current.get('apparent_temperature', '?')}°C",
        f"湿度：{current.get('relative_humidity_2m', '?')}%，风速：{current.get('wind_speed_10m', '?')} km/h",
    ]
    min_values = daily.get("temperature_2m_min") or []
    max_values = daily.get("temperature_2m_max") or []
    if min_values and max_values:
        parts.append(f"今日气温范围：{min_values[0]}°C ~ {max_values[0]}°C")
    return [
        SearchResult(
            title=f"{area_name}天气",
            summary="；".join(parts),
            url="https://open-meteo.com/",
            source="Open-Meteo天气",
        )
    ]


async def _get_weather_via_wttr(location: str, timeout: float, primary_error: str) -> list[SearchResult]:
    """Fallback weather source used when either Open-Meteo endpoint is unavailable."""
    try:
        async with _external_client(timeout) as client:
            response = await _get_with_retry(
                client,
                f"https://wttr.in/{location}",
                params={"format": "j1", "lang": "zh"},
            )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
        current = (payload.get("current_condition") or [{}])[0]
        today = (payload.get("weather") or [{}])[0]
        description_rows = current.get("lang_zh") or current.get("weatherDesc") or []
        description = str((description_rows[0] if description_rows else {}).get("value") or "未知")
        summary = "；".join(
            [
                f"地点：{location}",
                f"天气：{description}",
                f"气温：{current.get('temp_C', '?')}°C，体感：{current.get('FeelsLikeC', '?')}°C",
                f"湿度：{current.get('humidity', '?')}%，风速：{current.get('windspeedKmph', '?')} km/h",
                f"今日气温范围：{today.get('mintempC', '?')}°C ~ {today.get('maxtempC', '?')}°C",
            ]
        )
        return [SearchResult(f"{location}天气", summary, "https://wttr.in/", "wttr.in天气")]
    except Exception as fallback_error:  # noqa: BLE001
        return [
            _failed_result(
                "天气",
                f"天气查询失败：Open-Meteo={primary_error}；wttr.in={fallback_error}",
            )
        ]


def _weather_code_text(code: int) -> str:
    if code == 0:
        return "晴"
    if code in {1, 2, 3}:
        return "少云或多云"
    if code in {45, 48}:
        return "雾"
    if code in {51, 53, 55, 56, 57}:
        return "毛毛雨"
    if code in {61, 63, 65, 66, 67, 80, 81, 82}:
        return "有雨"
    if code in {71, 73, 75, 77, 85, 86}:
        return "有雪"
    if code in {95, 96, 99}:
        return "雷雨"
    return f"未知天气代码 {code}"


# ── Internal helpers ──────────────────────────────────────────────────────────

def _failed_result(source: str, message: str, url: str = "") -> SearchResult:
    return SearchResult(title=f"{source}失败", summary=message, url=url, source=source)


def _clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = htmlmod.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_title(text: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    return _clean_html(match.group(1)) if match else ""


def _extract_page_text(text: str) -> str:
    text = re.sub(r"<script\b.*?</script>", " ", text, re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b.*?</style>", " ", text, re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<(br|p|div|li|h[1-6])\b[^>]*>", "\n", text, re.IGNORECASE)
    return _clean_html(text)

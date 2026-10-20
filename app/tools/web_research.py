from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import hashlib
import html
import json
import logging
import math
import re
import time
from typing import Any, Awaitable, Callable, Literal, Protocol, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.models import SearchResult
from app.tools.contracts import Evidence, ToolContext, ToolError, ToolResult, ToolStatus
from app.tools.external import fetch_web_page
from app.tools.url_policy import Resolver, URLPolicyError, normalize_url, redact_url, resolve_approved_target


LOGGER = logging.getLogger(__name__)
SEARCH_ENDPOINT = "https://api.tavily.com/search"
EXTRACT_ENDPOINT = "https://api.tavily.com/extract"
SEARCH_DEPTHS = frozenset({"basic", "fast", "advanced", "ultra-fast"})
TIME_RANGES = frozenset({"day", "week", "month", "year"})
MAX_PROVIDER_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_QUERY_CHARS = 400
MAX_RESULTS_HARD_LIMIT = 10
MAX_DOMAIN_FILTERS = 10
MAX_EXTRACT_URLS = 3
_SENSITIVE_QUERY_NAMES = frozenset({
    "accesstoken", "authtoken", "apikey", "xapikey", "auth", "authorization", "bearer",
    "clientsecret", "credential", "credentials", "key", "password", "passwd", "secret", "sig",
    "signature", "token", "jwt", "session", "sessionid", "subscriptionkey", "xamzcredential",
    "xamzsecuritytoken", "xamzsignature",
})


class WebResearchError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SearchRequest:
    query: str
    topic: Literal["general", "news"] = "general"
    search_depth: str = "basic"
    max_results: int = 5
    time_range: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    include_domains: tuple[str, ...] = ()
    exclude_domains: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        query = self.query.strip()
        if not query or len(query) > MAX_QUERY_CHARS:
            raise ValueError(f"query must contain 1 to {MAX_QUERY_CHARS} characters")
        if self.topic not in {"general", "news"}:
            raise ValueError("topic must be general or news")
        if self.search_depth not in SEARCH_DEPTHS:
            raise ValueError("search_depth is not supported")
        if isinstance(self.max_results, bool) or not 1 <= self.max_results <= MAX_RESULTS_HARD_LIMIT:
            raise ValueError(f"max_results must be between 1 and {MAX_RESULTS_HARD_LIMIT}")
        if self.time_range is not None and self.time_range not in TIME_RANGES:
            raise ValueError("time_range must be day, week, month, or year")
        for field_name in ("start_date", "end_date"):
            value = getattr(self, field_name)
            if value is not None:
                if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError(f"{field_name} must use YYYY-MM-DD")
                try:
                    date.fromisoformat(value)
                except ValueError:
                    raise ValueError(f"{field_name} must be a real calendar date") from None
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        if len(self.include_domains) > MAX_DOMAIN_FILTERS or len(self.exclude_domains) > MAX_DOMAIN_FILTERS:
            raise ValueError(f"at most {MAX_DOMAIN_FILTERS} domains may be specified")
        for domain in (*self.include_domains, *self.exclude_domains):
            if not _valid_domain(domain):
                raise ValueError("domain filters must contain hostnames, not URLs")
        object.__setattr__(self, "query", query)


@dataclass(frozen=True, slots=True)
class WebSearchResult:
    title: str
    url: str
    content: str
    score: float | None
    published_at: str | None
    provider: str
    query: str
    retrieved_at: datetime
    source_class: str = "unknown"
    content_hash: str = ""


@dataclass(frozen=True, slots=True)
class SearchResponse:
    results: tuple[WebSearchResult, ...]
    credits: int | None = None


@dataclass(frozen=True, slots=True)
class ExtractRequest:
    urls: tuple[str, ...]
    query: str | None = None
    extract_depth: Literal["basic", "advanced"] = "basic"
    format: Literal["markdown", "text"] = "markdown"

    def __post_init__(self) -> None:
        if not 1 <= len(self.urls) <= MAX_EXTRACT_URLS:
            raise ValueError(f"between 1 and {MAX_EXTRACT_URLS} URLs are allowed")
        if self.extract_depth not in {"basic", "advanced"}:
            raise ValueError("extract_depth must be basic or advanced")
        if self.format not in {"markdown", "text"}:
            raise ValueError("format must be markdown or text")


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    title: str
    url: str
    content: str
    provider: str
    retrieved_at: datetime
    completeness: Literal["complete", "partial", "reference_only", "unavailable"]
    content_hash: str


@dataclass(frozen=True, slots=True)
class ExtractResponse:
    pages: tuple[ExtractedPage, ...]
    failed_urls: tuple[str, ...] = ()
    credits: int | None = None


class SearchProvider(Protocol):
    provider_id: str

    async def search(self, request: SearchRequest, *, timeout: float) -> SearchResponse: ...

    async def extract(self, request: ExtractRequest, *, timeout: float) -> ExtractResponse: ...


class TavilySearchProvider:
    """Small HTTPX client for the documented Tavily Search and Extract endpoints."""

    provider_id = "tavily"

    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        max_response_bytes: int = MAX_PROVIDER_RESPONSE_BYTES,
    ) -> None:
        self._api_key = api_key.strip()
        self._transport = transport
        self._max_response_bytes = max(1024, max_response_bytes)

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    async def search(self, request: SearchRequest, *, timeout: float) -> SearchResponse:
        payload: dict[str, Any] = {
            "query": request.query,
            "topic": request.topic,
            "search_depth": request.search_depth,
            "max_results": request.max_results,
            "include_answer": False,
            "include_raw_content": False,
            "include_published_date": True,
            "include_usage": True,
        }
        if request.time_range:
            payload["time_range"] = request.time_range
        if request.start_date:
            payload["start_date"] = request.start_date
        if request.end_date:
            payload["end_date"] = request.end_date
        if request.time_range or request.start_date or request.end_date:
            # Tavily documents this as the strict filter; undated pages should
            # not silently satisfy an explicit time-bounded query.
            payload["filter_by_published_date"] = True
        if request.include_domains:
            payload["include_domains"] = list(request.include_domains)
        if request.exclude_domains:
            payload["exclude_domains"] = list(request.exclude_domains)
        data = await self._post_json(SEARCH_ENDPOINT, payload, timeout)
        rows = data.get("results")
        if not isinstance(rows, list):
            raise WebResearchError("invalid_response", "搜索服务返回了无法识别的数据。")
        retrieved_at = datetime.now(timezone.utc)
        results: list[WebSearchResult] = []
        for row in rows[:MAX_RESULTS_HARD_LIMIT]:
            if not isinstance(row, dict):
                continue
            title = _clean_title(row.get("title"))
            content = _clean_snippet(row.get("content"))
            url = row.get("url")
            if not title or not content or not isinstance(url, str):
                continue
            try:
                normalized_url = normalize_web_source_url(url)
            except URLPolicyError:
                continue
            raw_score = row.get("score")
            score = None
            if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
                try:
                    parsed_score = float(raw_score)
                except (OverflowError, ValueError):
                    parsed_score = float("nan")
                if math.isfinite(parsed_score) and 0.0 <= parsed_score <= 1.0:
                    score = parsed_score
            published = row.get("published_date")
            published_at = (
                re.sub(r"[\x00-\x1f\x7f]+", " ", published).strip()[:128]
                if isinstance(published, str) and published.strip() else None
            )
            content_hash = _content_hash(content)
            results.append(
                WebSearchResult(
                    title=title[:500],
                    url=normalized_url,
                    content=content[:6000],
                    score=score,
                    published_at=published_at,
                    provider=self.provider_id,
                    query=request.query,
                    retrieved_at=retrieved_at,
                    source_class=_source_class(normalized_url),
                    content_hash=content_hash,
                )
            )
        credits = _credits(data.get("usage"))
        return SearchResponse(tuple(normalize_search_results(results)), credits)

    async def extract(self, request: ExtractRequest, *, timeout: float) -> ExtractResponse:
        payload: dict[str, Any] = {
            "urls": list(request.urls),
            "extract_depth": request.extract_depth,
            "format": request.format,
            "include_usage": True,
            "timeout": max(1.0, min(60.0, timeout)),
        }
        if request.query:
            payload["query"] = request.query[:MAX_QUERY_CHARS]
            payload["chunks_per_source"] = 3
        data = await self._post_json(EXTRACT_ENDPOINT, payload, timeout)
        rows = data.get("results")
        failures = data.get("failed_results", [])
        if not isinstance(rows, list) or not isinstance(failures, list):
            raise WebResearchError("invalid_response", "网页提取服务返回了无法识别的数据。")

        requested = {normalize_web_source_url(url) for url in request.urls}
        failed_urls: list[str] = []
        for item in failures[:MAX_EXTRACT_URLS]:
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                try:
                    failed_url = normalize_web_source_url(item["url"])
                except URLPolicyError:
                    continue
                if failed_url in requested:
                    failed_urls.append(failed_url)

        retrieved_at = datetime.now(timezone.utc)
        pages: list[ExtractedPage] = []
        seen_urls: set[str] = set()
        for row in rows[:MAX_EXTRACT_URLS]:
            if not isinstance(row, dict) or not isinstance(row.get("url"), str):
                continue
            try:
                url = normalize_web_source_url(row["url"])
            except URLPolicyError:
                continue
            if url not in requested:
                # Never trust a provider response to introduce a different URL.
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            body = _clean_document(row.get("raw_content"))
            if not body:
                failed_urls.append(url)
                continue
            clipped = len(body) > 40_000
            if clipped:
                body = body[:40_000].rstrip() + "\n…[提取内容已截断]"
            pages.append(
                ExtractedPage(
                    title=_clean_title(row.get("title")) or _title_from_url(url),
                    url=url,
                    content=body,
                    provider=self.provider_id,
                    retrieved_at=retrieved_at,
                    completeness="partial" if clipped or request.query else "complete",
                    content_hash=_content_hash(body),
                )
            )
        return ExtractResponse(tuple(pages), tuple(dict.fromkeys(failed_urls)), _credits(data.get("usage")))

    async def _post_json(self, endpoint: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        if not self._api_key:
            raise WebResearchError("not_configured", "Tavily 未配置 API Key。")
        if timeout <= 0:
            raise WebResearchError("timeout", "联网请求已超过剩余时间。")
        # The destination is fixed, redirects are disabled, and environment proxy
        # variables are deliberately ignored so the bearer key cannot be routed
        # through an unconfigured proxy. TLS verification remains HTTPX's default.
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(timeout),
                follow_redirects=False,
                trust_env=False,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            ) as client:
                async with client.stream("POST", endpoint, json=payload) as response:
                    if response.status_code == 401 or response.status_code == 403:
                        raise WebResearchError("auth_failed", "Tavily 拒绝了当前凭据。")
                    if response.status_code == 429:
                        raise WebResearchError("rate_limited", "Tavily 当前请求过于频繁，请稍后重试。")
                    if response.status_code in {432, 433}:
                        raise WebResearchError("quota_exceeded", "Tavily 当前额度或套餐限制了此请求。")
                    if response.status_code >= 500:
                        raise WebResearchError("provider_error", "Tavily 服务暂时不可用。", retryable=True)
                    if response.status_code >= 400:
                        raise WebResearchError("provider_error", "Tavily 无法处理当前请求。")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > self._max_response_bytes:
                            raise WebResearchError("response_too_large", "Tavily 响应超过安全大小限制。")
        except asyncio.CancelledError:
            raise
        except WebResearchError:
            raise
        except httpx.TimeoutException:
            raise WebResearchError("timeout", "Tavily 请求超时。", retryable=True) from None
        except httpx.RequestError:
            raise WebResearchError("network_error", "无法连接 Tavily。", retryable=True) from None
        try:
            decoded = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise WebResearchError("invalid_response", "Tavily 返回了无效 JSON。") from None
        if not isinstance(decoded, dict):
            raise WebResearchError("invalid_response", "Tavily 返回的数据格式无效。")
        return decoded


@dataclass(slots=True)
class WebRequestBudget:
    max_search_calls: int = 3
    max_extract_calls: int = 3
    max_read_calls: int = 3
    max_advanced_searches: int = 1
    search_calls: int = 0
    extract_calls: int = 0
    read_calls: int = 0
    advanced_searches: int = 0
    latest_query: str = ""
    discovered_urls: set[str] = field(default_factory=set)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def reserve_search(self, *, advanced: bool) -> bool:
        async with self._lock:
            if self.search_calls >= self.max_search_calls:
                return False
            if advanced and self.advanced_searches >= self.max_advanced_searches:
                return False
            self.search_calls += 1
            if advanced:
                self.advanced_searches += 1
            return True

    async def reserve_extract(self) -> bool:
        async with self._lock:
            if self.extract_calls >= self.max_extract_calls:
                return False
            self.extract_calls += 1
            return True

    async def reserve_read(self) -> bool:
        async with self._lock:
            if self.read_calls >= self.max_read_calls:
                return False
            self.read_calls += 1
            return True

    async def add_discovered(self, urls: Sequence[str], query: str) -> None:
        async with self._lock:
            self.discovered_urls.update(urls)
            self.latest_query = query[:MAX_QUERY_CHARS]

    async def was_discovered(self, url: str) -> bool:
        async with self._lock:
            return url in self.discovered_urls


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float
    value: object
    size: int


@dataclass(slots=True)
class _Flight:
    task: asyncio.Task[object]
    waiters: int = 0


class WebResponseCache:
    """Bounded process-local TTL cache with request coalescing."""

    def __init__(self, max_entries: int = 256, max_bytes: int = 8 * 1024 * 1024) -> None:
        self.max_entries = max(1, max_entries)
        self.max_bytes = max(1024, max_bytes)
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._flights: dict[str, _Flight] = {}
        self._bytes = 0
        self._lock = asyncio.Lock()
        self.hits = 0
        self.misses = 0
        self.singleflight_waits = 0
        self.expired = 0

    async def get_or_load(self, key: str, ttl: float, loader: Callable[[], Awaitable[object]]) -> object:
        async with self._lock:
            self._prune_locked()
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                self.hits += 1
                return entry.value
            self.misses += 1
            flight = self._flights.get(key)
            if flight is None:
                task = asyncio.create_task(self._load_and_store(key, ttl, loader))
                task.add_done_callback(_consume_task_exception)
                flight = _Flight(task=task)
                self._flights[key] = flight
            else:
                self.singleflight_waits += 1
            flight.waiters += 1
            task = flight.task
        cancelled = False
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            async with self._lock:
                current = self._flights.get(key)
                if current is flight:
                    current.waiters = max(0, current.waiters - 1)
                    if cancelled and current.waiters == 0 and not task.done():
                        task.cancel()

    async def _load_and_store(self, key: str, ttl: float, loader: Callable[[], Awaitable[object]]) -> object:
        try:
            value = await loader()
            size = _estimated_size(value)
            if ttl > 0 and size <= self.max_bytes:
                async with self._lock:
                    previous = self._entries.pop(key, None)
                    if previous is not None:
                        self._bytes -= previous.size
                    while self._entries and (
                        len(self._entries) >= self.max_entries or self._bytes + size > self.max_bytes
                    ):
                        _, evicted = self._entries.popitem(last=False)
                        self._bytes -= evicted.size
                    self._entries[key] = _CacheEntry(time.monotonic() + ttl, value, size)
                    self._bytes += size
            return value
        finally:
            async with self._lock:
                current = self._flights.get(key)
                if current is not None and current.task is asyncio.current_task():
                    del self._flights[key]

    def _prune_locked(self) -> None:
        now = time.monotonic()
        for key in [key for key, value in self._entries.items() if value.expires_at <= now]:
            entry = self._entries.pop(key)
            self._bytes -= entry.size
            self.expired += 1

    async def stats(self) -> dict[str, int]:
        async with self._lock:
            self._prune_locked()
            return {
                "entries": len(self._entries),
                "bytes": self._bytes,
                "hits": self.hits,
                "misses": self.misses,
                "singleflight_waits": self.singleflight_waits,
                "expired": self.expired,
            }


class WebResearchService:
    def __init__(
        self,
        provider: SearchProvider,
        *,
        configured: bool,
        search_depth: str = "basic",
        max_results: int = 5,
        timeout_seconds: float = 12.0,
        max_search_calls_per_request: int = 3,
        max_extract_calls_per_request: int = 3,
        cache: WebResponseCache | None = None,
        resolver: Resolver | None = None,
        local_reader: Callable[[str, float], Awaitable[list[SearchResult]]] = fetch_web_page,
        global_calls_per_minute: int = 60,
        max_concurrent_requests: int = 4,
        generic_cache_ttl: int = 1800,
        news_cache_ttl: int = 300,
        stable_cache_ttl: int = 21600,
        release_cache_ttl: int = 600,
        extract_cache_ttl: int = 3600,
        extract_max_chars: int = 12_000,
    ) -> None:
        self.provider = provider
        self.configured = configured
        self.search_depth = search_depth if search_depth in SEARCH_DEPTHS else "basic"
        self.max_results = max(1, min(MAX_RESULTS_HARD_LIMIT, max_results))
        self.timeout_seconds = max(1.0, min(60.0, timeout_seconds))
        self.max_search_calls_per_request = max(1, min(3, max_search_calls_per_request))
        self.max_extract_calls_per_request = max(1, min(3, max_extract_calls_per_request))
        self.cache = cache or WebResponseCache()
        self.resolver = resolver
        self.local_reader = local_reader
        self.global_calls_per_minute = max(1, global_calls_per_minute)
        self._global_calls: deque[float] = deque()
        self._global_lock = asyncio.Lock()
        self._network_semaphore = asyncio.Semaphore(max(1, max_concurrent_requests))
        self.generic_cache_ttl = max(0, generic_cache_ttl)
        self.news_cache_ttl = max(0, news_cache_ttl)
        self.stable_cache_ttl = max(0, stable_cache_ttl)
        self.release_cache_ttl = max(0, release_cache_ttl)
        self.extract_cache_ttl = max(0, extract_cache_ttl)
        self.extract_max_chars = max(1000, min(40_000, extract_max_chars))
        self._health = "unknown"
        self._health_at: datetime | None = None
        self._reported_credits = 0
        self._unknown_credit_responses = 0

    def new_request_budget(self) -> WebRequestBudget:
        return WebRequestBudget(
            max_search_calls=self.max_search_calls_per_request,
            max_extract_calls=self.max_extract_calls_per_request,
            max_read_calls=self.max_extract_calls_per_request,
        )

    def status_snapshot(self) -> dict[str, object]:
        return {
            "provider": "tavily",
            "configured": self.configured,
            "last_health": self._health,
            "last_verified_at": self._health_at.isoformat() if self._health_at else None,
        }

    async def cache_stats(self) -> dict[str, int]:
        return await self.cache.stats()

    async def search(
        self,
        query: str,
        context: ToolContext,
        *,
        topic: Literal["general", "news"] = "general",
        max_results: int | None = None,
        search_depth: str | None = None,
        time_range: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        include_domains: Sequence[str] = (),
        exclude_domains: Sequence[str] = (),
    ) -> ToolResult:
        if not self.configured:
            return _failure(ToolStatus.DATA_UNAVAILABLE, "not_configured", "Tavily Web Search 未配置（NOT_CONFIGURED）。")
        try:
            original_query = query.strip()
            enhanced_query = enhance_minecraft_query(original_query)
            request = SearchRequest(
                query=enhanced_query,
                topic=topic,
                search_depth=search_depth or self.search_depth,
                max_results=self.max_results if max_results is None else max_results,
                time_range=time_range,
                start_date=start_date,
                end_date=end_date,
                include_domains=tuple(include_domains),
                exclude_domains=tuple(exclude_domains),
            )
        except (AttributeError, TypeError, ValueError):
            return _failure(ToolStatus.INVALID_ARGUMENT, "invalid_arguments", "搜索参数无效；请检查查询、深度、时间范围和域名。")
        budget = self._request_budget(context)
        key = _cache_key("search", request)
        try:
            response = await self.cache.get_or_load(
                key,
                self._search_ttl(request),
                lambda: self._search_with_retry(request, budget, context),
            )
        except asyncio.CancelledError:
            raise
        except WebResearchError as exc:
            self._mark_health(False)
            return _failure(_status_for_error(exc.code), exc.code, exc.message)
        if not isinstance(response, SearchResponse):
            return _failure(ToolStatus.ERROR, "invalid_cached_response", "搜索缓存数据格式无效。")
        normalized = SearchResponse(
            tuple(normalize_search_results(response.results)[:request.max_results]),
            response.credits,
        )
        await budget.add_discovered([row.url for row in normalized.results], original_query)
        return _search_result(normalized, original_query)

    async def news_search(
        self,
        query: str,
        context: ToolContext,
        *,
        max_results: int | None = None,
        time_range: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> ToolResult:
        effective_time_range = time_range
        if effective_time_range is None and start_date is None and end_date is None:
            effective_time_range = "day"
        return await self.search(
            query.strip() or "今日新闻",
            context,
            topic="news",
            max_results=max_results,
            time_range=effective_time_range,
            start_date=start_date,
            end_date=end_date,
        )

    async def read(self, url: str, context: ToolContext) -> ToolResult:
        return await self.read_many((url,), context)

    async def read_many(self, urls: Sequence[str], context: ToolContext) -> ToolResult:
        if not 1 <= len(urls) <= MAX_EXTRACT_URLS:
            return _failure(ToolStatus.INVALID_ARGUMENT, "invalid_arguments", f"一次最多读取 {MAX_EXTRACT_URLS} 个网页。")
        try:
            safe_urls = tuple(dict.fromkeys(normalize_web_source_url(url) for url in urls))
        except (URLPolicyError, TypeError):
            return _failure(ToolStatus.INVALID_ARGUMENT, "invalid_url", "只支持格式有效的公开 HTTP/HTTPS 网页 URL。")
        budget = self._request_budget(context)
        if not await budget.reserve_read():
            return _failure(ToolStatus.DENIED, "budget_exceeded", "本次请求的网页读取次数已达到上限。")
        try:
            timeout = _request_timeout(context, self.timeout_seconds)
        except WebResearchError as exc:
            return _failure(ToolStatus.TIMED_OUT, exc.code, exc.message)
        discovered = all([await budget.was_discovered(url) for url in safe_urls])
        if self.configured and discovered and not any(_has_sensitive_query(url) for url in safe_urls):
            try:
                # Defense in depth before passing a search-discovered URL to the
                # provider-side extractor. The remote fetch happens on Tavily's
                # infrastructure; local direct fetch always uses SafeHTTPFetcher.
                for safe_url in safe_urls:
                    await resolve_approved_target(safe_url, self.resolver)
            except URLPolicyError:
                return await self._read_locally_many(safe_urls, timeout, warning="来源地址未通过公开目标校验")
            try:
                request = ExtractRequest(safe_urls, query=budget.latest_query or None)
                key = _cache_key("extract", request)
                response = await self.cache.get_or_load(
                    key,
                    self.extract_cache_ttl,
                    lambda: self._extract_with_retry(request, budget, context),
                )
                if not isinstance(response, ExtractResponse):
                    return _failure(ToolStatus.ERROR, "invalid_cached_response", "网页提取缓存数据格式无效。")
                if response.pages:
                    found_urls = {page.url for page in response.pages}
                    failed_urls = set(response.failed_urls).intersection(safe_urls)
                    missing = tuple(url for url in safe_urls if url not in found_urls or url in failed_urls)
                    extracted = _extracted_result(response.pages, self.extract_max_chars)
                    if not missing:
                        return extracted
                    local = await self._read_locally_many(
                        missing,
                        timeout,
                        warning="Tavily Extract 部分失败；以下内容由受 SSRF 防护的本地读取器补充",
                    ) if missing else None
                    return _merge_web_results(extracted, local, "Tavily Extract 有部分来源未返回内容。")
                return await self._read_locally_many(
                    safe_urls,
                    timeout,
                    warning="Tavily Extract 未能提取来源；以下内容由受 SSRF 防护的本地读取器提供",
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                self._mark_health(False)
                return _failure(ToolStatus.TIMED_OUT, "timeout", "Tavily 网页提取超时。")
            except WebResearchError as exc:
                self._mark_health(False)
                if exc.code in {
                    "not_configured", "rate_limited", "quota_exceeded", "auth_failed", "timeout",
                    "network_error", "provider_error", "invalid_response",
                }:
                    return await self._read_locally_many(
                        safe_urls,
                        timeout,
                        warning=f"Tavily Extract 不可用（{exc.code}）；以下内容由受 SSRF 防护的本地读取器提供",
                    )
                return _failure(_status_for_error(exc.code), exc.code, exc.message)
        return await self._read_locally_many(safe_urls, timeout)

    async def _read_locally_many(
        self, urls: Sequence[str], timeout: float, warning: str | None = None
    ) -> ToolResult:
        if not urls:
            return _failure(ToolStatus.NO_RESULTS, "no_results", "没有可读取的网页来源。")
        results = await asyncio.gather(
            *(self._read_locally(url, timeout, warning=warning) for url in urls),
            return_exceptions=True,
        )
        successful = [item for item in results if isinstance(item, ToolResult) and item.evidence]
        if not successful:
            failures = [item for item in results if isinstance(item, ToolResult)]
            if failures and all(item.status == ToolStatus.DENIED for item in failures):
                return failures[0]
            if failures and all(item.status == ToolStatus.TIMED_OUT for item in failures):
                return failures[0]
            return _failure(ToolStatus.DATA_UNAVAILABLE, "local_read_failed", "安全网页读取器未能读取任何来源。")
        content = "\n\n".join(
            str(item.content.get("text", "")) for item in successful if isinstance(item.content, dict)
        )
        failed_count = len(urls) - len(successful)
        if warning:
            content = f"{warning}。\n{content}"
        if failed_count:
            content += f"\n\n另有 {failed_count} 个 URL 未能读取。"
        return ToolResult(
            ToolStatus.PARTIAL,
            {"text": content[:self.extract_max_chars * MAX_EXTRACT_URLS]},
            evidence=tuple(evidence for item in successful for evidence in item.evidence),
            error=ToolError("partial_results", "安全本地读取返回的是有界网页片段。"),
            completeness="partial",
        )

    async def _read_locally(self, url: str, timeout: float, warning: str | None = None) -> ToolResult:
        key = f"safe_http:{hashlib.sha256(url.encode('utf-8')).hexdigest()}"

        async def load() -> object:
            result = await self._read_locally_uncached(url, timeout)
            if not result.evidence:
                error = result.error
                raise WebResearchError(
                    error.code if error else "local_read_failed",
                    error.message if error else "安全网页读取器无法读取该来源。",
                )
            return result

        try:
            result = await self.cache.get_or_load(key, self.extract_cache_ttl, load)
        except asyncio.CancelledError:
            raise
        except WebResearchError as exc:
            return _failure(_status_for_error(exc.code), exc.code, exc.message)
        if not isinstance(result, ToolResult):
            return _failure(ToolStatus.ERROR, "invalid_cached_response", "网页读取缓存数据格式无效。")
        if warning:
            return ToolResult(
                result.status,
                _prepend_warning(result.content, warning),
                evidence=result.evidence,
                usage=result.usage,
                error=result.error,
                completeness=result.completeness,
            )
        return result

    async def _read_locally_uncached(self, url: str, timeout: float) -> ToolResult:
        if not await self._take_global_slot(timeout):
            return _failure(ToolStatus.DENIED, "rate_limited", "全局联网请求频率已达到上限，请稍后重试。")
        try:
            async with asyncio.timeout(timeout):
                async with self._network_semaphore:
                    results = await self.local_reader(url, timeout)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return _failure(ToolStatus.TIMED_OUT, "timeout", "网页读取超时。")
        except Exception:
            return _failure(ToolStatus.ERROR, "local_reader_error", "安全网页读取器执行失败。")
        if not results or not results[0].url:
            return _failure(ToolStatus.DATA_UNAVAILABLE, "local_read_failed", "安全网页读取器没有返回可用内容。")
        item = results[0]
        if item.title.endswith("失败"):
            return _failure(ToolStatus.DATA_UNAVAILABLE, "local_read_failed", "安全网页读取器无法读取该来源。")
        content = item.summary[:self.extract_max_chars]
        clipped = len(item.summary) > len(content) or content.endswith("...")
        display_url = redact_url(normalize_web_source_url(item.url))
        evidence = _web_evidence(
            kind="web_page",
            title=item.title,
            url=display_url,
            provider="safe_http",
            retrieved_at=datetime.now(timezone.utc),
            content=content,
            completeness="partial" if clipped else "partial",
            query="",
        )
        retrieved_at = datetime.fromisoformat(evidence.retrieved_at) if evidence.retrieved_at else datetime.now(timezone.utc)
        text = _format_page(item.title, display_url, content, "safe_http", evidence.completeness, retrieved_at)
        return ToolResult(ToolStatus.PARTIAL, {"text": text}, evidence=(evidence,), completeness="partial")

    async def _search_with_retry(
        self,
        request: SearchRequest,
        budget: WebRequestBudget,
        context: ToolContext,
    ) -> SearchResponse:
        for attempt in range(2):
            if not await budget.reserve_search(advanced=request.search_depth == "advanced"):
                raise WebResearchError("budget_exceeded", "本次请求的联网搜索次数已达到上限。")
            timeout = _request_timeout(context, self.timeout_seconds)
            if not await self._take_global_slot(timeout):
                raise WebResearchError("rate_limited", "全局联网请求频率已达到上限，请稍后重试。")
            try:
                async with asyncio.timeout(timeout):
                    async with self._network_semaphore:
                        response = await self.provider.search(request, timeout=timeout)
                self._mark_health(True)
                self._record_credits(response.credits, "search")
                return response
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                raise WebResearchError("timeout", "Tavily 请求超时。", retryable=True) from None
            except WebResearchError as exc:
                if attempt == 0 and exc.retryable and request.search_depth != "advanced":
                    await asyncio.sleep(min(0.15, _request_timeout(context, 1.0) / 4))
                    continue
                raise
        raise WebResearchError("provider_error", "Tavily 搜索失败。")

    async def _extract_with_retry(
        self,
        request: ExtractRequest,
        budget: WebRequestBudget,
        context: ToolContext,
    ) -> ExtractResponse:
        for attempt in range(2):
            if not await budget.reserve_extract():
                raise WebResearchError("budget_exceeded", "本次请求的网页提取次数已达到上限。")
            timeout = _request_timeout(context, self.timeout_seconds)
            if not await self._take_global_slot(timeout):
                raise WebResearchError("rate_limited", "全局联网请求频率已达到上限，请稍后重试。")
            try:
                async with asyncio.timeout(timeout):
                    async with self._network_semaphore:
                        response = await self.provider.extract(request, timeout=timeout)
                self._mark_health(True)
                self._record_credits(response.credits, "extract")
                return response
            except asyncio.CancelledError:
                raise
            except WebResearchError as exc:
                if attempt == 0 and exc.retryable:
                    await asyncio.sleep(min(0.15, _request_timeout(context, 1.0) / 4))
                    continue
                raise
        raise WebResearchError("provider_error", "Tavily 网页提取失败。")

    async def _take_global_slot(self, timeout: float) -> bool:
        async with self._global_lock:
            now = time.monotonic()
            while self._global_calls and now - self._global_calls[0] >= 60:
                self._global_calls.popleft()
            if len(self._global_calls) >= self.global_calls_per_minute:
                return False
            if timeout <= 0:
                return False
            self._global_calls.append(now)
            return True

    def _request_budget(self, context: ToolContext) -> WebRequestBudget:
        budget = context.extensions.get("web_research_budget")
        if not isinstance(budget, WebRequestBudget):
            budget = self.new_request_budget()
            # Command contexts contain a per-request mutable extensions dict. The
            # Agent also injects one shared budget before dispatching parallel tools.
            if isinstance(context.extensions, dict):
                context.extensions["web_research_budget"] = budget
        return budget

    def _search_ttl(self, request: SearchRequest) -> int:
        if (
            request.topic == "news"
            or request.time_range in {"day", "week"}
            or request.start_date is not None
            or request.end_date is not None
        ):
            return self.news_cache_ttl
        query = request.query.casefold()
        if re.search(r"\b(?:release|version|latest|current|update|changelog|发布|版本|更新|最新)\b", query):
            return self.release_cache_ttl
        if re.search(r"\b(?:docs?|documentation|api|wiki|manual|specification)\b|文档|手册|语法", query):
            return self.stable_cache_ttl
        return self.generic_cache_ttl

    def _mark_health(self, success: bool) -> None:
        self._health = "healthy" if success else "unhealthy"
        self._health_at = datetime.now(timezone.utc)

    def _record_credits(self, credits: int | None, endpoint: str) -> None:
        if credits is None:
            self._unknown_credit_responses += 1
            reported: int | str = "unknown"
        else:
            self._reported_credits += credits
            reported = credits
        LOGGER.info("web provider request endpoint=%s provider=tavily credits=%s", endpoint, reported)


def enhance_minecraft_query(query: str) -> str:
    """Append small deterministic English hints without discarding user wording."""
    lowered = query.casefold()
    if not any(term in lowered for term in ("minecraft", "gtnh", "gregtech", "sfm", "modpack", "整合包", "模组")):
        return query
    terms: list[str] = []
    for marker, phrase in (
        ("铂", "platinum"),
        ("产线", "processing line"),
        ("流程", "processing flow"),
        ("教程", "guide"),
        ("配方", "recipe"),
        ("机器", "machine"),
    ):
        if marker in query:
            terms.append(phrase)
    if "gtnh" in lowered or "gregtech new horizons" in lowered:
        terms.append("GregTech New Horizons")
    suffix = " ".join(dict.fromkeys(terms))
    if not suffix or suffix.casefold() in lowered:
        return query
    return f"{query} {suffix}"[:MAX_QUERY_CHARS]


def normalize_web_source_url(raw_url: str) -> str:
    normalized = normalize_url(raw_url).url
    parsed = urlsplit(normalized)
    tracking = {"gclid", "fbclid", "dclid", "msclkid", "mc_cid", "mc_eid", "ref", "source"}
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in tracking and not key.casefold().startswith("utm_")
    ]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query, doseq=True), ""))


def _has_sensitive_query(url: str) -> bool:
    try:
        return any(
            re.sub(r"[^a-z0-9]", "", key.casefold()) in _SENSITIVE_QUERY_NAMES
            for key, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True)
        )
    except (TypeError, ValueError):
        return True


def normalize_search_results(results: Sequence[WebSearchResult]) -> list[WebSearchResult]:
    by_url: dict[str, WebSearchResult] = {}
    fingerprints: set[str] = set()
    for result in results:
        try:
            url = normalize_web_source_url(result.url)
        except (URLPolicyError, TypeError):
            continue
        if _has_sensitive_query(url):
            continue
        current = by_url.get(url)
        candidate = result if result.url == url else WebSearchResult(
            result.title, url, result.content, result.score, result.published_at,
            result.provider, result.query, result.retrieved_at, result.source_class, result.content_hash,
        )
        candidate_score = candidate.score if candidate.score is not None else -1.0
        current_score = current.score if current is not None and current.score is not None else -1.0
        if current is None or candidate_score > current_score:
            by_url[url] = candidate
    output: list[WebSearchResult] = []
    for result in sorted(
        by_url.values(),
        key=lambda row: (-(row.score if row.score is not None else -1.0), -_source_priority(row.source_class)),
    ):
        fingerprint = hashlib.sha256(
            f"{_normalize_text(result.title)}\n{_normalize_text(result.content)}".encode("utf-8")
        ).hexdigest()
        if fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        output.append(result)
        if len(output) >= MAX_RESULTS_HARD_LIMIT:
            break
    return output


def _search_result(response: SearchResponse, original_query: str) -> ToolResult:
    rows = list(response.results)
    if not rows:
        return _failure(ToolStatus.NO_RESULTS, "no_results", "Tavily 搜索没有返回有效来源；未生成联网答案。")
    evidence = tuple(
        _web_evidence(
            kind="web_search_result",
            title=row.title,
            url=row.url,
            provider=row.provider,
            retrieved_at=row.retrieved_at,
            published_at=row.published_at,
            content=row.content,
            completeness="partial",
            query=original_query,
        )
        for row in rows
    )
    lines = [f"互联网来源（Tavily；查询：{original_query}）："]
    for index, row in enumerate(rows, start=1):
        published = f"；发布时间：{row.published_at}" if row.published_at else "；发布时间：未知"
        score = f"；Tavily 相关度分数：{row.score:.3f}（不是可信度评分）" if row.score is not None else ""
        lines.append(
            f"[{index}] {row.title}\n来源类型：{row.source_class}；抓取时间：{row.retrieved_at.isoformat()}{published}{score}"
            f"\n摘要片段（partial）：{row.content}\nURL：{row.url}"
        )
    text = "\n\n".join(lines)
    return ToolResult(ToolStatus.OK, {"text": text}, evidence=evidence, completeness="partial")


def _extracted_result(pages: Sequence[ExtractedPage], max_chars: int) -> ToolResult:
    lines: list[str] = []
    evidence: list[Evidence] = []
    partial = False
    for page in pages[:MAX_EXTRACT_URLS]:
        content = page.content
        clipped = len(content) > max_chars
        if clipped:
            content = content[:max_chars].rstrip() + "\n…[正文已按配置截断]"
        completeness = "partial" if clipped or page.completeness != "complete" else "complete"
        partial = partial or completeness != "complete"
        evidence.append(
            _web_evidence(
                kind="web_page",
                title=page.title,
                url=page.url,
                provider=page.provider,
                retrieved_at=page.retrieved_at,
                content=content,
                completeness=completeness,
                query="",
                content_hash=page.content_hash,
            )
        )
        lines.append(_format_page(page.title, page.url, content, page.provider, completeness, page.retrieved_at))
    if not lines:
        return _failure(ToolStatus.NO_RESULTS, "no_results", "Tavily Extract 未返回可用正文。")
    return ToolResult(
        ToolStatus.PARTIAL if partial else ToolStatus.OK,
        {"text": "\n\n".join(lines)},
        evidence=tuple(evidence),
        completeness="partial" if partial else "complete",
    )


def _merge_web_results(first: ToolResult, second: ToolResult | None, message: str) -> ToolResult:
    parts = [str(first.content.get("text", ""))] if isinstance(first.content, dict) else [str(first.content)]
    evidence = list(first.evidence)
    if second is not None:
        if isinstance(second.content, dict):
            parts.append(str(second.content.get("text", "")))
        else:
            parts.append(str(second.content))
        evidence.extend(second.evidence)
    return ToolResult(
        ToolStatus.PARTIAL,
        {"text": f"{message}\n\n" + "\n\n".join(part for part in parts if part)},
        evidence=tuple(evidence),
        error=ToolError("partial_results", message),
        completeness="partial",
    )


def _format_page(
    title: str,
    url: str,
    content: str,
    provider: str,
    completeness: str,
    retrieved_at: datetime,
) -> str:
    return (
        f"网页正文（{provider}；完整性：{completeness}）\n标题：{title}\nURL：{url}\n"
        f"抓取时间：{retrieved_at.isoformat()}\n正文：\n{content}"
    )


def _web_evidence(
    *,
    kind: str,
    title: str,
    url: str,
    provider: str,
    retrieved_at: datetime,
    content: str,
    completeness: Literal["complete", "partial", "reference_only", "unavailable"],
    query: str,
    published_at: str | None = None,
    content_hash: str | None = None,
) -> Evidence:
    conditions = [("provider", provider)]
    if query:
        conditions.append(("query", query[:MAX_QUERY_CHARS]))
    return Evidence(
        kind=kind,
        source_id=f"web:{hashlib.sha256(url.encode('utf-8')).hexdigest()[:24]}",
        title=title[:500],
        locator=url,
        provider=provider,
        retrieved_at=retrieved_at.isoformat(),
        published_at=published_at,
        completeness=completeness,
        content_hash=content_hash or _content_hash(content),
        query_conditions=tuple(conditions),
    )


def _failure(status: ToolStatus, code: str, message: str) -> ToolResult:
    return ToolResult(status, message, error=ToolError(code, message))


def _status_for_error(code: str) -> ToolStatus:
    return {
        "not_configured": ToolStatus.DATA_UNAVAILABLE,
        "rate_limited": ToolStatus.DENIED,
        "quota_exceeded": ToolStatus.DENIED,
        "budget_exceeded": ToolStatus.DENIED,
        "timeout": ToolStatus.TIMED_OUT,
        "cancelled": ToolStatus.CANCELLED,
        "invalid_arguments": ToolStatus.INVALID_ARGUMENT,
        "invalid_url": ToolStatus.INVALID_ARGUMENT,
        "no_results": ToolStatus.NO_RESULTS,
        "partial_results": ToolStatus.PARTIAL,
    }.get(code, ToolStatus.ERROR)


def _prepend_warning(content: str | dict[str, Any], warning: str) -> dict[str, str]:
    text = content.get("text", "") if isinstance(content, dict) else str(content)
    return {"text": f"{warning}\n{text}"[:40_000]}


def _as_partial(evidence: Evidence) -> Evidence:
    from dataclasses import replace

    return replace(evidence, completeness="partial")


def _request_timeout(context: ToolContext, configured: float) -> float:
    timeout = min(configured, context.tool_timeout_seconds or configured)
    if context.deadline_monotonic is not None:
        timeout = min(timeout, context.deadline_monotonic - asyncio.get_running_loop().time())
    if timeout <= 0:
        raise WebResearchError("timeout", "联网请求已超过 Agent 总时限。")
    return timeout


def _cache_key(kind: str, request: SearchRequest | ExtractRequest) -> str:
    if isinstance(request, SearchRequest):
        payload = {
            "provider": "tavily",
            "query": _normalize_text(request.query),
            "topic": request.topic,
            "depth": request.search_depth,
            "max_results": request.max_results,
            "time_range": request.time_range,
            "start_date": request.start_date,
            "end_date": request.end_date,
            "filter_by_published_date": bool(request.time_range or request.start_date or request.end_date),
            "include_domains": sorted(item.casefold() for item in request.include_domains),
            "exclude_domains": sorted(item.casefold() for item in request.exclude_domains),
            "include_answer": False,
            "include_raw_content": False,
        }
    else:
        payload = {
            "provider": "tavily",
            "urls": sorted(normalize_web_source_url(item) for item in request.urls),
            "query": _normalize_text(request.query or ""),
            "extract_depth": request.extract_depth,
            "format": request.format,
        }
    return f"{kind}:{hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}"


def _clean_title(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _clean_snippet(value: object) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = html.unescape(value)
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _clean_document(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = html.unescape(value.replace("\x00", ""))
    text = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"[\t\r ]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def _valid_domain(value: str) -> bool:
    if not isinstance(value, str) or not value or len(value) > 253 or "/" in value or ":" in value:
        return False
    try:
        hostname = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return False
    return bool(re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", hostname))


def _source_class(url: str) -> str:
    host = (urlsplit(url).hostname or "").casefold()
    if host in {"github.com", "docs.github.com", "developer.mozilla.org", "minecraft.wiki", "wiki.gtnewhorizons.com"}:
        return "primary_or_official"
    if host.startswith("docs.") or host.startswith("wiki."):
        return "documentation_or_wiki"
    if host in {"reddit.com", "www.reddit.com", "discord.com", "forums.minecraftforge.net"}:
        return "community"
    return "unknown"


def _source_priority(source_class: str) -> int:
    return {"primary_or_official": 2, "documentation_or_wiki": 1, "unknown": 0, "community": -1}.get(source_class, 0)


def _title_from_url(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] or (urlsplit(url).hostname or url)


def _content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _credits(value: object) -> int | None:
    if not isinstance(value, dict):
        return None
    credits = value.get("credits")
    if isinstance(credits, bool) or not isinstance(credits, int) or credits < 0:
        return None
    return credits


def _estimated_size(value: object) -> int:
    return len(repr(value).encode("utf-8", errors="replace"))


def _consume_task_exception(task: asyncio.Task[object]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        pass

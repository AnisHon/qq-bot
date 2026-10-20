from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import unittest
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx

from app.config import Settings
from app.models import SearchResult
from app.tools.contracts import ToolContext, ToolStatus
from app.tools.web_research import (
    EXTRACT_ENDPOINT,
    SEARCH_ENDPOINT,
    ExtractRequest,
    ExtractResponse,
    ExtractedPage,
    SearchRequest,
    SearchResponse,
    TavilySearchProvider,
    WebRequestBudget,
    WebResearchError,
    WebResearchService,
    WebResponseCache,
    WebSearchResult,
    enhance_minecraft_query,
    normalize_search_results,
    normalize_web_source_url,
)


def result(
    url: str = "https://example.org/article",
    *,
    title: str = "Fixture page",
    content: str = "Fixture source content",
    score: float | None = 0.8,
    published_at: str | None = "2026-10-01",
) -> WebSearchResult:
    return WebSearchResult(
        title=title,
        url=url,
        content=content,
        score=score,
        published_at=published_at,
        provider="tavily",
        query="fixture query",
        retrieved_at=datetime.now(timezone.utc),
    )


def context(*, extensions: dict[str, Any] | None = None, deadline: float | None = None) -> ToolContext:
    return ToolContext(
        request_id="web-test",
        group_id="test-group",
        user_id="test-user",
        deadline_monotonic=deadline,
        tool_timeout_seconds=2,
        extensions=extensions if extensions is not None else {},
    )


class FakeResolver:
    def __init__(self, addresses: tuple[str, ...] = ("8.8.8.8",)) -> None:
        self.addresses = addresses
        self.calls: list[tuple[str, int]] = []

    async def resolve(self, hostname: str, port: int):
        self.calls.append((hostname, port))
        return self.addresses


class FakeProvider:
    provider_id = "fake"

    def __init__(self, searches=None, extracts=None) -> None:
        self.searches: list[SearchRequest] = []
        self.extracts: list[ExtractRequest] = []
        self.search_responses = list(searches or [])
        self.extract_responses = list(extracts or [])

    async def search(self, request: SearchRequest, *, timeout: float) -> SearchResponse:
        self.searches.append(request)
        value = self.search_responses.pop(0) if self.search_responses else SearchResponse(())
        if isinstance(value, Exception):
            raise value
        return value

    async def extract(self, request: ExtractRequest, *, timeout: float) -> ExtractResponse:
        self.extracts.append(request)
        value = self.extract_responses.pop(0) if self.extract_responses else ExtractResponse(())
        if isinstance(value, Exception):
            raise value
        return value


class TavilyProviderTest(unittest.IsolatedAsyncioTestCase):
    async def test_search_uses_bearer_and_documented_payload_and_maps_evidence_fields(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "results": [{
                        "title": "GTNH guide",
                        "url": "https://wiki.gtnewhorizons.com/guide?utm_source=fixture#part",
                        "content": "Platinum processing steps",
                        "score": 0.91,
                        "published_date": "2026-10-01",
                    }],
                    "usage": {"credits": 1},
                    "answer": "must not be used as evidence",
                },
            )

        provider = TavilySearchProvider("test-key", transport=httpx.MockTransport(handler))
        response = await provider.search(
            SearchRequest(
                "GTNH 铂处理",
                topic="general",
                search_depth="basic",
                max_results=5,
                time_range="week",
                start_date="2026-09-01",
                end_date="2026-10-01",
                include_domains=("wiki.gtnewhorizons.com",),
                exclude_domains=("reddit.com",),
            ),
            timeout=1,
        )

        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), SEARCH_ENDPOINT)
        self.assertEqual(requests[0].headers["Authorization"], "Bearer test-key")
        payload = json.loads(requests[0].content)
        self.assertEqual(payload["topic"], "general")
        self.assertEqual(payload["search_depth"], "basic")
        self.assertEqual(payload["max_results"], 5)
        self.assertEqual(payload["time_range"], "week")
        self.assertEqual(payload["start_date"], "2026-09-01")
        self.assertEqual(payload["end_date"], "2026-10-01")
        self.assertTrue(payload["filter_by_published_date"])
        self.assertEqual(payload["include_domains"], ["wiki.gtnewhorizons.com"])
        self.assertEqual(payload["exclude_domains"], ["reddit.com"])
        self.assertFalse(payload["include_answer"])
        self.assertFalse(payload["include_raw_content"])
        self.assertEqual(response.credits, 1)
        self.assertEqual(len(response.results), 1)
        item = response.results[0]
        self.assertEqual(item.url, "https://wiki.gtnewhorizons.com/guide")
        self.assertEqual(item.published_at, "2026-10-01")
        self.assertNotEqual(item.retrieved_at.isoformat(), item.published_at)

    async def test_search_without_usage_marks_credits_unknown(self) -> None:
        provider = TavilySearchProvider(
            "test-key",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"results": []})),
        )
        response = await provider.search(SearchRequest("fixture"), timeout=1)
        self.assertIsNone(response.credits)
        self.assertEqual(response.results, ())

    async def test_search_result_with_sensitive_query_url_is_excluded(self) -> None:
        provider = TavilySearchProvider(
            "test-key",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
                "results": [{
                    "title": "Credential URL", "url": "https://example.org/?access_token=fixture",
                    "content": "body", "score": 0.8,
                }],
            })),
        )
        response = await provider.search(SearchRequest("fixture"), timeout=1)
        self.assertEqual(response.results, ())

    async def test_malformed_score_is_not_used_as_relatedness(self) -> None:
        provider = TavilySearchProvider(
            "test-key",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
                "results": [{
                    "title": "Page", "url": "https://example.org/page", "content": "body",
                    "score": 10 ** 400,
                }],
            })),
        )
        response = await provider.search(SearchRequest("fixture"), timeout=1)
        self.assertIsNone(response.results[0].score)

    async def test_missing_key_fails_without_request(self) -> None:
        provider = TavilySearchProvider("", transport=httpx.MockTransport(lambda _: self.fail("network")))
        self.assertFalse(provider.configured)
        with self.assertRaises(WebResearchError) as raised:
            await provider.search(SearchRequest("fixture"), timeout=1)
        self.assertEqual(raised.exception.code, "not_configured")

    async def test_http_error_classes_and_malformed_json(self) -> None:
        expected = {401: "auth_failed", 403: "auth_failed", 429: "rate_limited", 432: "quota_exceeded",
                    433: "quota_exceeded", 503: "provider_error"}
        for status, code in expected.items():
            provider = TavilySearchProvider(
                "test-key", transport=httpx.MockTransport(lambda _, status=status: httpx.Response(status, text="{}"))
            )
            with self.subTest(status=status), self.assertRaises(WebResearchError) as raised:
                await provider.search(SearchRequest("fixture"), timeout=1)
            self.assertEqual(raised.exception.code, code)
        provider = TavilySearchProvider(
            "test-key", transport=httpx.MockTransport(lambda _: httpx.Response(200, text="not json"))
        )
        with self.assertRaises(WebResearchError) as raised:
            await provider.search(SearchRequest("fixture"), timeout=1)
        self.assertEqual(raised.exception.code, "invalid_response")

    async def test_bad_response_shape_and_oversized_body_are_rejected(self) -> None:
        provider = TavilySearchProvider(
            "test-key", transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"results": {}}))
        )
        with self.assertRaises(WebResearchError) as raised:
            await provider.search(SearchRequest("fixture"), timeout=1)
        self.assertEqual(raised.exception.code, "invalid_response")

        provider = TavilySearchProvider(
            "test-key",
            max_response_bytes=1024,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 2048)),
        )
        with self.assertRaises(WebResearchError) as raised:
            await provider.search(SearchRequest("fixture"), timeout=1)
        self.assertEqual(raised.exception.code, "response_too_large")

    async def test_timeout_and_cancellation_are_not_silently_converted(self) -> None:
        def timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("fixture timeout", request=request)

        provider = TavilySearchProvider("test-key", transport=httpx.MockTransport(timeout_handler))
        with self.assertRaises(WebResearchError) as raised:
            await provider.search(SearchRequest("fixture"), timeout=1)
        self.assertEqual(raised.exception.code, "timeout")
        self.assertTrue(raised.exception.retryable)

        gate = asyncio.Event()

        async def blocked(request: httpx.Request) -> httpx.Response:
            await gate.wait()
            return httpx.Response(200, json={"results": []})

        provider = TavilySearchProvider("test-key", transport=httpx.MockTransport(blocked))
        task = asyncio.create_task(provider.search(SearchRequest("fixture"), timeout=10))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_extract_maps_partial_results_and_rejects_unrequested_urls(self) -> None:
        url = "https://example.org/a"
        foreign = "https://attacker.example/injected"
        request_seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            request_seen.append(json.loads(request.content))
            return httpx.Response(200, json={
                "results": [
                    {"url": url, "title": "Page A", "raw_content": "A正文"},
                    {"url": foreign, "title": "Foreign", "raw_content": "not allowed"},
                ],
                "failed_results": [
                    {"url": "https://example.org/b", "error": "unavailable"},
                    {"url": foreign, "error": "unavailable"},
                ],
                "usage": {"credits": 2},
            })

        provider = TavilySearchProvider("test-key", transport=httpx.MockTransport(handler))
        response = await provider.extract(
            ExtractRequest((url, "https://example.org/b"), query="fixture query"), timeout=3,
        )
        self.assertEqual(request_seen[0]["urls"], [url, "https://example.org/b"])
        self.assertEqual(request_seen[0]["format"], "markdown")
        self.assertEqual(request_seen[0]["query"], "fixture query")
        self.assertEqual(len(response.pages), 1)
        self.assertEqual(response.pages[0].content, "A正文")
        self.assertEqual(response.pages[0].completeness, "partial")
        self.assertEqual(response.failed_urls, ("https://example.org/b",))
        self.assertEqual(response.credits, 2)

    async def test_search_parameter_validation(self) -> None:
        for kwargs in (
            {"query": ""},
            {"query": "x", "search_depth": "magic"},
            {"query": "x", "max_results": 11},
            {"query": "x", "time_range": "forever"},
            {"query": "x", "start_date": "2026-02-30"},
            {"query": "x", "start_date": "2026-02-01", "end_date": "2026-01-31"},
            {"query": "x", "include_domains": ("https://example.org",)},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                SearchRequest(**kwargs)


class WebResponseCacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_ttl_cache_and_singleflight(self) -> None:
        cache = WebResponseCache(max_entries=2, max_bytes=4096)
        gate = asyncio.Event()
        calls = 0

        async def loader():
            nonlocal calls
            calls += 1
            await gate.wait()
            return {"text": "same result"}

        first = asyncio.create_task(cache.get_or_load("same", 10, loader))
        await asyncio.sleep(0)
        second = asyncio.create_task(cache.get_or_load("same", 10, loader))
        await asyncio.sleep(0)
        gate.set()
        self.assertEqual(await asyncio.gather(first, second), [{"text": "same result"}] * 2)
        self.assertEqual(calls, 1)
        self.assertEqual(await cache.get_or_load("same", 10, loader), {"text": "same result"})
        stats = await cache.stats()
        self.assertEqual(stats["hits"], 1)
        self.assertGreaterEqual(stats["singleflight_waits"], 1)

    async def test_different_keys_and_ttl_expiry_do_not_share_results(self) -> None:
        cache = WebResponseCache(max_entries=2, max_bytes=4096)
        calls = 0

        async def loader():
            nonlocal calls
            calls += 1
            return calls

        self.assertEqual(await cache.get_or_load("params-a", 0.01, loader), 1)
        self.assertEqual(await cache.get_or_load("params-b", 1, loader), 2)
        await asyncio.sleep(0.02)
        self.assertEqual(await cache.get_or_load("params-a", 1, loader), 3)
        self.assertEqual((await cache.stats())["expired"], 1)

    async def test_cancellation_of_only_waiter_cancels_loader(self) -> None:
        cache = WebResponseCache()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def loader():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(cache.get_or_load("cancel", 1, loader))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        self.assertTrue(cancelled.is_set())


class WebResearchServiceTest(unittest.IsolatedAsyncioTestCase):
    def service(self, provider: FakeProvider, **kwargs):
        return WebResearchService(
            provider,
            configured=True,
            resolver=kwargs.pop("resolver", FakeResolver()),
            local_reader=kwargs.pop("local_reader", AsyncMock(return_value=[])),
            generic_cache_ttl=kwargs.pop("generic_cache_ttl", 0),
            stable_cache_ttl=0,
            release_cache_ttl=0,
            news_cache_ttl=0,
            extract_cache_ttl=kwargs.pop("extract_cache_ttl", 0),
            **kwargs,
        )

    async def test_search_normalizes_sources_enhances_minecraft_and_returns_evidence(self) -> None:
        fake = FakeProvider(searches=[SearchResponse((
            result("https://example.org/a?utm_campaign=x", title="A"),
            result("https://example.org/a", title="A duplicate", score=0.3),
            result("https://wiki.gtnewhorizons.com/guide", title="GTNH", content="guide", score=0.8),
        ), credits=1)])
        service = self.service(fake)
        ctx = context()
        tool_result = await service.search("GTNH 铂处理产线教程", ctx, exclude_domains=("reddit.com",))
        self.assertEqual(tool_result.status, ToolStatus.OK)
        self.assertEqual(len(tool_result.evidence), 2)
        self.assertEqual(fake.searches[0].query, "GTNH 铂处理产线教程 platinum processing line guide GregTech New Horizons")
        self.assertEqual(fake.searches[0].exclude_domains, ("reddit.com",))
        self.assertTrue(all(item.completeness == "partial" for item in tool_result.evidence))
        self.assertEqual(len({item.locator for item in tool_result.evidence}), 2)

    async def test_conflicting_pages_remain_separate_sources(self) -> None:
        provider = FakeProvider(searches=[SearchResponse((
            result("https://official.example/version", title="Version notice", content="Release date is June.", score=0.8),
            result("https://community.example/thread", title="Version notice", content="Release date is July.", score=0.7),
        ))])
        service = self.service(provider)
        response = await service.search("release date", context())
        self.assertEqual(len(response.evidence), 2)
        body = response.content["text"]
        self.assertIn("Release date is June.", body)
        self.assertIn("Release date is July.", body)

    async def test_minecraft_query_enhancement_does_not_rewrite_unrelated_queries(self) -> None:
        self.assertEqual(enhance_minecraft_query("请解释铂处理产线"), "请解释铂处理产线")
        self.assertIn("platinum processing line", enhance_minecraft_query("GTNH 铂处理产线"))

    async def test_unconfigured_and_empty_results_are_explicit(self) -> None:
        service = WebResearchService(FakeProvider(), configured=False)
        missing = await service.search("current issue", context())
        self.assertEqual(missing.error.code, "not_configured")
        service = self.service(FakeProvider(searches=[SearchResponse(())]))
        empty = await service.search("no source", context())
        self.assertEqual(empty.status, ToolStatus.NO_RESULTS)
        self.assertFalse(empty.evidence)
        self.assertIn("未生成联网答案", empty.content)

    async def test_news_uses_news_topic_and_short_default_time_range(self) -> None:
        provider = FakeProvider(searches=[SearchResponse((result(),))])
        service = self.service(provider)
        await service.news_search("GregTech news", context())
        self.assertEqual(provider.searches[0].topic, "news")
        self.assertEqual(provider.searches[0].time_range, "day")

    async def test_news_explicit_date_range_does_not_add_conflicting_default_day(self) -> None:
        provider = FakeProvider(searches=[SearchResponse((result(),))])
        service = self.service(provider)
        await service.news_search(
            "GregTech news", context(), start_date="2026-09-01", end_date="2026-10-01",
        )
        self.assertIsNone(provider.searches[0].time_range)
        self.assertEqual(provider.searches[0].start_date, "2026-09-01")
        self.assertEqual(provider.searches[0].end_date, "2026-10-01")

    async def test_request_search_and_advanced_limits_include_retries(self) -> None:
        provider = FakeProvider(searches=[
            WebResearchError("network_error", "offline", retryable=True),
            SearchResponse((result("https://example.org/one"),)),
        ])
        service = self.service(provider)
        ctx = context()
        await service.search("retry-1", ctx)
        self.assertEqual(len(provider.searches), 2)
        self.assertEqual(ctx.extensions["web_research_budget"].search_calls, 2)

        provider = FakeProvider(searches=[SearchResponse((result("https://example.org/a"),))])
        service = self.service(provider, max_search_calls_per_request=1)
        ctx = context()
        await service.search("advanced", ctx, search_depth="advanced")
        denied = await service.search("second advanced", ctx, search_depth="advanced")
        self.assertEqual(denied.error.code, "budget_exceeded")
        self.assertEqual(len(provider.searches), 1)

    async def test_deadline_expiration_returns_timeout_without_provider_call(self) -> None:
        provider = FakeProvider()
        service = self.service(provider)
        response = await service.search("deadline", context(deadline=time.monotonic() - 1))
        self.assertEqual(response.status, ToolStatus.TIMED_OUT)
        self.assertEqual(provider.searches, [])

    async def test_provider_timeout_and_quota_failures_are_explicit_and_bounded(self) -> None:
        for code, status in (("timeout", ToolStatus.TIMED_OUT), ("quota_exceeded", ToolStatus.DENIED),
                             ("rate_limited", ToolStatus.DENIED)):
            provider = FakeProvider(searches=[WebResearchError(code, "provider failure")])
            service = self.service(provider)
            response = await service.search(f"failure {code}", context())
            self.assertEqual(response.status, status)
            self.assertEqual(response.error.code, code)

    async def test_cache_parameter_separation_and_hits(self) -> None:
        provider = FakeProvider(searches=[SearchResponse((result(),)), SearchResponse((result(title="other"),))])
        cache = WebResponseCache()
        service = self.service(provider, cache=cache, generic_cache_ttl=60)
        first = await service.search("same query", context())
        cached = await service.search("same query", context())
        filtered = await service.search("same query", context(), include_domains=("example.org",))
        self.assertEqual(len(provider.searches), 2)
        self.assertEqual(first.evidence, cached.evidence)
        self.assertNotEqual(first.evidence, filtered.evidence)
        self.assertEqual((await cache.stats())["hits"], 1)

    async def test_web_read_extracts_only_discovered_public_urls_and_maps_evidence(self) -> None:
        url = "https://example.org/article"
        page_content = "Full article text"
        page = ExtractedPage(
            "Article", url, page_content, "tavily", datetime.now(timezone.utc), "complete",
            hashlib.sha256(page_content.encode()).hexdigest(),
        )
        provider = FakeProvider(
            searches=[SearchResponse((result(url),))],
            extracts=[ExtractResponse((page,), credits=1)],
        )
        service = self.service(provider, resolver=FakeResolver())
        ctx = context()
        await service.search("article details", ctx)
        read = await service.read(url, ctx)
        self.assertEqual(read.status, ToolStatus.OK)
        self.assertEqual(provider.extracts[0].urls, (url,))
        self.assertEqual(read.evidence[0].provider, "tavily")
        self.assertEqual(read.evidence[0].completeness, "complete")
        self.assertEqual(read.evidence[0].locator, url)
        self.assertEqual(read.evidence[0].content_hash, page.content_hash)

    async def test_direct_url_uses_safe_local_reader_and_rejects_private_url(self) -> None:
        local = AsyncMock(return_value=[SearchResult("Direct", "bounded body", "https://example.org/direct", "safe")])
        service = self.service(FakeProvider(), local_reader=local)
        direct = await service.read("https://example.org/direct", context())
        self.assertEqual(direct.status, ToolStatus.PARTIAL)
        self.assertEqual(local.await_count, 1)
        denied = await service.read("http://127.0.0.1/admin", context())
        self.assertEqual(denied.error.code, "invalid_url")
        self.assertEqual(local.await_count, 1)

    async def test_local_fetch_failure_is_not_promoted_to_page_evidence(self) -> None:
        local = AsyncMock(return_value=[SearchResult(
            "网页读取失败", "目标网页返回 HTTP 502。", "https://example.org/down", "网页读取",
        )])
        service = self.service(FakeProvider(), local_reader=local)
        response = await service.read("https://example.org/down", context())
        self.assertEqual(response.status, ToolStatus.DATA_UNAVAILABLE)
        self.assertFalse(response.evidence)
        self.assertNotIn("502", response.content)

    async def test_local_reads_share_the_global_rate_limit(self) -> None:
        local = AsyncMock(return_value=[SearchResult("Page", "body", "https://example.org/a", "safe")])
        service = self.service(FakeProvider(), local_reader=local, global_calls_per_minute=1)
        first = await service.read("https://example.org/a", context())
        second = await service.read("https://example.org/b", context())
        self.assertEqual(first.status, ToolStatus.PARTIAL)
        self.assertEqual(second.error.code, "rate_limited")
        self.assertEqual(local.await_count, 1)

    async def test_page_read_operations_are_bounded_per_agent_request(self) -> None:
        local = AsyncMock(side_effect=lambda url, timeout: [SearchResult("Page", "body", url, "safe")])
        service = self.service(FakeProvider(), local_reader=local, max_extract_calls_per_request=1)
        ctx = context()
        first = await service.read("https://example.org/a", ctx)
        second = await service.read("https://example.org/b", ctx)
        self.assertEqual(first.status, ToolStatus.PARTIAL)
        self.assertEqual(second.error.code, "budget_exceeded")
        self.assertEqual(local.await_count, 1)

    async def test_successful_local_page_is_cached_by_normalized_url(self) -> None:
        calls: list[str] = []

        async def reader(url: str, timeout: float):
            calls.append(url)
            return [SearchResult("Page", "body", url, "safe")]

        service = self.service(FakeProvider(), local_reader=reader, extract_cache_ttl=60)
        first = await service.read("https://example.org/a?utm_source=x", context())
        cached = await service.read("https://example.org/a", context())
        separate = await service.read("https://example.org/b", context())
        self.assertEqual(len(calls), 2)
        self.assertEqual(first.evidence[0].locator, cached.evidence[0].locator)
        self.assertEqual(separate.evidence[0].locator, "https://example.org/b")

    async def test_local_failures_are_not_cached(self) -> None:
        local = AsyncMock(return_value=[SearchResult(
            "网页读取失败", "unavailable", "https://example.org/down", "网页读取",
        )])
        service = self.service(FakeProvider(), local_reader=local, extract_cache_ttl=60)
        await service.read("https://example.org/down", context())
        await service.read("https://example.org/down", context())
        self.assertEqual(local.await_count, 2)

    async def test_sensitive_url_uses_local_reader_and_redacts_evidence_url(self) -> None:
        url = "https://example.org/article?access_token=fixture-value"
        provider = FakeProvider(extracts=[ExtractResponse((ExtractedPage(
            "should not be returned", url, "remote body", "fake", datetime.now(timezone.utc), "complete", "h",
        ),))])
        local = AsyncMock(return_value=[SearchResult("Article", "local body", url, "safe")])
        service = self.service(provider, local_reader=local)
        ctx = context()
        budget = WebRequestBudget()
        await budget.add_discovered([normalize_web_source_url(url)], "fixture")
        ctx.extensions["web_research_budget"] = budget
        response = await service.read(url, ctx)
        self.assertEqual(provider.extracts, [])
        self.assertEqual(local.await_count, 1)
        self.assertEqual(response.evidence[0].locator, "https://example.org/article?access_token=REDACTED")
        self.assertNotIn("fixture-value", response.content["text"])

    async def test_untrusted_failed_remote_extract_falls_back_to_safe_local_reader(self) -> None:
        url = "https://example.org/a"
        provider = FakeProvider(
            searches=[SearchResponse((result(url),))],
            extracts=[WebResearchError("rate_limited", "limited")],
        )
        local = AsyncMock(return_value=[SearchResult("A", "bounded local body", url, "safe")])
        service = self.service(provider, local_reader=local)
        ctx = context()
        await service.search("extract fallback", ctx)
        response = await service.read(url, ctx)
        self.assertEqual(response.status, ToolStatus.PARTIAL)
        self.assertEqual(local.await_count, 1)
        self.assertEqual(response.evidence[0].provider, "safe_http")

    async def test_partial_extract_keeps_available_pages_and_marks_partial(self) -> None:
        urls = ("https://example.org/a", "https://example.org/b")
        page = ExtractedPage("A", urls[0], "body", "tavily", datetime.now(timezone.utc), "complete", "hash")
        provider = FakeProvider(
            searches=[SearchResponse((
                result(urls[0], title="A", content="body A"),
                result(urls[1], title="B", content="body B"),
            ))],
            extracts=[ExtractResponse((page,), failed_urls=(urls[1],))],
        )
        local = AsyncMock(return_value=[])
        service = self.service(provider, local_reader=local)
        ctx = context()
        await service.search("partial", ctx)
        response = await service.read_many(urls, ctx)
        self.assertEqual(response.status, ToolStatus.PARTIAL)
        self.assertEqual(response.evidence[0].locator, urls[0])
        self.assertIn("部分来源", response.content["text"])


class URLAndDomainNormalizationTest(unittest.TestCase):
    def test_url_normalization_removes_tracking_parameters_and_fragments(self) -> None:
        self.assertEqual(
            normalize_web_source_url("https://example.org/a?utm_source=x&keep=1#section"),
            "https://example.org/a?keep=1",
        )

    def test_dedup_prefers_higher_score_and_removes_duplicate_content(self) -> None:
        rows = normalize_search_results([
            result("https://example.org/a?utm_source=one", title="same", score=0.3),
            result("https://example.org/a", title="better", score=0.9),
            result("https://elsewhere.org/b", title="better", score=0.5),
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].title, "better")

    def test_sensitive_query_urls_are_not_search_evidence(self) -> None:
        self.assertEqual(
            normalize_search_results([result("https://example.org/?api-key=fixture")]),
            [],
        )


class WebResearchConfigTest(unittest.TestCase):
    def test_missing_key_keeps_bot_unconfigured_and_limits_are_bounded(self) -> None:
        with patch.dict(os.environ, {
            "TAVILY_API_KEY": "",
            "TAVILY_MAX_RESULTS": "999",
            "WEB_SEARCH_MAX_CALLS_PER_REQUEST": "999",
            "WEB_EXTRACT_MAX_CALLS_PER_REQUEST": "0",
        }, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.tavily_api_key, "")
        self.assertEqual(settings.tavily_max_results, 10)
        self.assertEqual(settings.web_search_max_calls_per_request, 3)
        self.assertEqual(settings.web_extract_max_calls_per_request, 1)
        self.assertFalse(Settings.__dataclass_fields__["tavily_api_key"].repr)


if __name__ == "__main__":
    unittest.main()

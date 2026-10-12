from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, urlparse, unquote

import httpx

from app.models import SearchResult


def _clean_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _unwrap_url(url: str) -> str:
    url = html.unescape(url)
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.query:
        query = parse_qs(parsed.query)
        for key in ("uddg", "u"):
            if key in query and query[key]:
                return unquote(query[key][0])
    return url


def _is_mcmod_page(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not (host == "mcmod.cn" or host.endswith(".mcmod.cn")):
        return False
    path = parsed.path.rstrip("/")
    return path not in {"", "/"}


def _append_result(results: list[SearchResult], seen: set[str], title: str, url: str, limit: int) -> None:
    if len(results) >= limit:
        return
    if not title or not _is_mcmod_page(url) or url in seen:
        return
    seen.add(url)
    results.append(
        SearchResult(
            title=title,
            summary="我的世界百科搜索结果",
            url=url,
            source="MC百科",
        )
    )


def _extract_direct_results(body: str, limit: int) -> list[SearchResult]:
    results: list[SearchResult] = []
    seen: set[str] = set()
    anchor_pattern = re.compile(
        r"<a[^>]+href=\"(?P<href>https?://(?:www\.)?mcmod\.cn/[^\"]+)\"[^>]*>(?P<title>.*?)</a>",
        re.IGNORECASE | re.DOTALL,
    )
    for match in anchor_pattern.finditer(body):
        href = match.group("href")
        title = _clean_text(match.group("title"))
        if "javascript:" in href:
            continue
        _append_result(results, seen, title, href, limit)
        if len(results) >= limit:
            break
    return results


def _extract_search_engine_results(body: str, limit: int) -> list[SearchResult]:
    results: list[SearchResult] = []
    seen: set[str] = set()
    patterns = [
        re.compile(r"<a[^>]+class=\"result__a\"[^>]+href=\"(?P<href>[^\"]+)\"[^>]*>(?P<title>.*?)</a>", re.IGNORECASE | re.DOTALL),
        re.compile(r"<a[^>]+rel=\"nofollow\"[^>]+href=\"(?P<href>[^\"]+)\"[^>]*>(?P<title>.*?)</a>", re.IGNORECASE | re.DOTALL),
    ]
    for pattern in patterns:
        for match in pattern.finditer(body):
            href = _unwrap_url(match.group("href"))
            title = _clean_text(match.group("title"))
            _append_result(results, seen, title, href, limit)
            if len(results) >= limit:
                return results
    return results


async def search_mcbaike(query: str, timeout: float = 15.0, limit: int = 3) -> list[SearchResult]:
    headers = {"User-Agent": "Mozilla/5.0"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        try:
            resp = await client.get("https://search.mcmod.cn/s", params={"key": query})
            if resp.status_code == 200:
                results = _extract_direct_results(resp.text, limit)
                if results:
                    return results
        except Exception:
            pass

        for search_query in (f"MC百科 {query} mcmod", f"mcmod {query} 我的世界百科"):
            try:
                ddg = await client.get("https://html.duckduckgo.com/html/", params={"q": search_query})
                if ddg.status_code == 200:
                    results = _extract_search_engine_results(ddg.text, limit)
                    if results:
                        return results
            except Exception:
                pass

    return []

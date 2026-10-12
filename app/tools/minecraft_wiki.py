from __future__ import annotations

import html
import re
from urllib.parse import quote

import httpx

from app.models import SearchResult


def _clean_summary(summary: str) -> str:
    summary = re.sub(r"<[^>]+>", "", summary)
    summary = html.unescape(summary)
    summary = re.sub(r"\s+", " ", summary).strip()
    return summary


async def search_minecraft_wiki(query: str, timeout: float = 15.0, limit: int = 3) -> list[SearchResult]:
    url = "https://minecraft.wiki/api.php"
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": str(limit),
        "format": "json",
        "utf8": "1",
    }
    headers = {"User-Agent": "qq-deepseek-bot/0.1"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

    results: list[SearchResult] = []
    for item in data.get("query", {}).get("search", [])[:limit]:
        title = str(item.get("title", "")).strip()
        snippet = _clean_summary(str(item.get("snippet", "")))
        results.append(
            SearchResult(
                title=title or query,
                summary=snippet or "Minecraft Wiki 搜索结果",
                url=f"https://minecraft.wiki/w/{quote(title.replace(' ', '_'))}" if title else "https://minecraft.wiki/",
                source="Minecraft Wiki",
            )
        )
    return results


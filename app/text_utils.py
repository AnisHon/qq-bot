from __future__ import annotations

import re


def split_qq_text(text: str, max_chars: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    parts: list[str] = []
    current = ""
    paragraphs = re.split(r"(\n+)", text)
    for item in paragraphs:
        if not item:
            continue
        candidate = current + item
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current.strip():
            parts.append(current.strip())
        current = ""
        if len(item) <= max_chars:
            current = item
            continue
        parts.extend(_split_long_run(item, max_chars))

    if current.strip():
        parts.append(current.strip())
    return parts


def _split_long_run(text: str, max_chars: int) -> list[str]:
    chunks: list[str] = []
    remaining = text.strip()
    while len(remaining) > max_chars:
        cut = _find_cut_point(remaining, max_chars)
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _find_cut_point(text: str, max_chars: int) -> int:
    window = text[:max_chars]
    for pattern in ["。", "！", "？", "\n", "；", ";", ".", "!", "?", "，", ",", " "]:
        idx = window.rfind(pattern)
        if idx >= max_chars // 2:
            return idx + 1
    return max_chars

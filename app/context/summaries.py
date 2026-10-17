from __future__ import annotations

import json
from typing import Any

from app.context.contracts import SummarySnapshot


class SummaryOutputError(ValueError):
    """Model output did not satisfy the structured summary contract."""


def parse_episode_summary(raw: str, snapshot: SummarySnapshot, *, max_chars: int) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 64_000:
        raise SummaryOutputError("summary response is missing or too large")

    def reject_constant(value: str) -> None:
        raise SummaryOutputError(f"invalid JSON constant: {value}")

    try:
        value = json.loads(raw, parse_constant=reject_constant)
    except (json.JSONDecodeError, SummaryOutputError) as exc:
        raise SummaryOutputError("summary response must be a JSON object") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise SummaryOutputError("unsupported summary schema")
    if set(value) != {"schema_version", "topic", "summary", "decisions", "open_questions"}:
        raise SummaryOutputError("summary object has missing or unknown fields")
    topic = value.get("topic")
    summary = value.get("summary")
    if not isinstance(topic, str) or len(topic) > 160:
        raise SummaryOutputError("summary topic is invalid")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > max_chars:
        raise SummaryOutputError("summary text is empty or exceeds its configured limit")
    allowed_ids = {int(row["id"]) for row in snapshot.rows}
    normalized: dict[str, Any] = {
        "schema_version": 1,
        "topic": topic.strip(),
        "summary": summary.strip(),
    }
    for field in ("decisions", "open_questions"):
        entries = value.get(field)
        if not isinstance(entries, list) or len(entries) > 12:
            raise SummaryOutputError(f"{field} must be a bounded array")
        cleaned: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"text", "source_row_ids"}:
                raise SummaryOutputError(f"{field} contains an invalid entry")
            text = entry.get("text")
            row_ids = entry.get("source_row_ids")
            if not isinstance(text, str) or not text.strip() or len(text) > 500:
                raise SummaryOutputError(f"{field} entry text is invalid")
            if not isinstance(row_ids, list) or not row_ids or len(row_ids) > 8:
                raise SummaryOutputError(f"{field} entry must cite source rows")
            ids: list[int] = []
            for row_id in row_ids:
                if isinstance(row_id, bool) or not isinstance(row_id, int) or row_id not in allowed_ids:
                    raise SummaryOutputError(f"{field} cites a row outside the summary snapshot")
                if row_id not in ids:
                    ids.append(row_id)
            cleaned.append({"text": text.strip(), "source_row_ids": ids})
        normalized[field] = cleaned
    return normalized

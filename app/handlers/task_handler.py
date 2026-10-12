from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence


def _truncate_context_text(text: str, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


@dataclass(frozen=True, slots=True)
class ParsedTaskSchedule:
    next_run_at: int
    interval_seconds: int
    schedule_type: str
    prompt: str


def _parse_duration(value: str) -> int | None:
    match = re.fullmatch(
        r"(?P<num>\d+)\s*(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hour|hours|d|day|days)",
        value.strip(),
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    amount = int(match.group("num"))
    unit = match.group("unit").lower()
    if unit.startswith("s"):
        return amount
    if unit.startswith("m"):
        return amount * 60
    if unit.startswith("h") or unit == "hr":
        return amount * 3600
    return amount * 86400


def _parse_task_schedule(payload: str, now: float | None = None) -> ParsedTaskSchedule | None:
    payload = payload.strip()
    if not payload:
        return None
    base_now = now if now is not None else time.time()
    now_dt = datetime.fromtimestamp(base_now)

    in_match = re.match(r"^in\s+(?P<duration>\S+)\s+(?P<prompt>.+)$", payload, flags=re.IGNORECASE | re.DOTALL)
    if in_match:
        seconds = _parse_duration(in_match.group("duration"))
        prompt = in_match.group("prompt").strip()
        if seconds is None or not prompt:
            return None
        return ParsedTaskSchedule(
            next_run_at=int(base_now + max(seconds, 1)),
            interval_seconds=0,
            schedule_type="once",
            prompt=prompt,
        )

    every_match = re.match(r"^every\s+(?P<duration>\S+)\s+(?P<prompt>.+)$", payload, flags=re.IGNORECASE | re.DOTALL)
    if every_match:
        seconds = _parse_duration(every_match.group("duration"))
        prompt = every_match.group("prompt").strip()
        if seconds is None or not prompt:
            return None
        seconds = max(seconds, 60)
        return ParsedTaskSchedule(
            next_run_at=int(base_now + seconds),
            interval_seconds=seconds,
            schedule_type="interval",
            prompt=prompt,
        )

    at_match = re.match(
        r"^at\s+(?P<date>\d{4}-\d{1,2}-\d{1,2})[ T](?P<time>\d{1,2}:\d{2}(?::\d{2})?)\s+(?P<prompt>.+)$",
        payload,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if at_match:
        date_text = at_match.group("date")
        time_text = at_match.group("time")
        fmt = "%Y-%m-%d %H:%M:%S" if time_text.count(":") == 2 else "%Y-%m-%d %H:%M"
        run_at = datetime.strptime(f"{date_text} {time_text}", fmt)
        prompt = at_match.group("prompt").strip()
        return ParsedTaskSchedule(int(run_at.timestamp()), 0, "once", prompt) if prompt else None

    today_match = re.match(
        r"^at\s+(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\s+(?P<prompt>.+)$",
        payload,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if today_match:
        time_text = today_match.group("time")
        parts = [int(item) for item in time_text.split(":")]
        hour, minute = parts[0], parts[1]
        second = parts[2] if len(parts) > 2 else 0
        run_at = now_dt.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if run_at.timestamp() <= base_now:
            run_at += timedelta(days=1)
        prompt = today_match.group("prompt").strip()
        return ParsedTaskSchedule(int(run_at.timestamp()), 0, "once", prompt) if prompt else None

    # /task add daily 08:30 <prompt> — repeat every 24 h starting at given clock time
    daily_match = re.match(
        r"^daily\s+(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\s+(?P<prompt>.+)$",
        payload,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if daily_match:
        time_text = daily_match.group("time")
        parts = [int(item) for item in time_text.split(":")]
        hour, minute = parts[0], parts[1]
        second = parts[2] if len(parts) > 2 else 0
        run_at = now_dt.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if run_at.timestamp() <= base_now:
            run_at += timedelta(days=1)
        prompt = daily_match.group("prompt").strip()
        if not prompt:
            return None
        return ParsedTaskSchedule(
            next_run_at=int(run_at.timestamp()),
            interval_seconds=86400,
            schedule_type="daily",
            prompt=prompt,
        )

    return None


def _format_time(ts: int) -> str:
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")


def _format_duration(seconds: int) -> str:
    seconds = int(seconds)
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _format_task_list(rows: Sequence[dict[str, str]]) -> str:
    if not rows:
        return "当前没有定时任务。"
    lines = ["当前定时任务："]
    for row in rows:
        prompt = _truncate_context_text(str(row["prompt"]), 80)
        interval = int(row.get("interval_seconds") or 0)
        stype = str(row.get("schedule_type") or "")
        if stype == "daily" or interval == 86400:
            schedule = "daily"
        elif interval > 0:
            schedule = f"every {_format_duration(interval)}"
        else:
            schedule = "once"
        lines.append(f"#{row['id']} {schedule} 下次：{_format_time(int(row['next_run_at']))}，提示词：{prompt}")
    return "\n".join(lines)

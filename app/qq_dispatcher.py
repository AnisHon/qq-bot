from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
import time

from app.natcapqq_adapter import NatcapQQAdapter
from app.text_utils import split_qq_text


@dataclass(slots=True)
class EventDeduplicator:
    ttl: float = 120.0
    _seen: dict[str, float] = field(default_factory=dict, init=False)

    def accept(self, message_id: str) -> bool:
        if not message_id:
            return True
        now = time.monotonic()
        expired = [key for key, timestamp in self._seen.items() if now - timestamp > self.ttl]
        for key in expired:
            self._seen.pop(key, None)
        if message_id in self._seen:
            return False
        self._seen[message_id] = now
        return True


@dataclass(slots=True)
class QQMessageDispatcher:
    adapter: NatcapQQAdapter
    max_chars: int
    part_delay: float = 0.45
    duplicate_window: float = 8.0
    status_window: float = 2.0
    _locks: dict[str, asyncio.Lock] = field(default_factory=lambda: defaultdict(asyncio.Lock), init=False)
    _recent: dict[tuple[str, str], float] = field(default_factory=dict, init=False)
    _last_status: dict[str, float] = field(default_factory=dict, init=False)

    async def send(
        self,
        group_id: str,
        text: str,
        *,
        reply_to: str = "",
        deduplicate: bool = True,
        status: bool = False,
    ) -> bool:
        cleaned = text.strip()
        if not cleaned:
            return False
        key = (str(group_id), cleaned)
        now = time.monotonic()
        group_key = str(group_id)
        if status and now - self._last_status.get(group_key, -10_000) < self.status_window:
            return False
        if deduplicate and now - self._recent.get(key, -10_000) < self.duplicate_window:
            return False
        if status:
            self._last_status[group_key] = now
        self._recent[key] = now

        async with self._locks[group_key]:
            parts = split_qq_text(cleaned, self.max_chars)
            for index, part in enumerate(parts):
                await self.adapter.send_group_message(
                    group_id,
                    part,
                    reply_to=reply_to if index == 0 else "",
                )
                if index < len(parts) - 1:
                    await asyncio.sleep(self.part_delay)
        return True

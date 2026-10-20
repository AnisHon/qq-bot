from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
import time
from typing import AsyncIterator

from app.natcapqq_adapter import NatcapQQAdapter
from app.text_utils import split_qq_text


class DeliveryResult(StrEnum):
    SENT = "sent"  # Adapter accepted the HTTP action; client display is not confirmed.
    SUPPRESSED = "suppressed"


@dataclass(slots=True)
class EventDeduplicator:
    ttl: float = 120.0
    _seen: dict[str, float] = field(default_factory=dict, init=False)

    def accept(self, message_id: str) -> bool:
        if not message_id:
            return True
        if self.contains(message_id):
            return False
        self.mark(message_id)
        return True

    def contains(self, message_id: str) -> bool:
        if not message_id:
            return False
        now = time.monotonic()
        expired = [key for key, timestamp in self._seen.items() if now - timestamp > self.ttl]
        for key in expired:
            self._seen.pop(key, None)
        return message_id in self._seen

    def mark(self, message_id: str) -> None:
        if not message_id:
            return
        now = time.monotonic()
        expired = [key for key, timestamp in self._seen.items() if now - timestamp > self.ttl]
        for key in expired:
            self._seen.pop(key, None)
        self._seen[message_id] = now


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

    @asynccontextmanager
    async def delivery_barrier(self, group_id: str) -> AsyncIterator[None]:
        """Serialize all outbound messages for a group, including reset fencing."""
        async with self._locks[str(group_id)]:
            yield

    async def send(
        self,
        group_id: str,
        text: str,
        *,
        reply_to: str = "",
        deduplicate: bool = True,
        status: bool = False,
    ) -> bool:
        async with self.delivery_barrier(group_id):
            result = await self.send_locked(
                group_id, text, reply_to=reply_to, deduplicate=deduplicate, status=status
            )
        return result == DeliveryResult.SENT

    async def send_locked(
        self,
        group_id: str,
        text: str,
        *,
        reply_to: str = "",
        deduplicate: bool = True,
        status: bool = False,
    ) -> DeliveryResult:
        """Send while the caller holds delivery_barrier(group_id)."""
        cleaned = text.strip()
        if not cleaned:
            return DeliveryResult.SUPPRESSED
        key = (str(group_id), cleaned)
        now = time.monotonic()
        group_key = str(group_id)
        if status and now - self._last_status.get(group_key, -10_000) < self.status_window:
            return DeliveryResult.SUPPRESSED
        if deduplicate and now - self._recent.get(key, -10_000) < self.duplicate_window:
            return DeliveryResult.SUPPRESSED
        if status:
            self._last_status[group_key] = now
        self._recent[key] = now
        parts = split_qq_text(cleaned, self.max_chars)
        for index, part in enumerate(parts):
            await self.adapter.send_group_message(
                group_id,
                part,
                reply_to=reply_to if index == 0 else "",
            )
            if index < len(parts) - 1:
                await asyncio.sleep(self.part_delay)
        return DeliveryResult.SENT

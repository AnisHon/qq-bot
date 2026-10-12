from __future__ import annotations

import asyncio
import unittest

from app.qq_dispatcher import EventDeduplicator, QQMessageDispatcher


class _FakeAdapter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send_group_message(self, group_id, text, reply_to="") -> None:
        self.sent.append((str(group_id), text, reply_to))


class DispatcherTest(unittest.TestCase):
    def test_event_deduplicator(self) -> None:
        dedupe = EventDeduplicator()
        self.assertTrue(dedupe.accept("1"))
        self.assertFalse(dedupe.accept("1"))
        self.assertTrue(dedupe.accept("2"))

    def test_long_reply_only_quotes_first_part(self) -> None:
        adapter = _FakeAdapter()
        dispatcher = QQMessageDispatcher(adapter=adapter, max_chars=20, part_delay=0)  # type: ignore[arg-type]
        asyncio.run(dispatcher.send("100", "第一句。" * 20, reply_to="88"))
        self.assertGreater(len(adapter.sent), 1)
        self.assertEqual(adapter.sent[0][2], "88")
        self.assertTrue(all(reply_to == "" for _, _, reply_to in adapter.sent[1:]))

    def test_duplicate_and_rapid_status_messages_are_suppressed(self) -> None:
        adapter = _FakeAdapter()
        dispatcher = QQMessageDispatcher(adapter=adapter, max_chars=100, part_delay=0, status_window=10)  # type: ignore[arg-type]
        async def run() -> None:
            self.assertTrue(await dispatcher.send("100", "查询中", status=True))
            self.assertFalse(await dispatcher.send("100", "查询中", status=True))
            self.assertFalse(await dispatcher.send("100", "正在读取网页", status=True))
            self.assertTrue(await dispatcher.send("100", "最终答案"))
        asyncio.run(run())
        self.assertEqual([item[1] for item in adapter.sent], ["查询中", "最终答案"])


if __name__ == "__main__":
    unittest.main()

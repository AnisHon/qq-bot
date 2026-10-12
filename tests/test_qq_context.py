from __future__ import annotations

import unittest

from app.models import GroupMessageEvent, QQMention, QuotedMessage
from app.qq_context import format_mentions, format_quote, format_recent_messages, select_recent_context


class QQContextTest(unittest.TestCase):
    def test_active_thread_is_preferred_over_group_noise(self) -> None:
        rows = [
            {"role": "user", "user_id": "other1", "nickname": "a", "content": "旁支1"},
            {"role": "user", "user_id": "current", "nickname": "me", "content": "问题1"},
            {"role": "assistant", "user_id": "bot", "nickname": "bot", "content": "回答1"},
            {"role": "user", "user_id": "other2", "nickname": "b", "content": "旁支2"},
            {"role": "user", "user_id": "other3", "nickname": "c", "content": "旁支3"},
            {"role": "user", "user_id": "current", "nickname": "me", "content": "问题2"},
        ]
        selected = select_recent_context(rows, "current", 5)
        contents = [row["content"] for row in selected]
        self.assertIn("问题1", contents)
        self.assertIn("回答1", contents)
        self.assertIn("问题2", contents)
        self.assertLessEqual(sum(item.startswith("旁支") for item in contents), 2)

    def test_format_recent_messages_produces_multiturn_roles(self) -> None:
        rows = [
            {"role": "user", "user_id": "111", "nickname": "alice", "content": "第一问"},
            {"role": "assistant", "user_id": "bot", "nickname": "bot", "content": "第一答"},
        ]
        turns = format_recent_messages(rows)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["role"], "user")
        self.assertIn("第一问", turns[0]["content"])
        self.assertIn("alice", turns[0]["content"])
        self.assertEqual(turns[1]["role"], "assistant")
        self.assertEqual(turns[1]["content"], "第一答")

    def test_quote_and_mentions_are_explicitly_labeled(self) -> None:
        event = GroupMessageEvent(
            "1", "100", "200", "alice", "怎么看", False, 0, {},
            mentions=(QQMention("300", "bob"),),
            quote=QuotedMessage("9", "400", "charlie", "原消息"),
        )
        self.assertIn("primary referent", format_quote(event))
        self.assertIn("原消息", format_quote(event))
        self.assertIn("bob", format_mentions(event))


if __name__ == "__main__":
    unittest.main()

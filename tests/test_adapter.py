from __future__ import annotations

import unittest

from app.natcapqq_adapter import NatcapQQAdapter


class NatcapQQAdapterTest(unittest.TestCase):
    def test_array_message_group_event(self) -> None:
        adapter = NatcapQQAdapter("http://127.0.0.1:3000", bot_qq_id="123")
        event = adapter.parse_group_event(
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": 10,
                "group_id": 20,
                "user_id": 30,
                "time": 40,
                "sender": {"nickname": "alice"},
                "message": [
                    {"type": "at", "data": {"qq": "123"}},
                    {"type": "text", "data": {"text": " 你好 "}},
                ],
            }
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.text, "你好")
        self.assertTrue(event.is_at_bot)

    def test_reply_and_other_mentions_are_preserved(self) -> None:
        adapter = NatcapQQAdapter("http://127.0.0.1:3000", bot_qq_id="123")
        event = adapter.parse_group_event(
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": 10,
                "group_id": 20,
                "user_id": 30,
                "sender": {"nickname": "alice"},
                "message": [
                    {"type": "reply", "data": {"id": "99"}},
                    {"type": "at", "data": {"qq": "456", "name": "bob"}},
                    {"type": "text", "data": {"text": "怎么看"}},
                ],
            }
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.reply_message_id, "99")
        self.assertEqual(event.mentions[0].user_id, "456")
        self.assertEqual(event.mentions[0].display, "bob")

    def test_bare_reply_segment_is_not_dropped(self) -> None:
        adapter = NatcapQQAdapter("http://127.0.0.1:3000", bot_qq_id="123")
        event = adapter.parse_group_event(
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": 10,
                "group_id": 20,
                "user_id": 30,
                "message": [{"type": "reply", "data": {"id": "99"}}],
            }
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.reply_message_id, "99")

    def test_self_message_is_ignored(self) -> None:
        adapter = NatcapQQAdapter("http://127.0.0.1:3000", bot_qq_id="123")
        event = adapter.parse_group_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 20,
                "user_id": 123,
                "message": "self echo",
            }
        )
        self.assertIsNone(event)

    def test_cq_string_at_with_extra_params(self) -> None:
        adapter = NatcapQQAdapter("http://127.0.0.1:3000", bot_qq_id="123")
        event = adapter.parse_group_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": "20",
                "user_id": "30",
                "message": "[CQ:at,qq=123,name=bot] 查一下钻石",
                "sender": {"card": "alice"},
            }
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertTrue(event.is_at_bot)
        self.assertEqual(event.text, "查一下钻石")

    def test_array_message_attachment(self) -> None:
        adapter = NatcapQQAdapter("http://127.0.0.1:3000", bot_qq_id="123")
        event = adapter.parse_group_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": "20",
                "user_id": "30",
                "message": [
                    {"type": "at", "data": {"qq": "123"}},
                    {"type": "image", "data": {"file": "pic.jpg", "url": "https://example.com/pic.jpg"}},
                ],
                "sender": {"card": "alice"},
            }
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertTrue(event.is_at_bot)
        self.assertEqual(event.text, "")
        self.assertEqual(event.attachments[0].type, "image")
        self.assertEqual(event.attachments[0].name, "pic.jpg")

    def test_cq_string_attachment(self) -> None:
        adapter = NatcapQQAdapter("http://127.0.0.1:3000", bot_qq_id="123")
        event = adapter.parse_group_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": "20",
                "user_id": "30",
                "message": "[CQ:at,qq=123][CQ:image,file=pic.jpg,url=https://example.com/pic.jpg]",
                "sender": {"card": "alice"},
            }
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertTrue(event.is_at_bot)
        self.assertEqual(event.text, "")
        self.assertEqual(event.attachments[0].type, "image")
        self.assertEqual(event.attachments[0].url, "https://example.com/pic.jpg")


if __name__ == "__main__":
    unittest.main()

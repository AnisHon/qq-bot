from __future__ import annotations

import hashlib
import os
import unittest
from unittest.mock import patch

from app.config import Settings
from app.natcapqq_adapter import NatcapQQAdapter


class NatcapQQAdapterTest(unittest.TestCase):
    def test_callback_token_config_defaults_to_unconfigured(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.natcapqq_callback_token, "")

    def test_callback_token_config_trims_outer_whitespace(self) -> None:
        with patch.dict(os.environ, {"NATCAPQQ_CALLBACK_TOKEN": " configured "}, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.natcapqq_callback_token, "configured")

    def test_settings_repr_omits_api_keys_and_tokens(self) -> None:
        settings = Settings(
            deepseek_api_key="model-secret",
            deepseek_base_url="https://user:password@example.invalid/",
            natcapqq_api_token="napcat-secret",
            network_proxy_url="http://user:password@proxy.invalid",
            natcapqq_callback_token="callback-secret",
        )

        rendered = repr(settings)
        self.assertNotIn("model-secret", rendered)
        self.assertNotIn("password", rendered)
        self.assertNotIn("napcat-secret", rendered)
        self.assertNotIn("callback-secret", rendered)

    def test_callback_token_missing_fails_closed(self) -> None:
        adapter = NatcapQQAdapter("http://127.0.0.1:3000")

        self.assertFalse(adapter.callback_auth_configured)
        self.assertFalse(adapter.verify_callback_token({}))
        self.assertFalse(adapter.verify_callback_token({"Authorization": "anything"}))

    def test_whitespace_callback_token_fails_closed(self) -> None:
        adapter = NatcapQQAdapter("http://127.0.0.1:3000", callback_token=" \t ")

        self.assertFalse(adapter.callback_auth_configured)
        self.assertFalse(adapter.verify_callback_token({"Authorization": " \t "}))

    def test_wrong_callback_token_is_rejected(self) -> None:
        adapter = NatcapQQAdapter("http://127.0.0.1:3000", callback_token="expected")

        self.assertFalse(adapter.verify_callback_token({"Authorization": "wrong"}))
        self.assertFalse(adapter.verify_callback_token({"Authorization": "Bearer wrong"}))

    def test_callback_token_accepts_bearer_and_raw_header_case_insensitively(self) -> None:
        adapter = NatcapQQAdapter("http://127.0.0.1:3000", callback_token="expected")

        self.assertTrue(adapter.verify_callback_token({"Authorization": "Bearer expected"}))
        self.assertTrue(adapter.verify_callback_token({"AUTHORIZATION": "bEaReR expected"}))
        self.assertTrue(adapter.verify_callback_token({"authorization": "expected"}))

    def test_callback_token_accepts_napcat_http_client_signature(self) -> None:
        token = "expected"
        body = b'{"post_type":"notice"}'
        signature = "sha1=" + hashlib.sha1(token.encode() + body).hexdigest()
        adapter = NatcapQQAdapter("http://127.0.0.1:3000", callback_token=token)

        self.assertTrue(adapter.verify_callback_token({"x-signature": signature}, body))
        self.assertFalse(adapter.verify_callback_token({"x-signature": signature}, body + b" "))
        self.assertFalse(adapter.verify_callback_token({"x-signature": "sha1=wrong"}, body))

    def test_adapter_repr_omits_api_credentials_and_callback_token(self) -> None:
        adapter = NatcapQQAdapter(
            "http://example.invalid",
            api_token="napcat-secret",
            callback_token="callback-secret",
        )

        rendered = repr(adapter)
        self.assertNotIn("napcat-secret", rendered)
        self.assertNotIn("callback-secret", rendered)
        self.assertNotIn("example.invalid", rendered)

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

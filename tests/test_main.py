from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.config import Settings
from app.models import GroupMessageEvent

_IMPORT_STATE = tempfile.TemporaryDirectory(prefix="qq-bot-main-import-")
_root_logger = logging.getLogger()
_null_handler = logging.NullHandler()
_root_logger.addHandler(_null_handler)
with patch.dict(
    os.environ,
    {
        "SQLITE_PATH": str(Path(_IMPORT_STATE.name) / "import.db"),
        "RAG_ENABLED": "false",
        "NATCAPQQ_CALLBACK_TOKEN": "fixture-callback-token",
    },
):
    from app.main import create_app
_root_logger.removeHandler(_null_handler)


def _payload(message_id: str) -> dict[str, object]:
    return {
        "post_type": "message",
        "message_type": "group",
        "message_id": message_id,
        "group_id": "fixture-group",
        "user_id": "fixture-user",
        "sender": {"nickname": "Fixture"},
        "message": "hello bot",
    }


class OneBotAdmissionTest(unittest.IsolatedAsyncioTestCase):
    def test_worker_and_queue_settings_are_bounded_to_positive_values(self) -> None:
        with patch.dict(
            os.environ,
            {
                "QQ_MESSAGE_WORKERS": "0",
                "QQ_MESSAGE_QUEUE_CAPACITY": "-4",
                "SHUTDOWN_DRAIN_TIMEOUT": "0",
                "STORAGE_SQLITE_WORKERS": "999",
                "STORAGE_VECTOR_WORKERS": "999",
                "STORAGE_QUEUE_CAPACITY": "999",
                "LLM_MAX_CONCURRENT_REQUESTS": "999",
            },
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.qq_message_workers, 1)
        self.assertEqual(settings.qq_message_queue_capacity, 1)
        self.assertEqual(settings.shutdown_drain_timeout, 1.0)
        self.assertEqual(settings.storage_sqlite_workers, 4)
        self.assertEqual(settings.storage_vector_workers, 1)
        self.assertEqual(settings.storage_queue_capacity, 128)
        self.assertEqual(settings.llm_max_concurrent_requests, 32)

    def test_budget_config_is_explicit_and_reservation_lease_covers_longest_timeout(self) -> None:
        with patch.dict(
            os.environ,
            {
                "NEWAPI_TIMEOUT": "500",
                "SHUTDOWN_DRAIN_TIMEOUT": "20",
                "LLM_BUDGET_GLOBAL_REQUESTS_PER_DAY": "120",
            },
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.llm_budget_global_requests_per_day, 120)
        self.assertGreaterEqual(settings.llm_budget_reservation_lease_seconds, 550)

    def test_invalid_explicit_budget_limit_fails_closed(self) -> None:
        for value in ("0", "-1", "not-a-number"):
            with self.subTest(value=value):
                with patch.dict(
                    os.environ,
                    {"LLM_BUDGET_GLOBAL_REQUESTS_PER_DAY": value},
                    clear=True,
                ):
                    with self.assertRaises(ValueError):
                        Settings.from_env()

    async def test_unconfigured_callback_is_not_ready_and_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qq-bot-readiness-test-") as temp_dir:
            env = {
                "SQLITE_PATH": str(Path(temp_dir) / "bot.db"),
                "RAG_ENABLED": "false",
                "NATCAPQQ_CALLBACK_TOKEN": "",
            }
            with patch.dict(os.environ, env):
                _root_logger.addHandler(_null_handler)
                try:
                    app = create_app()
                finally:
                    _root_logger.removeHandler(_null_handler)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test",
                ) as client:
                    ready = await client.get("/ready")
                    callback = await client.post("/onebot/event", json=_payload("unauthenticated"))
            self.assertEqual(ready.status_code, 503)
            self.assertEqual(ready.json()["reason"], "onebot_callback_auth_not_configured")
            self.assertEqual(callback.status_code, 401)

    async def test_napcat_http_signature_authenticates_exact_request_body(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qq-bot-napcat-signature-test-") as temp_dir:
            token = "fixture-callback-token"
            with patch.dict(
                os.environ,
                {
                    "SQLITE_PATH": str(Path(temp_dir) / "bot.db"),
                    "RAG_ENABLED": "false",
                    "NATCAPQQ_CALLBACK_TOKEN": token,
                },
            ):
                _root_logger.addHandler(_null_handler)
                try:
                    app = create_app()
                finally:
                    _root_logger.removeHandler(_null_handler)
            body = json.dumps({"post_type": "notice"}, separators=(",", ":")).encode()
            signature = "sha1=" + hashlib.sha1(token.encode() + body).hexdigest()
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    accepted = await client.post(
                        "/onebot/event", content=body, headers={"X-Signature": signature}
                    )
                    tampered = await client.post(
                        "/onebot/event",
                        content=body + b" ",
                        headers={"X-Signature": signature},
                    )
            self.assertEqual((accepted.status_code, accepted.json()["status"]), (200, "ignored"))
            self.assertEqual(tampered.status_code, 401)

    async def test_full_queue_returns_503_and_does_not_poison_deduplication(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qq-bot-main-test-") as temp_dir:
            env = {
                "SQLITE_PATH": str(Path(temp_dir) / "bot.db"),
                "RAG_ENABLED": "false",
                "NATCAPQQ_CALLBACK_TOKEN": "fixture-callback-token",
                "QQ_MESSAGE_WORKERS": "1",
                "QQ_MESSAGE_QUEUE_CAPACITY": "1",
                "SHUTDOWN_DRAIN_TIMEOUT": "1",
            }
            with patch.dict(os.environ, env):
                _root_logger.addHandler(_null_handler)
                try:
                    app = create_app()
                finally:
                    _root_logger.removeHandler(_null_handler)
            runtime = app.state.message_runtime
            first_started = asyncio.Event()
            release_first = asyncio.Event()
            handled: list[str] = []

            async def fake_handler(event) -> None:
                if event.message_id == "m1":
                    first_started.set()
                    await release_first.wait()
                handled.append(event.message_id)

            runtime._handler = fake_handler
            transport = httpx.ASGITransport(app=app)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    headers = {"Authorization": "Bearer fixture-callback-token"}
                    first = await client.post("/onebot/event", json=_payload("m1"), headers=headers)
                    self.assertEqual((first.status_code, first.json()["status"]), (200, "accepted"))
                    await first_started.wait()

                    second = await client.post("/onebot/event", json=_payload("m2"), headers=headers)
                    self.assertEqual((second.status_code, second.json()["status"]), (200, "accepted"))
                    rejected = await client.post("/onebot/event", json=_payload("m3"), headers=headers)
                    self.assertEqual((rejected.status_code, rejected.json()["status"]), (503, "busy"))
                    self.assertEqual(rejected.headers["retry-after"], "1")

                    release_first.set()
                    await runtime._queue.join()
                    retried = await client.post("/onebot/event", json=_payload("m3"), headers=headers)
                    self.assertEqual((retried.status_code, retried.json()["status"]), (200, "accepted"))
                    duplicate = await client.post("/onebot/event", json=_payload("m3"), headers=headers)
                    self.assertEqual((duplicate.status_code, duplicate.json()["status"]), (200, "duplicate"))
                    await runtime._queue.join()

            self.assertCountEqual(handled, ["m1", "m2", "m3"])

    async def test_slash_command_reply_does_not_trigger_model_summary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qq-bot-command-summary-test-") as temp_dir:
            env = {
                "SQLITE_PATH": str(Path(temp_dir) / "bot.db"),
                "RAG_ENABLED": "false",
                "NATCAPQQ_CALLBACK_TOKEN": "fixture-callback-token",
                "QQ_MESSAGE_WORKERS": "1",
            }
            with patch.dict(os.environ, env):
                _root_logger.addHandler(_null_handler)
                try:
                    app = create_app()
                finally:
                    _root_logger.removeHandler(_null_handler)

            service = app.state.service
            adapter = app.state.adapter
            summary_called = asyncio.Event()
            summary_calls: list[str] = []

            async def record_summary(bound_service, group_id: str) -> None:
                self.assertIs(bound_service, service)
                self.assertEqual(group_id, "fixture-group")
                summary_calls.append(group_id)
                summary_called.set()

            service._llm_handler._reply_with_llm = AsyncMock(return_value="normal response")

            def event(message_id: str, text: str, *, is_at_bot: bool) -> GroupMessageEvent:
                return GroupMessageEvent(
                    message_id=message_id,
                    group_id="fixture-group",
                    user_id="fixture-user",
                    nickname="Fixture",
                    text=text,
                    is_at_bot=is_at_bot,
                    timestamp=1,
                    raw_event={"post_type": "message", "message_type": "group"},
                )

            with (
                patch.object(
                    type(adapter),
                    "hydrate_quote",
                    new=AsyncMock(side_effect=lambda value: value),
                ),
                patch.object(type(adapter), "get_group_bot_names", new=AsyncMock(return_value=())),
                patch.object(type(adapter), "send_group_message", new=AsyncMock()),
                patch.object(type(service), "maybe_update_group_summary", new=record_summary),
            ):
                async with app.router.lifespan_context(app):
                    runtime = app.state.message_runtime
                    self.assertTrue(runtime.try_submit(event("command", "/help", is_at_bot=False)))
                    await runtime._queue.join()
                    self.assertEqual(summary_calls, [])
                    service._llm_handler._reply_with_llm.assert_not_awaited()

                    self.assertTrue(runtime.try_submit(event("chat", "hello", is_at_bot=True)))
                    await runtime._queue.join()
                    await asyncio.wait_for(summary_called.wait(), timeout=1)
                    self.assertEqual(summary_calls, ["fixture-group"])


if __name__ == "__main__":
    unittest.main()

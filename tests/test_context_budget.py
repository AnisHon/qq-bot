from __future__ import annotations

import json
import unittest

import httpx

from app.config import Settings
from app.context.budget import (
    ContextWindowBudget,
    ContextWindowError,
    ContextWindowProfile,
    profile_key,
)
from app.llm import DeepSeekClient


class ContextWindowBudgetTest(unittest.TestCase):
    def test_profile_key_is_exact_and_no_model_name_guessing(self) -> None:
        budget = ContextWindowBudget(
            (ContextWindowProfile("provider-a", "model-32k", 32768),), mode="enforce"
        )
        payload = {"model": "model-32k", "messages": [{"role": "user", "content": "hello"}]}
        with self.assertRaises(ContextWindowError) as raised:
            budget.prepare_payload(payload, profile_key("provider-b", "model-32k"))
        self.assertEqual(raised.exception.code, "profile_missing")

    def test_shadow_measures_but_never_modifies_or_rejects_payload(self) -> None:
        budget = ContextWindowBudget(
            (ContextWindowProfile("p", "m", 1024),), mode="shadow", safety_floor_tokens=64
        )
        payload = {"model": "m", "messages": [{"role": "user", "content": "x" * 10000}]}
        prepared, decision = budget.prepare_payload(payload, "p:m")
        self.assertIs(prepared, payload)
        self.assertFalse(decision.within_estimate)
        self.assertEqual(decision.profile_status, "operator_declared")
        self.assertEqual(decision.effective_output_tokens, 0)

    def test_enforce_clamps_completion_tokens_and_counts_tool_schemas(self) -> None:
        budget = ContextWindowBudget(
            (ContextWindowProfile("p", "m", 4096),), mode="enforce", safety_floor_tokens=128
        )
        payload = {"model": "m", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 8000}
        prepared, decision = budget.prepare_payload(payload, "p:m")
        self.assertLess(prepared["max_tokens"], 8000)
        self.assertEqual(decision.effective_output_tokens, prepared["max_tokens"])

        schema_payload = {
            **payload,
            "tools": [{"type": "function", "function": {"name": "x", "description": "schema " * 5000}}],
        }
        with self.assertRaises(ContextWindowError) as raised:
            budget.prepare_payload(schema_payload, "p:m")
        self.assertEqual(raised.exception.code, "context_too_large")

    def test_enforce_applies_payload_byte_limit_and_respects_distinct_windows(self) -> None:
        bounded = ContextWindowBudget(
            (ContextWindowProfile("p", "m", 8192),),
            mode="enforce",
            max_payload_bytes=1024,
        )
        with self.assertRaises(ContextWindowError) as raised:
            bounded.prepare_payload(
                {"model": "m", "messages": [{"role": "user", "content": "x" * 2000}]}, "p:m"
            )
        self.assertEqual(raised.exception.code, "payload_too_large")

        payload = {
            "model": "m", "messages": [{"role": "user", "content": "small prompt"}], "max_tokens": 10000
        }
        compact = ContextWindowBudget(
            (ContextWindowProfile("p", "m", 8192),), mode="enforce"
        )
        larger = ContextWindowBudget(
            (ContextWindowProfile("p", "m", 32768),), mode="enforce"
        )
        _, compact_decision = compact.prepare_payload(payload, "p:m")
        _, larger_decision = larger.prepare_payload(payload, "p:m")
        self.assertEqual(compact_decision.context_window_tokens, 8192)
        self.assertEqual(larger_decision.context_window_tokens, 32768)
        self.assertGreater(
            larger_decision.effective_output_tokens, compact_decision.effective_output_tokens
        )

    def test_invalid_or_unbounded_profile_values_are_rejected(self) -> None:
        for window in (0, 100, 10_000_001):
            with self.subTest(window=window), self.assertRaises(ValueError):
                ContextWindowProfile("p", "m", window)

    def test_profile_output_limits_are_respected(self) -> None:
        budget = ContextWindowBudget(
            (
                ContextWindowProfile(
                    "p", "m", 8192, max_output_tokens=1024, preferred_output_tokens=768
                ),
            ),
            mode="enforce",
        )
        payload = {"model": "m", "messages": [{"role": "user", "content": "small"}]}
        prepared, decision = budget.prepare_payload(payload, "p:m")
        self.assertEqual(prepared["max_tokens"], 768)
        self.assertEqual(decision.effective_output_tokens, 768)
        self.assertEqual(decision.profile_status, "operator_declared")

    def test_settings_load_explicit_operator_declared_profiles(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(
            os.environ,
            {
                "LLM_CONTEXT_BUDGET_MODE": "ENFORCE",
                "LLM_CONTEXT_WINDOW_PROFILES": json.dumps(
                    {"Provider-X:model-y": {"context_window_tokens": 32768}}
                ),
            },
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.llm_context_budget_mode, "enforce")
        self.assertEqual(settings.llm_context_window_profiles[0].key, "provider-x:model-y")
        self.assertEqual(settings.llm_context_window_profiles[0].status, "operator_declared")


class ContextWindowDispatchTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.sent_payloads: list[dict[str, object]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            self.sent_payloads.append(json.loads(request.content))
            if json.loads(request.content).get("stream"):
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n',
                )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
            )

        self.transport = httpx.MockTransport(handler)

    async def _client(self, *, mode: str, profile: bool = True) -> DeepSeekClient:
        profiles = (ContextWindowProfile("fixture", "small-model", 4096),) if profile else ()
        client = DeepSeekClient(
            "fixture-key",
            "https://fixture.invalid/v1",
            "small-model",
            context_budget=ContextWindowBudget(profiles, mode=mode, safety_floor_tokens=128),
            context_profile_key="fixture:small-model",
        )
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=self.transport)
        return client

    async def asyncTearDown(self) -> None:
        # Each test closes its client explicitly.
        pass

    async def test_unknown_profile_is_rejected_before_http_and_usage_reservation(self) -> None:
        class ReservationSpy:
            def __init__(self):
                self.reserved = False

            def prepare_payload(self, payload):
                return payload

            def model_request_slot(self):
                from contextlib import asynccontextmanager

                @asynccontextmanager
                async def slot():
                    yield

                return slot()

            async def reserve_budget(self, **kwargs):
                self.reserved = True
                raise AssertionError("budget reservation must follow the context gate")

        client = await self._client(mode="enforce", profile=False)
        spy = ReservationSpy()
        client.budget_controller = spy  # type: ignore[assignment]
        try:
            with self.assertRaises(ContextWindowError):
                await client.chat([{"role": "user", "content": "hello"}])
            self.assertEqual(self.sent_payloads, [])
            self.assertFalse(spy.reserved)
        finally:
            await client.aclose()

    async def test_final_payload_gate_covers_tool_schemas_and_clamps_output(self) -> None:
        client = await self._client(mode="enforce")
        try:
            await client.chat([{"role": "user", "content": "hello"}], max_tokens=9000)
            self.assertGreater(self.sent_payloads[0]["max_tokens"], 0)
            self.assertLess(self.sent_payloads[0]["max_tokens"], 9000)
        finally:
            await client.aclose()

        self.sent_payloads.clear()
        client = await self._client(mode="enforce")
        try:
            await client.chat_with_tools(
                [{"role": "user", "content": "hello"}],
                [{"type": "function", "function": {"name": "huge", "description": "schema " * 5000}}],
            )
            self.fail("oversized tool schemas must be rejected before dispatch")
        except ContextWindowError:
            self.assertEqual(self.sent_payloads, [])
        finally:
            await client.aclose()

    async def test_streaming_path_checks_profile_before_dispatch(self) -> None:
        client = await self._client(mode="enforce", profile=False)
        try:
            with self.assertRaises(ContextWindowError):
                async for _ in client.chat_stream([{"role": "user", "content": "hello"}]):
                    pass
            self.assertEqual(self.sent_payloads, [])
        finally:
            await client.aclose()

    async def test_streaming_success_payload_gets_a_bounded_output_reservation(self) -> None:
        client = await self._client(mode="enforce")
        try:
            chunks = [chunk async for chunk in client.chat_stream([{"role": "user", "content": "hello"}])]
            self.assertEqual(chunks, ["ok"])
            self.assertTrue(self.sent_payloads[0]["stream"])
            self.assertGreater(self.sent_payloads[0]["max_tokens"], 0)
            self.assertLessEqual(self.sent_payloads[0]["max_tokens"], 2048)
        finally:
            await client.aclose()

    async def test_structured_provider_context_error_retries_once_with_lower_output(self) -> None:
        payloads: list[dict[str, object]] = []
        attempts = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            payloads.append(json.loads(request.content))
            if attempts == 1:
                return httpx.Response(
                    400,
                    json={"error": {"code": "context_length_exceeded", "message": "private detail"}},
                )
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        client = await self._client(mode="enforce")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            self.assertEqual(
                await client.chat([{"role": "user", "content": "hello"}], max_tokens=1000), "ok"
            )
            self.assertEqual(len(payloads), 2)
            self.assertLess(payloads[1]["max_tokens"], payloads[0]["max_tokens"])
            self.assertNotIn("private detail", str(payloads))
        finally:
            await client.aclose()

    async def test_stream_context_error_retries_before_yielding_any_content(self) -> None:
        payloads: list[dict[str, object]] = []
        attempts = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            payloads.append(json.loads(request.content))
            if attempts == 1:
                return httpx.Response(
                    400,
                    headers={"content-type": "application/json"},
                    content=b'{"error":{"code":"context_length_exceeded"}}',
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n',
            )

        client = await self._client(mode="enforce")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            chunks = [chunk async for chunk in client.chat_stream([{"role": "user", "content": "hello"}])]
            self.assertEqual(chunks, ["ok"])
            self.assertEqual(len(payloads), 2)
            self.assertLess(payloads[1]["max_tokens"], payloads[0]["max_tokens"])
        finally:
            await client.aclose()

    async def test_shadow_and_off_modes_do_not_block_dispatch(self) -> None:
        for mode in ("shadow", "off"):
            client = await self._client(mode=mode, profile=False)
            try:
                self.assertEqual(await client.chat([{"role": "user", "content": "hello"}]), "ok")
            finally:
                await client.aclose()
        self.assertEqual(len(self.sent_payloads), 2)


if __name__ == "__main__":
    unittest.main()

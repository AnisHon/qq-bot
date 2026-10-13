from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading
import unittest
import uuid

import httpx

from app.llm import DeepSeekClient
from app.storage_executor import StorageExecutor
from app.usage_budget import (
    BudgetConfigurationError,
    BudgetController,
    BudgetExceeded,
    BudgetLimits,
    BudgetScope,
    ProviderUsage,
    UsageBudgetStore,
    reset_budget_scope,
    set_budget_scope,
)


def _response(
    request: httpx.Request,
    *,
    content: str | None = "ok",
    usage: dict[str, int] | None = None,
    tool_calls: list[dict[str, object]] | None = None,
) -> httpx.Response:
    message: dict[str, object] = {"content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body: dict[str, object] = {"choices": [{"message": message}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body, request=request)


class UsageBudgetTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.sqlite_path = Path(self.tempdir.name) / "budget.db"
        self.executor = StorageExecutor(sqlite_workers=1, vector_workers=1, queue_capacity=8)
        self.store = UsageBudgetStore(self.sqlite_path)
        self.clients: list[DeepSeekClient] = []

    async def asyncTearDown(self) -> None:
        for client in self.clients:
            await client.aclose()
        await self.executor.aclose()
        self.tempdir.cleanup()

    def controller(
        self,
        limits: BudgetLimits | None = None,
        *,
        max_concurrent_model_requests: int = 4,
    ) -> BudgetController:
        return BudgetController(
            self.store,
            self.executor,
            limits,
            max_concurrent_model_requests=max_concurrent_model_requests,
        )

    async def client(self, controller: BudgetController, handler) -> DeepSeekClient:
        client = DeepSeekClient("fixture-credential", "https://model.invalid/v1", "fixture-model", budget_controller=controller)
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.clients.append(client)
        return client

    async def rows(self) -> list[dict[str, object]]:
        def read_rows():
            conn = self.store._connect()
            try:
                return [dict(row) for row in conn.execute(
                    "SELECT * FROM model_usage_reservations ORDER BY created_at, rowid"
                ).fetchall()]
            finally:
                conn.close()

        return await self.executor.run_sqlite(read_rows)

    async def test_concurrent_requests_atomically_compete_for_global_cap(self) -> None:
        controller = self.controller(BudgetLimits(global_requests_per_day=1))

        async def reserve(index: int):
            return await controller.reserve_budget(
                BudgetScope(group_id=f"g-{index}", user_id=f"u-{index}"),
                f"request-{index}",
                "fixture-model",
                {"messages": [{"role": "user", "content": "fixture"}]},
                None,
            )

        results = await asyncio.gather(reserve(1), reserve(2), return_exceptions=True)
        self.assertEqual(sum(not isinstance(item, BaseException) for item in results), 1)
        self.assertEqual(sum(isinstance(item, BudgetExceeded) for item in results), 1)
        self.assertEqual(len(await self.rows()), 1)

    async def test_daily_request_budget_rolls_over_at_utc_midnight(self) -> None:
        limits = BudgetLimits(global_requests_per_day=1)
        day_start = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
        for day, suffix in ((day_start, "day-one"), (day_start + 86400, "day-two")):
            await self.executor.run_sqlite(
                self.store.reserve,
                str(uuid.uuid4()),
                suffix,
                "fixture-model",
                None,
                10,
                None,
                day + 86300,
                300,
                limits,
            )
        self.assertEqual(len(await self.rows()), 2)

    async def test_model_request_slot_caps_all_shared_clients_and_releases_on_cancel(self) -> None:
        controller = self.controller(max_concurrent_model_requests=1)
        entered = asyncio.Event()
        unblock = asyncio.Event()
        active = 0
        peak = 0

        async def occupy_slot() -> None:
            nonlocal active, peak
            async with controller.model_request_slot():
                active += 1
                peak = max(peak, active)
                entered.set()
                try:
                    await unblock.wait()
                finally:
                    active -= 1

        first = asyncio.create_task(occupy_slot())
        await asyncio.wait_for(entered.wait(), timeout=1)
        second = asyncio.create_task(occupy_slot())
        await asyncio.sleep(0)
        self.assertFalse(second.done())
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        unblock.set()
        await asyncio.wait_for(second, timeout=1)
        self.assertEqual(peak, 1)
        self.assertEqual(active, 0)

    async def test_cancellation_while_sqlite_reservation_finishes_releases_before_dispatch(self) -> None:
        started = threading.Event()
        unblock = threading.Event()
        original_reserve = self.store.reserve

        def slow_reserve(*args, **kwargs):
            started.set()
            unblock.wait(timeout=2)
            return original_reserve(*args, **kwargs)

        self.store.reserve = slow_reserve  # type: ignore[method-assign]
        sent = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal sent
            sent += 1
            return _response(request)

        client = await self.client(self.controller(), handler)
        request_task = asyncio.create_task(
            client.chat([{"role": "user", "content": "cancel before dispatch"}], max_tokens=20)
        )
        deadline = asyncio.get_running_loop().time() + 1
        while not started.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
        self.assertTrue(started.is_set())
        request_task.cancel()
        unblock.set()
        with self.assertRaises(asyncio.CancelledError):
            await request_task

        rows = await self.rows()
        self.assertEqual(sent, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "released")
        self.assertEqual(rows[0]["outcome"], "caller_cancelled_before_dispatch")

    async def test_user_request_limit_is_independent_of_session_and_group(self) -> None:
        controller = self.controller(BudgetLimits(user_requests_per_day=1))
        await controller.reserve_budget(
            BudgetScope(group_id="group-a", user_id="same-user"),
            "request-a",
            "fixture-model",
            {"messages": [{"role": "user", "content": "a"}]},
            None,
        )
        with self.assertRaises(BudgetExceeded):
            await controller.reserve_budget(
                BudgetScope(group_id="group-b", user_id="same-user"),
                "request-b",
                "fixture-model",
                {"messages": [{"role": "user", "content": "b"}]},
                None,
            )
        self.assertEqual(len(await self.rows()), 1)

    async def test_reported_usage_releases_estimate_and_missing_usage_keeps_unknown_reserve(self) -> None:
        controller = self.controller(
            BudgetLimits(global_tokens_per_day=250, default_max_completion_tokens=64)
        )
        first = await controller.reserve_budget(
            BudgetScope(group_id="g", user_id="u"),
            "request-1",
            "fixture-model",
            {"messages": [{"role": "user", "content": "short"}]},
            64,
        )
        self.assertIsNotNone(first.reserved_tokens_estimate)
        await controller.settle_budget(first, ProviderUsage(10, 5), "response_received")
        second = await controller.reserve_budget(
            BudgetScope(group_id="g", user_id="u"),
            "request-2",
            "fixture-model",
            {"messages": [{"role": "user", "content": "short"}]},
            64,
        )
        await controller.settle_budget(second, None, "response_usage_unknown")
        rows = await self.rows()
        self.assertEqual([row["status"] for row in rows], ["settled", "unknown_settled"])
        self.assertEqual(rows[0]["reported_input_tokens"], 10)
        self.assertEqual(rows[0]["reported_output_tokens"], 5)
        self.assertIsNone(rows[0]["cost_minor_units"])
        self.assertEqual(rows[0]["cost_status"], "unknown")
        self.assertIsNone(rows[1]["reported_input_tokens"])
        self.assertIsNone(rows[1]["reported_output_tokens"])
        snapshot = await controller.get_usage_snapshot("group", "g")
        self.assertEqual(snapshot.request_count, 2)
        self.assertEqual(snapshot.reported_requests, 1)
        self.assertEqual(snapshot.unknown_requests, 1)
        self.assertEqual(snapshot.reported_input_tokens, 10)
        self.assertEqual(snapshot.reported_output_tokens, 5)
        self.assertGreater(snapshot.estimated_unknown_tokens, 0)
        self.assertIsNone(snapshot.cost_minor_units)

    async def test_actual_usage_over_reservation_blocks_later_admission_and_settle_is_idempotent(self) -> None:
        controller = self.controller(
            BudgetLimits(global_tokens_per_day=200, default_max_completion_tokens=20)
        )
        reservation = await controller.reserve_budget(
            None,
            "request-overrun",
            "fixture-model",
            {"messages": [{"role": "user", "content": "small"}]},
            20,
        )
        self.assertEqual(reservation.scope_keys, ("global",))
        await controller.settle_budget(reservation, ProviderUsage(300, 40), "response_received")
        await controller.settle_budget(reservation, ProviderUsage(1, 1), "duplicate_settle")
        with self.assertRaises(BudgetExceeded):
            await controller.reserve_budget(
                None,
                "request-after-overrun",
                "fixture-model",
                {"messages": [{"role": "user", "content": "small"}]},
                20,
            )
        rows = await self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reported_input_tokens"], 300)
        self.assertEqual(rows[0]["reported_output_tokens"], 40)

    async def test_explicit_currency_budget_is_rejected_without_auditable_rate_card(self) -> None:
        with self.assertRaises(BudgetConfigurationError):
            await self.controller().reserve_budget(
                None,
                "request-cost",
                "fixture-model",
                {"messages": [{"role": "user", "content": "small"}]},
                None,
                cost_limit_minor_units=1,
            )
        self.assertEqual(await self.rows(), [])

    async def test_tool_schema_bytes_are_included_in_token_admission_estimate(self) -> None:
        sent = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal sent
            sent += 1
            return _response(request)

        controller = self.controller(
            BudgetLimits(global_tokens_per_day=250, default_max_completion_tokens=20)
        )
        client = await self.client(controller, handler)
        with self.assertRaises(BudgetExceeded):
            await client.chat_with_tools(
                [{"role": "user", "content": "small"}],
                [{"type": "function", "function": {"name": "large", "description": "x" * 1000}}],
            )
        self.assertEqual(sent, 0)
        self.assertEqual(await self.rows(), [])

    async def test_release_and_expired_recovery_do_not_charge_unsubmitted_or_guess_actual_tokens(self) -> None:
        controller = self.controller(BudgetLimits(global_requests_per_day=1))
        never_sent = await controller.reserve_budget(
            None, "request-a", "fixture-model", {"messages": [{"role": "user", "content": "x"}]}, None
        )
        await controller.release_budget(never_sent, "transport_not_dispatched")
        active = await controller.reserve_budget(
            None, "request-b", "fixture-model", {"messages": [{"role": "user", "content": "x"}]}, None
        )
        row = await self.executor.run_sqlite(self.store.get_reservation, active.reservation_id)
        assert row is not None
        recovered = await controller.recover_expired_reservations(now=int(row["lease_expires_at"]) + 1)
        self.assertEqual(recovered, 1)
        row = await self.executor.run_sqlite(self.store.get_reservation, active.reservation_id)
        assert row is not None
        self.assertEqual(row["status"], "unknown_settled")
        self.assertIsNone(row["reported_input_tokens"])
        self.assertEqual(row["usage_status"], "unknown")

    async def test_model_calls_are_reserved_before_http_and_provider_usage_is_exposed(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return _response(
                request,
                usage={"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
            )

        controller = self.controller(BudgetLimits(global_requests_per_day=1))
        client = await self.client(controller, handler)
        token = set_budget_scope(BudgetScope(group_id="group-1", user_id="user-1", request_id="message-1"))
        try:
            self.assertEqual(await client.chat([{"role": "user", "content": "hello"}], max_tokens=80), "ok")
            with self.assertRaises(BudgetExceeded):
                await client.chat([{"role": "user", "content": "second"}], max_tokens=80)
        finally:
            reset_budget_scope(token)
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(calls[0].content)["max_tokens"], 80)
        row = (await self.rows())[0]
        self.assertEqual(row["request_id"], "message-1")
        self.assertEqual(row["group_id"], "group-1")
        self.assertEqual(row["user_id"], "user-1")
        self.assertEqual(row["status"], "settled")

    async def test_empty_chat_retry_is_a_second_model_request(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return _response(request, content=None, usage={"prompt_tokens": 10, "completion_tokens": 3})
            return _response(request, usage={"prompt_tokens": 11, "completion_tokens": 5})

        client = await self.client(self.controller(BudgetLimits(global_requests_per_day=2)), handler)
        self.assertEqual(await client.chat([{"role": "user", "content": "retry"}], max_tokens=40), "ok")
        rows = await self.rows()
        self.assertEqual(calls, 2)
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["status"] for row in rows], ["settled", "settled"])

    async def test_connect_failure_releases_but_read_timeout_is_unknown_and_counted(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ConnectError("fixture connect failure", request=request)
            return _response(request, usage={"prompt_tokens": 4, "completion_tokens": 2})

        controller = self.controller(BudgetLimits(group_requests_per_day=1))
        client = await self.client(controller, handler)
        connect_scope = set_budget_scope(BudgetScope(group_id="connect-group", user_id="connect-user"))
        try:
            with self.assertRaisesRegex(RuntimeError, "ConnectError"):
                await client.chat([{"role": "user", "content": "retry"}], max_tokens=20)
            self.assertEqual(await client.chat([{"role": "user", "content": "retry"}], max_tokens=20), "ok")
        finally:
            reset_budget_scope(connect_scope)
        rows = await self.rows()
        self.assertEqual([row["status"] for row in rows], ["released", "settled"])

        timeout_calls = 0

        def timeout_handler(request: httpx.Request) -> httpx.Response:
            nonlocal timeout_calls
            timeout_calls += 1
            raise httpx.ReadTimeout("failed URL contained test-secret-value", request=request)

        timeout_controller = self.controller(BudgetLimits(group_requests_per_day=1))
        timeout_client = await self.client(timeout_controller, timeout_handler)
        timeout_scope = set_budget_scope(BudgetScope(group_id="timeout-group", user_id="timeout-user"))
        try:
            with self.assertRaisesRegex(RuntimeError, "ReadTimeout") as failure:
                await timeout_client.chat([{"role": "user", "content": "timeout"}], max_tokens=20)
            self.assertNotIn("test-secret-value", str(failure.exception))
            with self.assertRaises(BudgetExceeded):
                await timeout_client.chat([{"role": "user", "content": "blocked"}], max_tokens=20)
        finally:
            reset_budget_scope(timeout_scope)

        def read_timeout_rows():
            conn = self.store._connect()
            try:
                return [
                    dict(row)
                    for row in conn.execute(
                        "SELECT * FROM model_usage_reservations WHERE group_id = ?",
                        ("timeout-group",),
                    ).fetchall()
                ]
            finally:
                conn.close()

        timeout_rows = await self.executor.run_sqlite(read_timeout_rows)
        self.assertEqual(timeout_calls, 1)
        self.assertTrue(any(row["status"] == "unknown_settled" for row in timeout_rows))

    async def test_chat_with_tools_returns_optional_provider_usage(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _response(
                request,
                content=None,
                usage={"prompt_tokens": 9, "completion_tokens": 2},
                tool_calls=[
                    {
                        "id": "call-fixture",
                        "function": {"name": "knowledge_search", "arguments": "{}"},
                    }
                ],
            )

        client = await self.client(self.controller(), handler)
        response = await client.chat_with_tools([{"role": "user", "content": "search"}], [])
        self.assertEqual(response.usage, ProviderUsage(9, 2))
        self.assertEqual(response.tool_calls[0].name, "knowledge_search")

    async def test_each_agent_tool_round_model_call_uses_its_own_reservation(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _response(request, content=None, usage={"prompt_tokens": 5, "completion_tokens": 1})

        controller = self.controller(BudgetLimits(global_requests_per_day=2))
        client = await self.client(controller, handler)
        await client.chat_with_tools([{"role": "user", "content": "round one"}], [])
        await client.chat_with_tools([{"role": "assistant", "content": "round two"}], [])
        with self.assertRaises(BudgetExceeded):
            await client.chat_with_tools([{"role": "assistant", "content": "round three"}], [])
        self.assertEqual(calls, 2)
        self.assertEqual(len(await self.rows()), 2)

    async def test_stream_without_usage_keeps_unknown_reservation(self) -> None:
        request_bodies: list[dict[str, object]] = []
        sse = (
            b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            b"data: [DONE]\n\n"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            request_bodies.append(json.loads(request.content))
            return httpx.Response(200, content=sse, request=request)

        client = await self.client(
            self.controller(BudgetLimits(global_tokens_per_day=400, default_max_completion_tokens=32)),
            handler,
        )
        chunks = [chunk async for chunk in client.chat_stream([{"role": "user", "content": "stream"}])]
        self.assertEqual(chunks, ["hello"])
        self.assertEqual(request_bodies[0]["max_tokens"], 32)
        row = (await self.rows())[0]
        self.assertEqual(row["status"], "unknown_settled")
        self.assertIsNone(row["reported_input_tokens"])
        self.assertEqual(row["usage_status"], "unknown")

    async def test_stream_reported_usage_settles_exact_provider_fields(self) -> None:
        sse = (
            b'data: {"choices":[{"delta":{"content":"answer"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":17,"completion_tokens":3}}\n\n'
            b"data: [DONE]\n\n"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=sse, request=request)

        client = await self.client(self.controller(), handler)
        self.assertEqual(
            [chunk async for chunk in client.chat_stream([{"role": "user", "content": "stream"}])],
            ["answer"],
        )
        row = (await self.rows())[0]
        self.assertEqual(row["status"], "settled")
        self.assertEqual(row["reported_input_tokens"], 17)
        self.assertEqual(row["reported_output_tokens"], 3)

    async def test_stream_lease_renews_while_consumer_is_paused_between_chunks(self) -> None:
        chunk_delivered = asyncio.Event()
        pause_consumer = asyncio.Event()

        class LongStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
                await asyncio.Event().wait()

            async def aclose(self) -> None:
                return None

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=LongStream(), request=request)

        client = await self.client(
            self.controller(
                BudgetLimits(global_requests_per_day=1, reservation_lease_seconds=3)
            ),
            handler,
        )

        stream = client.chat_stream([{"role": "user", "content": "long stream"}])

        async def consume_stream() -> None:
            try:
                async for _chunk in stream:
                    chunk_delivered.set()
                    await pause_consumer.wait()
            finally:
                await stream.aclose()

        task = asyncio.create_task(consume_stream())
        await asyncio.wait_for(chunk_delivered.wait(), timeout=1)
        before = (await self.rows())[0]
        try:
            await asyncio.sleep(1.3)
            after = (await self.rows())[0]
            self.assertGreater(after["lease_expires_at"], before["lease_expires_at"])
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        final = (await self.rows())[0]
        self.assertEqual(final["status"], "unknown_settled")

    async def test_stream_error_status_does_not_require_unbounded_response_body_read(self) -> None:
        class ErrorBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"provider-secret-value"

            async def aclose(self) -> None:
                return None

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, stream=ErrorBody(), request=request)

        client = await self.client(self.controller(), handler)
        with self.assertRaisesRegex(RuntimeError, "429") as caught:
            async for _chunk in client.chat_stream([{"role": "user", "content": "rate limited"}]):
                pass
        self.assertNotIn("ResponseNotRead", str(caught.exception))
        self.assertNotIn("provider-secret-value", str(caught.exception))
        row = (await self.rows())[0]
        self.assertEqual(row["status"], "unknown_settled")

    async def test_cancelled_stream_after_dispatch_is_unknown(self) -> None:
        started = asyncio.Event()

        class BlockingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                started.set()
                await asyncio.Event().wait()
                yield b""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=BlockingStream(), request=request)

        client = await self.client(self.controller(BudgetLimits(global_requests_per_day=1)), handler)

        async def consume() -> None:
            async for _chunk in client.chat_stream([{"role": "user", "content": "cancel"}]):
                pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        row = (await self.rows())[0]
        self.assertEqual(row["status"], "unknown_settled")
        self.assertEqual(row["outcome"], "stream_outcome_unknown")

    async def test_token_budget_adds_provider_max_completion_only_when_configured(self) -> None:
        seen: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return _response(request, usage={"prompt_tokens": 20, "completion_tokens": 5})

        controller = self.controller(
            BudgetLimits(
                global_tokens_per_day=500,
                default_max_completion_tokens=64,
            )
        )
        client = await self.client(controller, handler)
        await client.chat([{"role": "user", "content": "short"}])
        self.assertEqual(seen[0]["max_tokens"], 64)
        row = (await self.rows())[0]
        self.assertEqual(row["status"], "settled")
        self.assertEqual(row["reported_input_tokens"], 20)
        self.assertEqual(row["reported_output_tokens"], 5)
        self.assertEqual(row["estimate_method"], "utf8-bytes-plus-message-overhead-v1")


if __name__ == "__main__":
    unittest.main()

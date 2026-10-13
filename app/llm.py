from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
import json
import logging
from typing import Any
import uuid

import httpx

from app.usage_budget import (
    BudgetController,
    BudgetReservation,
    ProviderUsage,
    current_budget_scope,
)


LOGGER = logging.getLogger(__name__)


@asynccontextmanager
async def _empty_async_context():
    yield


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    # Do not echo the provider URL or response body: either can contain
    # credentials. This also avoids reading an unbounded streaming error body.
    raise RuntimeError(f"LLM request failed with HTTP {response.status_code}")


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    """A single tool-call emitted by the model in a chat_with_tools response."""
    id: str
    name: str
    arguments: str  # raw JSON string, e.g. '{"query": "today news"}'
    extra_content: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChatWithToolsResponse:
    """Return value of DeepSeekClient.chat_with_tools()."""
    text: str | None              # final text content (None when tool_calls present)
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    extra_content: dict[str, Any] = field(default_factory=dict)
    usage: ProviderUsage | None = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@dataclass(slots=True)
class DeepSeekClient:
    api_key: str
    base_url: str
    model: str
    timeout: float = 60.0
    budget_controller: BudgetController | None = field(default=None, repr=False)
    _client: httpx.AsyncClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _definitely_not_dispatched(exc: BaseException) -> bool:
        return isinstance(
            exc,
            (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.PoolTimeout,
                httpx.InvalidURL,
                httpx.UnsupportedProtocol,
            ),
        )

    async def _finish_reservation(
        self,
        reservation: BudgetReservation | None,
        usage: ProviderUsage | None,
        outcome: str,
    ) -> None:
        if reservation is None or self.budget_controller is None:
            return
        try:
            await self.budget_controller.settle_budget(reservation, usage, outcome)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # The reserved row remains charged and startup recovery marks it unknown.
            LOGGER.exception("failed to settle model usage reservation")

    async def _release_reservation(
        self,
        reservation: BudgetReservation | None,
        reason: str,
    ) -> None:
        if reservation is None or self.budget_controller is None:
            return
        try:
            await self.budget_controller.release_budget(reservation, reason)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            LOGGER.exception("failed to release undispatched model reservation")

    async def _renew_reservation_during_stream(
        self,
        reservation: BudgetReservation,
    ) -> None:
        controller = self.budget_controller
        if controller is None:
            return
        interval = max(0.1, controller.limits.reservation_lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await controller.renew_budget(reservation)
                if not renewed:
                    LOGGER.warning("streaming model reservation lease is no longer active")
                    return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                LOGGER.warning("failed to renew streaming model reservation")

    @staticmethod
    async def _stop_lease_renewal(task: asyncio.Task[None] | None) -> None:
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _request_json(
        self,
        url: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], ProviderUsage | None]:
        controller = self.budget_controller
        if controller is not None:
            payload = controller.prepare_payload(payload)
        # Build / serialize locally before reserving; invalid requests cannot
        # consume a request slot or reach the provider.
        try:
            request = self._client.build_request("POST", url, json=payload)
        except httpx.HTTPError:
            raise RuntimeError("LLM request could not be constructed") from None
        reservation = None
        slot = controller.model_request_slot() if controller is not None else _empty_async_context()
        async with slot:
            if controller is not None:
                scope = current_budget_scope()
                reservation = await controller.reserve_budget(
                    scope=scope,
                    request_id=scope.request_id if scope else str(uuid.uuid4()),
                    model=self.model,
                    request_payload=payload,
                    max_output_tokens=payload.get("max_tokens"),
                )
            return await self._dispatch_json_request(request, reservation)

    async def _dispatch_json_request(
        self,
        request: httpx.Request,
        reservation: BudgetReservation | None,
    ) -> tuple[dict[str, Any], ProviderUsage | None]:
        try:
            response = await self._client.send(request)
        except BaseException as exc:
            if self._definitely_not_dispatched(exc):
                await self._release_reservation(reservation, "transport_not_dispatched")
            else:
                await self._finish_reservation(reservation, None, "dispatch_outcome_unknown")
            if isinstance(exc, httpx.HTTPError):
                raise RuntimeError(f"LLM transport failed ({type(exc).__name__})") from None
            raise

        try:
            _raise_for_status(response)
            data = response.json()
            if not isinstance(data, dict):
                raise RuntimeError("LLM returned an invalid response object")
        except BaseException:
            await self._finish_reservation(reservation, None, "response_usage_unknown")
            raise

        usage = ProviderUsage.from_payload(data.get("usage"))
        await self._finish_reservation(reservation, usage, "response_received")
        return data, usage

    async def chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: int | None = None,
        presence_penalty: float | None = None,
        frequency_penalty: float | None = None,
    ) -> str:
        if not self.api_key:
            raise RuntimeError("LLM API key is not configured")
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if presence_penalty is not None:
            payload["presence_penalty"] = presence_penalty
        if frequency_penalty is not None:
            payload["frequency_penalty"] = frequency_penalty
        for attempt in range(2):
            data, _usage = await self._request_json(url, payload)
            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError("LLM returned no choices")
            message = choices[0].get("message") or {}
            content = message.get("content")
            if content:
                return str(content).strip()

            # Reasoning models can consume a small completion budget entirely in
            # reasoning_content. Retry once with enough room for the final answer.
            if attempt == 0 and max_tokens is not None:
                payload["max_tokens"] = max(1200, max_tokens * 3)
                continue
            raise RuntimeError("LLM returned empty content")
        raise RuntimeError("LLM returned empty content")

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float = 0.25,
        presence_penalty: float | None = None,
        frequency_penalty: float | None = None,
    ) -> ChatWithToolsResponse:
        """Call the model with tool definitions.

        Returns a ChatWithToolsResponse: either the model's final text reply,
        or a list of tool_calls it wants to execute before answering.
        """
        if not self.api_key:
            raise RuntimeError("LLM API key is not configured")
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "tools": tools,
            "tool_choice": "auto",
            "max_tokens": 2048,
        }
        if presence_penalty is not None:
            payload["presence_penalty"] = presence_penalty
        if frequency_penalty is not None:
            payload["frequency_penalty"] = frequency_penalty
        data, usage = await self._request_json(url, payload)
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("LLM returned no choices")
        message = choices[0].get("message") or {}
        content = message.get("content")  # may be None/null when emitting tool_calls
        raw_tool_calls: list[dict[str, Any]] = message.get("tool_calls") or []
        tool_calls = []
        for tc in raw_tool_calls:
            func = tc.get("function") or {}
            tool_calls.append(
                ToolCallRequest(
                    id=str(tc.get("id") or f"call_{len(tool_calls)}"),
                    name=str(func.get("name") or ""),
                    arguments=str(func.get("arguments") or "{}"),
                    extra_content=dict(tc.get("extra_content") or {}),
                )
            )
        return ChatWithToolsResponse(
            text=str(content).strip() if content else None,
            tool_calls=tool_calls,
            extra_content=dict(message.get("extra_content") or {}),
            usage=usage,
        )

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.25,
        presence_penalty: float | None = None,
        frequency_penalty: float | None = None,
    ) -> AsyncIterator[str]:
        """Stream text tokens from the model.

        Yields str chunks as they arrive from the SSE stream.
        Raises RuntimeError on API or protocol errors.
        """
        if not self.api_key:
            raise RuntimeError("LLM API key is not configured")
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if presence_penalty is not None:
            payload["presence_penalty"] = presence_penalty
        if frequency_penalty is not None:
            payload["frequency_penalty"] = frequency_penalty
        controller = self.budget_controller
        if controller is not None:
            payload = controller.prepare_payload(payload)
        try:
            request = self._client.build_request("POST", url, json=payload)
        except httpx.HTTPError:
            raise RuntimeError("LLM request could not be constructed") from None
        slot = controller.model_request_slot() if controller is not None else _empty_async_context()
        async with slot:
            reservation = None
            if controller is not None:
                scope = current_budget_scope()
                reservation = await controller.reserve_budget(
                    scope=scope,
                    request_id=scope.request_id if scope else str(uuid.uuid4()),
                    model=self.model,
                    request_payload=payload,
                    max_output_tokens=payload.get("max_tokens"),
                )

            response: httpx.Response | None = None
            usage: ProviderUsage | None = None
            renewal_task = (
                asyncio.create_task(
                    self._renew_reservation_during_stream(reservation),
                    name="llm-budget-stream-lease",
                )
                if reservation is not None
                else None
            )
            try:
                response = await self._client.send(request, stream=True)
                _raise_for_status(response)
                async for line in response.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    try:
                        chunk = json.loads(line[6:])
                        reported_usage = ProviderUsage.from_payload(chunk.get("usage"))
                        if reported_usage is not None:
                            usage = reported_usage
                        choices = chunk.get("choices") or []
                        delta = choices[0].get("delta", {}).get("content") or "" if choices else ""
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError, AttributeError):
                        continue
            except BaseException as exc:
                await self._stop_lease_renewal(renewal_task)
                if self._definitely_not_dispatched(exc) and response is None:
                    await self._release_reservation(reservation, "stream_not_dispatched")
                else:
                    await self._finish_reservation(reservation, None, "stream_outcome_unknown")
                if isinstance(exc, httpx.HTTPError):
                    raise RuntimeError(f"LLM transport failed ({type(exc).__name__})") from None
                raise
            else:
                await self._stop_lease_renewal(renewal_task)
                await self._finish_reservation(
                    reservation,
                    usage,
                    "stream_completed" if usage is not None else "stream_usage_unknown",
                )
            finally:
                if response is not None:
                    await response.aclose()

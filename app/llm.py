from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
import json
from typing import Any

import httpx


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    detail = response.text.strip()
    if len(detail) > 1600:
        detail = detail[:1600].rstrip() + "..."
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        suffix = f" Response: {detail}" if detail else ""
        raise RuntimeError(f"LLM request failed: {exc}.{suffix}") from exc


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

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@dataclass(slots=True)
class DeepSeekClient:
    api_key: str
    base_url: str
    model: str
    timeout: float = 60.0
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
            resp = await self._client.post(url, json=payload)
            _raise_for_status(resp)
            data: dict[str, Any] = resp.json()
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
        resp = await self._client.post(url, json=payload)
        _raise_for_status(resp)
        data: dict[str, Any] = resp.json()
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
        async with self._client.stream("POST", url, json=payload) as resp:
            _raise_for_status(resp)
            async for line in resp.aiter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                try:
                    chunk = json.loads(line[6:])
                    delta = chunk["choices"][0]["delta"].get("content") or ""
                    if delta:
                        yield delta
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

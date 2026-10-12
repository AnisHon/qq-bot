from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
import logging
import re
import time
from typing import Any

from app.config import Settings
from app.llm import DeepSeekClient
from app.memory import MemoryStore
from app.models import BotCommand, ChatReply, GroupMessageEvent
from app.rag import RagStore
from app.vector_rag import VectorRagStore
from app.handlers.command_handler import CommandHandler, _format_memory_lines
from app.handlers.llm_handler import (
    LLMHandler,
    _format_rag_chunks,
    _friendly_llm_failure,
    _is_name_triggered,
    _looks_like_minecraft_query,
    _looks_like_sfm_query,
    _message_content_for_memory,
    _sanitize_qq_text,
    _strip_name_trigger,
    _strip_trigger,
    _unique_names,
)
from app.handlers.task_handler import _parse_task_schedule


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class BotService:
    settings: Settings
    memory: MemoryStore
    llm: DeepSeekClient
    rag: RagStore | None = None
    llms: dict[str, DeepSeekClient] = field(default_factory=dict)
    vector_rag: VectorRagStore | None = None
    _active_groups: set[str] = field(default_factory=set, init=False, repr=False)
    _active_guard: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _command_handler: Any = field(default=None, init=False, repr=False)
    _llm_handler: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._command_handler = CommandHandler(self.settings, self.memory, self.llm, self.llms)
        self._llm_handler = LLMHandler(
            self.settings, self.memory, self.llm, self.rag, self.vector_rag, self.llms
        )

    async def handle_group_message(
        self,
        event: GroupMessageEvent,
        on_tool_call=None,
    ) -> ChatReply | None:
        if not event.text.strip() and not event.attachments and not event.quote:
            return None

        command_text = self._request_text(event)
        command = self._parse_command(command_text)
        forced_tool = command if command and command.name == "tool" else None
        if command is not None and forced_tool is None:
            return self._command_handler.handle_command(event, command)

        memory_content = _message_content_for_memory(event)
        self.memory.add_message(
            group_id=event.group_id,
            user_id=event.user_id,
            nickname=event.nickname,
            role="user",
            content=memory_content,
        )

        if forced_tool is None and not self._should_reply(event):
            return None

        if not await self._try_begin_group_reply(event.group_id):
            return ChatReply(text="我正在回答上一条问题，先不切换话题。请稍后再问我。")

        try:
            try:
                reply_text = await self._llm_handler._reply_with_llm(
                    event, forced_tool=forced_tool, on_tool_call=on_tool_call
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("LLM reply failed: %s", exc)
                reply_text = _friendly_llm_failure(exc)
            if reply_text:
                self.memory.add_message(
                    group_id=event.group_id,
                    user_id=self.settings.bot_qq_id or "bot",
                    nickname="bot",
                    role="assistant",
                    content=reply_text,
                )
                return ChatReply(text=reply_text)
            return None
        finally:
            await self._end_group_reply(event.group_id)

    def _should_reply(self, event: GroupMessageEvent) -> bool:
        if event.quote and event.quote.is_from_bot:
            return True
        if self._is_event_name_triggered(event):
            return True
        mode = self.settings.bot_trigger_mode
        if mode == "prefix":
            return event.text.strip().startswith(self.settings.bot_prefix)
        if mode == "both":
            return event.is_at_bot or event.text.strip().startswith(self.settings.bot_prefix)
        return event.is_at_bot

    def expects_reply(self, event: GroupMessageEvent) -> bool:
        if not event.text.strip():
            return bool((event.attachments and event.is_at_bot) or (event.quote and event.quote.is_from_bot))
        command_text = self._request_text(event)
        command = self._parse_command(command_text)
        if command is not None:
            return True
        return self._should_reply(event)

    def _event_bot_names(self, event: GroupMessageEvent) -> tuple[str, ...]:
        return _unique_names(event.bot_names, self.settings.bot_default_name)

    def _is_event_name_triggered(self, event: GroupMessageEvent) -> bool:
        return _is_name_triggered(event.text, self._event_bot_names(event))

    def _request_text(self, event: GroupMessageEvent) -> str:
        text = _strip_trigger(event.text, self.settings.bot_prefix)
        return _strip_name_trigger(text, self._event_bot_names(event))

    async def _try_begin_group_reply(self, group_id: str) -> bool:
        group_key = str(group_id)
        async with self._active_guard:
            if group_key in self._active_groups:
                return False
            self._active_groups.add(group_key)
            return True

    async def _end_group_reply(self, group_id: str) -> None:
        async with self._active_guard:
            self._active_groups.discard(str(group_id))

    async def is_group_busy(self, group_id: str) -> bool:
        async with self._active_guard:
            return str(group_id) in self._active_groups

    async def run_scheduled_task(self, task: dict[str, str], on_tool_call=None) -> str | None:
        group_id = str(task["group_id"])
        if not await self._try_begin_group_reply(group_id):
            return None
        prompt = str(task["prompt"])
        event = GroupMessageEvent(
            message_id=f"task-{task['id']}",
            group_id=group_id,
            user_id=str(task["created_by"]),
            nickname="scheduled-task",
            text=prompt,
            is_at_bot=True,
            timestamp=int(time.time()),
            raw_event={"scheduled_task_id": task["id"]},
            bot_names=(self.settings.bot_default_name,),
        )
        try:
            self.memory.add_message(
                group_id=event.group_id,
                user_id=event.user_id,
                nickname=event.nickname,
                role="user",
                content=prompt,
            )
            reply_text = await self._llm_handler._reply_with_llm(event, on_tool_call=on_tool_call)
            self.memory.add_message(
                group_id=event.group_id,
                user_id=self.settings.bot_qq_id or "bot",
                nickname="bot",
                role="assistant",
                content=reply_text,
            )
            return reply_text
        finally:
            await self._end_group_reply(group_id)

    async def maybe_update_group_summary(self, group_id: str) -> None:
        await self._llm_handler.maybe_update_group_summary(group_id)

    def _parse_command(self, text: str) -> BotCommand | None:
        cleaned = text.strip()
        if not cleaned.startswith("/"):
            return None

        first_line, _, rest = cleaned.partition("\n")
        parts = first_line.split(maxsplit=2)
        root = parts[0].lower()
        action = parts[1].lower() if len(parts) >= 2 else ""
        inline_payload = parts[2] if len(parts) >= 3 else ""
        payload = "\n".join(item for item in [inline_payload, rest] if item).strip()

        if root in {"/help", "/commands"}:
            return BotCommand("help")
        if root in {"/weather", "/news", "/web"}:
            tool_payload = first_line[len(parts[0]):].strip()
            if rest:
                tool_payload = "\n".join(item for item in [tool_payload, rest] if item).strip()
            return BotCommand("tool", root.removeprefix("/"), tool_payload)
        if root == "/memory":
            if action in {"add", "set"}:
                return BotCommand("memory", "add", payload)
            if action in {"delete", "del", "remove", "rm"}:
                return BotCommand("memory", "delete", payload)
            if action == "list":
                return BotCommand("memory", "list")
            if action == "clear":
                return BotCommand("memory", "clear")
            return BotCommand("unknown")
        if root == "/context" and action == "clear":
            return BotCommand("context", "clear")
        if root == "/summary" and action in {"show", "clear"}:
            return BotCommand("summary", action)
        if root in {"/context", "/summary"}:
            return BotCommand("unknown")
        if root == "/task":
            if action in {"add", "set"}:
                return BotCommand("task", "add", payload)
            if action == "list":
                return BotCommand("task", "list")
            if action in {"delete", "del", "remove", "rm"}:
                return BotCommand("task", "delete", payload)
            if action == "clear":
                return BotCommand("task", "clear")
            return BotCommand("unknown")
        if root == "/model":
            if action in {"show", "list"}:
                return BotCommand("model", action)
            if action in {"use", "set"}:
                return BotCommand("model", "use", payload)
            return BotCommand("unknown")
        return None

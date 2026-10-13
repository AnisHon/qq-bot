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
from app.models import ChatReply, GroupMessageEvent
from app.rag import RagStore
from app.vector_rag import VectorRagStore
from app.storage_executor import StorageExecutor
from app.usage_budget import BudgetScope, reset_budget_scope, set_budget_scope
from app.commands.service import CommandService
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


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class BotService:
    settings: Settings
    memory: MemoryStore
    llm: DeepSeekClient
    rag: RagStore | None = None
    llms: dict[str, DeepSeekClient] = field(default_factory=dict)
    vector_rag: VectorRagStore | None = None
    storage_executor: StorageExecutor | None = None
    _active_groups: set[str] = field(default_factory=set, init=False, repr=False)
    _active_guard: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _command_service: Any = field(default=None, init=False, repr=False)
    _llm_handler: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm_handler = LLMHandler(
            self.settings,
            self.memory,
            self.llm,
            self.rag,
            self.vector_rag,
            self.llms,
            storage_executor=self.storage_executor,
        )
        self._command_service = CommandService(
            self.settings,
            self.memory,
            self._llm_handler,
            storage_executor=self.storage_executor,
        )

    async def _storage_call(self, function, /, *args, **kwargs):
        if self.storage_executor is None:
            return function(*args, **kwargs)
        return await self.storage_executor.run_sqlite(function, *args, **kwargs)

    async def handle_group_message(
        self,
        event: GroupMessageEvent,
        on_tool_call=None,
    ) -> ChatReply | None:
        if not event.text.strip() and not event.attachments and not event.quote:
            return None

        command_text = self._request_text(event)
        if self._command_service.is_command_text(command_text):
            result = await self._command_service.dispatch(event, command_text, on_tool_call=on_tool_call)
            return self._command_service.renderer.render(result)

        memory_content = _message_content_for_memory(event)
        await self._storage_call(
            self.memory.add_message,
            group_id=event.group_id,
            user_id=event.user_id,
            nickname=event.nickname,
            role="user",
            content=memory_content,
        )

        if not self._should_reply(event):
            return None

        if not await self._try_begin_group_reply(event.group_id):
            return ChatReply(text="我正在回答上一条问题，先不切换话题。请稍后再问我。")

        budget_token = set_budget_scope(
            BudgetScope(
                group_id=str(event.group_id),
                user_id=str(event.user_id) if event.user_id else None,
                request_id=str(event.message_id),
            )
        )
        try:
            try:
                reply_text = await self._llm_handler._reply_with_llm(
                    event, on_tool_call=on_tool_call
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("LLM reply failed: %s", exc)
                reply_text = _friendly_llm_failure(exc)
            finally:
                reset_budget_scope(budget_token)
            if reply_text:
                await self._storage_call(
                    self.memory.add_message,
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
        if self.is_command_event(event):
            return True
        return self._should_reply(event)

    def is_command_event(self, event: GroupMessageEvent) -> bool:
        """Identify slash commands after the same trigger stripping used for routing."""
        return self._command_service.is_command_text(self._request_text(event))

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
        budget_token = set_budget_scope(
            BudgetScope(
                group_id=group_id,
                user_id=str(task["created_by"]) if task.get("created_by") else None,
                request_id=f"scheduled-task-{task['id']}",
            )
        )
        try:
            await self._storage_call(
                self.memory.add_message,
                group_id=event.group_id,
                user_id=event.user_id,
                nickname=event.nickname,
                role="user",
                content=prompt,
            )
            reply_text = await self._llm_handler._reply_with_llm(event, on_tool_call=on_tool_call)
            await self._storage_call(
                self.memory.add_message,
                group_id=event.group_id,
                user_id=self.settings.bot_qq_id or "bot",
                nickname="bot",
                role="assistant",
                content=reply_text,
            )
            return reply_text
        finally:
            reset_budget_scope(budget_token)
            await self._end_group_reply(group_id)

    async def maybe_update_group_summary(self, group_id: str) -> None:
        await self._llm_handler.maybe_update_group_summary(group_id)

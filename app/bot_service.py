from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
import logging
import re
import time
from typing import Any
import uuid

from app.config import Settings
from app.context.contracts import ContextSnapshot
from app.llm import DeepSeekClient
from app.memory import MemoryStore
from app.models import ChatReply, GroupMessageEvent
from app.commands.contracts import CommandErrorKind, CommandResult
from app.rag import RagStore
from app.vector_rag import VectorRagStore
from app.storage_executor import StorageExecutor
from app.usage_budget import BudgetScope, reset_budget_scope, set_budget_scope
from app.progress.events import RequestAgentEventSink, TaskIdentity
from app.progress.events import AgentEventKind, AgentEventPayload, ExecutionStatus
from app.context.contracts import ContextEpochChanged
from app.progress.coordinator import ProgressMode
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
    progress_coordinator: Any = field(default=None, repr=False)
    dispatcher: Any = field(default=None, repr=False)
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

    def configure_progress(self, coordinator, dispatcher) -> None:
        self.progress_coordinator = coordinator
        self.dispatcher = dispatcher
        self._command_service.progress_coordinator = coordinator
        self._command_service.dispatcher = dispatcher

    async def _storage_call(self, function, /, *args, **kwargs):
        if self.storage_executor is None:
            return function(*args, **kwargs)
        return await self.storage_executor.run_sqlite(function, *args, **kwargs)

    async def _progress_mode(self, group_id: str) -> ProgressMode:
        try:
            value = await self._storage_call(self.memory.get_group_setting, str(group_id), "progress_mode")
            return ProgressMode(value or ProgressMode.BALANCED)
        except Exception:  # noqa: BLE001
            return ProgressMode.BALANCED

    def _event_sink(self, identity: TaskIdentity, mode: ProgressMode):
        if self.progress_coordinator is not None:
            return self.progress_coordinator.register(identity, mode)
        return RequestAgentEventSink(identity)

    async def handle_group_message(
        self,
        event: GroupMessageEvent,
    ) -> ChatReply | None:
        if not event.text.strip() and not event.attachments and not event.quote:
            return None

        command_text = self._request_text(event)
        if self._command_service.is_command_text(command_text):
            request_id = str(event.message_id or uuid.uuid4())
            epoch = await self._storage_call(self.memory.get_context_epoch, str(event.group_id))
            identity = TaskIdentity.create(
                request_id=request_id, group_id=str(event.group_id), user_id=str(event.user_id),
                epoch=epoch, origin="slash_command",
            )
            sink = self._event_sink(identity, await self._progress_mode(str(event.group_id)))
            sink.emit(AgentEventKind.TASK_STARTED, AgentEventPayload(
                execution_status=ExecutionStatus.RUNNING,
            ))
            try:
                result = await self._command_service.dispatch(
                    event, command_text, task_identity=identity, event_sink=sink
                )
            except asyncio.CancelledError:
                sink.emit(AgentEventKind.TASK_CANCELLED, AgentEventPayload(
                    execution_status=ExecutionStatus.CANCELLED,
                ))
                raise
            except Exception as exc:  # noqa: BLE001 - internal details must not reach QQ
                LOGGER.warning("command execution failed (%s)", type(exc).__name__)
                sink.emit(AgentEventKind.TASK_FAILED, AgentEventPayload(
                    execution_status=ExecutionStatus.FAILED,
                ))
                result = CommandResult(CommandErrorKind.INTERNAL_ERROR, "命令执行失败，请稍后重试。")
            else:
                sink.emit(AgentEventKind.TASK_COMPLETED, AgentEventPayload(
                    execution_status=ExecutionStatus.COMPLETED,
                ))
            reply = self._command_service.renderer.render(result)
            reply.context_epoch = await self._storage_call(
                self.memory.get_context_epoch, str(event.group_id)
            )
            reply.task_id = identity.task_id
            return reply

        memory_content = _message_content_for_memory(event)
        if len(memory_content.encode("utf-8")) > self.settings.context_max_user_input_bytes:
            if self._should_reply(event):
                return ChatReply(
                    text="这条消息超过上下文输入长度限制，请拆分后重发。超长原文未保存，也不会交给模型。"
                )
            return None
        request_id = str(event.message_id or uuid.uuid4())
        snapshot = await self._storage_call(
            self.memory.record_message_snapshot,
            group_id=event.group_id,
            user_id=event.user_id,
            nickname=event.nickname,
            content=memory_content,
            request_id=request_id,
            onebot_message_id=str(event.message_id) if event.message_id else None,
            reply_message_id=event.reply_message_id or None,
            event_timestamp=event.timestamp,
        )

        if not self._should_reply(event):
            return None

        if not await self._try_begin_group_reply(event.group_id):
            return ChatReply(text="我正在回答上一条问题，先不切换话题。请稍后再问我。")

        budget_token = set_budget_scope(
            BudgetScope(
                group_id=str(event.group_id),
                user_id=str(event.user_id) if event.user_id else None,
                request_id=request_id,
            )
        )
        task_identity = TaskIdentity.create(
            request_id=request_id,
            group_id=str(event.group_id),
            user_id=str(event.user_id),
            epoch=snapshot.epoch,
            origin="user_message",
        )
        event_sink = self._event_sink(task_identity, await self._progress_mode(str(event.group_id)))
        event_sink.emit(AgentEventKind.TASK_STARTED, AgentEventPayload(
            execution_status=ExecutionStatus.RUNNING,
        ))
        terminal: tuple[AgentEventKind, ExecutionStatus] | None = None
        try:
            try:
                reply_text = await self._llm_handler._reply_with_llm(
                    event,
                    context_snapshot=snapshot,
                    task_identity=task_identity,
                    event_sink=event_sink,
                )
            except ContextEpochChanged:
                event_sink.emit(AgentEventKind.TASK_STALE, AgentEventPayload(
                    execution_status=ExecutionStatus.STALE,
                ))
                return ChatReply(text="", should_send=False, task_id=task_identity.task_id)
            except asyncio.CancelledError:
                terminal = (AgentEventKind.TASK_CANCELLED, ExecutionStatus.CANCELLED)
                raise
            except TimeoutError as exc:
                terminal = (AgentEventKind.TASK_TIMED_OUT, ExecutionStatus.TIMED_OUT)
                LOGGER.warning("Agent request timed out: %s", type(exc).__name__)
                reply_text = _friendly_llm_failure(exc)
            except Exception as exc:  # noqa: BLE001
                terminal = (AgentEventKind.TASK_FAILED, ExecutionStatus.FAILED)
                LOGGER.warning("LLM reply failed: %s", exc)
                reply_text = _friendly_llm_failure(exc)
            finally:
                reset_budget_scope(budget_token)
            if reply_text:
                stored = False
                if snapshot.persisted:
                    stored = await self._storage_call(
                        self.memory.record_assistant_if_epoch,
                        snapshot,
                        self.settings.bot_qq_id or "bot",
                        "bot",
                        reply_text,
                    )
                epoch_current = await self.is_context_epoch_current(event.group_id, snapshot.epoch)
                if not epoch_current:
                    event_sink.emit(AgentEventKind.TASK_STALE, AgentEventPayload(
                        execution_status=ExecutionStatus.STALE,
                    ))
                    return ChatReply(text="", should_send=False)
                kind, status = terminal or (
                    AgentEventKind.TASK_COMPLETED, ExecutionStatus.COMPLETED
                )
                event_sink.emit(kind, AgentEventPayload(execution_status=status))
                return ChatReply(
                    text=reply_text,
                    context_epoch=snapshot.epoch,
                    context_persisted=bool(snapshot.persisted and stored),
                    task_id=task_identity.task_id,
                )
            kind, status = terminal or (AgentEventKind.TASK_COMPLETED, ExecutionStatus.COMPLETED)
            event_sink.emit(kind, AgentEventPayload(execution_status=status))
            return None
        except asyncio.CancelledError:
            kind, status = terminal or (AgentEventKind.TASK_CANCELLED, ExecutionStatus.CANCELLED)
            event_sink.emit(kind, AgentEventPayload(execution_status=status))
            raise
        except Exception:
            if terminal is None:
                event_sink.emit(AgentEventKind.TASK_FAILED, AgentEventPayload(
                    execution_status=ExecutionStatus.FAILED,
                ))
            raise
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

    async def is_context_epoch_current(self, group_id: str, epoch: int) -> bool:
        return await self._storage_call(self.memory.get_context_epoch, group_id) == int(epoch)

    async def run_scheduled_task(self, task: dict[str, str]) -> str | None:
        group_id = str(task["group_id"])
        if not await self._try_begin_group_reply(group_id):
            return None
        prompt = str(task["prompt"])
        request_id = f"scheduled-task-{task['id']}-{time.time_ns()}"
        event = GroupMessageEvent(
            message_id=request_id,
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
                request_id=request_id,
            )
        )
        try:
            snapshot = await self._storage_call(
                self.memory.record_message_snapshot,
                group_id=event.group_id,
                user_id=event.user_id,
                nickname=event.nickname,
                content=prompt,
                request_id=request_id,
                event_timestamp=event.timestamp,
            )
            task_identity = TaskIdentity.create(
                request_id=request_id,
                group_id=group_id,
                user_id=str(event.user_id),
                epoch=snapshot.epoch,
                origin="scheduled_task",
            )
            reply_text = await self._llm_handler._reply_with_llm(
                event,
                context_snapshot=snapshot,
                task_identity=task_identity,
                event_sink=RequestAgentEventSink(task_identity),
            )
            stored = False
            if snapshot.persisted:
                stored = await self._storage_call(
                    self.memory.record_assistant_if_epoch,
                    snapshot,
                    self.settings.bot_qq_id or "bot",
                    "bot",
                    reply_text,
                )
            if snapshot.persisted and not stored and not await self.is_context_epoch_current(group_id, snapshot.epoch):
                return None
            if not await self.is_context_epoch_current(group_id, snapshot.epoch):
                return None
            return reply_text
        finally:
            reset_budget_scope(budget_token)
            await self._end_group_reply(group_id)

    async def maybe_update_group_summary(self, group_id: str) -> None:
        await self._llm_handler.maybe_update_group_summary(group_id)

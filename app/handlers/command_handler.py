from __future__ import annotations

import re
import shlex
import time
from typing import Sequence

from app.config import Settings
from app.llm import DeepSeekClient
from app.memory import MemoryStore
from app.models import BotCommand, ChatReply, GroupMessageEvent
from app.handlers.task_handler import (
    _format_duration,
    _format_task_list,
    _format_time,
    _parse_task_schedule,
)


def _format_help() -> str:
    return "\n".join(
        [
            "管理命令：",
            "/memory add <内容>",
            "/memory add 后换行，每行会保存成一条记忆",
            "/memory delete <编号或关键词>，可一次写多个",
            "/memory list",
            "/memory clear",
            "/context clear",
            "/summary show",
            "/summary clear",
            "/weather <地点>",
            "/news <关键词>",
            "/web <关键词或URL>",
            "/model show | list | use <模型名或模型ID>",
            "/task add in 10m <提示词>",
            "/task add at 2026-07-12 18:30 <提示词>",
            "/task add every 1h <提示词>",
            "/task add daily 08:00 <提示词>",
            "/task list",
            "/task delete <编号>",
            "/task clear",
        ]
    )


def _clean_memory_entry(line: str) -> str:
    line = line.strip()
    line = re.sub(r"^\s*(?:[-*+]|\d+[.)、])\s*", "", line)
    return line.strip()


def _split_memory_entries(payload: str) -> list[str]:
    payload = payload.strip()
    if not payload:
        return []
    if "\n" in payload:
        items = [_clean_memory_entry(line) for line in payload.splitlines()]
    elif ";;" in payload:
        items = [_clean_memory_entry(line) for line in payload.split(";;")]
    else:
        items = [_clean_memory_entry(payload)]
    return [item for item in items if item]


def _split_delete_targets(payload: str) -> list[str]:
    payload = payload.strip()
    if not payload:
        return []
    if "\n" in payload:
        raw_items = payload.splitlines()
    elif re.search(r"[,，;；]", payload):
        raw_items = re.split(r"[,，;；]", payload)
    else:
        try:
            raw_items = shlex.split(payload)
        except ValueError:
            raw_items = [payload]
    return [_clean_memory_entry(item).lstrip("#") for item in raw_items if _clean_memory_entry(item)]


def _format_memory_lines(rows: Sequence[dict[str, str]]) -> str:
    if not rows:
        return "当前没有保存的长期记忆。"
    lines = ["当前长期记忆："]
    for row in rows:
        lines.append(f"{row['id']}. {row['content']}")
    return "\n".join(lines)


class CommandHandler:
    def __init__(
        self,
        settings: Settings,
        memory: MemoryStore,
        llm: DeepSeekClient,
        llms: dict[str, DeepSeekClient],
    ) -> None:
        self.settings = settings
        self.memory = memory
        self.llm = llm
        self.llms = llms

    def _available_llms(self) -> dict[str, DeepSeekClient]:
        providers = {name.strip().lower(): client for name, client in self.llms.items() if client.api_key}
        if self.llm.api_key:
            providers.setdefault("deepseek", self.llm)
        return providers

    def _current_model_provider(self, group_id: str) -> str:
        providers = self._available_llms()
        selected = (self.memory.get_group_setting(group_id, "model_provider") or "").lower()
        if selected in providers:
            return selected
        default = self.settings.default_model_provider.lower()
        if default in providers:
            return default
        return next(iter(providers), "deepseek")

    def handle_command(self, event: GroupMessageEvent, command: BotCommand) -> ChatReply:
        if command.name == "help":
            return ChatReply(text=_format_help())
        if command.name == "unknown":
            return ChatReply(text="未知命令。发送 /help 查看可用命令。")

        if not self.settings.is_admin(event.user_id):
            return ChatReply(text="只有管理员可以使用管理命令。")

        if command.name == "memory":
            return self._handle_memory_command(event, command)
        if command.name == "task":
            return self._handle_task_command(event, command)
        if command.name == "model":
            return self._handle_model_command(event, command)
        if command.name == "context" and command.action == "clear":
            cleared = self.memory.clear_group_context(event.group_id)
            if cleared <= 0:
                return ChatReply(text="当前没有可清空的上下文。")
            return ChatReply(text=f"已清空本群上下文和自动摘要，共 {cleared} 条聊天记录。")
        if command.name == "summary" and command.action == "show":
            summary = self.memory.get_group_summary(event.group_id)
            if not summary:
                return ChatReply(text="当前没有自动摘要。")
            return ChatReply(text=f"当前自动摘要：\n{summary['summary']}")
        if command.name == "summary" and command.action == "clear":
            deleted = self.memory.clear_group_summary(event.group_id)
            return ChatReply(text="已清空自动摘要。" if deleted else "当前没有自动摘要。")
        return ChatReply(text="未知命令。发送 /help 查看可用命令。")

    def _handle_memory_command(self, event: GroupMessageEvent, command: BotCommand) -> ChatReply:
        if command.action == "add":
            entries = _split_memory_entries(command.payload)
            if not entries:
                return ChatReply(text="用法：/memory add <内容>。多条记忆可换行填写。")
            ids = [
                self.memory.add_memory("group", event.group_id, entry, event.user_id)
                for entry in entries
            ]
            id_text = "、".join(f"#{memory_id}" for memory_id in ids)
            return ChatReply(text=f"已添加 {len(ids)} 条长期记忆：{id_text}")

        if command.action == "delete":
            targets = _split_delete_targets(command.payload)
            if not targets:
                return ChatReply(text="用法：/memory delete <编号或关键词>。可一次写多个。")
            deleted = sum(self._delete_memory(event, target) for target in targets)
            if deleted <= 0:
                return ChatReply(text="没有找到匹配的长期记忆。")
            return ChatReply(text=f"已删除 {deleted} 条长期记忆。")

        if command.action == "clear":
            deleted = self.memory.delete_memories_by_keyword("", scope="group", scope_id=event.group_id)
            return ChatReply(text=f"已清空本群长期记忆，共 {deleted} 条。")

        rows = self.memory.list_memories(
            scope="group",
            scope_id=event.group_id,
            limit=self.settings.memory_query_limit,
        )
        return ChatReply(text=_format_memory_lines(rows))

    def _handle_task_command(self, event: GroupMessageEvent, command: BotCommand) -> ChatReply:
        if command.action == "add":
            schedule = _parse_task_schedule(command.payload)
            if schedule is None:
                return ChatReply(
                    text=(
                        "用法：\n"
                        "/task add in 10m <提示词>\n"
                        "/task add at 2026-07-12 18:30 <提示词>\n"
                        "/task add every 1h <提示词>"
                    )
                )
            task_id = self.memory.add_scheduled_task(
                group_id=event.group_id,
                created_by=event.user_id,
                prompt=schedule.prompt,
                schedule_type=schedule.schedule_type,
                next_run_at=schedule.next_run_at,
                interval_seconds=schedule.interval_seconds,
                created_at=int(time.time()),
            )
            interval_text = ""
            if schedule.interval_seconds > 0:
                interval_text = f"，重复间隔 {_format_duration(schedule.interval_seconds)}"
            return ChatReply(text=f"已添加定时任务 #{task_id}，下次执行：{_format_time(schedule.next_run_at)}{interval_text}")

        if command.action == "list":
            rows = self.memory.list_scheduled_tasks(event.group_id)
            return ChatReply(text=_format_task_list(rows))

        if command.action == "delete":
            targets = _split_delete_targets(command.payload)
            if not targets:
                return ChatReply(text="用法：/task delete <编号>，可一次写多个。")
            deleted = 0
            for target in targets:
                if target.isdigit() and self.memory.delete_scheduled_task(int(target), group_id=event.group_id):
                    deleted += 1
            return ChatReply(text=f"已删除 {deleted} 个定时任务。" if deleted else "没有找到匹配的定时任务。")

        if command.action == "clear":
            deleted = self.memory.clear_scheduled_tasks(event.group_id)
            return ChatReply(text=f"已清空本群定时任务，共 {deleted} 个。")

        return ChatReply(text="未知任务命令。发送 /help 查看用法。")

    def _handle_model_command(self, event: GroupMessageEvent, command: BotCommand) -> ChatReply:
        providers = self._available_llms()
        current = self._current_model_provider(event.group_id)
        if command.action == "show":
            client = providers.get(current)
            model = client.model if client else "未配置"
            return ChatReply(text=f"当前模型：{current}（{model}）")
        if command.action == "list":
            lines = ["可用模型："]
            for name, client in providers.items():
                marker = "（当前）" if name == current else ""
                lines.append(f"- {name}: {client.model}{marker}")
            return ChatReply(text="\n".join(lines))
        provider = command.payload.strip().lower()
        client = providers.get(provider)
        if client is None:
            available = "、".join(providers) or "无"
            return ChatReply(text=f"模型 {provider or '未指定'} 不可用。当前可用：{available}")
        self.memory.set_group_setting(event.group_id, "model_provider", provider)
        return ChatReply(text=f"本群已切换到 {provider}（{client.model}）。")

    def _delete_memory(self, event: GroupMessageEvent, payload: str) -> int:
        payload = payload.strip()
        if payload.isdigit():
            return 1 if self.memory.delete_memory(int(payload), scope="group", scope_id=event.group_id) else 0
        return self.memory.delete_memories_by_keyword(payload, scope="group", scope_id=event.group_id)

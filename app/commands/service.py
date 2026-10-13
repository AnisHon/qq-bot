from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

from app.commands.contracts import (
    ArgumentSpec,
    CommandContext,
    CommandError,
    CommandErrorKind,
    CommandPolicy,
    CommandResult,
    CommandSpec,
    OptionSpec,
    ParsedCommand,
)
from app.commands.registry import CommandRegistry
from app.commands.renderer import QQCommandRenderer
from app.knowledge.documents import DocumentFilter, IndexState
from app.recipes.models import DatasetAvailability, PackVersionKey
from app.recipes.query import (
    MatchQuality,
    RecipeQueryResult,
    RecipeQueryState,
    ingredient_to_dict,
    locator_to_string,
)
from app.sfm.contracts import (
    MAX_SFM_SOURCE_BYTES,
    SFMTargetVersion,
    ValidationStatus,
    safe_diagnostic_code,
    safe_diagnostic_text,
)
from app.recipes.models import IngredientKind, validate_exact_identifier
from app.tools.agent import TOOL_REGISTRY
from app.tools.contracts import ToolResult, ToolStatus, format_search_results, tool_result_to_search_results
from app.usage_budget import BudgetScope, reset_budget_scope, set_budget_scope


LOGGER = logging.getLogger(__name__)
CONFIRMATION_TTL_SECONDS = 300
MAX_PENDING_CONFIRMATIONS = 128
SAFE_GROUP_CONFIG_KEYS = frozenset({"model_provider", "default_pack"})


@dataclass(frozen=True, slots=True)
class _PendingConfirmation:
    actor_id: str
    group_id: str
    operation: str
    target: str
    expires_at: float


class CommandService:
    """Command handlers over the existing Bot, Tool, and domain services."""

    def __init__(self, settings, memory, llm_handler, storage_executor=None) -> None:
        self.settings = settings
        self.memory = memory
        self.llm_handler = llm_handler
        self.storage_executor = storage_executor
        self.registry = CommandRegistry()
        self.renderer = QQCommandRenderer()
        self._pending_confirmations: dict[str, _PendingConfirmation] = {}
        self._register_commands()

    @staticmethod
    def is_command_text(text: str) -> bool:
        return isinstance(text, str) and text.lstrip().startswith("/")

    async def dispatch(self, event, text: str, on_tool_call=None) -> CommandResult:
        context = CommandContext(
            event=event,
            settings=self.settings,
            memory=self.memory,
            llm_handler=self.llm_handler,
            storage_executor=self.storage_executor,
            request_id=str(event.message_id or secrets.token_hex(8)),
            is_admin=bool(event.user_id and self.settings.is_admin(event.user_id)),
            services={
                "documents": self.llm_handler.document_knowledge,
                "recipes": self.llm_handler.recipe_query_service,
                "repositories": self.llm_handler.local_repository_service,
                "sfm_knowledge": self.llm_handler.sfm_knowledge_service,
                "sfm_validator": self.llm_handler.sfm_validation_service,
                "on_tool_call": on_tool_call,
            },
        )
        return await self.registry.dispatch(text, context)

    def _register_commands(self) -> None:
        public = CommandPolicy()
        admin = CommandPolicy(admin_only=True)
        register = self.registry.register

        def root(name: str, description: str) -> None:
            register(CommandSpec((name,), description, f"/{name}", policy=public))

        root("model", "查看或切换本群模型")
        register(CommandSpec(("model", "show"), "查看当前模型", "/model show", self._model_show))
        register(CommandSpec(("model", "list"), "列出已配置模型", "/model list", self._model_list))
        register(CommandSpec(("model", "use"), "切换本群模型", "/model use <model>", self._model_use,
                             (ArgumentSpec("model", max_length=128),), policy=admin))

        root("context", "查看、清理或查看摘要")
        register(CommandSpec(("context", "show"), "查看本群上下文状态", "/context show", self._context_show))
        register(CommandSpec(("context", "summary"), "查看已有自动摘要", "/context summary", self._context_summary))
        register(CommandSpec(("context", "clear"), "清除本群上下文和摘要", "/context clear", self._context_clear,
                             policy=admin))

        root("memory", "管理本群长期记忆")
        register(CommandSpec(("memory", "list"), "查看本群长期记忆", "/memory list", self._memory_list, policy=admin))
        register(CommandSpec(("memory", "add"), "添加长期记忆", "/memory add <content>", self._memory_add,
                             (ArgumentSpec("content", variadic=True, max_length=8000),), policy=admin,
                             allow_multiline=True))
        register(CommandSpec(("memory", "delete"), "按编号删除一条记忆", "/memory delete <id>", self._memory_delete,
                             (ArgumentSpec("id", value_type="int"),), policy=admin))
        register(CommandSpec(("memory", "clear"), "清除本群全部长期记忆", "/memory clear", self._memory_clear,
                             policy=admin))

        root("task", "管理本群定时任务")
        register(CommandSpec(("task", "list"), "列出本群定时任务", "/task list", self._task_list, policy=admin))
        register(CommandSpec(("task", "add"), "创建定时任务", "/task add <description> --in 10m | --at \"YYYY-MM-DD HH:MM\" | --every 1h | --daily HH:MM",
                             self._task_add, (ArgumentSpec("description", variadic=True, max_length=2000),),
                             (OptionSpec("in"), OptionSpec("at"), OptionSpec("every"), OptionSpec("daily")),
                             policy=admin))
        register(CommandSpec(("task", "cancel"), "取消本群的指定任务", "/task cancel <id>", self._task_cancel,
                             (ArgumentSpec("id", value_type="int"),), policy=admin))

        root("pack", "选择本群精确的 Minecraft 整合包数据版本")
        register(CommandSpec(("pack", "show"), "查看本群整合包选择", "/pack show", self._pack_show))
        register(CommandSpec(("pack", "list"), "列出实际可用的配方数据集", "/pack list", self._pack_list))
        register(CommandSpec(("pack", "use"), "设置本群整合包和版本", "/pack use <pack> <version> [--game-version <version>]",
                             self._pack_use, (ArgumentSpec("pack", max_length=128), ArgumentSpec("version", max_length=128)),
                             (OptionSpec("game-version", max_length=64),), policy=admin))

        register(CommandSpec(("wiki",), "检索本地知识库", "/wiki <query>", self._wiki,
                             (ArgumentSpec("query", variadic=True, max_length=2048),)))
        register(CommandSpec(("recipe",), "按精确物品 ID 查询配方", "/recipe <item-id> [--direction produces|consumes] [--kind item|fluid]",
                             self._recipe, (ArgumentSpec("item_id", max_length=256),),
                             (OptionSpec("direction", choices=("produces", "consumes")),
                              OptionSpec("kind", choices=("item", "fluid")))))
        register(CommandSpec(("machine",), "查询机器支持的配方", "/machine <machine-id>", self._machine,
                             (ArgumentSpec("machine_id", max_length=256),)))

        root("sfm", "检索 SFM 文档或检查程序")
        register(CommandSpec(("sfm", "help"), "查看 SFM 功能和版本要求", "/sfm help", self._sfm_help))
        register(CommandSpec(("sfm", "search"), "检索精确版本的 SFM 文档", "/sfm search <query> --sfm-version <version> --mc-version <version>",
                             self._sfm_search, (ArgumentSpec("query", variadic=True, max_length=1024),),
                             (OptionSpec("sfm-version", required=True, max_length=128),
                              OptionSpec("mc-version", required=True, max_length=128))))
        register(CommandSpec(("sfm", "validate"), "调用配置的 SFM Validator", "/sfm validate <code> --sfm-version <version> --mc-version <version>",
                             self._sfm_validate, (ArgumentSpec("code", variadic=True, max_length=65536),),
                             (OptionSpec("sfm-version", required=True, max_length=128),
                              OptionSpec("mc-version", required=True, max_length=128)), allow_multiline=True))

        root("repo", "只读搜索白名单 Git 仓库")
        register(CommandSpec(("repo", "search"), "搜索仓库路径或文本", "/repo search <query> --repo <id> [--mode path|text] [--commit <sha>]",
                             self._repo_search, (ArgumentSpec("query", variadic=True, max_length=256),),
                             (OptionSpec("repo", required=True, max_length=64),
                              OptionSpec("mode", choices=("path", "text")),
                              OptionSpec("commit", max_length=64))))
        register(CommandSpec(("repo", "read"), "读取固定 commit 中的安全文件行范围", "/repo read <path> --repo <id> --commit <sha> --start <line> --end <line>",
                             self._repo_read, (ArgumentSpec("path", max_length=4096),),
                             (OptionSpec("repo", required=True, max_length=64),
                              OptionSpec("commit", required=True, max_length=64),
                              OptionSpec("start", value_type="int", required=True),
                              OptionSpec("end", value_type="int", required=True))))

        register(CommandSpec(("weather",), "查询天气", "/weather <location>", self._weather,
                             (ArgumentSpec("location", variadic=True, max_length=256),)))
        register(CommandSpec(("news",), "搜索近期新闻", "/news [topic]", self._news,
                             (ArgumentSpec("topic", variadic=True, required=False, max_length=256),)))
        register(CommandSpec(("web",), "搜索网页或读取 URL", "/web <query-or-url>", self._web,
                             (ArgumentSpec("query", variadic=True, max_length=1024),)))

        root("config", "查看或修改安全白名单内的群配置")
        register(CommandSpec(("config", "show"), "查看安全群配置", "/config show", self._config_show))
        register(CommandSpec(("config", "set"), "修改白名单内的群配置", "/config set <key> <value> [--version <pack-version>] [--game-version <version>]",
                             self._config_set, (ArgumentSpec("key", max_length=64), ArgumentSpec("value", max_length=128)),
                             (OptionSpec("version", max_length=128), OptionSpec("game-version", max_length=64)),
                             policy=admin))

        register(CommandSpec(("confirm",), "确认一项待执行的群级操作", "/confirm <token>", self._confirm,
                             (ArgumentSpec("token", max_length=64),), policy=admin))
        register(CommandSpec(("help",), "根据当前权限显示帮助", "/help [command ...]", self._help,
                             (ArgumentSpec("command", required=False, variadic=True, max_length=64),)))
        register(CommandSpec(("status",), "查看 Bot、模型和知识服务状态", "/status", self._status))
        register(CommandSpec(("usage",), "查看当前授权范围的请求和 token 统计", "/usage", self._usage))

    async def _help(self, context: CommandContext, command: ParsedCommand) -> CommandResult:
        raw_path = str(command.arguments.get("command", ""))
        path = tuple(item.casefold().lstrip("/") for item in raw_path.split())
        return CommandResult.success(self.registry.render_help(path, context))

    async def _status(self, context: CommandContext, _: ParsedCommand) -> CommandResult:
        provider = await self.llm_handler._current_model_provider(context.group_id)
        client = self.llm_handler._available_llms().get(provider)
        model_state = f"{provider} / {client.model}" if client is not None else "未配置"
        documents = await context.services["documents"].catalog()
        if documents is None:
            document_state = "未配置"
        else:
            indexed = sum(1 for item in documents if getattr(item.state, "value", item.state) == "indexed")
            document_state = f"{indexed} 个已索引来源 / {len(documents)} 个来源"
        datasets = await self._db(context, context.services["recipes"].available_datasets)
        selected = _selected_pack(await self._group_setting(context, "default_pack"))
        recipe_state = f"{len(datasets)} 个活动数据集；本群{'已选择版本' if selected else '未选择版本'}"
        validator = context.services["sfm_validator"]
        validator_id = getattr(getattr(validator, "validator", None), "validator_id", "unavailable")
        real_validator = bool(validator.has_configured_validator and validator_id != "fake")
        sfm_state = (
            f"已配置：{safe_diagnostic_text(str(validator_id), limit=128)}"
            if real_validator else ("仅测试模拟" if validator_id == "fake" else "不可用")
        )
        tool_count = len(TOOL_REGISTRY.names())
        repositories = context.services["repositories"].configured_repo_ids
        return CommandResult.success(
            "Bot：正在运行\n"
            f"模型：{model_state}\n"
            f"本地知识：{document_state}\n"
            f"配方：{recipe_state}\n"
            f"Agent Tool Calling：{'启用' if self.settings.agent_tools_enabled else '关闭'}\n"
            f"工具：{tool_count} 个已注册；本地 Git 白名单 {len(repositories)} 个；Web/天气连通性未主动探测\n"
            f"SFM Validator：{sfm_state}"
        )

    async def _usage(self, context: CommandContext, _: ParsedCommand) -> CommandResult:
        client = await self.llm_handler._llm_for_group(context.group_id)
        controller = getattr(client, "budget_controller", None)
        if controller is None or not hasattr(controller, "get_usage_snapshot"):
            raise CommandError(CommandErrorKind.NOT_CONFIGURED, "当前模型没有可查询的 usage 账本。")
        if context.is_admin:
            snapshot = await controller.get_usage_snapshot("group", context.group_id)
            label = f"本群 {context.group_id} 今日 UTC"
        else:
            if not context.user_id:
                raise CommandError(CommandErrorKind.NOT_CONFIGURED, "当前消息没有可计量的用户标识。")
            snapshot = await controller.get_usage_snapshot("user", context.user_id)
            label = "你的今日 UTC"
        return CommandResult.success(_format_usage(label, snapshot))

    async def _model_show(self, context: CommandContext, _: ParsedCommand) -> CommandResult:
        provider = await self.llm_handler._current_model_provider(context.group_id)
        client = self.llm_handler._available_llms().get(provider)
        if client is None:
            return CommandResult(CommandErrorKind.NOT_CONFIGURED, "当前没有可用模型配置。")
        return CommandResult.success(f"当前模型：{provider}（{client.model}）")

    async def _model_list(self, context: CommandContext, _: ParsedCommand) -> CommandResult:
        providers = self.llm_handler._available_llms()
        current = await self.llm_handler._current_model_provider(context.group_id)
        if not providers:
            return CommandResult(CommandErrorKind.NOT_CONFIGURED, "当前没有已配置模型。")
        lines = ["可用模型："]
        lines.extend(f"- {name}: {client.model}{'（当前）' if name == current else ''}" for name, client in providers.items())
        return CommandResult.success("\n".join(lines))

    async def _model_use(self, context: CommandContext, command: ParsedCommand) -> CommandResult:
        model = command.arguments["model"].casefold()
        client = self.llm_handler._available_llms().get(model)
        if client is None:
            available = "、".join(self.llm_handler._available_llms()) or "无"
            raise CommandError(CommandErrorKind.NOT_FOUND, f"模型 {model} 不可用。当前可用：{available}")
        await self._db(context, self.memory.set_group_setting, context.group_id, "model_provider", model)
        return CommandResult.success(f"本群已切换到 {model}（{client.model}）。")

    async def _context_show(self, context: CommandContext, _: ParsedCommand) -> CommandResult:
        count, summary = await asyncio.gather(
            self._db(context, self.memory.count_group_history, context.group_id),
            self._db(context, self.memory.get_group_summary, context.group_id),
        )
        return CommandResult.success(
            f"本群上下文：{count} 条聊天记录；自动摘要{'已生成' if summary else '尚无'}。"
        )

    async def _context_summary(self, context: CommandContext, _: ParsedCommand) -> CommandResult:
        summary = await self._db(context, self.memory.get_group_summary, context.group_id)
        if not summary:
            return CommandResult(CommandErrorKind.NOT_FOUND, "当前没有自动摘要。")
        return CommandResult.success(f"本群自动摘要：\n{summary['summary']}")

    async def _context_clear(self, context: CommandContext, _: ParsedCommand) -> CommandResult:
        return self._request_confirmation(context, "context.clear", f"group:{context.group_id}")

    async def _memory_list(self, context: CommandContext, _: ParsedCommand) -> CommandResult:
        display_limit = max(1, min(100, int(self.settings.memory_query_limit)))
        rows = await self._db(
            context, self.memory.list_memories, scope="group", scope_id=context.group_id,
            limit=display_limit + 1,
        )
        if not rows:
            return CommandResult.success("当前没有保存的长期记忆。")
        lines = ["本群长期记忆：", *(f"{row['id']}. {row['content']}" for row in rows[:display_limit])]
        if len(rows) > display_limit:
            lines.append(f"已截断：当前显示前 {display_limit} 条。")
        return CommandResult.success("\n".join(lines))

    async def _memory_add(self, context: CommandContext, command: ParsedCommand) -> CommandResult:
        content = str(command.arguments.get("content", "")).strip()
        entries = [line.strip() for line in content.splitlines() if line.strip()]
        if not entries:
            raise CommandError(CommandErrorKind.INVALID_ARGUMENT, "用法：/memory add <content>")
        if len(entries) > 20:
            raise CommandError(CommandErrorKind.INVALID_ARGUMENT, "一次最多添加 20 条记忆。")
        ids = []
        for entry in entries:
            clean = _clean_memory_text(entry)
            ids.append(await self._db(context, self.memory.add_memory, "group", context.group_id, clean, context.user_id))
        return CommandResult.success("已添加本群长期记忆：" + "、".join(f"#{item}" for item in ids))

    async def _memory_delete(self, context: CommandContext, command: ParsedCommand) -> CommandResult:
        deleted = await self._db(
            context, self.memory.delete_memory, command.arguments["id"],
            scope="group", scope_id=context.group_id,
        )
        if not deleted:
            raise CommandError(CommandErrorKind.NOT_FOUND, "本群没有这条记忆。")
        return CommandResult.success("已删除这条本群记忆。")

    async def _memory_clear(self, context: CommandContext, _: ParsedCommand) -> CommandResult:
        return self._request_confirmation(context, "memory.clear", f"group:{context.group_id}")

    async def _task_list(self, context: CommandContext, _: ParsedCommand) -> CommandResult:
        rows = await self._db(context, self.memory.list_scheduled_tasks, context.group_id, 51)
        if not rows:
            return CommandResult.success("当前没有定时任务。")
        lines = ["本群定时任务："]
        for row in rows[:20]:
            lines.append(f"#{row['id']} 下次执行 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(row['next_run_at'])))}：{str(row['prompt'])[:100]}")
        if len(rows) > 20:
            count_note = f"当前至少 {len(rows)} 项" if len(rows) > 50 else f"共 {len(rows)} 项"
            lines.append(f"仅显示前 20 项；{count_note}。")
        return CommandResult.success("\n".join(lines))

    async def _task_add(self, context: CommandContext, command: ParsedCommand) -> CommandResult:
        description = str(command.arguments.get("description", "")).strip()
        options = command.options
        schedule_options = [name for name in ("in", "at", "every", "daily") if name in options]
        if not description:
            raise CommandError(CommandErrorKind.INVALID_ARGUMENT, "缺少任务描述。")
        if len(schedule_options) != 1:
            raise CommandError(CommandErrorKind.INVALID_ARGUMENT,
                               "必须且只能指定一个计划选项：--in、--at、--every 或 --daily。")
        schedule = schedule_options[0]
        raw = f"{schedule} {options[schedule]} {description}"
        from app.handlers.task_handler import _format_duration, _format_time, _parse_task_schedule

        parsed = _parse_task_schedule(raw)
        if parsed is None:
            raise CommandError(CommandErrorKind.INVALID_ARGUMENT, f"时间格式无效。用法：{self._usage_for(('task', 'add'))}")
        now = int(time.time())
        if parsed.next_run_at <= now:
            raise CommandError(CommandErrorKind.INVALID_ARGUMENT, "任务时间必须晚于当前时间。")
        if parsed.next_run_at > now + 10 * 365 * 24 * 60 * 60:
            raise CommandError(CommandErrorKind.INVALID_ARGUMENT, "任务时间不能超过当前时间 10 年。")
        task_id = await self._db(
            context,
            self.memory.add_scheduled_task,
            group_id=context.group_id,
            created_by=context.user_id,
            prompt=parsed.prompt,
            schedule_type=parsed.schedule_type,
            next_run_at=parsed.next_run_at,
            interval_seconds=parsed.interval_seconds,
            created_at=int(time.time()),
        )
        repeat = f"，间隔 {_format_duration(parsed.interval_seconds)}" if parsed.interval_seconds else ""
        return CommandResult.success(f"已添加本群定时任务 #{task_id}，下次执行：{_format_time(parsed.next_run_at)}{repeat}")

    async def _task_cancel(self, context: CommandContext, command: ParsedCommand) -> CommandResult:
        deleted = await self._db(
            context, self.memory.delete_scheduled_task, command.arguments["id"], group_id=context.group_id
        )
        if not deleted:
            raise CommandError(CommandErrorKind.NOT_FOUND, "本群没有这项定时任务。")
        return CommandResult.success("已取消本群定时任务。")

    async def _pack_show(self, context: CommandContext, _: ParsedCommand) -> CommandResult:
        key = _selected_pack(await self._group_setting(context, "default_pack"))
        if key is None:
            return CommandResult.success("本群尚未选择整合包版本。管理员可用 /pack list 查看已索引文档与活动配方数据。")
        sources = await self._available_pack_sources(context)
        capabilities = sources.get(key, frozenset())
        if not capabilities:
            return CommandResult(
                CommandErrorKind.DATA_UNAVAILABLE,
                f"本群选择的 {key.pack_id} {key.pack_version}（Minecraft {key.game_version}）当前没有可用的本地数据源。",
            )
        lines = [f"本群整合包：{key.pack_id} {key.pack_version}（Minecraft {key.game_version}）"]
        if "documents" in capabilities:
            lines.append("本地文档：有已索引来源")
        else:
            lines.append("本地文档：没有此精确版本的已索引来源")
        if "recipes" in capabilities:
            status = await self._db(context, self.llm_handler.recipe_query_service.repository.dataset_status, key)
            lines.append(
                f"配方数据：{status.availability.value}，可查询 {status.queryable_recipe_count} 条，隔离 {status.quarantined_recipe_count} 条。"
            )
        else:
            lines.append("配方数据：此版本没有活动配方数据集")
        return CommandResult.success(
            "\n".join(lines)
        )

    async def _pack_list(self, context: CommandContext, _: ParsedCommand) -> CommandResult:
        sources = await self._available_pack_sources(context)
        if not sources:
            raise CommandError(CommandErrorKind.DATA_UNAVAILABLE, "当前没有带有版本元数据的已索引文档或活动配方数据集。")
        entries = sorted(
            sources.items(),
            key=lambda item: (item[0].pack_id, item[0].pack_version, item[0].game_version),
        )
        lines = ["可用整合包版本（精确版本与数据源）："]
        for key, capabilities in entries[:30]:
            kinds = "、".join(label for source, label in (("documents", "文档"), ("recipes", "配方")) if source in capabilities)
            lines.append(f"- {key.pack_id} {key.pack_version}（Minecraft {key.game_version}）：{kinds}")
        if len(entries) > 30:
            lines.append(f"仅显示前 30 项；共 {len(entries)} 项。")
        return CommandResult.success("\n".join(lines))

    async def _pack_use(self, context: CommandContext, command: ParsedCommand) -> CommandResult:
        pack_id = command.arguments["pack"]
        pack_version = command.arguments["version"]
        game_version = command.options.get("game_version")
        sources = await self._available_pack_sources(context)
        matches = [key for key in sources if key.pack_id == pack_id and key.pack_version == pack_version]
        if game_version:
            matches = [key for key in matches if key.game_version == game_version]
        if not matches:
            raise CommandError(CommandErrorKind.DATA_UNAVAILABLE,
                               "没有完全匹配的已索引文档或活动配方数据集；不会回退到其他版本。请检查 /pack list。")
        if len(matches) > 1:
            versions = "、".join(sorted(item.game_version for item in matches))
            raise CommandError(CommandErrorKind.INVALID_ARGUMENT,
                               f"该整合包版本对应多个 Minecraft 版本，请指定 --game-version：{versions}")
        key = matches[0]
        await self._set_pack(context, key)
        return CommandResult.success(f"本群整合包已设为 {key.pack_id} {key.pack_version}（Minecraft {key.game_version}）。")

    async def _wiki(self, context: CommandContext, command: ParsedCommand) -> CommandResult:
        query = command.arguments["query"]
        key = _selected_pack(await self._group_setting(context, "default_pack"))
        if key is None:
            raise CommandError(
                CommandErrorKind.NOT_CONFIGURED,
                "本群尚未选择整合包与版本。为避免混合不同版本的文档，请管理员先执行 /pack list 和 /pack use。",
            )
        filters = DocumentFilter(
            pack_id=key.pack_id,
            pack_version=key.pack_version,
            game_version=key.game_version,
        )
        result = await context.services["documents"].search(query, filters, limit=6, evidence_limit=6)
        rendered = _tool_result(result, "本地知识库")
        scope = f"知识范围：{key.pack_id} {key.pack_version}（Minecraft {key.game_version}）"
        return CommandResult(rendered.kind, f"{scope}\n{rendered.text}")

    async def _recipe(self, context: CommandContext, command: ParsedCommand) -> CommandResult:
        key = await self._require_pack(context)
        service = self.llm_handler.recipe_query_service
        identifier_kind = IngredientKind.ITEM_EXACT if command.options.get("kind", "item") == "item" else IngredientKind.FLUID_EXACT
        try:
            validate_exact_identifier(identifier_kind, command.arguments["item_id"])
        except ValueError:
            raise CommandError(CommandErrorKind.INVALID_ARGUMENT,
                               "请提供精确的 namespaced item/fluid ID，例如 mod:item_name。") from None
        result = await self._db(
            context,
            service.find_by_identifier,
            key,
            command.arguments["item_id"],
            direction=command.options.get("direction", "produces"),
            kind=command.options.get("kind", "item"),
            limit=10,
        )
        return CommandResult.success(_format_recipe_query(result)) if result.state in {
            RecipeQueryState.AVAILABLE, RecipeQueryState.NO_RESULTS
        } else CommandResult(_recipe_error_kind(result.state), _recipe_state_message(result))

    async def _machine(self, context: CommandContext, command: ParsedCommand) -> CommandResult:
        key = await self._require_pack(context)
        try:
            validate_exact_identifier(IngredientKind.ITEM_EXACT, command.arguments["machine_id"])
        except ValueError:
            raise CommandError(CommandErrorKind.INVALID_ARGUMENT,
                               "请提供精确的 namespaced machine ID，例如 mod:machine_name。") from None
        result = await self._db(
            context,
            self.llm_handler.recipe_query_service.find_by_machine,
            key,
            command.arguments["machine_id"],
            limit=10,
        )
        return CommandResult.success(_format_recipe_query(result)) if result.state in {
            RecipeQueryState.AVAILABLE, RecipeQueryState.NO_RESULTS
        } else CommandResult(_recipe_error_kind(result.state), _recipe_state_message(result))

    async def _sfm_help(self, _: CommandContext, __: ParsedCommand) -> CommandResult:
        return CommandResult.success(
            "SFM 命令：/sfm search <query> --sfm-version <version> --mc-version <version>；"
            "/sfm validate <code> --sfm-version <version> --mc-version <version>。\n"
            "validate 可将代码放在命令下一行并使用多行内容。\n"
            "validate 只报告配置的 Validator 结果；当前未配置真实 Parser 时会明确返回不可用。程序不会执行。"
        )

    async def _sfm_search(self, context: CommandContext, command: ParsedCommand) -> CommandResult:
        result = await context.services["sfm_knowledge"].search_examples(
            command.arguments["query"],
            sfm_version=command.options["sfm_version"],
            minecraft_version=command.options["mc_version"],
        )
        return _tool_result(result, "SFM 文档")

    async def _sfm_validate(self, context: CommandContext, command: ParsedCommand) -> CommandResult:
        source = command.arguments["code"]
        if not source.strip():
            raise CommandError(CommandErrorKind.INVALID_ARGUMENT, "缺少 SFM 程序内容。")
        try:
            source_bytes = len(source.encode("utf-8"))
        except UnicodeEncodeError:
            raise CommandError(CommandErrorKind.INVALID_ARGUMENT, "SFM 程序必须是有效的 UTF-8 文本。") from None
        if source_bytes > MAX_SFM_SOURCE_BYTES:
            raise CommandError(
                CommandErrorKind.INVALID_ARGUMENT,
                f"SFM 程序超过 {MAX_SFM_SOURCE_BYTES} 字节上限；请拆分后重试。",
            )
        target = SFMTargetVersion(command.options["sfm_version"], command.options["mc_version"])
        service = context.services["sfm_validator"]
        if not service.has_configured_validator:
            raise CommandError(CommandErrorKind.NOT_CONFIGURED, "没有配置经验证的 SFM Parser；未执行语法验证。")
        result = await service.validate(source, target=target)
        if result.status == ValidationStatus.UNAVAILABLE:
            raise CommandError(CommandErrorKind.NOT_CONFIGURED, "SFM Validator 不可用；未得到语法验证结论。")
        if result.is_simulated:
            label = "测试模拟结果，不代表真实 SFM 语法验证"
        elif result.status == ValidationStatus.VALID and result.is_real_validation:
            label = f"真实语法验证通过（{safe_diagnostic_text(result.validator_id, limit=128)}）"
        elif result.status == ValidationStatus.INVALID and result.is_real_validation:
            label = f"真实语法验证未通过（{safe_diagnostic_text(result.validator_id, limit=128)}）"
        elif result.status == ValidationStatus.VERSION_UNSUPPORTED:
            label = "Validator 不支持此版本"
        elif result.status == ValidationStatus.TIMEOUT:
            label = "SFM Validator 超时，未得到结论"
        else:
            label = "SFM Validator 执行失败，未得到结论"
        lines = [label]
        for diagnostic in result.diagnostics[:8]:
            location = f"第 {diagnostic.line} 行" if diagnostic.line else ""
            code = safe_diagnostic_code(diagnostic.code)
            message = safe_diagnostic_text(diagnostic.message)
            lines.append(f"- {location} {diagnostic.severity.value} [{code}]: {message}".strip())
        return CommandResult.success("\n".join(lines))

    async def _repo_search(self, context: CommandContext, command: ParsedCommand) -> CommandResult:
        args = {
            "repo_id": command.options["repo"],
            "query": command.arguments["query"],
            "mode": command.options.get("mode", "path"),
            "limit": 10,
            "offset": 0,
        }
        if "commit" in command.options:
            args["commit_sha"] = command.options["commit"]
        result = await self._dispatch_tool(context, "repository_search", args)
        return _tool_result(result, "Git 仓库")

    async def _repo_read(self, context: CommandContext, command: ParsedCommand) -> CommandResult:
        result = await self._dispatch_tool(context, "repository_read", {
            "repo_id": command.options["repo"],
            "commit_sha": command.options["commit"],
            "path": command.arguments["path"],
            "start_line": command.options["start"],
            "end_line": command.options["end"],
        })
        return _tool_result(result, "Git 仓库")

    async def _weather(self, context: CommandContext, command: ParsedCommand) -> CommandResult:
        result = await self._dispatch_tool(context, "weather", {"location": command.arguments["location"]})
        return _tool_result(result, "天气")

    async def _news(self, context: CommandContext, command: ParsedCommand) -> CommandResult:
        topic = command.arguments.get("topic") or "今天有什么新闻"
        result = await self._dispatch_tool(context, "news_search", {"query": topic})
        return _tool_result(result, "新闻")

    async def _web(self, context: CommandContext, command: ParsedCommand) -> CommandResult:
        query = command.arguments["query"]
        if query[:8].casefold().startswith(("https://", "http://")):
            result = await self._dispatch_tool(context, "fetch_url", {"url": query})
        else:
            result = await self._dispatch_tool(context, "web_search", {"query": query})
        return _tool_result(result, "网页")

    async def _config_show(self, context: CommandContext, _: ParsedCommand) -> CommandResult:
        provider = await self._group_setting(context, "model_provider")
        key = _selected_pack(await self._group_setting(context, "default_pack"))
        pack = f"{key.pack_id} {key.pack_version}（Minecraft {key.game_version}）" if key else "未选择"
        return CommandResult.success(f"本群安全配置：\nmodel_provider：{provider or '使用默认模型'}\ndefault_pack：{pack}")

    async def _config_set(self, context: CommandContext, command: ParsedCommand) -> CommandResult:
        key = command.arguments["key"].casefold()
        value = command.arguments["value"]
        if key not in SAFE_GROUP_CONFIG_KEYS:
            raise CommandError(CommandErrorKind.INVALID_ARGUMENT,
                               f"不允许修改 {key}；可配置项：{', '.join(sorted(SAFE_GROUP_CONFIG_KEYS))}。")
        if key == "model_provider":
            if command.options:
                raise CommandError(CommandErrorKind.INVALID_ARGUMENT, "model_provider 不接受版本选项。")
            return await self._model_use(context, ParsedCommand(
                self._spec(("model", "use")), {"model": value}, {}, raw_text=command.raw_text
            ))
        version = command.options.get("version")
        game_version = command.options.get("game_version")
        if not version:
            raise CommandError(CommandErrorKind.INVALID_ARGUMENT, "设置 default_pack 必须提供 --version。")
        return await self._select_pack(context, value, version, game_version)

    async def _confirm(self, context: CommandContext, command: ParsedCommand) -> CommandResult:
        token = command.arguments["token"]
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        self._purge_confirmations()
        pending = self._pending_confirmations.get(token_hash)
        if pending is None or pending.actor_id != context.user_id or pending.group_id != context.group_id:
            raise CommandError(CommandErrorKind.NOT_FOUND, "确认令牌无效、已过期，或不属于当前用户与群。")
        del self._pending_confirmations[token_hash]
        if not context.is_admin:
            raise CommandError(CommandErrorKind.PERMISSION_DENIED, "你没有权限执行此确认操作。")
        if pending.operation == "context.clear" and pending.target == f"group:{context.group_id}":
            count = await self._db(context, self.memory.clear_group_context, context.group_id)
            return CommandResult.success(f"已清除本群上下文和自动摘要，共 {count} 条聊天记录。")
        if pending.operation == "memory.clear" and pending.target == f"group:{context.group_id}":
            count = await self._db(context, self.memory.delete_memories_by_keyword,
                                   "", scope="group", scope_id=context.group_id)
            return CommandResult.success(f"已清空本群长期记忆，共 {count} 条。")
        raise CommandError(CommandErrorKind.INVALID_ARGUMENT, "确认操作已失效，请重新发起命令。")

    def _request_confirmation(self, context: CommandContext, operation: str, target: str) -> CommandResult:
        self._purge_confirmations()
        raw_token = secrets.token_urlsafe(9)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        if len(self._pending_confirmations) >= MAX_PENDING_CONFIRMATIONS:
            oldest = min(self._pending_confirmations, key=lambda key: self._pending_confirmations[key].expires_at)
            del self._pending_confirmations[oldest]
        self._pending_confirmations[token_hash] = _PendingConfirmation(
            actor_id=context.user_id,
            group_id=context.group_id,
            operation=operation,
            target=target,
            expires_at=time.monotonic() + CONFIRMATION_TTL_SECONDS,
        )
        title = "清除本群上下文和自动摘要" if operation == "context.clear" else "清除本群全部长期记忆"
        return CommandResult.success(
            f"请确认：{title}。此令牌 5 分钟内有效且仅限当前用户和群使用。\n/confirm {raw_token}"
        )

    def _purge_confirmations(self) -> None:
        now = time.monotonic()
        for key in [key for key, item in self._pending_confirmations.items() if item.expires_at <= now]:
            del self._pending_confirmations[key]

    async def _dispatch_tool(self, context: CommandContext, name: str, arguments: dict[str, Any]) -> ToolResult:
        on_tool_call = context.services.get("on_tool_call")
        if on_tool_call is not None:
            try:
                await on_tool_call("正在查询，请稍等。")
            except Exception:  # noqa: BLE001
                pass
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.network_tool_timeout
        scope = BudgetScope(
            group_id=context.group_id,
            user_id=context.user_id or None,
            request_id=context.request_id,
        )
        budget_token = set_budget_scope(scope)
        try:
            tool_context = await self.llm_handler.command_tool_context(
                context.event, deadline_monotonic=deadline
            )
            return await asyncio.wait_for(
                TOOL_REGISTRY.dispatch(name, arguments, tool_context),
                timeout=self.settings.network_tool_timeout,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return ToolResult(ToolStatus.TIMED_OUT, "工具请求超时。")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("command tool failed (%s)", type(exc).__name__)
            return ToolResult(ToolStatus.ERROR, "工具执行失败。")
        finally:
            reset_budget_scope(budget_token)

    async def _require_pack(self, context: CommandContext) -> PackVersionKey:
        key = _selected_pack(await self._group_setting(context, "default_pack"))
        if key is None:
            raise CommandError(CommandErrorKind.NOT_CONFIGURED,
                               "本群尚未选择整合包版本。管理员可先执行 /pack list 和 /pack use。")
        status = await self._db(context, self.llm_handler.recipe_query_service.repository.dataset_status, key)
        if status.availability not in {DatasetAvailability.AVAILABLE, DatasetAvailability.EMPTY}:
            kind = CommandErrorKind.NOT_FOUND if status.availability == DatasetAvailability.VERSION_MISMATCH else CommandErrorKind.DATA_UNAVAILABLE
            raise CommandError(kind, _dataset_status_text(status.availability, key))
        return key

    async def _select_pack(self, context: CommandContext, pack_id: str, version: str,
                           game_version: str | None) -> CommandResult:
        sources = await self._available_pack_sources(context)
        matches = [key for key in sources if key.pack_id == pack_id and key.pack_version == version]
        if game_version:
            matches = [key for key in matches if key.game_version == game_version]
        if not matches:
            raise CommandError(CommandErrorKind.DATA_UNAVAILABLE,
                               "没有完全匹配的已索引文档或活动配方数据集；不会回退到其他版本。请检查 /pack list。")
        if len(matches) != 1:
            raise CommandError(CommandErrorKind.INVALID_ARGUMENT,
                               "该整合包版本对应多个 Minecraft 版本，请指定 --game-version。")
        await self._set_pack(context, matches[0])
        key = matches[0]
        return CommandResult.success(f"本群整合包已设为 {key.pack_id} {key.pack_version}（Minecraft {key.game_version}）。")

    async def _set_pack(self, context: CommandContext, key: PackVersionKey) -> None:
        if key not in await self._available_pack_sources(context):
            raise CommandError(CommandErrorKind.DATA_UNAVAILABLE, "指定版本没有可用的已索引文档或活动配方数据，未更改本群配置。")
        encoded = json.dumps({"pack_id": key.pack_id, "pack_version": key.pack_version,
                              "game_version": key.game_version}, ensure_ascii=False, separators=(",", ":"))
        await self._db(context, self.memory.set_group_setting, context.group_id, "default_pack", encoded)

    async def _available_pack_sources(self, context: CommandContext) -> dict[PackVersionKey, frozenset[str]]:
        sources: dict[PackVersionKey, set[str]] = {}
        datasets = await self._db(context, self.llm_handler.recipe_query_service.available_datasets)
        for dataset in datasets:
            sources.setdefault(dataset.key, set()).add("recipes")

        document_catalog = await context.services["documents"].catalog()
        for status in document_catalog or ():
            source = status.source
            if source is None or status.state not in {IndexState.INDEXED, IndexState.EMPTY}:
                continue
            if not source.pack_id or not source.pack_version or not source.game_version:
                continue
            try:
                key = PackVersionKey(source.pack_id, source.pack_version, source.game_version)
            except ValueError:
                continue
            sources.setdefault(key, set()).add("documents")
        return {key: frozenset(values) for key, values in sources.items()}

    async def _group_setting(self, context: CommandContext, key: str) -> str | None:
        return await self._db(context, self.memory.get_group_setting, context.group_id, key)

    async def _db(self, context: CommandContext, function, /, *args, **kwargs):
        if context.storage_executor is not None:
            operation = asyncio.create_task(
                context.storage_executor.run_sqlite(function, *args, **kwargs),
                name="slash-command-sqlite-operation",
            )
        else:
            operation = asyncio.create_task(
                asyncio.to_thread(function, *args, **kwargs),
                name="slash-command-sqlite-operation",
            )
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            # The worker thread may still finish a submitted operation; don't
            # hold the command deadline open while it does. Retrieve any late
            # exception so the background task cannot leak an unhandled error.
            operation.add_done_callback(_consume_cancelled_operation)
            raise

    def _spec(self, path: tuple[str, ...]) -> CommandSpec:
        return next(spec for spec in self.registry.specs if spec.path == path)

    def _usage_for(self, path: tuple[str, ...]) -> str:
        return self._spec(path).usage


def _selected_pack(value: str | None) -> PackVersionKey | None:
    if not value:
        return None
    try:
        payload = json.loads(value)
        return PackVersionKey(payload["pack_id"], payload["pack_version"], payload["game_version"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _dataset_status_text(state: DatasetAvailability, key: PackVersionKey) -> str:
    suffix = f"{key.pack_id} {key.pack_version} / Minecraft {key.game_version}"
    if state == DatasetAvailability.VERSION_MISMATCH:
        return f"有其他版本的数据，但没有 {suffix} 的精确数据集；不会回退到其他版本。"
    if state == DatasetAvailability.EMPTY:
        return f"{suffix} 的数据集已激活，但没有可查询配方。"
    return f"没有 {suffix} 的配方数据集；不会回退到其他版本。请管理员检查数据导入状态。"


def _recipe_error_kind(state: RecipeQueryState) -> CommandErrorKind:
    if state == RecipeQueryState.VERSION_MISMATCH:
        return CommandErrorKind.NOT_FOUND
    if state == RecipeQueryState.DATA_UNAVAILABLE:
        return CommandErrorKind.DATA_UNAVAILABLE
    return CommandErrorKind.NOT_FOUND


def _recipe_state_message(result: RecipeQueryResult) -> str:
    if result.state == RecipeQueryState.DATA_UNAVAILABLE:
        return "所选整合包版本没有可用配方数据；不会查询其他版本。"
    if result.state == RecipeQueryState.VERSION_MISMATCH:
        return "只有其他版本的配方数据；不会回退到其他版本。"
    if result.state == RecipeQueryState.EMPTY:
        return "该版本数据集已激活，但没有可查询配方。"
    return "没有找到匹配的配方。"


def _format_recipe_query(result: RecipeQueryResult) -> str:
    if result.state == RecipeQueryState.NO_RESULTS:
        return "所选整合包版本中没有匹配配方。"
    quality = {
        MatchQuality.EXACT: "精确匹配",
        MatchQuality.CONDITIONAL: "含条件匹配",
        MatchQuality.UNSUPPORTED: "包含未支持条件",
    }.get(result.match_quality, "匹配")
    lines = [
        f"查询结果：{quality}；整合包 {result.key.pack_id} {result.key.pack_version}"
        f"（Minecraft {result.key.game_version}）；数据集 {result.dataset_id or 'unknown'}；来源 {result.source_id or 'unknown'}。"
    ]
    for index, recipe in enumerate(result.recipes[:10], start=1):
        lines.append(f"{index}. 配方 {recipe.recipe_id}；机器：{', '.join(recipe.machine_ids) or '未标注'}")
        lines.append("   输入：" + ("；".join(_ingredient_label(item) for item in recipe.inputs) or "无"))
        lines.append("   输出：" + ("；".join(_ingredient_label(item) for item in recipe.outputs) or "无"))
        if recipe.energy_eut is not None:
            lines.append(f"   能耗：{recipe.energy_eut} EU/t")
        if recipe.duration_ticks is not None:
            lines.append(f"   时间：{recipe.duration_ticks} ticks")
        if recipe.conditions:
            lines.append("   条件：" + json.dumps(recipe.conditions, ensure_ascii=False)[:400])
        if index <= len(result.source_locators) and result.source_locators[index - 1] is not None:
            lines.append(f"   来源位置：{locator_to_string(result.source_locators[index - 1])}")
    if result.has_more:
        lines.append(f"还有结果未显示（共 {result.total_matches} 条）。")
    if result.quarantined_recipe_count:
        lines.append(f"另有 {result.quarantined_recipe_count} 条含未知关键条件的配方已隔离，不参与查询。")
    return "\n".join(lines)


def _ingredient_label(item) -> str:
    detail = ingredient_to_dict(item)
    prefix = "" if detail["interpretation"] == "exact" else f"[{detail['interpretation']}] "
    value = detail.get("id") or detail["kind"]
    amount = f" × {detail['amount']} {detail['unit'] or ''}" if detail.get("amount") is not None else ""
    role = f" ({detail['role']})" if detail.get("role") not in {None, "material"} else ""
    consumed = "" if detail.get("consumed") is not False else "（不消耗）"
    probability = detail.get("probability")
    chance = f"（概率 {probability}）" if probability is not None and probability != "1" else ""
    constraints = detail.get("constraints")
    constraint_text = f" 约束={json.dumps(constraints, ensure_ascii=False)[:160]}" if constraints else ""
    return f"{prefix}{value}{amount}{role}{consumed}{chance}{constraint_text}"


def _tool_result(result: ToolResult, label: str) -> CommandResult:
    if result.status in {ToolStatus.NO_RESULTS, ToolStatus.NOT_INDEXED}:
        kind = CommandErrorKind.NOT_FOUND if result.status == ToolStatus.NO_RESULTS else CommandErrorKind.DATA_UNAVAILABLE
    elif result.status == ToolStatus.DATA_UNAVAILABLE:
        kind = CommandErrorKind.DATA_UNAVAILABLE
    elif result.status == ToolStatus.VERSION_MISMATCH:
        kind = CommandErrorKind.NOT_FOUND
    elif result.status == ToolStatus.INVALID_ARGUMENT:
        kind = CommandErrorKind.INVALID_ARGUMENT
    elif result.status == ToolStatus.DENIED:
        kind = CommandErrorKind.PERMISSION_DENIED
    elif result.status == ToolStatus.TIMED_OUT:
        kind = CommandErrorKind.TIMEOUT
    elif result.status == ToolStatus.CANCELLED:
        kind = CommandErrorKind.TIMEOUT
    elif result.status == ToolStatus.ERROR:
        kind = CommandErrorKind.INTERNAL_ERROR
    else:
        kind = CommandErrorKind.SUCCESS
    if isinstance(result.content, dict):
        text = result.content.get("text")
        if not isinstance(text, str) or not text.strip():
            search_results = tool_result_to_search_results(result)
            text = format_search_results(search_results, 5000) if search_results else "操作完成，但没有可显示的结果。"
    else:
        text = str(result.content)
    if result.evidence:
        cites = []
        for item in result.evidence[:8]:
            version = " / ".join(value for value in (item.pack_id, item.pack_version) if value)
            if version and item.revision:
                version = f"{version} / {item.revision}"
            elif not version and item.revision:
                version = item.revision
            details = "；".join(value for value in (version, item.locator) if value)
            if details:
                cites.append(f"来源：{item.title}（{details}）")
        if cites:
            text = f"{text}\n" + "\n".join(dict.fromkeys(cites))
    return CommandResult(kind, f"{text}" if text else f"{label}未返回结果。")


def _format_usage(label: str, snapshot: Any) -> str:
    reported = int(getattr(snapshot, "reported_requests", 0))
    reserved = int(getattr(snapshot, "reserved_requests", 0))
    unknown = int(getattr(snapshot, "unknown_requests", 0))
    input_tokens = getattr(snapshot, "reported_input_tokens", None)
    output_tokens = getattr(snapshot, "reported_output_tokens", None)
    lines = [f"{label} usage：共 {snapshot.request_count} 次模型请求"]
    if input_tokens is not None and output_tokens is not None:
        lines.append(f"Provider 已报告 token：输入 {input_tokens} / 输出 {output_tokens}（{reported} 次有 usage）")
    else:
        lines.append("Provider 没有提供可汇总的精确 token usage。")
    estimated = getattr(snapshot, "estimated_reserved_tokens", 0)
    estimated_unknown = getattr(snapshot, "estimated_unknown_tokens", 0)
    if estimated:
        lines.append(f"当前预留估算：约 {estimated} tokens（不是实际消耗）")
    if estimated_unknown:
        lines.append(f"usage 缺失请求的保守估算：约 {estimated_unknown} tokens（不是精确值）")
    if reserved:
        lines.append(f"尚未结算：{reserved} 次")
    if unknown:
        lines.append(f"usage 未知：{unknown} 次")
    cost = getattr(snapshot, "cost_minor_units", None)
    if cost is None:
        lines.append("费用：未知（没有可信费率账本）")
    else:
        lines.append(f"已记录费用：{cost} 个最小货币单位")
    return "\n".join(lines)


def _clean_memory_text(value: str) -> str:
    text = value.strip()
    for prefix in ("- ", "* ", "+ "):
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def _consume_cancelled_operation(operation: asyncio.Task[Any]) -> None:
    try:
        operation.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("SQLite command work failed after cancellation (%s)", type(exc).__name__)

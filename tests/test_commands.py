from __future__ import annotations

import asyncio
from decimal import Decimal
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.bot_service import BotService
from app.commands.contracts import (
    ArgumentSpec,
    CommandContext,
    CommandErrorKind,
    CommandPolicy,
    CommandResult,
    CommandSpec,
    OptionSpec,
)
from app.commands.parser import CommandParser
from app.commands.registry import CommandRegistry
from app.commands.renderer import QQCommandRenderer
from app.commands.service import CommandService
from app.config import Settings
from app.context.budget import ContextWindowBudget
from app.knowledge.documents import DocumentIndexStatus, DocumentSource, IndexState
from app.memory import MemoryStore
from app.models import GroupMessageEvent
from app.progress.coordinator import ProgressCoordinator
from app.progress.events import AgentEventKind, AgentEventPayload, TaskIdentity, ToolCategory
from app.qq_dispatcher import QQMessageDispatcher
from app.recipes.models import (
    DatasetState,
    Ingredient,
    IngredientKind,
    PackVersionKey,
    RawRecipeRecord,
    Recipe,
    RecipeDataset,
    RecipeOutput,
    SourceLocator,
    SourceMetadata,
)
from app.storage_executor import StorageExecutor
from app.tools.contracts import Evidence, ToolResult, ToolStatus


class CommandParserTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = CommandParser()
        self.spec = CommandSpec(
            ("search",),
            "test command",
            "/search <query> [--limit <int>] [--verbose]",
            arguments=(ArgumentSpec("query", variadic=True, max_length=256),),
            options=(OptionSpec("limit", value_type="int"), OptionSpec("verbose", flag=True)),
        )

    def test_quotes_escapes_chinese_and_long_options(self) -> None:
        parsed = self.parser.parse('/SEARCH "北京 \\"中心\\"" --limit=3 --verbose', (self.spec,))
        self.assertEqual(parsed.arguments["query"], '北京 "中心"')
        self.assertEqual(parsed.options, {"limit": 3, "verbose": True})

    def test_subcommand_and_option_names_are_case_insensitive(self) -> None:
        child = CommandSpec(("model", "use"), "switch", "/model use <name>",
                            arguments=(ArgumentSpec("name"),))
        parsed = self.parser.parse("/MODEL USE NVIDIA", (child,))
        self.assertEqual(parsed.spec.path, ("model", "use"))
        self.assertEqual(parsed.arguments["name"], "NVIDIA")

    def test_multiline_code_is_preserved_after_options(self) -> None:
        spec = CommandSpec(
            ("sfm", "validate"), "validate", "/sfm validate <code>",
            arguments=(ArgumentSpec("code", variadic=True, max_length=65536),),
            options=(OptionSpec("sfm-version", required=True),),
            allow_multiline=True,
        )
        parsed = self.parser.parse("/sfm validate --sfm-version 4.0\nEVERY 20 TICKS\n  INPUT west", (spec,))
        self.assertEqual(parsed.arguments["code"], "EVERY 20 TICKS\n  INPUT west")
        self.assertEqual(parsed.options["sfm_version"], "4.0")

    def test_unexpected_multiline_arguments_are_rejected_instead_of_ignored(self) -> None:
        with self.assertRaises(Exception):
            self.parser.parse("/search first line\nsecond line", (self.spec,))

    def test_invalid_arguments_quotes_and_lengths_are_rejected(self) -> None:
        with self.assertRaises(Exception):
            self.parser.parse('/search "unfinished', (self.spec,))
        with self.assertRaises(Exception):
            self.parser.parse("/search query --limit nope", (self.spec,))
        with self.assertRaises(Exception):
            self.parser.parse("/search " + "x" * 300, (self.spec,))

    def test_unknown_root_suggests_registered_command(self) -> None:
        recipe = CommandSpec(("recipe",), "recipe", "/recipe <item>")
        with self.assertRaises(Exception) as raised:
            self.parser.parse("/recpie diamond", (self.spec, recipe))
        self.assertIn("/recipe", str(raised.exception))

    def test_unknown_subcommand_is_not_reported_as_excess_arguments(self) -> None:
        root = CommandSpec(("model",), "models", "/model")
        show = CommandSpec(("model", "show"), "show", "/model show")
        with self.assertRaises(Exception) as raised:
            self.parser.parse("/model shwo", (root, show))
        self.assertIn("未知子命令", str(raised.exception))
        self.assertIn("/model show", str(raised.exception))


class CommandRegistryTest(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_command_path_is_rejected(self) -> None:
        registry = CommandRegistry()
        spec = CommandSpec(("status",), "status", "/status")
        registry.register(spec)
        with self.assertRaises(ValueError):
            registry.register(spec)

    async def test_help_is_generated_from_metadata_and_filtered_by_policy(self) -> None:
        registry = CommandRegistry()

        async def noop(_context, _parsed):
            return CommandResult.success("ok")

        registry.register(CommandSpec(("help",), "help", "/help", noop))
        registry.register(CommandSpec(("admin",), "admin root", "/admin"))
        registry.register(CommandSpec(("admin", "clear"), "clear data", "/admin clear", noop,
                                      policy=CommandPolicy(admin_only=True)))
        event = GroupMessageEvent("m", "g", "u", "user", "", False, 0, {})
        ordinary = CommandContext(event, SimpleNamespace(), None, None, is_admin=False)
        admin = CommandContext(event, SimpleNamespace(), None, None, is_admin=True)
        member_root_help = registry.render_help((), ordinary)
        admin_root_help = registry.render_help((), admin)
        self.assertIn("/admin — admin root", admin_root_help)
        self.assertNotIn("/admin clear", admin_root_help)
        self.assertNotIn("/admin", member_root_help)
        self.assertNotIn("/admin clear", registry.render_help((), ordinary))
        self.assertIn("/admin clear", registry.render_help(("admin",), admin))

    async def test_registry_dispatch_returns_uniform_command_result(self) -> None:
        async def handler(_context, _parsed):
            return CommandResult.success("done")

        registry = CommandRegistry()
        registry.register(CommandSpec(("go",), "run", "/go", handler))
        event = GroupMessageEvent("m", "g", "u", "user", "/go", False, 0, {})
        result = await registry.dispatch("/go", CommandContext(event, None, None, None))
        self.assertEqual(result.kind, CommandErrorKind.SUCCESS)
        self.assertEqual(result.text, "done")

    async def test_registry_handler_timeout_is_a_safe_command_result(self) -> None:
        started = asyncio.Event()

        async def blocks(_context, _parsed):
            started.set()
            await asyncio.Event().wait()

        registry = CommandRegistry()
        registry.register(CommandSpec(("slow",), "slow", "/slow", blocks, timeout_seconds=0.01))
        event = GroupMessageEvent("m", "g", "u", "user", "/slow", False, 0, {})
        result = await registry.dispatch("/slow", CommandContext(event, None, None, None))
        self.assertTrue(started.is_set())
        self.assertEqual(result.kind, CommandErrorKind.TIMEOUT)
        self.assertNotIn("Traceback", result.text)

    async def test_sqlite_command_deadline_does_not_wait_for_uncancellable_worker(self) -> None:
        class SlowExecutor:
            def __init__(self):
                self.started = asyncio.Event()
                self.finished = asyncio.Event()

            async def run_sqlite(self, function, /, *args, **kwargs):
                self.started.set()
                await asyncio.sleep(0.08)
                self.finished.set()
                return function(*args, **kwargs)

        executor = SlowExecutor()
        command_service = CommandService(SimpleNamespace(), None, None)
        event = GroupMessageEvent("m", "g", "u", "user", "/slow", False, 0, {})
        context = CommandContext(event, None, None, None, storage_executor=executor)

        async def slow(context, _parsed):
            await command_service._db(context, lambda: "finished")
            return CommandResult.success("done")

        registry = CommandRegistry()
        registry.register(CommandSpec(("slow",), "slow", "/slow", slow, timeout_seconds=0.01))
        result = await registry.dispatch("/slow", context)
        self.assertEqual(result.kind, CommandErrorKind.TIMEOUT)
        self.assertTrue(executor.started.is_set())
        self.assertFalse(executor.finished.is_set())
        await asyncio.wait_for(executor.finished.wait(), timeout=1)


class QQCommandRendererTest(unittest.TestCase):
    def test_cq_markup_in_untrusted_command_output_is_rendered_as_literal_text(self) -> None:
        from app.text_utils import split_qq_text

        result = QQCommandRenderer().render(
            CommandResult.success("外部结果。" * 180 + "\n[CQ:at,qq=all]")
        )
        self.assertNotIn("[CQ:", result.text.upper())
        chunks = split_qq_text(result.text, 850)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all("[CQ:" not in chunk.upper() for chunk in chunks))
        self.assertIn("[CQ：at,qq=all]", result.text)


class CommandServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "bot.db"
        self.settings = Settings(
            sqlite_path=self.db,
            bot_admin_qq_list=("admin",),
            agent_tools_enabled=False,
        )
        self.memory = MemoryStore(self.db, history_limit=50)

    def service(self, llm=None, settings=None) -> BotService:
        return BotService(
            settings=settings or self.settings,
            memory=self.memory,
            llm=llm or _DummyLLM(),
        )

    def test_permission_matrix_and_group_isolation(self) -> None:
        self.memory.add_memory("group", "group-a", "A only", "admin")
        service = self.service()
        ordinary = _run(service.handle_group_message(_event("/memory list", group="group-a", user="member")))
        self.assertEqual(ordinary.text, "你没有权限执行此命令。")
        visible = _run(service.handle_group_message(_event("/model show", group="group-a", user="member")))
        self.assertIn("当前模型", visible.text)
        admin_a = _run(service.handle_group_message(_event("/memory list", group="group-a", user="admin")))
        admin_b = _run(service.handle_group_message(_event("/memory list", group="group-b", user="admin")))
        self.assertIn("A only", admin_a.text)
        self.assertNotIn("A only", admin_b.text)

    def test_enforce_mode_rejects_model_switch_without_explicit_profile(self) -> None:
        llm = _DummyLLM()
        llm.context_budget = ContextWindowBudget((), mode="enforce")
        llm.context_profile_key = "deepseek:test-model"
        service = self.service(llm)

        reply = _run(
            service.handle_group_message(_event("/model use deepseek", group="group-a", user="admin"))
        )

        self.assertIn("Profile", reply.text)
        self.assertIsNone(self.memory.get_group_setting("group-a", "model_provider"))

    def test_help_hides_admin_subcommands_from_members(self) -> None:
        service = self.service()
        member_help = _run(service.handle_group_message(_event("/help", user="member")))
        admin_help = _run(service.handle_group_message(_event("/help", user="admin")))
        self.assertNotIn("/model use", member_help.text)
        self.assertNotIn("/memory clear", member_help.text)
        self.assertNotIn("/model use", admin_help.text)
        self.assertNotIn("/memory clear", admin_help.text)
        self.assertIn("/model — 查看或切换本群模型", admin_help.text)
        self.assertIn("/recipe", member_help.text)
        nested_help = _run(service.handle_group_message(_event("/HELP MODEL", user="member")))
        admin_nested_help = _run(service.handle_group_message(_event("/help model", user="admin")))
        flag_help = _run(service.handle_group_message(_event("/Model --HELP", user="member")))
        self.assertIn("/model show", nested_help.text)
        self.assertIn("/model show", flag_help.text)
        self.assertNotIn("/model use", flag_help.text)
        self.assertIn("/model use", admin_nested_help.text)

    def test_progress_mode_config_is_admin_only_group_scoped_and_shown(self) -> None:
        service = self.service()
        denied = _run(service.handle_group_message(_event(
            "/config set progress_mode verbose", group="group-a", user="member"
        )))
        self.assertEqual(denied.text, "你没有权限执行此命令。")
        updated = _run(service.handle_group_message(_event(
            "/config set progress_mode quiet", group="group-a", user="admin"
        )))
        self.assertIn("已设置为 quiet", updated.text)
        self.assertEqual(self.memory.get_group_setting("group-a", "progress_mode"), "quiet")
        self.assertIsNone(self.memory.get_group_setting("group-b", "progress_mode"))
        shown = _run(service.handle_group_message(_event(
            "/config show", group="group-a", user="member"
        )))
        self.assertIn("progress_mode：quiet", shown.text)
        invalid = _run(service.handle_group_message(_event(
            "/config set progress_mode loud", group="group-a", user="admin"
        )))
        self.assertIn("仅支持 quiet、balanced、verbose", invalid.text)

    def test_agent_status_reads_live_scoped_coordinator_without_exposing_task_details(self) -> None:
        service = self.service()
        coordinator = ProgressCoordinator()
        service.configure_progress(coordinator, object())
        identity = TaskIdentity.create(
            request_id="private-query-id", group_id="group-a", user_id="member",
            epoch=0, origin="user_message",
        )
        sink = coordinator.register(identity)
        sink.emit(AgentEventKind.TASK_STARTED)
        sink.emit(AgentEventKind.TOOL_STARTED, AgentEventPayload(
            category=ToolCategory.WEB_SEARCH,
        ), tool_call_id="private-tool-id")

        own = _run(service.handle_group_message(_event(
            "/agent status", group="group-a", user="member"
        )))
        other = _run(service.handle_group_message(_event(
            "/agent status", group="group-a", user="stranger"
        )))
        admin = _run(service.handle_group_message(_event(
            "/agent status", group="group-a", user="admin"
        )))
        self.assertIn("你的 Agent 任务正在运行", own.text)
        self.assertIn("没有正在运行", other.text)
        self.assertIn("1 个 Agent 任务", admin.text)
        self.assertNotIn("private-query-id", admin.text)
        self.assertNotIn("member", admin.text)

    def test_confirmed_context_reset_fences_live_progress_inside_dispatcher_barrier(self) -> None:
        service = self.service()
        coordinator = ProgressCoordinator()
        dispatcher = QQMessageDispatcher(adapter=object(), max_chars=500)  # type: ignore[arg-type]
        service.configure_progress(coordinator, dispatcher)

        async def exercise() -> None:
            identity = TaskIdentity.create(
                request_id="old-task", group_id="group-a", user_id="member",
                epoch=0, origin="user_message",
            )
            sink = coordinator.register(identity)
            sink.emit(AgentEventKind.TASK_STARTED)
            sink.emit(AgentEventKind.TOOL_STARTED, AgentEventPayload(
                category=ToolCategory.WEB_SEARCH,
            ), tool_call_id="search")
            requested = await service.handle_group_message(
                _event("/context reset", group="group-a", user="admin")
            )
            token = requested.text.rsplit(" ", 1)[-1]
            confirmed = await service.handle_group_message(
                _event(f"/confirm {token}", group="group-a", user="admin")
            )
            self.assertIn("开启新的上下文 epoch", confirmed.text)
            self.assertEqual(coordinator.snapshot("group-a", "member"), ())
            self.assertEqual(coordinator.metrics["epoch_fenced"], 2)

        _run(exercise())

    def test_unknown_slash_is_not_sent_to_llm_and_regular_message_is(self) -> None:
        llm = _DummyLLM("regular")
        service = self.service(llm)
        unknown = _run(service.handle_group_message(_event("/not-a-command", user="member")))
        self.assertIn("未知命令", unknown.text)
        self.assertEqual(llm.calls, 0)
        regular = _run(service.handle_group_message(_event("regular question", user="member", is_at_bot=True)))
        self.assertEqual(regular.text, "regular")
        self.assertEqual(llm.calls, 1)

    def test_web_and_news_commands_route_through_tool_registry_without_llm(self) -> None:
        from app.tools.contracts import ToolResult, ToolStatus

        llm = _DummyLLM("must not be called")
        service = self.service(llm)
        result = ToolResult(ToolStatus.OK, {"text": "fixture web response"})
        with patch("app.commands.service.TOOL_REGISTRY.dispatch", new=AsyncMock(return_value=result)) as dispatch:
            web = _run(service.handle_group_message(_event(
                "/web search GTNH 铂处理 --depth basic --start-date 2026-09-01 --end-date 2026-10-01"
            )))
            read = _run(service.handle_group_message(_event("/web read https://example.org/article")))
            news = _run(service.handle_group_message(_event("/news GregTech --time-range week")))

        self.assertIn("fixture web response", web.text)
        self.assertIn("fixture web response", read.text)
        self.assertIn("fixture web response", news.text)
        self.assertEqual(dispatch.await_count, 3)
        self.assertEqual(dispatch.await_args_list[0].args[0], "web_search")
        self.assertEqual(dispatch.await_args_list[0].args[1]["query"], "GTNH 铂处理")
        self.assertEqual(dispatch.await_args_list[0].args[1]["start_date"], "2026-09-01")
        self.assertEqual(dispatch.await_args_list[0].args[1]["end_date"], "2026-10-01")
        self.assertEqual(dispatch.await_args_list[1].args[0], "web_read")
        self.assertEqual(dispatch.await_args_list[1].args[1]["url"], "https://example.org/article")
        self.assertEqual(dispatch.await_args_list[2].args[0], "news_search")
        self.assertEqual(dispatch.await_args_list[2].args[1]["time_range"], "week")
        self.assertEqual(llm.calls, 0)

    def test_destructive_confirmation_is_bound_to_user_group_operation_and_single_use(self) -> None:
        service = self.service()
        self.memory.add_message("group-a", "admin", "admin", "user", "keep")
        requested = _run(service.handle_group_message(_event("/context reset", group="group-a", user="admin")))
        token = requested.text.rsplit(" ", 1)[-1]
        wrong_scope = _run(service.handle_group_message(_event(f"/confirm {token}", group="group-b", user="admin")))
        self.assertIn("无效", wrong_scope.text)
        self.assertEqual(len(self.memory.get_recent_messages("group-a")), 1)
        confirmed = _run(service.handle_group_message(_event(f"/confirm {token}", group="group-a", user="admin")))
        self.assertIn("开启新的上下文 epoch", confirmed.text)
        self.assertEqual(self.memory.count_group_history("group-a"), 1)
        self.assertEqual(self.memory.get_recent_messages("group-a"), [])
        replay = _run(service.handle_group_message(_event(f"/confirm {token}", group="group-a", user="admin")))
        self.assertIn("无效", replay.text)

    def test_context_purge_requires_confirmation_and_deletes_in_bounded_batches(self) -> None:
        service = self.service()
        for index in range(501):
            self.memory.add_message("group-a", f"u{index}", "member", "user", f"line {index}")
        request = _run(service.handle_group_message(_event("/context purge", group="group-a", user="admin")))
        token = request.text.rsplit(" ", 1)[-1]
        self.assertEqual(self.memory.count_group_history("group-a"), 501)
        first = _run(service.handle_group_message(_event(f"/confirm {token}", group="group-a", user="admin")))
        self.assertIn("尚余 1 条", first.text)
        self.assertEqual(self.memory.get_recent_messages("group-a"), [])
        self.assertEqual(self.memory.count_group_history("group-a"), 1)
        next_request = _run(service.handle_group_message(_event("/context purge", group="group-a", user="admin")))
        next_token = next_request.text.rsplit(" ", 1)[-1]
        done = _run(service.handle_group_message(_event(f"/confirm {next_token}", group="group-a", user="admin")))
        self.assertIn("已隐藏并物理删除", done.text)
        self.assertEqual(self.memory.count_group_history("group-a"), 0)

    def test_context_stats_and_summary_flag_report_observed_state(self) -> None:
        service = self.service()
        self.memory.add_message("group-a", "u", "member", "user", "a question")
        self.memory.upsert_group_summary("group-a", "stored summary", 1)
        epoch = self.memory.get_context_epoch("group-a")
        self.assertTrue(self.memory.record_context_compaction(
            group_id="group-a", epoch=epoch, archived_tool_blocks=2, dropped_context_items=3
        ))
        stats = _run(service.handle_group_message(_event("/context stats", group="group-a", user="member")))
        show = _run(service.handle_group_message(_event("/context show --summary", group="group-a", user="member")))
        self.assertIn("当前请求估算：UNKNOWN", stats.text)
        self.assertIn("归档工具块 2", stats.text)
        self.assertIn("stored summary", show.text)
        self.assertIn("不可信历史摘要", show.text)
        old = _run(service.handle_group_message(_event("/context summary", group="group-a", user="member")))
        self.assertIn("未知子命令", old.text)

    def test_context_confirmation_is_invalidated_when_epoch_changes(self) -> None:
        service = self.service()
        self.memory.add_message("group-a", "u", "member", "user", "preserve this")
        request = _run(service.handle_group_message(_event("/context reset", group="group-a", user="admin")))
        token = request.text.rsplit(" ", 1)[-1]
        self.memory.reset_group_context("group-a")
        result = _run(service.handle_group_message(_event(f"/confirm {token}", group="group-a", user="admin")))
        self.assertIn("版本已变化", result.text)
        self.assertEqual(self.memory.count_group_history("group-a"), 1)

    def test_context_compact_is_admin_only_and_does_not_call_model_when_ineligible(self) -> None:
        llm = _DummyLLM("must not be called")
        service = self.service(llm)
        denied = _run(service.handle_group_message(_event("/context compact", user="member")))
        self.assertEqual(denied.text, "你没有权限执行此命令。")
        response = _run(service.handle_group_message(_event("/context compact", user="admin")))
        self.assertIn("没有达到压缩条件", response.text)
        self.assertEqual(llm.calls, 0)

    def test_memory_search_is_admin_only_and_group_scoped(self) -> None:
        service = self.service()
        self.memory.add_structured_memory(
            group_id="group-a", owner_user_id=None, content="GTNH blast furnace fact",
            actor_user_id="admin", verification="admin_confirmed", visibility="group",
            source_kind="fixture", source_id="source-a",
        )
        self.memory.add_structured_memory(
            group_id="group-b", owner_user_id=None, content="GTNH hidden other group",
            actor_user_id="admin", verification="admin_confirmed", visibility="group",
            source_kind="fixture", source_id="source-b",
        )
        denied = _run(service.handle_group_message(_event("/memory search GTNH", group="group-a", user="member")))
        visible = _run(service.handle_group_message(_event("/memory search GTNH --kind fact", group="group-a", user="admin")))
        self.assertEqual(denied.text, "你没有权限执行此命令。")
        self.assertIn("blast furnace", visible.text)
        self.assertNotIn("hidden other group", visible.text)

    def test_expired_confirmation_is_rejected(self) -> None:
        service = self.service()
        requested = _run(service.handle_group_message(_event("/memory clear", user="admin")))
        token = requested.text.rsplit(" ", 1)[-1]
        digest = __import__("hashlib").sha256(token.encode()).hexdigest()
        pending = service._command_service._pending_confirmations[digest]
        service._command_service._pending_confirmations[digest] = pending.__class__(
            pending.actor_id, pending.group_id, pending.operation, pending.target, 0
        )
        reply = _run(service.handle_group_message(_event(f"/confirm {token}", user="admin")))
        self.assertIn("无效", reply.text)

    def test_recipe_commands_report_missing_data_without_fallback(self) -> None:
        service = self.service()
        self.memory.set_group_setting("g", "default_pack", '{"pack_id":"gtnh","pack_version":"2.6","game_version":"1.7.10"}')
        recipe = _run(service.handle_group_message(_event("/recipe gregtech:iron_dust")))
        machine = _run(service.handle_group_message(_event("/machine gregtech:assembler")))
        self.assertIn("没有 gtnh 2.6", recipe.text)
        self.assertIn("不会回退到其他版本", machine.text)

    def test_recipe_command_without_pack_has_explicit_configuration_error(self) -> None:
        reply = _run(self.service().handle_group_message(_event("/recipe minecraft:diamond")))
        self.assertIn("尚未选择整合包版本", reply.text)

    def test_wiki_empty_index_and_sfm_unavailable_are_explicit(self) -> None:
        service = self.service()
        wiki = _run(service.handle_group_message(_event("/wiki 机器配方")))
        sfm = _run(service.handle_group_message(_event(
            "/sfm validate --sfm-version 4.0 --mc-version 1.20\nEVERY 20 TICKS\n  INPUT west"
        )))
        self.assertIn("尚未选择整合包与版本", wiki.text)
        self.assertIn("没有配置经验证的 SFM Parser", sfm.text)
        self.assertNotIn("验证通过", sfm.text)

    def test_wiki_requires_and_displays_exact_pack_scope(self) -> None:
        service = self.service()
        self.memory.set_group_setting(
            "g", "default_pack",
            '{"pack_id":"fixture-pack","pack_version":"1.0","game_version":"1.20.1"}',
        )
        documents = service._llm_handler.document_knowledge
        evidence = Evidence(
            kind="document", source_id="fixture-source", title="Fixture Wiki",
            locator="L4-L8", revision="rev-a", pack_id="fixture-pack", pack_version="1.0",
        )
        with patch.object(
            documents, "search",
            new=AsyncMock(return_value=ToolResult(ToolStatus.OK, {"text": "Fixture result"}, evidence=(evidence,))),
        ) as search:
            reply = _run(service.handle_group_message(_event("/wiki recipe", group="g")))
        filters = search.await_args.args[1]
        self.assertEqual((filters.pack_id, filters.pack_version, filters.game_version),
                         ("fixture-pack", "1.0", "1.20.1"))
        self.assertIn("知识范围：fixture-pack 1.0", reply.text)
        self.assertIn("Fixture Wiki（fixture-pack / 1.0 / rev-a；L4-L8）", reply.text)

    def test_fake_sfm_validator_is_never_rendered_as_real_validation(self) -> None:
        from app.sfm.contracts import SFMTargetVersion
        from app.sfm.fake_validator import FakeSFMValidator
        from app.sfm.validator import SFMValidationService

        service = self.service()
        target = SFMTargetVersion("fixture-sfm", "fixture-mc")
        service._llm_handler.sfm_validation_service = SFMValidationService(
            FakeSFMValidator(valid_samples=("EVERY 20 TICKS",))
        )
        reply = _run(service.handle_group_message(_event(
            "/sfm validate --sfm-version fixture-sfm --mc-version fixture-mc\nEVERY 20 TICKS"
        )))
        self.assertIn("测试模拟结果", reply.text)
        self.assertNotIn("真实语法验证通过", reply.text)

    def test_sfm_diagnostic_redacts_local_paths_and_credentials(self) -> None:
        from app.sfm.contracts import ValidatorDiagnostic
        from app.sfm.fake_validator import FakeSFMValidator
        from app.sfm.validator import SFMValidationService

        service = self.service()
        diagnostic = ValidatorDiagnostic(
            code="parse_error",
            message=("failed /home/bot/.cache/parser.py:23; C:\\Users\\bot\\parser.exe; "
                     "api_key=fixture-secret sk-1234567890abcdefgh"),
            line=2,
        )
        service._llm_handler.sfm_validation_service = SFMValidationService(
            FakeSFMValidator(invalid_samples={"BROKEN": (diagnostic,)})
        )
        reply = _run(service.handle_group_message(_event(
            "/sfm validate --sfm-version fixture-sfm --mc-version fixture-mc\nBROKEN"
        )))
        self.assertIn("诊断含本地路径，已隐藏完整内容。", reply.text)
        self.assertNotIn("/home/bot", reply.text)
        self.assertNotIn("C:\\Users", reply.text)
        self.assertNotIn("fixture-secret", reply.text)
        self.assertNotIn("sk-1234567890", reply.text)

    def test_sfm_multibyte_source_is_bounded_by_encoded_bytes(self) -> None:
        service = self.service()
        source = "界" * 22_000
        reply = _run(service.handle_group_message(_event(
            "/sfm validate --sfm-version fixture-sfm --mc-version fixture-mc\n" + source
        )))
        self.assertIn("超过 65536 字节上限", reply.text)
        self.assertNotIn("INTERNAL_ERROR", reply.text)

    def test_repo_read_uses_tool_registry_and_path_security(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            subprocess.run(["git", "init", "-q", repo_dir], check=True)
            subprocess.run(["git", "-C", repo_dir, "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", repo_dir, "config", "user.name", "Fixture"], check=True)
            secret = Path(repo_dir) / ".env"
            secret.write_text("PRIVATE=not-for-output\n", encoding="utf-8")
            subprocess.run(["git", "-C", repo_dir, "add", ".env"], check=True)
            subprocess.run(["git", "-C", repo_dir, "commit", "-qm", "fixture"], check=True)
            commit = subprocess.run(
                ["git", "-C", repo_dir, "rev-parse", "HEAD"], check=True,
                capture_output=True, text=True,
            ).stdout.strip()
            settings = Settings(sqlite_path=self.db, local_git_repositories=(("fixture", Path(repo_dir)),))
            executor = StorageExecutor(sqlite_workers=1, vector_workers=1, queue_capacity=4)
            self.addCleanup(lambda: _run(executor.aclose()))
            service = BotService(settings=settings, memory=self.memory, llm=_DummyLLM(), storage_executor=executor)
            reply = _run(service.handle_group_message(_event(
                f"/repo read .env --repo fixture --commit {commit} --start 1 --end 1", user="member"
            )))
            self.assertIn("安全策略拒绝", reply.text)
            self.assertNotIn("PRIVATE=", reply.text)

    def test_shortcut_tools_dispatch_without_model_generation(self) -> None:
        llm = _DummyLLM("must not be called")
        service = self.service(llm)
        tool_result = ToolResult(ToolStatus.OK, {"text": "北京晴"})
        with patch("app.commands.service.TOOL_REGISTRY.dispatch", new=AsyncMock(return_value=tool_result)) as dispatch:
            reply = _run(service.handle_group_message(_event("/weather 北京", user="member")))
        self.assertEqual(reply.text, "北京晴")
        self.assertEqual(llm.calls, 0)
        dispatch.assert_awaited_once()
        self.assertEqual(dispatch.await_args.args[0], "weather")

    def test_usage_uses_only_authorized_scope(self) -> None:
        class Controller:
            def __init__(self):
                self.scopes = []

            async def get_usage_snapshot(self, kind, scope_id):
                self.scopes.append((kind, scope_id))
                return SimpleNamespace(
                    request_count=2, reported_requests=1, reserved_requests=1,
                    unknown_requests=0, reported_input_tokens=20,
                    reported_output_tokens=5, estimated_reserved_tokens=90,
                    estimated_unknown_tokens=0, cost_minor_units=None,
                )

        llm = _DummyLLM()
        controller = Controller()
        llm.budget_controller = controller
        service = self.service(llm)
        user = _run(service.handle_group_message(_event("/usage", group="g", user="member")))
        admin = _run(service.handle_group_message(_event("/usage", group="g", user="admin")))
        self.assertEqual(controller.scopes, [("user", "member"), ("group", "g")])
        self.assertIn("不是实际消耗", user.text)
        self.assertIn("费用：未知", admin.text)

    def test_config_is_allowlisted_and_has_no_secret_fields(self) -> None:
        service = self.service()
        denied = _run(service.handle_group_message(_event("/config set api_key secret", user="admin")))
        shown = _run(service.handle_group_message(_event("/config show", user="admin")))
        status = _run(service.handle_group_message(_event("/status", user="member")))
        self.assertIn("不允许修改", denied.text)
        self.assertNotIn("api_key", shown.text)
        self.assertNotIn("secret", shown.text)
        self.assertIn("本地 Git 白名单", status.text)
        self.assertIn("Web Search：Tavily NOT_CONFIGURED", status.text)
        self.assertIn("/status 不发起请求", status.text)
        self.assertNotIn("fixture-key", status.text)

    def test_pack_use_rejects_missing_exact_dataset_and_does_not_write_setting(self) -> None:
        service = self.service()
        reply = _run(service.handle_group_message(_event("/pack use gtnh 2.6 --game-version 1.7.10", user="admin")))
        self.assertIn("没有完全匹配", reply.text)
        self.assertIsNone(self.memory.get_group_setting("g", "default_pack"))

    def test_pack_selection_and_wiki_work_with_document_only_data(self) -> None:
        service = self.service()
        documents = service._llm_handler.document_knowledge
        source = DocumentSource(
            source_id="synthetic-wiki-source",
            source_uri="fixture://wiki/guide.md",
            source_type="markdown",
            corpus_id="synthetic-wiki",
            pack_id="fixture-wiki-pack",
            pack_version="wiki-v1",
            game_version="mc-fixture",
            source_revision="wiki-rev-1",
        )
        documents.catalog = AsyncMock(return_value=[DocumentIndexStatus(
            document_id="synthetic-wiki-doc",
            state=IndexState.INDEXED,
            chunk_count=1,
            source=source,
        )])
        documents.search = AsyncMock(return_value=ToolResult(ToolStatus.OK, {"text": "fixture wiki result"}))

        listed = _run(service.handle_group_message(_event("/pack list", user="member")))
        self.assertIn("fixture-wiki-pack wiki-v1", listed.text)
        self.assertIn("文档", listed.text)
        self.assertNotIn("配方", listed.text)

        selected = _run(service.handle_group_message(_event(
            "/pack use fixture-wiki-pack wiki-v1 --game-version mc-fixture", user="admin"
        )))
        self.assertIn("已设为 fixture-wiki-pack wiki-v1", selected.text)
        shown = _run(service.handle_group_message(_event("/pack show", user="member")))
        self.assertIn("本地文档：有已索引来源", shown.text)
        self.assertIn("没有活动配方数据集", shown.text)

        wiki = _run(service.handle_group_message(_event("/wiki fixture query", user="member")))
        self.assertIn("fixture wiki result", wiki.text)
        filters = documents.search.await_args.args[1]
        self.assertEqual(
            (filters.pack_id, filters.pack_version, filters.game_version),
            ("fixture-wiki-pack", "wiki-v1", "mc-fixture"),
        )
        recipe = _run(service.handle_group_message(_event("/recipe fixture:item", user="member")))
        self.assertIn("没有", recipe.text)

    def test_pack_selection_recipe_and_machine_commands_use_active_exact_service(self) -> None:
        service = self.service()
        recipe_service = service._llm_handler.recipe_query_service
        repository = recipe_service.repository
        payload = b'{"synthetic":"command fixture; not GTNH data"}'
        digest = hashlib.sha256(payload).hexdigest()
        key = PackVersionKey("fixture-pack", "fixture-1", "fixture-game")
        dataset = RecipeDataset(
            "command-fixture",
            key,
            SourceMetadata(
                source_id="command-fixture-source",
                source_revision="fixture-revision-1",
                exporter_id="hand-authored-fixture",
                exporter_version="fixture-v1",
                license_id="fixture-only",
                content_hash=digest,
            ),
            state=DatasetState.STAGING,
        )
        repository.add_dataset(dataset)
        locator = SourceLocator("fixture://recipes.json", json_pointer="/recipes/0")
        raw = RawRecipeRecord(
            dataset_id=dataset.dataset_id,
            source_record_id="fixture-row",
            source_locator=locator,
            raw_payload=payload,
        )
        repository.add_raw_record(raw)
        recipe = Recipe(
            dataset_id=dataset.dataset_id,
            recipe_id="fixture:iron-plate",
            source_record_id=raw.source_record_id,
            raw_source_hash=raw.raw_hash,
            machine_ids=("fixture:assembler",),
            inputs=(Ingredient(
                IngredientKind.ITEM_EXACT, "minecraft:iron_ingot", Decimal("2"), "item", consumed=True
            ),),
            outputs=(RecipeOutput(
                IngredientKind.ITEM_EXACT, "fixture:iron_plate", Decimal("1"), "item"
            ),),
            energy_eut=32,
            duration_ticks=20,
            conditions={},
            source_locator=locator,
        )
        repository.add_recipe(recipe)
        repository.set_active_dataset(dataset.dataset_id)
        alternate = RecipeDataset(
            "command-fixture-game-v2",
            PackVersionKey("fixture-pack", "fixture-1", "fixture-game-alt"),
            SourceMetadata(
                source_id="command-fixture-source-alt",
                source_revision="fixture-revision-alt",
                exporter_id="hand-authored-fixture",
                exporter_version="fixture-v1",
                license_id="fixture-only",
                content_hash="c" * 64,
            ),
            state=DatasetState.STAGING,
        )
        repository.add_dataset(alternate)
        repository.set_active_dataset(alternate.dataset_id)

        listed = _run(service.handle_group_message(_event("/pack list", user="member")))
        self.assertIn("fixture-pack fixture-1", listed.text)
        ambiguous = _run(service.handle_group_message(_event("/pack use fixture-pack fixture-1", user="admin")))
        self.assertIn("多个 Minecraft 版本", ambiguous.text)
        self.assertIsNone(self.memory.get_group_setting("g", "default_pack"))
        selected = _run(service.handle_group_message(_event(
            "/pack use fixture-pack fixture-1 --game-version fixture-game", user="admin"
        )))
        self.assertIn("fixture-pack", selected.text)
        by_item = _run(service.handle_group_message(_event("/recipe fixture:iron_plate", user="member")))
        by_machine = _run(service.handle_group_message(_event("/machine fixture:assembler", user="member")))
        self.assertIn("minecraft:iron_ingot", by_item.text)
        self.assertIn("32 EU/t", by_machine.text)
        self.assertIn("fixture://recipes.json", by_machine.text)
        other_group = _run(service.handle_group_message(_event(
            "/recipe fixture:iron_plate", group="other", user="member"
        )))
        self.assertIn("尚未选择整合包版本", other_group.text)


class _DummyLLM:
    def __init__(self, reply: str = "ok", model: str = "test-model") -> None:
        self.api_key = "fixture-key"
        self.reply = reply
        self.model = model
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        return self.reply


def _event(text: str, *, group: str = "g", user: str = "admin", is_at_bot: bool = True):
    return GroupMessageEvent("cmd-" + str(abs(hash((text, group, user)))), group, user, user, text,
                             is_at_bot, 0, {})


def _run(coro):
    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()

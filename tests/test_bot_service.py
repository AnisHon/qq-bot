from __future__ import annotations

import asyncio
import json
import threading
import tempfile
from pathlib import Path
import unittest

from app.bot_service import (
    BotService,
    _looks_like_minecraft_query,
    _sanitize_qq_text,
)
from app.handlers.task_handler import _parse_task_schedule
from app.tools.external import _is_generic_news_query
from app.config import Settings
from app.memory import MemoryStore
from app.models import GroupMessageEvent, QuotedMessage, SearchResult
from app.storage_executor import StorageExecutor
from app.text_utils import split_qq_text


class MinecraftTriggerTest(unittest.TestCase):
    def test_common_queries_trigger(self) -> None:
        self.assertTrue(_looks_like_minecraft_query("钻石怎么获得"))
        self.assertTrue(_looks_like_minecraft_query("石磨怎么做"))
        self.assertTrue(_looks_like_minecraft_query("actually additions是什么东西"))
        self.assertTrue(_looks_like_minecraft_query("IC2支持哪些版本"))


class MarkdownSanitizeTest(unittest.TestCase):
    def test_markdown_removed(self) -> None:
        text = _sanitize_qq_text("""# 标题
**加粗** `代码` [链接](https://example.com)
> 引用
```python
print(1)
```""")
        self.assertNotIn("**", text)
        self.assertNotIn("```", text)
        self.assertNotIn("# ", text)
        self.assertIn("加粗", text)

    def test_bot_name_prefix_removed(self) -> None:
        text = _sanitize_qq_text("胖虎: 我知道了\nAI：下一句")
        self.assertEqual(text, "我知道了\n下一句")


    def test_identifiers_and_urls_keep_underscores(self) -> None:
        text = _sanitize_qq_text(
            "allthetweaks:atm_star https://github.com/x/blob/main/atm_star.js"
        )
        self.assertIn("allthetweaks:atm_star", text)
        self.assertIn("atm_star.js", text)

    def test_internal_tool_protocol_is_not_sent_to_qq(self) -> None:
        text = _sanitize_qq_text("<｜｜DSML｜｜tool_calls>hidden</｜｜DSML｜｜tool_calls>")
        self.assertNotIn("DSML", text)
        self.assertIn("请稍后重试", text)


class NewsQueryTest(unittest.TestCase):
    def test_date_rewritten_generic_news_query(self) -> None:
        self.assertTrue(_is_generic_news_query("今天有什么新闻"))
        self.assertTrue(_is_generic_news_query("2026年7月12日 新闻 热点"))
        self.assertFalse(_is_generic_news_query("2026年7月12日 科技新闻"))


class CommandTest(unittest.TestCase):
    def test_name_trigger_wakes_bot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings(
                bot_trigger_mode="at",
                bot_default_name="胖虎",
                sqlite_path=Path(tmpdir) / "bot.db",
            )
            store = MemoryStore(settings.sqlite_path, history_limit=20)
            svc = BotService(settings=settings, memory=store, llm=_DummyLLM())

            self.assertTrue(
                svc.expects_reply(GroupMessageEvent("1", "100", "111", "user", "@胖虎 你说呢", False, 0, {}))
            )
            self.assertTrue(
                svc.expects_reply(GroupMessageEvent("2", "100", "111", "user", "胖虎你说呢", False, 0, {}))
            )
            self.assertFalse(
                svc.expects_reply(GroupMessageEvent("3", "100", "111", "user", "我觉得胖虎可以", False, 0, {}))
            )

    def test_tool_command_expects_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings(sqlite_path=Path(tmpdir) / "bot.db")
            store = MemoryStore(settings.sqlite_path, history_limit=20)
            svc = BotService(settings=settings, memory=store, llm=_DummyLLM())
            event = GroupMessageEvent("1", "100", "111", "user", "/weather 北京", False, 0, {})
            self.assertTrue(svc.expects_reply(event))

    def test_non_admin_weather_command_remains_available_via_registry(self) -> None:
        from unittest.mock import AsyncMock, patch

        from app.tools.cache import ToolResultCache

        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings(
                bot_admin_qq_list=("administrator",),
                sqlite_path=Path(tmpdir) / "bot.db",
            )
            store = MemoryStore(settings.sqlite_path, history_limit=20)
            llm = _DummyLLM("LLM must not handle a slash command")
            svc = BotService(settings=settings, memory=store, llm=llm)
            event = GroupMessageEvent("cmd-1", "100", "ordinary-user", "user", "/weather 北京", False, 0, {})
            rows = [SearchResult("北京", "晴", "", "天气")]
            with patch("app.tools.legacy_tools.get_cache", return_value=ToolResultCache()):
                with patch("app.tools.legacy_tools.get_weather", new=AsyncMock(return_value=rows)) as weather:
                    reply = _run(svc.handle_group_message(event))

            self.assertIn("北京", reply.text)
            self.assertIn("晴", reply.text)
            self.assertEqual(llm.calls, 0)
            weather.assert_awaited_once()

    def test_unknown_slash_command_is_rejected_without_entering_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings(sqlite_path=Path(tmpdir) / "bot.db")
            store = MemoryStore(settings.sqlite_path, history_limit=20)
            llm = _DummyLLM("must not be used")
            service = BotService(settings=settings, memory=store, llm=llm)
            event = GroupMessageEvent("unknown-1", "100", "111", "user", "/recpie diamond", False, 0, {})
            self.assertTrue(service.expects_reply(event))
            reply = _run(service.handle_group_message(event))
            self.assertIn("未知命令", reply.text)
            self.assertIn("/recipe", reply.text)
            self.assertEqual(llm.calls, 0)

    def test_ordinary_message_still_enters_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings(sqlite_path=Path(tmpdir) / "bot.db", agent_tools_enabled=False)
            store = MemoryStore(settings.sqlite_path, history_limit=20)
            llm = _DummyLLM("ordinary reply")
            service = BotService(settings=settings, memory=store, llm=llm)
            reply = _run(service.handle_group_message(
                GroupMessageEvent("normal-1", "100", "111", "user", "请回答", True, 0, {})
            ))
            self.assertEqual(reply.text, "ordinary reply")
            self.assertEqual(llm.calls, 1)

    def test_history_capacity_keeps_current_chat_ephemeral(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bot.db"
            settings = Settings(sqlite_path=path, agent_tools_enabled=False)
            store = MemoryStore(path, history_max_messages_per_group=1, history_max_bytes_per_group=1024)
            store.add_message("100", "other", "member", "user", "existing row at capacity")
            llm = _DummyLLM("ephemeral answer")
            service = BotService(settings=settings, memory=store, llm=llm)

            reply = _run(service.handle_group_message(
                GroupMessageEvent("overflow-1", "100", "111", "user", "new question", True, 0, {})
            ))

            self.assertEqual(reply.text, "ephemeral answer")
            self.assertFalse(reply.context_persisted)
            self.assertEqual(llm.calls, 1)
            self.assertEqual(store.count_group_history("100"), 1)
            self.assertEqual(store.get_recent_messages("100")[-1]["content"], "existing row at capacity")

    def test_reply_to_bot_wakes_without_text_or_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings(sqlite_path=Path(tmpdir) / "bot.db")
            store = MemoryStore(settings.sqlite_path, history_limit=20)
            svc = BotService(settings=settings, memory=store, llm=_DummyLLM())
            event = GroupMessageEvent(
                "1", "100", "111", "user", "", False, 0, {},
                quote=QuotedMessage("99", "2287869558", "胖虎", "上一条回答", is_from_bot=True),
            )
            self.assertTrue(svc.expects_reply(event))

    def test_admin_can_switch_group_model_persistently(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings(
                bot_admin_qq_list=("1392953682",),
                sqlite_path=Path(tmpdir) / "bot.db",
            )
            store = MemoryStore(settings.sqlite_path, history_limit=20)
            deepseek = _DummyLLM(model="deepseek-chat")
            nvidia = _DummyLLM(model="openai/gpt-oss-120b")
            svc = BotService(
                settings=settings,
                memory=store,
                llm=deepseek,
                llms={"deepseek": deepseek, "nvidia": nvidia},
            )
            event = GroupMessageEvent("1", "100", "1392953682", "admin", "/model use nvidia", True, 0, {})
            reply = _run(svc.handle_group_message(event))
            self.assertIn("openai/gpt-oss-120b", reply.text)
            self.assertEqual(store.get_group_setting("100", "model_provider"), "nvidia")
            shown = _run(
                svc.handle_group_message(
                    GroupMessageEvent("2", "100", "1392953682", "admin", "/model show", True, 0, {})
                )
            )
            self.assertIn("nvidia", shown.text)

    def test_non_admin_cannot_switch_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings(bot_admin_qq_list=("1392953682",), sqlite_path=Path(tmpdir) / "bot.db")
            store = MemoryStore(settings.sqlite_path, history_limit=20)
            deepseek = _DummyLLM(model="deepseek-chat")
            svc = BotService(settings=settings, memory=store, llm=deepseek, llms={"deepseek": deepseek})
            reply = _run(
                svc.handle_group_message(
                    GroupMessageEvent("1", "100", "111", "user", "/model use deepseek", True, 0, {})
                )
            )
            self.assertEqual(reply.text, "你没有权限执行此命令。")

    def test_admin_can_reset_group_context_and_retain_raw_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings(
                bot_admin_qq_list=("1392953682",),
                bot_trigger_mode="prefix",
                bot_prefix="/bot",
                bot_qq_id="2287869558",
                sqlite_path=Path(tmpdir) / "bot.db",
            )
            store = MemoryStore(settings.sqlite_path, history_limit=20)
            svc = BotService(settings=settings, memory=store, llm=_DummyLLM())
            store.add_message("100", "1", "u1", "user", "hello")
            store.add_message("100", "2", "u2", "user", "world")
            store.upsert_group_summary("100", "旧摘要", 2)
            reply = _run(svc.handle_group_message(GroupMessageEvent("1", "100", "1392953682", "admin", "/context reset", True, 0, {})))
            self.assertIn("请确认", reply.text)
            token = reply.text.rsplit(" ", 1)[-1]
            reply = _run(svc.handle_group_message(
                GroupMessageEvent("2", "100", "1392953682", "admin", f"/confirm {token}", True, 0, {})
            ))
            self.assertIn("开启新的上下文 epoch", reply.text)
            self.assertEqual(store.get_recent_messages("100"), [])
            self.assertEqual(store.count_group_history("100"), 2)
            self.assertIsNone(store.get_group_summary("100"))

    def test_normal_user_cannot_reset_group_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings(
                bot_admin_qq_list=("1392953682",),
                bot_trigger_mode="prefix",
                bot_prefix="/bot",
                bot_qq_id="2287869558",
                sqlite_path=Path(tmpdir) / "bot.db",
            )
            store = MemoryStore(settings.sqlite_path, history_limit=20)
            svc = BotService(settings=settings, memory=store, llm=_DummyLLM())
            store.add_message("100", "1", "u1", "user", "hello")
            reply = _run(svc.handle_group_message(GroupMessageEvent("1", "100", "111", "user", "/context reset", True, 0, {})))
            self.assertEqual(reply.text, "你没有权限执行此命令。")
            self.assertIn("hello", [row["content"] for row in store.get_recent_messages("100")])

    def test_admin_can_add_multiline_memories_and_delete_one_scoped_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings(
                bot_admin_qq_list=("1392953682",),
                bot_trigger_mode="prefix",
                bot_prefix="/bot",
                bot_qq_id="2287869558",
                sqlite_path=Path(tmpdir) / "bot.db",
            )
            store = MemoryStore(settings.sqlite_path, history_limit=20)
            svc = BotService(settings=settings, memory=store, llm=_DummyLLM())
            reply = _run(
                svc.handle_group_message(
                    GroupMessageEvent("1", "100", "1392953682", "admin", "/memory add 第一条\n第二条", True, 0, {})
                )
            )
            self.assertIn("已添加本群长期记忆", reply.text)
            rows = store.list_memories(scope="group", scope_id="100", limit=10)
            self.assertEqual({row["content"] for row in rows}, {"第一条", "第二条"})
            reply = _run(
                svc.handle_group_message(
                    GroupMessageEvent("2", "100", "1392953682", "admin", f"/memory delete {rows[0]['id']}", True, 0, {})
                )
            )
            self.assertIn("已删除这条", reply.text)
            self.assertEqual(len(store.list_memories(scope="group", scope_id="100", limit=10)), 1)
            reply = _run(svc.handle_group_message(
                GroupMessageEvent("3", "100", "1392953682", "admin", "/memory clear", True, 0, {})
            ))
            token = reply.text.rsplit(" ", 1)[-1]
            _run(svc.handle_group_message(
                GroupMessageEvent("4", "100", "1392953682", "admin", f"/confirm {token}", True, 0, {})
            ))
            self.assertEqual(store.list_memories(scope="group", scope_id="100", limit=10), [])

    def test_admin_can_add_and_delete_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings(
                bot_admin_qq_list=("1392953682",),
                bot_trigger_mode="prefix",
                bot_prefix="/bot",
                sqlite_path=Path(tmpdir) / "bot.db",
            )
            store = MemoryStore(settings.sqlite_path, history_limit=20)
            svc = BotService(settings=settings, memory=store, llm=_DummyLLM())
            reply = _run(
                svc.handle_group_message(
                    GroupMessageEvent("1", "100", "1392953682", "admin", '/task add "说一句测试" --in 10m', True, 0, {})
                )
            )
            self.assertIn("已添加本群定时任务", reply.text)
            rows = store.list_scheduled_tasks("100")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["prompt"], "说一句测试")
            reply = _run(
                svc.handle_group_message(
                    GroupMessageEvent("2", "100", "1392953682", "admin", f"/task cancel {rows[0]['id']}", True, 0, {})
                )
            )
            self.assertIn("已取消本群", reply.text)
            self.assertEqual(store.list_scheduled_tasks("100"), [])

    def test_old_chinese_command_is_not_management_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings(
                bot_admin_qq_list=("1392953682",),
                bot_trigger_mode="prefix",
                bot_prefix="/bot",
                sqlite_path=Path(tmpdir) / "bot.db",
            )
            store = MemoryStore(settings.sqlite_path, history_limit=20)
            svc = BotService(settings=settings, memory=store, llm=_DummyLLM())
            store.add_message("100", "1", "u1", "user", "hello")
            reply = _run(
                svc.handle_group_message(
                    GroupMessageEvent("1", "100", "1392953682", "admin", "清空上下文", True, 0, {})
                )
            )
            self.assertIsNone(reply)
            self.assertIn("hello", [row["content"] for row in store.get_recent_messages("100")])


class SplitQQTextTest(unittest.TestCase):
    def test_split_long_text(self) -> None:
        text = "第一句很长。" * 120
        parts = split_qq_text(text, 120)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 120 for part in parts))


class SummaryTest(unittest.TestCase):
    @staticmethod
    def _summary_reply(text: str) -> str:
        return json.dumps({
            "schema_version": 1, "topic": "fixture topic", "summary": text,
            "decisions": [{"text": "fixture decision", "source_row_ids": [1]}],
            "open_questions": [],
        }, ensure_ascii=False)

    def test_auto_summary_keeps_original_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings(
                sqlite_path=Path(tmpdir) / "bot.db",
                message_history_limit=20,
                context_recent_message_limit=2,
                summary_trigger_message_count=5,
            )
            store = MemoryStore(settings.sqlite_path, history_limit=20)
            svc = BotService(settings=settings, memory=store, llm=_DummyLLM(self._summary_reply("摘要内容")))
            for idx in range(5):
                store.add_message("100", f"u{idx}", f"n{idx}", "user", f"m{idx}")

            _run(svc.maybe_update_group_summary("100"))

            summary = store.get_group_summary("100")
            self.assertIsNotNone(summary)
            self.assertEqual(summary["summary"], "摘要内容")
            self.assertEqual([row["content"] for row in store.get_recent_messages("100", 10)], [
                "m0", "m1", "m2", "m3", "m4"
            ])
            self.assertEqual(summary["last_message_id"], 3)
            episodes = store.search_episodes("fixture topic", group_id="100", epoch=0)
            self.assertEqual(len(episodes), 1)
            self.assertEqual(episodes[0]["summary"], "摘要内容")

    def test_summary_is_single_flight_per_group(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                settings = Settings(
                    sqlite_path=Path(tmpdir) / "bot.db",
                    message_history_limit=20,
                    context_recent_message_limit=2,
                    summary_trigger_message_count=5,
                )
                store = MemoryStore(settings.sqlite_path, history_limit=20)
                for idx in range(5):
                    store.add_message("100", f"u{idx}", f"n{idx}", "user", f"m{idx}")

                class BlockingSummaryLLM(_DummyLLM):
                    def __init__(self) -> None:
                        super().__init__(SummaryTest._summary_reply("摘要内容"))
                        self.started = asyncio.Event()
                        self.release = asyncio.Event()

                    async def chat(self, messages, **kwargs):
                        self.calls += 1
                        self.started.set()
                        await self.release.wait()
                        return self.reply

                llm = BlockingSummaryLLM()
                service = BotService(settings=settings, memory=store, llm=llm)
                first = asyncio.create_task(service.maybe_update_group_summary("100"))
                await asyncio.wait_for(llm.started.wait(), timeout=1)
                second = asyncio.create_task(service.maybe_update_group_summary("100"))
                await asyncio.wait_for(second, timeout=1)
                self.assertEqual(llm.calls, 1)
                llm.release.set()
                await asyncio.wait_for(first, timeout=1)
                self.assertEqual(store.get_group_summary("100")["summary"], "摘要内容")
                self.assertEqual(len(store.get_recent_messages("100", 10)), 5)

        _run(scenario())

    def test_clear_during_summary_prevents_stale_commit(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                settings = Settings(
                    sqlite_path=Path(tmpdir) / "bot.db",
                    context_recent_message_limit=2,
                    summary_trigger_message_count=5,
                )
                store = MemoryStore(settings.sqlite_path)
                for idx in range(5):
                    store.add_message("100", f"u{idx}", f"n{idx}", "user", f"m{idx}")
                llm = _DummyLLM(SummaryTest._summary_reply("late summary"))
                service = BotService(settings=settings, memory=store, llm=llm)
                entered = asyncio.Event()
                release = asyncio.Event()
                original_chat = llm.chat

                async def blocked_chat(messages, **kwargs):
                    entered.set()
                    await release.wait()
                    return await original_chat(messages, **kwargs)

                llm.chat = blocked_chat
                task = asyncio.create_task(service.maybe_update_group_summary("100"))
                await asyncio.wait_for(entered.wait(), timeout=1)
                store.clear_group_context("100")
                release.set()
                await asyncio.wait_for(task, timeout=1)
                self.assertIsNone(store.get_group_summary("100"))
                self.assertEqual(store.get_recent_messages("100", 10), [])

        _run(scenario())

    def test_context_reset_during_summary_prevents_stale_commit_and_retains_raw_history(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                settings = Settings(
                    sqlite_path=Path(tmpdir) / "bot.db",
                    context_recent_message_limit=2,
                    summary_trigger_message_count=5,
                )
                store = MemoryStore(settings.sqlite_path)
                old_ids = [
                    store.add_message("100", f"u{idx}", f"n{idx}", "user", f"m{idx}")
                    for idx in range(5)
                ]
                llm = _DummyLLM(SummaryTest._summary_reply("late summary"))
                service = BotService(settings=settings, memory=store, llm=llm)
                entered = asyncio.Event()
                release = asyncio.Event()
                original_chat = llm.chat

                async def blocked_chat(messages, **kwargs):
                    entered.set()
                    await release.wait()
                    return await original_chat(messages, **kwargs)

                llm.chat = blocked_chat
                task = asyncio.create_task(service.maybe_update_group_summary("100"))
                await asyncio.wait_for(entered.wait(), timeout=1)
                store.reset_group_context("100")
                release.set()
                await asyncio.wait_for(task, timeout=1)

                self.assertIsNone(store.get_group_summary("100"))
                self.assertEqual(store.get_recent_messages("100", 10), [])
                self.assertTrue(all(store.message_exists("100", row_id) for row_id in old_ids))

        _run(scenario())

    def test_empty_or_overlong_summary_does_not_advance_coverage(self) -> None:
        for text in ("", "x" * 301):
            with self.subTest(length=len(text)), tempfile.TemporaryDirectory() as tmpdir:
                settings = Settings(
                    sqlite_path=Path(tmpdir) / "bot.db",
                    context_recent_message_limit=2,
                    summary_trigger_message_count=5,
                    summary_max_chars=300,
                )
                store = MemoryStore(settings.sqlite_path)
                for idx in range(5):
                    store.add_message("100", f"u{idx}", f"n{idx}", "user", f"m{idx}")
                service = BotService(
                    settings=settings, memory=store, llm=_DummyLLM(self._summary_reply(text))
                )
                _run(service.maybe_update_group_summary("100"))
                self.assertIsNone(store.get_group_summary("100"))
                candidate = store.begin_group_summary_snapshot("100", keep_recent=2, trigger_count=5)
                self.assertIsNotNone(candidate)


class TaskScheduleParseTest(unittest.TestCase):
    def test_parse_in_and_every(self) -> None:
        once = _parse_task_schedule("in 10m 做检查", now=1000)
        self.assertIsNotNone(once)
        assert once is not None
        self.assertEqual(once.next_run_at, 1600)
        self.assertEqual(once.interval_seconds, 0)
        self.assertEqual(once.prompt, "做检查")

        repeated = _parse_task_schedule("every 30s 做检查", now=1000)
        self.assertIsNotNone(repeated)
        assert repeated is not None
        self.assertEqual(repeated.interval_seconds, 60)

    def test_parse_daily(self) -> None:
        # Use a fixed timestamp: 2026-07-12 08:00:00 UTC+8 → pick a time later today
        import time as _time
        now = _time.mktime(_time.strptime("2026-07-12 08:00:00", "%Y-%m-%d %H:%M:%S"))
        sched = _parse_task_schedule("daily 09:30 早报推送", now=now)
        self.assertIsNotNone(sched)
        assert sched is not None
        self.assertEqual(sched.interval_seconds, 86400)
        self.assertEqual(sched.schedule_type, "daily")
        self.assertEqual(sched.prompt, "早报推送")
        # next_run should be 09:30 today (later than 08:00)
        import datetime as _dt
        run_dt = _dt.datetime.fromtimestamp(sched.next_run_at)
        self.assertEqual(run_dt.hour, 9)
        self.assertEqual(run_dt.minute, 30)

    def test_parse_daily_past_time_advances_to_tomorrow(self) -> None:
        import time as _time
        import datetime as _dt
        # 11:00 today, requesting daily at 09:00 → should be 09:00 tomorrow
        now = _time.mktime(_time.strptime("2026-07-12 11:00:00", "%Y-%m-%d %H:%M:%S"))
        sched = _parse_task_schedule("daily 09:00 晨报", now=now)
        self.assertIsNotNone(sched)
        assert sched is not None
        run_dt = _dt.datetime.fromtimestamp(sched.next_run_at)
        self.assertEqual(run_dt.day, 13)
        self.assertEqual(run_dt.hour, 9)


class _DummyLLM:
    def __init__(self, reply: str = "ok", model: str = "test-model") -> None:
        self.reply = reply
        self.model = model
        self.api_key = "test-key"
        self.calls = 0

    async def chat(self, messages, temperature=0.7, max_tokens=None):
        self.calls += 1
        return self.reply


def _run(coro):
    return asyncio.run(coro)


class GroupReplyCancellationTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_reply_releases_group_busy_marker(self) -> None:
        class BlockingLLM:
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.api_key = "fixture-key"

            async def chat(self, messages, temperature=0.7, max_tokens=None, **kwargs):
                self.started.set()
                await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings(
                sqlite_path=Path(tmpdir) / "bot.db",
                agent_tools_enabled=False,
                agent_total_timeout=10,
            )
            memory = MemoryStore(settings.sqlite_path, history_limit=20)
            llm = BlockingLLM()
            service = BotService(settings=settings, memory=memory, llm=llm)
            event = GroupMessageEvent("cancel-1", "group-1", "user-1", "User", "hello", True, 0, {})
            task = asyncio.create_task(service.handle_group_message(event))
            await llm.started.wait()

            self.assertTrue(await service.is_group_busy("group-1"))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(await service.is_group_busy("group-1"))


class StorageOffloadIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_bot_service_memory_io_runs_off_loop_with_injected_executor(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        executor = StorageExecutor(sqlite_workers=1, vector_workers=1, queue_capacity=4)

        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings(
                sqlite_path=Path(tmpdir) / "bot.db",
                agent_tools_enabled=False,
                agent_total_timeout=10,
            )
            memory = MemoryStore(settings.sqlite_path, history_limit=20)
            original_record_message_snapshot = memory.record_message_snapshot

            def blocking_record_message_snapshot(*args, **kwargs):
                if not entered.is_set():
                    entered.set()
                    did_release = release.wait(timeout=0.25)
                    if not did_release:
                        raise RuntimeError("test did not release blocked storage fixture")
                result = original_record_message_snapshot(*args, **kwargs)
                finished.set()
                return result

            memory.record_message_snapshot = blocking_record_message_snapshot
            service = BotService(
                settings=settings,
                memory=memory,
                llm=_DummyLLM(),
                storage_executor=executor,
            )
            task = asyncio.create_task(
                service.handle_group_message(
                    GroupMessageEvent("offload-1", "group-1", "user-1", "User", "hello", True, 0, {})
                )
            )
            try:
                deadline = asyncio.get_running_loop().time() + 1
                while not entered.is_set() and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.005)
                self.assertTrue(entered.is_set(), "storage call did not reach the blocking fixture")
                ticks = 0
                for _ in range(5):
                    await asyncio.sleep(0.005)
                    ticks += 1
                self.assertEqual(ticks, 5)
            finally:
                release.set()

            deadline = asyncio.get_running_loop().time() + 1
            while not finished.is_set() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.005)
            self.assertTrue(finished.is_set(), "storage fixture did not finish")
            reply = await asyncio.wait_for(task, timeout=3)
            self.assertIsNotNone(reply)
            self.assertEqual(reply.text, "ok")
        await executor.aclose()

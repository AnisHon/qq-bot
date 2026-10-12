from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from app.bot_service import (
    BotService,
    _format_memory_lines,
    _looks_like_minecraft_query,
    _parse_task_schedule,
    _sanitize_qq_text,
)
from app.tools.external import _is_generic_news_query
from app.config import Settings
from app.memory import MemoryStore
from app.models import GroupMessageEvent, QuotedMessage
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

    def test_memory_format_plain_text(self) -> None:
        text = _format_memory_lines([{"id": "1", "scope": "group", "scope_id": "100", "content": "我是服主"}])
        self.assertEqual(text, "当前长期记忆：\n1. 我是服主")

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
            self.assertEqual(reply.text, "只有管理员可以使用管理命令。")

    def test_admin_can_clear_group_history(self) -> None:
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
            reply = _run(svc.handle_group_message(GroupMessageEvent("1", "100", "1392953682", "admin", "/context clear", True, 0, {})))
            self.assertIn("已清空本群上下文和自动摘要", reply.text)
            self.assertEqual(store.get_recent_messages("100"), [])
            self.assertIsNone(store.get_group_summary("100"))

    def test_normal_user_cannot_clear_group_history(self) -> None:
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
            reply = _run(svc.handle_group_message(GroupMessageEvent("1", "100", "111", "user", "/context clear", True, 0, {})))
            self.assertEqual(reply.text, "只有管理员可以使用管理命令。")
            self.assertIn("hello", [row["content"] for row in store.get_recent_messages("100")])

    def test_admin_can_add_and_delete_multiple_memories(self) -> None:
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
            self.assertIn("已添加 2 条长期记忆", reply.text)
            rows = store.list_memories(scope="group", scope_id="100", limit=10)
            self.assertEqual({row["content"] for row in rows}, {"第一条", "第二条"})
            ids = " ".join(str(row["id"]) for row in rows)
            reply = _run(
                svc.handle_group_message(
                    GroupMessageEvent("2", "100", "1392953682", "admin", f"/memory delete {ids}", True, 0, {})
                )
            )
            self.assertIn("已删除 2 条长期记忆", reply.text)
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
                    GroupMessageEvent("1", "100", "1392953682", "admin", "/task add in 10m 说一句测试", True, 0, {})
                )
            )
            self.assertIn("已添加定时任务", reply.text)
            rows = store.list_scheduled_tasks("100")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["prompt"], "说一句测试")
            reply = _run(
                svc.handle_group_message(
                    GroupMessageEvent("2", "100", "1392953682", "admin", f"/task delete {rows[0]['id']}", True, 0, {})
                )
            )
            self.assertIn("已删除 1 个定时任务", reply.text)
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
    def test_auto_summary_prunes_old_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings(
                sqlite_path=Path(tmpdir) / "bot.db",
                message_history_limit=20,
                context_recent_message_limit=2,
                summary_trigger_message_count=5,
            )
            store = MemoryStore(settings.sqlite_path, history_limit=20)
            svc = BotService(settings=settings, memory=store, llm=_DummyLLM("摘要内容"))
            for idx in range(5):
                store.add_message("100", f"u{idx}", f"n{idx}", "user", f"m{idx}")

            _run(svc.maybe_update_group_summary("100"))

            summary = store.get_group_summary("100")
            self.assertIsNotNone(summary)
            self.assertEqual(summary["summary"], "摘要内容")
            self.assertEqual([row["content"] for row in store.get_recent_messages("100", 10)], ["m3", "m4"])


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

    async def chat(self, messages, temperature=0.7, max_tokens=None):
        return self.reply


def _run(coro):
    import asyncio
    return asyncio.run(coro)

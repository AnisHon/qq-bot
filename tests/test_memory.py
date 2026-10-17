from __future__ import annotations

import tempfile
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest

from app.memory import MemoryStore


class MemoryStoreTest(unittest.TestCase):
    def test_history_is_retained_past_legacy_window_and_memory_crud(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bot.db"
            store = MemoryStore(db_path, history_limit=3)
            for idx in range(5):
                store.add_message("1", f"u{idx}", f"n{idx}", "user", f"m{idx}")
            recent = store.get_recent_messages("1", limit=10)
            self.assertEqual([row["content"] for row in recent], ["m0", "m1", "m2", "m3", "m4"])

            cleared = store.clear_group_history("1")
            self.assertEqual(cleared, 5)
            self.assertEqual(store.get_recent_messages("1"), [])

            memory_id = store.add_memory("group", "1", "hello", "admin")
            rows = store.list_memories(scope="group", scope_id="1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], memory_id)
            deleted = store.delete_memories_by_keyword("hello", scope="group", scope_id="1")
            self.assertEqual(deleted, 1)
            self.assertEqual(store.list_memories(scope="group", scope_id="1"), [])

    def test_summary_crud_and_context_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bot.db"
            store = MemoryStore(db_path, history_limit=20)
            for idx in range(6):
                store.add_message("1", f"u{idx}", f"n{idx}", "user", f"m{idx}")

            snapshot = store.begin_group_summary_snapshot("1", keep_recent=2, trigger_count=5)
            self.assertIsNotNone(snapshot)
            self.assertEqual([row["content"] for row in snapshot.rows], ["m0", "m1", "m2", "m3"])

            self.assertTrue(store.commit_group_summary(snapshot, "摘要"))
            summary = store.get_group_summary("1")
            self.assertIsNotNone(summary)
            self.assertEqual(summary["summary"], "摘要")
            self.assertEqual(summary["last_message_id"], snapshot.through_message_id)
            self.assertEqual(len(store.get_recent_messages("1", 10)), 6)
            self.assertTrue(store.message_exists("1", snapshot.through_message_id))
            self.assertIsNone(
                store.begin_group_summary_snapshot("1", keep_recent=2, trigger_count=5)
            )

            cleared = store.clear_group_context("1")
            self.assertEqual(cleared, 6)
            self.assertEqual(store.get_recent_messages("1", 10), [])
            self.assertIsNone(store.get_group_summary("1"))

    def test_request_snapshot_is_idempotent_and_excludes_later_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "bot.db", history_limit=20)
            store.add_message("g", "u0", "zero", "user", "before")
            first = store.record_message_snapshot(
                group_id="g", user_id="u1", nickname="one", content="same",
                request_id="m1", onebot_message_id="m1", event_timestamp=100,
            )
            store.add_message("g", "u2", "two", "user", "later")
            repeated = store.record_message_snapshot(
                group_id="g", user_id="u1", nickname="one", content="same",
                request_id="m1", onebot_message_id="m1", event_timestamp=100,
            )
            self.assertEqual(first.current_row_id, repeated.current_row_id)
            self.assertEqual(first.epoch, repeated.epoch)
            self.assertEqual(
                [row["content"] for row in store.get_recent_messages_before(first, 10)], ["before"]
            )
            second_same_text = store.record_message_snapshot(
                group_id="g", user_id="u1", nickname="one", content="same",
                request_id="m2", onebot_message_id="m2", event_timestamp=101,
            )
            self.assertNotEqual(first.current_row_id, second_same_text.current_row_id)
            self.assertEqual(
                [row["content"] for row in store.get_recent_messages("g", 10)].count("same"), 2
            )

    def test_two_concurrent_same_group_messages_keep_independent_snapshot_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "bot.db", history_limit=20)
            barrier = threading.Barrier(2)

            def record(request_id: str, text: str):
                barrier.wait(timeout=2)
                return store.record_message_snapshot(
                    group_id="shared-group", user_id=request_id, nickname=request_id,
                    content=text, request_id=request_id, onebot_message_id=request_id,
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(record, "message-a", "concurrent message A")
                second = executor.submit(record, "message-b", "concurrent message B")
                snapshots = (first.result(timeout=5), second.result(timeout=5))

            earlier, later = sorted(snapshots, key=lambda item: item.current_row_id)
            earlier_text = "concurrent message A" if earlier.request_id == "message-a" else "concurrent message B"
            later_text = "concurrent message A" if later.request_id == "message-a" else "concurrent message B"
            earlier_context = store.get_recent_messages_before(earlier, 10)
            later_context = store.get_recent_messages_before(later, 10)

            self.assertNotEqual(earlier.current_row_id, later.current_row_id)
            self.assertNotIn(later_text, [row["content"] for row in earlier_context])
            self.assertIn(earlier_text, [row["content"] for row in later_context])

    def test_group_epoch_isolation_and_assistant_write_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "bot.db", history_limit=20)
            snapshot = store.record_message_snapshot(
                group_id="g1", user_id="u1", nickname="one", content="question", request_id="m1"
            )
            store.record_message_snapshot(
                group_id="g2", user_id="u2", nickname="two", content="private", request_id="m2"
            )
            self.assertTrue(store.record_assistant_if_epoch(snapshot, "bot", "bot", "answer"))
            self.assertEqual(store.get_recent_messages_before(snapshot, 10), [])
            self.assertEqual(store.get_recent_messages("g1", 10)[-1]["content"], "answer")
            self.assertEqual(store.get_recent_messages("g2", 10)[-1]["content"], "private")

    def test_context_clear_invalidates_inflight_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "bot.db", history_limit=20)
            snapshot = store.record_message_snapshot(
                group_id="g", user_id="u", nickname="member", content="question", request_id="r1"
            )

            store.clear_group_context("g")

            self.assertFalse(store.context_snapshot_is_current(snapshot))
            self.assertFalse(store.record_assistant_if_epoch(snapshot, "bot", "bot", "late answer"))
            self.assertEqual(store.get_recent_messages("g", 10), [])

    def test_summary_committed_after_request_snapshot_is_not_visible_to_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "bot.db", history_limit=20)
            snapshot = store.record_message_snapshot(
                group_id="g", user_id="u1", nickname="one", content="current question", request_id="m1"
            )
            future_id = store.add_message("g", "u2", "two", "user", "future message")
            store.upsert_group_summary("g", "summary contains future message", future_id)

            self.assertIsNone(store.get_group_summary_for_snapshot(snapshot))

    def test_summary_revision_cas_allows_only_one_concurrent_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "bot.db", history_limit=20)
            for idx in range(6):
                store.add_message("g", f"u{idx}", f"n{idx}", "user", f"m{idx}")
            first = store.begin_group_summary_snapshot("g", keep_recent=2, trigger_count=5)
            second = store.begin_group_summary_snapshot("g", keep_recent=2, trigger_count=5)
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)

            self.assertTrue(store.commit_group_summary(first, "first summary"))
            self.assertFalse(store.commit_group_summary(second, "stale competing summary"))
            summary = store.get_group_summary("g")
            self.assertEqual(summary["summary"], "first summary")
            self.assertEqual(summary["revision"], 1)
            self.assertEqual(len(store.get_recent_messages("g", 10)), 6)

    def test_context_migration_preserves_legacy_messages_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "legacy.db"
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "CREATE TABLE message_history (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "group_id TEXT NOT NULL, user_id TEXT NOT NULL, nickname TEXT NOT NULL DEFAULT '', "
                    "role TEXT NOT NULL, content TEXT NOT NULL, "
                    "created_at TEXT NOT NULL DEFAULT (datetime('now')))"
                )
                conn.execute(
                    "CREATE TABLE conversation_summaries (group_id TEXT PRIMARY KEY, summary TEXT NOT NULL, "
                    "last_message_id INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
                )
                conn.execute(
                    "INSERT INTO message_history(group_id,user_id,nickname,role,content) "
                    "VALUES ('legacy-group','member','Member','user','old source row')"
                )
                conn.execute(
                    "INSERT INTO conversation_summaries(group_id,summary,last_message_id) "
                    "VALUES ('legacy-group','legacy summary',1)"
                )
                conn.commit()
            finally:
                conn.close()

            store = MemoryStore(path, history_limit=20)
            rows = store.get_recent_messages("legacy-group", 10)
            self.assertEqual([row["content"] for row in rows], ["old source row"])
            self.assertEqual(store.get_group_summary("legacy-group")["summary"], "legacy summary")
            snapshot = store.record_message_snapshot(
                group_id="legacy-group", user_id="new", nickname="New", content="new question",
                request_id="new-request", onebot_message_id="new-message",
            )
            self.assertEqual(snapshot.epoch, 0)
            self.assertEqual(
                [row["content"] for row in store.get_recent_messages_before(snapshot, 10)],
                ["old source row"],
            )

    def test_scheduled_task_crud(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bot.db"
            store = MemoryStore(db_path, history_limit=20)
            task_id = store.add_scheduled_task(
                group_id="1",
                created_by="admin",
                prompt="测试任务",
                schedule_type="once",
                next_run_at=100,
                created_at=50,
            )
            self.assertEqual(len(store.list_scheduled_tasks("1")), 1)
            self.assertEqual(store.get_due_scheduled_tasks(99), [])
            due = store.get_due_scheduled_tasks(100)
            self.assertEqual(due[0]["id"], task_id)
            store.reschedule_scheduled_task(task_id, 200)
            self.assertEqual(store.get_due_scheduled_tasks(100), [])
            self.assertEqual(store.get_due_scheduled_tasks(200)[0]["id"], task_id)
            store.finish_scheduled_task(task_id, 200)
            self.assertEqual(store.list_scheduled_tasks("1"), [])

            interval_id = store.add_scheduled_task(
                group_id="1",
                created_by="admin",
                prompt="循环任务",
                schedule_type="interval",
                interval_seconds=60,
                next_run_at=300,
                created_at=250,
            )
            store.finish_scheduled_task(interval_id, 300)
            rows = store.list_scheduled_tasks("1")
            self.assertEqual(rows[0]["id"], interval_id)
            self.assertEqual(rows[0]["next_run_at"], 360)
            self.assertEqual(store.clear_scheduled_tasks("1"), 1)


if __name__ == "__main__":
    unittest.main()

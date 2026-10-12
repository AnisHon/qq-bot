from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from app.memory import MemoryStore


class MemoryStoreTest(unittest.TestCase):
    def test_history_trim_and_memory_crud(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bot.db"
            store = MemoryStore(db_path, history_limit=3)
            for idx in range(5):
                store.add_message("1", f"u{idx}", f"n{idx}", "user", f"m{idx}")
            recent = store.get_recent_messages("1", limit=10)
            self.assertEqual([row["content"] for row in recent], ["m2", "m3", "m4"])

            cleared = store.clear_group_history("1")
            self.assertEqual(cleared, 3)
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

            rows = store.get_messages_for_summary("1", keep_recent=2)
            self.assertEqual([row["content"] for row in rows], ["m0", "m1", "m2", "m3"])
            self.assertTrue(store.message_exists("1", int(rows[-1]["id"])))

            store.upsert_group_summary("1", "摘要", int(rows[-1]["id"]))
            summary = store.get_group_summary("1")
            self.assertIsNotNone(summary)
            self.assertEqual(summary["summary"], "摘要")

            deleted = store.delete_messages_through("1", int(rows[-1]["id"]))
            self.assertEqual(deleted, 4)
            self.assertFalse(store.message_exists("1", int(rows[-1]["id"])))
            self.assertEqual([row["content"] for row in store.get_recent_messages("1", 10)], ["m4", "m5"])

            cleared = store.clear_group_context("1")
            self.assertEqual(cleared, 2)
            self.assertEqual(store.get_recent_messages("1", 10), [])
            self.assertIsNone(store.get_group_summary("1"))

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

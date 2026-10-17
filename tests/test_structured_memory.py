from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.context.memories import MemoryService
from app.memory import MemoryStore
from app.tools.contracts import Evidence, ToolResult, ToolStatus


class StructuredMemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "memory.db"
        self.store = MemoryStore(self.path)
        self.service = MemoryService(self.store)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_legacy_ids_and_content_survive_as_unverified(self) -> None:
        legacy_id = self.store.add_memory("group", "g1", "legacy tungsten fact", "u1")
        user_id = self.store.add_memory("user", "u1", "legacy private statement", "u1")

        legacy = self.store.list_memories(scope="group", scope_id="g1")
        user_row = self.store.list_memories(scope="user", scope_id="u1")

        self.assertEqual(legacy[0]["id"], legacy_id)
        self.assertEqual(legacy[0]["content"], "legacy tungsten fact")
        self.assertEqual(legacy[0]["verification"], "legacy_unknown")
        self.assertEqual(user_row[0]["id"], user_id)
        self.assertEqual(user_row[0]["visibility"], "legacy_unscoped")
        self.assertEqual(self.service.select_for_context(
            "tungsten", group_id="g1", user_id="u1", limit=10
        ), [])

    def test_additive_old_schema_migration_preserves_record_identity(self) -> None:
        legacy_path = Path(self.tempdir.name) / "old.db"
        with closing(sqlite3.connect(legacy_path)) as conn:
            conn.execute(
                "CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT,scope TEXT NOT NULL,"
                "scope_id TEXT NOT NULL,content TEXT NOT NULL,source_user_id TEXT NOT NULL,"
                "created_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
            conn.execute(
                "INSERT INTO memories(id,scope,scope_id,content,source_user_id) "
                "VALUES(42,'group','legacy-group','old note','writer')"
            )
            conn.commit()
        migrated = MemoryStore(legacy_path)
        row = migrated.list_memories(scope="group", scope_id="legacy-group")[0]
        self.assertEqual(row["id"], 42)
        self.assertEqual(row["content"], "old note")
        self.assertEqual(row["verification"], "legacy_unknown")
        self.assertEqual(row["group_id"], "legacy-group")

    def test_admin_confirmed_fact_is_searchable_with_revision_and_provenance(self) -> None:
        memory_id = self.service.add_admin_fact(
            group_id="g1", actor_user_id="admin", content="GTNH 默认配方版本是 2.7.3",
            source_message_id="qq-100",
        )
        self.assertEqual(self.service.add_admin_fact(
            group_id="g1", actor_user_id="admin", content="GTNH 默认配方版本是 2.7.3",
            source_message_id="qq-101",
        ), memory_id)
        self.store.rebuild_memory_search_batch()

        matches = self.service.search("2.7.3 配方", group_id="g1", user_id="member")
        with closing(sqlite3.connect(self.path)) as conn:
            row = conn.execute(
                "SELECT verification,status,revision,visibility,group_id FROM memories WHERE id=?",
                (memory_id,),
            ).fetchone()
            sources = conn.execute(
                "SELECT source_kind,source_ref,verification FROM memory_sources WHERE memory_id=?",
                (memory_id,),
            ).fetchall()
            revisions = conn.execute(
                "SELECT revision,content FROM memory_revisions WHERE memory_id=? ORDER BY revision",
                (memory_id,),
            ).fetchall()

        self.assertEqual(len(matches), 1)
        self.assertEqual(row, ("admin_confirmed", "active", 1, "group", "g1"))
        self.assertEqual(sources[0], ("qq_admin_command", "qq-100", "admin_confirmed"))
        self.assertEqual(len(revisions), 1)

    def test_user_assertion_is_private_to_one_group_and_owner(self) -> None:
        memory_id = self.service.add_user_assertion(
            group_id="g1", user_id="u1", content="我偏好简短回答", source_message_id="message-9",
        )
        scoped = self.store.search_memories("简短回答", group_id="g1", user_id="u1")
        other_user = self.store.search_memories("简短回答", group_id="g1", user_id="u2")
        other_group = self.store.search_memories("简短回答", group_id="g2", user_id="u1")
        injected = self.service.select_for_context("简短回答", group_id="g1", user_id="u1", limit=10)

        self.assertEqual([int(row["id"]) for row in scoped], [memory_id])
        self.assertEqual(other_user, [])
        self.assertEqual(other_group, [])
        self.assertEqual(injected, [])  # user_asserted is searchable but not silently promoted into shared prompt facts.

    def test_tool_observed_requires_typed_successful_evidence(self) -> None:
        with self.assertRaises(ValueError):
            self.service.add_tool_observation(
                group_id="g1", actor_user_id="u1", content="not actually verified",
                result={"status": "ok", "evidence": []},  # type: ignore[arg-type]
            )
        with self.assertRaises(ValueError):
            self.service.add_tool_observation(
                group_id="g1", actor_user_id="u1", content="failed tool result",
                result=ToolResult(ToolStatus.ERROR, "error"),
            )

        memory_id = self.service.add_tool_observation(
            group_id="g1", actor_user_id="u1", content="repo hash verified",
            result=ToolResult(
                ToolStatus.OK, {"text": "observed"},
                evidence=(Evidence("repository_file", "repo", "file.py", "file.py:1-4", revision="abc123"),),
            ),
        )
        row = self.store.list_memories(scope="group", scope_id="g1")[0]
        self.assertEqual(int(row["id"]), memory_id)
        self.assertEqual(row["verification"], "tool_observed")

    def test_conflicting_key_preserves_both_versions_and_marks_conflict(self) -> None:
        first, _ = self.store.add_structured_memory(
            group_id="g1", owner_user_id=None, content="Pack version is 2.7.3",
            actor_user_id="admin", verification="admin_confirmed", source_kind="command",
            normalized_key="pack-version",
        )
        second, created = self.store.add_structured_memory(
            group_id="g1", owner_user_id=None, content="Pack version is 2.8.0",
            actor_user_id="admin", verification="admin_confirmed", source_kind="command",
            normalized_key="pack-version",
        )
        rows = self.store.list_memories(scope="group", scope_id="g1")
        self.assertTrue(created)
        self.assertNotEqual(first, second)
        self.assertEqual({row["status"] for row in rows}, {"conflict"})
        self.assertEqual(len(rows), 2)

    def test_retraction_is_logical_and_search_no_longer_returns_memory(self) -> None:
        memory_id = self.service.add_admin_fact(
            group_id="g1", actor_user_id="admin", content="设备标签 alpha-404",
            source_message_id="m1",
        )
        self.assertTrue(self.service.retract(memory_id, group_id="g1"))
        self.assertEqual(self.store.search_memories(
            "alpha-404", group_id="g1", user_id="u1"
        ), [])
        with closing(sqlite3.connect(self.path)) as conn:
            status, source_count = conn.execute(
                "SELECT status,(SELECT COUNT(*) FROM memory_sources WHERE memory_id=memories.id) "
                "FROM memories WHERE id=?", (memory_id,),
            ).fetchone()
        self.assertEqual(status, "retracted")
        self.assertEqual(source_count, 1)

    def test_revision_retains_previous_text_and_source_and_purge_marks_source_deleted(self) -> None:
        memory_id = self.service.add_admin_fact(
            group_id="g1", actor_user_id="admin", content="工厂配置使用旧名称",
            source_message_id="qq-original",
        )
        self.assertTrue(self.service.revise_admin_fact(
            memory_id, group_id="g1", actor_user_id="admin",
            content="工厂配置已改名", source_message_id="qq-revision",
        ))
        self.store.record_message_snapshot(
            group_id="g1", user_id="admin", nickname="admin", content="purge trigger",
            request_id="purge", onebot_message_id="purge-trigger",
        )
        self.store.begin_group_history_purge("g1")
        with closing(sqlite3.connect(self.path)) as conn:
            revisions = conn.execute(
                "SELECT content FROM memory_revisions WHERE memory_id=? ORDER BY revision", (memory_id,)
            ).fetchall()
            source_states = conn.execute(
                "SELECT source_state FROM memory_sources WHERE memory_id=? ORDER BY rowid", (memory_id,)
            ).fetchall()
            current_content = conn.execute(
                "SELECT content FROM memories WHERE id=?", (memory_id,)
            ).fetchone()[0]
        self.assertEqual([row[0] for row in revisions], ["工厂配置使用旧名称", "工厂配置已改名"])
        self.assertEqual({row[0] for row in source_states}, {"source_deleted"})
        self.assertEqual(current_content, "工厂配置已改名")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from app.context.summaries import SummaryOutputError, parse_episode_summary
from app.memory import MemoryStore


class StructuredSummaryTest(unittest.TestCase):
    def test_parser_accepts_only_snapshot_source_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "summary.db")
            store.add_message("g", "u", "member", "user", "source row")
            snapshot = store.begin_group_summary_snapshot("g", keep_recent=0, trigger_count=1)
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            allowed = int(snapshot.rows[0]["id"])
            raw = json.dumps({
                "schema_version": 1, "topic": "topic", "summary": "one fact",
                "decisions": [{"text": "decision", "source_row_ids": [allowed]}],
                "open_questions": [],
            })
            parsed = parse_episode_summary(raw, snapshot, max_chars=100)
            self.assertEqual(parsed["decisions"][0]["source_row_ids"], [allowed])

            malicious = json.loads(raw)
            malicious["decisions"][0]["source_row_ids"] = [allowed + 100]
            with self.assertRaises(SummaryOutputError):
                parse_episode_summary(json.dumps(malicious), snapshot, max_chars=100)
            with self.assertRaises(SummaryOutputError):
                parse_episode_summary("plain text", snapshot, max_chars=100)

    def test_summary_and_episode_commit_atomically_and_are_searchable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "summary.db")
            store.add_message("g", "u", "member", "user", "讨论配方版本 2.7.3")
            store.add_message("g", "u", "member", "user", "决定使用版本 2.7.3")
            snapshot = store.begin_group_summary_snapshot("g", keep_recent=0, trigger_count=1)
            assert snapshot is not None
            payload = parse_episode_summary(json.dumps({
                "schema_version": 1, "topic": "配方版本", "summary": "版本 2.7.3",
                "decisions": [{"text": "使用 2.7.3", "source_row_ids": [int(snapshot.rows[1]["id"])]}],
                "open_questions": [],
            }, ensure_ascii=False), snapshot, max_chars=100)

            self.assertTrue(store.commit_group_summary(snapshot, payload["summary"], payload))
            self.assertEqual(store.get_group_summary("g")["summary"], "版本 2.7.3")
            episodes = store.search_episodes("配方版本", group_id="g", epoch=snapshot.epoch)
            self.assertEqual(len(episodes), 1)
            self.assertIn("2.7.3", episodes[0]["summary"])
            self.assertEqual(json.loads(episodes[0]["source_refs_json"]), [
                f"history:g:{snapshot.epoch}:{row['id']}" for row in snapshot.rows
            ])

    def test_modified_or_retracted_source_prevents_commit(self) -> None:
        for mutate in ("edit", "retract"):
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as tmpdir:
                store = MemoryStore(Path(tmpdir) / "summary.db")
                row_id = store.add_message("g", "u", "member", "user", "original")
                snapshot = store.begin_group_summary_snapshot("g", keep_recent=0, trigger_count=1)
                assert snapshot is not None
                payload = {
                    "schema_version": 1, "topic": "topic", "summary": "summary",
                    "decisions": [], "open_questions": [],
                }
                if mutate == "edit":
                    store.update_history_message_content("g", row_id, "edited")
                else:
                    store.retract_history_message("g", row_id)
                self.assertFalse(store.commit_group_summary(snapshot, "summary", payload))
                self.assertIsNone(store.get_group_summary("g"))
                with store._connect() as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM episodic_memory").fetchone()[0], 0)

    def test_token_trigger_and_input_byte_cap_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "summary.db")
            store.add_message("g", "u", "member", "user", "small stable fact")
            store.add_message("g", "u", "member", "user", "x" * 5000)
            store.add_message("g", "u", "member", "user", "keep recent")
            bounded_prefix = store.begin_group_summary_snapshot(
                "g", keep_recent=1, trigger_count=99, trigger_tokens=256, max_input_bytes=4096,
            )
            self.assertIsNotNone(bounded_prefix)
            assert bounded_prefix is not None
            self.assertEqual(len(bounded_prefix.rows), 1)
            self.assertEqual(bounded_prefix.rows[0]["content"], "small stable fact")

            store.add_message("huge", "u", "member", "user", "y" * 5000)
            store.add_message("huge", "u", "member", "user", "keep")
            self.assertIsNone(store.begin_group_summary_snapshot(
                "huge", keep_recent=1, trigger_count=99, trigger_tokens=256, max_input_bytes=4096,
            ))


if __name__ == "__main__":
    unittest.main()

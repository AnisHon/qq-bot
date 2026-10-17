from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.context.history import ChromaHistoryVectorIndex, HistoryRetriever, HistoryVectorSync
from app.memory import MemoryStore


class HistoryIndexTest(unittest.TestCase):
    def test_thousand_row_fixture_finds_old_chinese_and_technical_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "history.db")
            target_id = None
            for index in range(1000):
                content = f"群聊噪声消息 {index} 今天天气很好"
                if index == 17:
                    content = "GTNH 2.7.3: 钨酸钠在 EBF IV 的配方需要 sodium tungstate 和氦气。"
                row_id = store.add_message("pack-a", f"u{index % 7}", "member", "user", content)
                if index == 17:
                    target_id = row_id
            store.add_message("other-pack", "u", "member", "user", "钨酸钠和 EBF IV")
            snapshot = store.record_message_snapshot(
                group_id="pack-a", user_id="u0", nickname="now", content="请回忆 GTNH 钨酸钠 EBF IV 配方",
                request_id="current", onebot_message_id="current",
            )

            results = store.search_history_fts("GTNH 钨酸钠 EBF IV", snapshot, limit=12)

            self.assertTrue(any(int(row["id"]) == target_id for row in results))
            self.assertTrue(all(row["group_id"] == "pack-a" for row in results))
            self.assertTrue(all(int(row["id"]) < snapshot.current_row_id for row in results))
            matched = next(row for row in results if int(row["id"]) == target_id)
            self.assertEqual(matched["completeness"], "complete")
            self.assertEqual(matched["source_ref"], f"history:pack-a:{snapshot.epoch}:{target_id}")

    def test_old_schema_backfill_is_resumable_and_reports_not_indexed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "legacy.db"
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE message_history (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL, "
                "user_id TEXT NOT NULL, nickname TEXT NOT NULL DEFAULT '', role TEXT NOT NULL, content TEXT NOT NULL, "
                "created_at TEXT NOT NULL DEFAULT (datetime('now')) )"
            )
            conn.executemany(
                "INSERT INTO message_history(group_id,user_id,nickname,role,content) VALUES('g','u','member','user',?)",
                [(f"legacy row {index}",) for index in range(4)],
            )
            conn.commit()
            conn.close()
            store = MemoryStore(path)
            snapshot = store.record_message_snapshot(
                group_id="g", user_id="u", nickname="now", content="current query",
                request_id="now", onebot_message_id="now",
            )
            retriever = HistoryRetriever(store, _async_immediate)

            first = store.rebuild_chat_history_index_batch(limit=2)
            state = store.chat_history_index_status()
            migrated_rows = store.get_recent_messages("g", limit=10)
            self.assertEqual([row["id"] for row in migrated_rows], [1, 2, 3, 4, 5])
            self.assertEqual(migrated_rows[0]["content"], "legacy row 0")
            store = MemoryStore(path)  # Restart resumes from the committed batch cursor.
            second = store.rebuild_chat_history_index_batch(limit=2)
            third = store.rebuild_chat_history_index_batch(limit=2)
            fourth = store.rebuild_chat_history_index_batch(limit=2)
            result = _run(retriever.search("legacy row 1", snapshot, limit=10))

            self.assertEqual(first["indexed"], 2)
            self.assertFalse(state["backfill_complete"])
            self.assertTrue(third["complete"])
            self.assertTrue(fourth["complete"])
            self.assertEqual(result.status, "results")
            self.assertTrue(any("legacy row 1" in str(row["content"]) for row in result.rows))

    def test_index_update_retraction_reset_and_purge_stay_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "history.db")
            first_id = store.add_message("g", "u", "member", "user", "稀有钨酸盐技术标识 recipe_alpha")
            second_id = store.add_message("g", "u", "member", "user", "ordinary followup")
            current = store.record_message_snapshot(
                group_id="g", user_id="u", nickname="now", content="lookup",
                request_id="current", onebot_message_id="current",
            )
            self.assertTrue(store.search_history_fts("钨酸盐 recipe_alpha", current, limit=10))
            self.assertTrue(store.update_history_message_content("g", first_id, "编辑后的普通内容"))
            self.assertEqual(store.search_history_fts("钨酸盐 recipe_alpha", current, limit=10), [])
            self.assertTrue(store.retract_history_message("g", first_id))
            self.assertFalse(store.message_exists("g", -1))

            retired_count = store.reset_group_context("g")
            self.assertEqual(retired_count, 2)
            self.assertTrue(store.message_exists("g", second_id))
            self.assertEqual(store.get_recent_messages("g", 20), [])
            self.assertEqual(store.search_history_fts("ordinary followup", current, limit=10), [])
            fresh = store.record_message_snapshot(
                group_id="g", user_id="u", nickname="now", content="fresh",
                request_id="fresh", onebot_message_id="fresh",
            )
            self.assertEqual(store.search_history_fts("ordinary followup", fresh, limit=10), [])

            purge_count = store.begin_group_history_purge("g")
            self.assertEqual(purge_count, 3)
            page1 = store.purge_group_history_batch("g", limit=2)
            page2 = store.purge_group_history_batch("g", limit=2)
            self.assertEqual(page1["deleted"], 2)
            self.assertTrue(page2["complete"])
            self.assertEqual(store.count_group_history("g"), 0)
            self.assertEqual(store.chat_history_index_status()["indexed_messages"], 0)

    def test_fts_optimization_is_idempotent_and_keeps_searchable_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "history.db")
            target_id = None
            for index in range(100):
                content = f"普通群聊噪声 {index}"
                if index == 37:
                    content = "GTNH fixture rare_identifier molybdenum"
                row_id = store.add_message("g", f"u{index % 5}", "member", "user", content)
                if index == 37:
                    target_id = row_id
            snapshot = store.record_message_snapshot(
                group_id="g", user_id="current", nickname="now", content="lookup",
                request_id="current", onebot_message_id="current",
            )

            first = store.optimize_context_search_indexes()
            second = store.optimize_context_search_indexes()
            hits = store.search_history_fts("rare_identifier molybdenum", snapshot, limit=8)

            self.assertGreaterEqual(first["optimized_indexes"], 3)
            self.assertEqual(first, second)
            self.assertTrue(any(int(row["id"]) == target_id for row in hits))

    def test_cjk_bigram_fallback_and_untrusted_match_text_are_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "history.db")
            store.chat_trigram_available = False
            store.add_message("g", "u", "member", "user", "记住钨酸盐设备配方编号 recipe_007")
            snapshot = store.record_message_snapshot(
                group_id="g", user_id="u", nickname="now", content="search",
                request_id="r", onebot_message_id="r",
            )

            rows = store.search_history_fts("钨酸盐 recipe_007", snapshot, limit=8)
            hostile = store.search_history_fts('" OR * NEAR(content)', snapshot, limit=8)

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["completeness"], "complete")
            self.assertEqual(hostile, [])

    def test_capacity_degrades_with_healthy_epoch_snapshot_and_no_fake_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(
                Path(tmpdir) / "history.db", history_max_messages_per_group=1,
                history_max_bytes_per_group=1024,
            )
            first = store.record_message_snapshot(
                group_id="g", user_id="u", nickname="member", content="first",
                request_id="r1", onebot_message_id="m1",
            )
            overflow = store.record_message_snapshot(
                group_id="g", user_id="u", nickname="member", content="second",
                request_id="r2", onebot_message_id="m2",
            )
            self.assertTrue(first.persisted)
            self.assertFalse(overflow.persisted)
            self.assertEqual(overflow.current_row_id, None)
            self.assertEqual(overflow.persistence_status, "capacity_degraded")
            self.assertTrue(store.context_snapshot_is_current(overflow))
            self.assertEqual(store.count_group_history("g"), 1)

    def test_sqlite_capacity_query_failure_is_not_converted_to_degraded_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "history.db")
            store._history_capacity_available = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                sqlite3.OperationalError("disk I/O error")
            )
            with self.assertRaises(sqlite3.OperationalError):
                store.record_message_snapshot(
                    group_id="g", user_id="u", nickname="member", content="x",
                    request_id="r", onebot_message_id="m",
                )

    def test_explicit_retention_batch_invalidates_snapshots_and_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "history.db"
            store = MemoryStore(path)
            old_id = store.add_message("g", "u", "member", "user", "old source rare_identifier")
            store.add_message("g", "u", "member", "user", "recent source")
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("UPDATE message_history SET created_at='2000-01-01 00:00:00' WHERE id=?", (old_id,))
                conn.commit()
            before = store.record_message_snapshot(
                group_id="g", user_id="u", nickname="now", content="lookup",
                request_id="before", onebot_message_id="before",
            )

            removed = store.prune_history_before("2001-01-01 00:00:00", batch_size=1)
            after = store.record_message_snapshot(
                group_id="g", user_id="u", nickname="now", content="lookup",
                request_id="after", onebot_message_id="after",
            )

            self.assertEqual(removed, 1)
            self.assertFalse(store.context_snapshot_is_current(before))
            self.assertEqual(store.search_history_fts("rare_identifier", after, limit=10), [])
            self.assertTrue(store.message_exists("g", 2))

    def test_fake_vector_outbox_retries_and_sqlite_rejects_stale_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "history.db", history_vector_enabled=True)
            row_id = store.add_message("g", "u", "member", "user", "the old source about tungsten")
            snapshot = store.record_message_snapshot(
                group_id="g", user_id="u", nickname="now", content="question",
                request_id="r", onebot_message_id="current",
            )
            index = FakeHistoryVectorIndex()
            sync = HistoryVectorSync(store, _async_immediate, index, _async_vector)
            failed = _run(sync.drain_once())
            self.assertEqual(failed["failed"], 0)
            stale_hit = index.query(
                query="paraphrase", group_id="g", epoch=snapshot.epoch,
                embedding_version="chat-default-v1", limit=10,
            )
            self.assertTrue(stale_hit)

            self.assertTrue(store.update_history_message_content("g", row_id, "new source about nickel"))
            result = _run(HistoryRetriever(
                store, _async_immediate, vector_index=index, vector_call=_async_vector
            ).search("paraphrase", snapshot, limit=10))
            self.assertFalse(any(int(row["id"]) == row_id for row in result.rows))
            self.assertEqual(result.vector_status, "no_results")

            self.assertEqual(_run(sync.drain_once())["failed"], 0)
            fresh = _run(HistoryRetriever(
                store, _async_immediate, vector_index=index, vector_call=_async_vector
            ).search("paraphrase", snapshot, limit=10))
            self.assertTrue(any(int(row["id"]) == row_id for row in fresh.rows))
            self.assertEqual(store.pending_history_vector_operations(), [])

    def test_vector_fault_keeps_fts_results_and_outbox_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "history.db", history_vector_enabled=True)
            row_id = store.add_message("g", "u", "member", "user", "rare CJK 钨酸盐 recipe_998")
            snapshot = store.record_message_snapshot(
                group_id="g", user_id="u", nickname="now", content="lookup",
                request_id="r", onebot_message_id="current",
            )
            index = FakeHistoryVectorIndex(fail=True)
            sync = HistoryVectorSync(store, _async_immediate, index, _async_vector)
            result = _run(sync.drain_once())
            self.assertEqual(result["failed"], 2)
            self.assertEqual(len(store.pending_history_vector_operations()), 2)

            searched = _run(HistoryRetriever(
                store, _async_immediate, vector_index=index, vector_call=_async_vector
            ).search("钨酸盐 recipe_998", snapshot, limit=10))
            self.assertEqual(searched.vector_status, "unavailable")
            self.assertTrue(any(int(row["id"]) == row_id for row in searched.rows))

    def test_chroma_adapter_uses_explicit_fake_embedder_and_scope_metadata(self) -> None:
        collection = FakeChromaCollection()
        adapter = ChromaHistoryVectorIndex(collection, FakeEmbedder())
        adapter.upsert(
            group_id="g", row_id=7, epoch=2, content="sample", content_hash="hash",
            embedding_version="fixture-v1",
        )
        hits = adapter.query(
            query="similar", group_id="g", epoch=2,
            embedding_version="fixture-v1", limit=8,
        )
        self.assertEqual(hits[0]["id"], 7)
        self.assertEqual(hits[0]["content_hash"], "hash")
        self.assertEqual(collection.last_where["$and"][0], {"group_id": "g"})
        self.assertEqual(collection.metadatas[0]["embedding_version"], "fixture-v1")

    def test_temporary_chroma_collection_with_fake_embedder_is_scope_filtered(self) -> None:
        try:
            import chromadb
        except ImportError:
            self.skipTest("chromadb is not installed in this environment")

        with tempfile.TemporaryDirectory() as tmpdir:
            client = chromadb.PersistentClient(path=str(Path(tmpdir) / "chroma"))
            collection = client.get_or_create_collection(
                name="context_v2_fixture",
                metadata={"hnsw:space": "cosine"},
            )
            adapter = ChromaHistoryVectorIndex(collection, FakeEmbedder())
            adapter.upsert(
                group_id="g", row_id=1, epoch=2, content="钨酸钠 GTNH fixture",
                content_hash="hash-visible", embedding_version="fixture-v1",
            )
            adapter.upsert(
                group_id="g", row_id=2, epoch=3, content="same group, retired epoch",
                content_hash="hash-old-epoch", embedding_version="fixture-v1",
            )
            adapter.upsert(
                group_id="other", row_id=3, epoch=2, content="other group",
                content_hash="hash-other-group", embedding_version="fixture-v1",
            )

            hits = adapter.query(
                query="钨酸钠 query", group_id="g", epoch=2,
                embedding_version="fixture-v1", limit=10,
            )

            self.assertEqual([(hit["id"], hit["content_hash"]) for hit in hits], [(1, "hash-visible")])
            client.close()


class FakeHistoryVectorIndex:
    def __init__(self, *, fail: bool = False) -> None:
        self.rows: dict[tuple[str, int], dict[str, object]] = {}
        self.fail = fail

    def upsert(self, *, group_id, row_id, epoch, content, content_hash, embedding_version):
        if self.fail:
            raise OSError("fixture vector failure")
        self.rows[(group_id, row_id)] = {
            "id": row_id, "epoch": epoch, "content": content,
            "content_hash": content_hash, "embedding_version": embedding_version,
        }

    def delete(self, *, group_id, row_id):
        if self.fail:
            raise OSError("fixture vector failure")
        self.rows.pop((group_id, row_id), None)

    def query(self, *, query, group_id, epoch, embedding_version, limit):
        if self.fail:
            raise OSError("fixture vector failure")
        return [
            {"id": row_id, "content_hash": row["content_hash"], "rank": 0.1}
            for (candidate_group, row_id), row in self.rows.items()
            if candidate_group == group_id and row["epoch"] == epoch
            and row["embedding_version"] == embedding_version
        ][:limit]


class FakeEmbedder:
    def encode(self, texts, show_progress_bar=False):
        return [[float(len(text)), 1.0] for text in texts]


class FakeChromaCollection:
    def __init__(self):
        self.ids = []
        self.metadatas = []
        self.last_where = None

    def upsert(self, *, ids, embeddings, documents, metadatas):
        self.ids = list(ids)
        self.metadatas = list(metadatas)

    def delete(self, *, ids):
        self.ids = []
        self.metadatas = []

    def query(self, *, query_embeddings, n_results, where, include):
        self.last_where = where
        return {
            "ids": [self.ids[:n_results]], "metadatas": [self.metadatas[:n_results]],
            "distances": [[0.1] * min(n_results, len(self.ids))],
        }


def _immediate(function, /, *args, **kwargs):
    return function(*args, **kwargs)


async def _async_immediate(function, /, *args, **kwargs):
    return _immediate(function, *args, **kwargs)


async def _async_vector(function, /, *args, **kwargs):
    return function(*args, **kwargs)


def _run(awaitable):
    import asyncio

    return asyncio.run(awaitable)


if __name__ == "__main__":
    unittest.main()

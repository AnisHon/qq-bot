from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app.rag import RagStore
from app.knowledge.documents import DocumentSource, IndexState, LocatorKind
from app.knowledge.ingestion import (
    MarkdownParseError,
    chunk_markdown_document,
    ingest_markdown_file,
    parse_markdown_document,
)
from app.bot_service import _looks_like_sfm_query, _format_rag_chunks
from app.vector_rag import VectorRagStore
from scripts.index_sfm_pdf import delete_document_indexes


class _FakeEmbeddings(list):
    def tolist(self):
        return list(self)


class _FakeEmbedder:
    def __init__(self) -> None:
        self.fail = False

    def encode(self, texts, show_progress_bar=False):
        if self.fail:
            raise RuntimeError("fixture embedding failure")
        return _FakeEmbeddings([[float(len(text))] for text in texts])


class _FakeCollection:
    def __init__(self) -> None:
        self.rows: dict[str, tuple[list[float], str, dict[str, object]]] = {}
        self.fail_delete = False
        self.fail_add = False

    def get(self, include=None):
        return {"ids": list(self.rows)}

    def count(self):
        return len(self.rows)

    def delete(self, ids):
        if self.fail_delete:
            raise RuntimeError("fixture Chroma delete failure")
        for chunk_id in ids:
            self.rows.pop(chunk_id, None)

    def add(self, *, ids, embeddings, documents, metadatas):
        if self.fail_add:
            raise RuntimeError("fixture Chroma add failure")
        for chunk_id, embedding, document, metadata in zip(ids, embeddings, documents, metadatas):
            self.rows[chunk_id] = (embedding, document, metadata)


class _FakeChromaClient:
    def __init__(self) -> None:
        self.collections: dict[str, _FakeCollection] = {}

    def get_or_create_collection(self, *, name, metadata):
        return self.collections.setdefault(name, _FakeCollection())

    def get_collection(self, *, name):
        if name not in self.collections:
            raise KeyError(name)
        return self.collections[name]

    def delete_collection(self, *, name):
        self.collections.pop(name, None)


class VectorRagStoreTest(unittest.TestCase):
    def make_store(self, tmpdir: str) -> tuple[VectorRagStore, _FakeChromaClient, _FakeEmbedder]:
        store = VectorRagStore(Path(tmpdir) / "chromadb")
        client = _FakeChromaClient()
        embedder = _FakeEmbedder()
        store._client = client
        store._embedder = embedder
        return store, client, embedder

    def test_replacement_clears_old_tail_is_idempotent_and_isolated_by_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, client, _embedder = self.make_store(tmpdir)
            self.assertEqual(
                store.upsert_chunks(
                    "sfm",
                    [
                        ("sfm:c1", 1, "SFM", "first old chunk"),
                        ("sfm:c2", 1, "SFM", "second old chunk"),
                        ("sfm:c3", 2, "SFM", "tail old chunk"),
                    ],
                ),
                3,
            )
            store.upsert_chunks("other", [("other:c1", 1, "Other", "other document")])

            self.assertEqual(
                store.upsert_chunks("sfm", [("sfm:new", 1, "SFM", "replacement text")]),
                1,
            )
            sfm_collection = client.collections["rag_sfm"]
            self.assertEqual(list(sfm_collection.rows), ["sfm:new"])
            self.assertEqual(sfm_collection.rows["sfm:new"][1], "SFM replacement text")
            self.assertTrue(store.has_document("sfm"))
            self.assertTrue(store.has_document("other"))

            store.upsert_chunks("sfm", [("sfm:new", 1, "SFM", "updated in place")])
            self.assertEqual(list(sfm_collection.rows), ["sfm:new"])
            self.assertEqual(sfm_collection.rows["sfm:new"][1], "SFM updated in place")
            self.assertEqual(list(client.collections["rag_other"].rows), ["other:c1"])

    def test_empty_replacement_and_explicit_delete_remove_all_vectors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, client, _embedder = self.make_store(tmpdir)
            store.upsert_chunks("sfm", [("sfm:c1", 1, "SFM", "old content")])
            self.assertEqual(store.upsert_chunks("sfm", []), 0)
            self.assertFalse(store.has_document("sfm"))
            self.assertEqual(client.collections["rag_sfm"].rows, {})

            store.upsert_chunks("sfm", [("sfm:c2", 2, "SFM", "new content")])
            store.delete_document("sfm")
            self.assertFalse(store.has_document("sfm"))
            self.assertNotIn("rag_sfm", client.collections)
            store.delete_document("sfm")
            self.assertNotIn("rag_sfm", client.collections)

    def test_embedding_failure_happens_before_existing_vectors_are_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, client, embedder = self.make_store(tmpdir)
            store.upsert_chunks("sfm", [("sfm:old", 1, "SFM", "old indexed content")])
            embedder.fail = True

            with self.assertRaisesRegex(RuntimeError, "fixture embedding failure"):
                store.upsert_chunks("sfm", [("sfm:new", 1, "SFM", "new content")])

            self.assertEqual(list(client.collections["rag_sfm"].rows), ["sfm:old"])

    def test_chroma_cleanup_failure_is_not_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, client, _embedder = self.make_store(tmpdir)
            store.upsert_chunks("sfm", [("sfm:old", 1, "SFM", "old content")])
            client.collections["rag_sfm"].fail_delete = True

            with self.assertRaisesRegex(RuntimeError, "fixture Chroma delete failure"):
                store.upsert_chunks("sfm", [("sfm:new", 1, "SFM", "new content")])

            self.assertEqual(list(client.collections["rag_sfm"].rows), ["sfm:old"])


class RagStoreTest(unittest.TestCase):
    def test_additive_metadata_tables_keep_existing_pdf_schema_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "old.db"
            legacy_conn = sqlite3.connect(db_path)
            with legacy_conn as conn:
                conn.execute(
                    "CREATE TABLE rag_documents (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "name TEXT NOT NULL UNIQUE, source_path TEXT NOT NULL, content_hash TEXT NOT NULL, "
                    "created_at TEXT NOT NULL DEFAULT (datetime('now'))"
                    ")"
                )
                conn.execute(
                    "CREATE TABLE rag_chunks (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "document_name TEXT NOT NULL, chunk_id TEXT NOT NULL UNIQUE, source TEXT NOT NULL, "
                    "page INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '', content TEXT NOT NULL)"
                )
                conn.execute(
                    "CREATE VIRTUAL TABLE rag_chunks_fts USING fts5("
                    "chunk_id UNINDEXED, document_name UNINDEXED, title, content, "
                    "tokenize = 'unicode61 remove_diacritics 2')"
                )
                conn.execute(
                    "INSERT INTO rag_documents (name, source_path, content_hash) VALUES (?, ?, ?)",
                    ("legacy-pdf", "legacy.pdf", "legacy-hash"),
                )
                conn.execute(
                    "INSERT INTO rag_chunks (document_name, chunk_id, source, page, title, content) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("legacy-pdf", "legacy:p1:c1", "legacy.pdf", 1, "Legacy", "legacy keyword text"),
                )
                conn.execute(
                    "INSERT INTO rag_chunks_fts (chunk_id, document_name, title, content) VALUES (?, ?, ?, ?)",
                    ("legacy:p1:c1", "legacy-pdf", "Legacy", "legacy keyword text"),
                )
            legacy_conn.close()

            store = RagStore(db_path)
            self.assertEqual(store.index_status("legacy-pdf").state, IndexState.INDEXED)
            self.assertIsNone(store.index_status("legacy-pdf").source)
            results = store.search("keyword", document_name="legacy-pdf")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].page, 1)
            self.assertIsNone(results[0].locator)

    def test_rag_connection_closes_on_success_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = RagStore(Path(tmpdir) / "bot.db")
            real_connect = sqlite3.connect
            opened: list[sqlite3.Connection] = []

            def track_connect(*args, **kwargs):
                connection = real_connect(*args, **kwargs)
                opened.append(connection)
                return connection

            with patch("app.rag.sqlite3.connect", side_effect=track_connect):
                with store._connect() as conn:
                    conn.execute("CREATE TABLE fixture_connection (value TEXT)")
                with self.assertRaisesRegex(ValueError, "fixture rollback"):
                    with store._connect() as conn:
                        conn.execute("INSERT INTO fixture_connection VALUES ('not persisted')")
                        raise ValueError("fixture rollback")

            self.assertEqual(len(opened), 2)
            for connection in opened:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")
            self.assertEqual(store.search("not persisted"), [])

    def test_replace_and_search_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bot.db"
            store = RagStore(db_path)
            count = store.replace_document(
                "sfm",
                Path("sfm.pdf"),
                "hash",
                [
                    ("sfm:p1:c1", 1, "SFM", "every 20 ticks input from chest output to furnace"),
                    ("sfm:p2:c1", 2, "SFM", "label gun 可以给方块添加标签"),
                ],
            )
            self.assertEqual(count, 2)
            results = store.search("label gun 标签枪", document_name="sfm", limit=1)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].page, 2)

    def test_delete_document_removes_only_matching_sqlite_fts_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = RagStore(Path(tmpdir) / "bot.db")
            store.replace_document(
                "sfm",
                Path("sfm.pdf"),
                "hash-one",
                [("sfm:p1:c1", 1, "SFM", "input from chest")],
            )
            store.replace_document(
                "other",
                Path("other.md"),
                "hash-two",
                [("other:c1", 1, "Other", "input from furnace")],
            )

            self.assertEqual(store.delete_document("sfm"), 1)
            self.assertEqual(store.delete_document("sfm"), 0)
            self.assertEqual(store.search("chest", document_name="sfm"), [])
            self.assertEqual(len(store.search("furnace", document_name="other")), 1)
            with closing(sqlite3.connect(store.sqlite_path)) as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM rag_documents WHERE name = 'sfm'").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM rag_chunks_fts WHERE document_name = 'sfm'").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM rag_chunks WHERE document_name = 'sfm'").fetchone()[0],
                    0,
                )

    def test_document_delete_coordinator_reports_partial_and_retry_completes(self) -> None:
        class FakeRagStore:
            fail = True
            calls = 0

            def delete_document(self, name):
                self.calls += 1
                if self.fail:
                    raise RuntimeError("fixture SQLite failure")
                return 3

        class FakeVectorStore:
            fail = False
            calls = 0

            def delete_document(self, name):
                self.calls += 1
                if self.fail:
                    raise RuntimeError("fixture Chroma failure")

        rag = FakeRagStore()
        vector = FakeVectorStore()
        with self.assertRaisesRegex(RuntimeError, "partial document deletion"):
            delete_document_indexes(Path("fixture.db"), "sfm", rag_store=rag, vector_store=vector)
        self.assertEqual(rag.calls, 1)
        self.assertEqual(vector.calls, 1)

        rag.fail = False
        vector.fail = True
        with self.assertRaisesRegex(RuntimeError, "partial document deletion"):
            delete_document_indexes(Path("fixture.db"), "sfm", rag_store=rag, vector_store=vector)
        self.assertEqual(rag.calls, 2)
        self.assertEqual(vector.calls, 2)

        vector.fail = False
        self.assertEqual(
            delete_document_indexes(Path("fixture.db"), "sfm", rag_store=rag, vector_store=vector),
            3,
        )
        self.assertEqual(rag.calls, 3)
        self.assertEqual(vector.calls, 3)

    def test_sfm_trigger_and_format(self) -> None:
        self.assertTrue(_looks_like_sfm_query("SFM 的 input output 怎么写"))
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bot.db"
            store = RagStore(db_path)
            store.replace_document(
                "sfm",
                Path("sfm.pdf"),
                "hash",
                [("sfm:p1:c1", 1, "SFM", "input 从标签中输入物品，output 输出到目标。")],
            )
            text = _format_rag_chunks(store.search("input output", document_name="sfm", limit=1))
            self.assertIn("第 1 页", text)
            self.assertIn("input", text)


class MarkdownIngestionTest(unittest.TestCase):
    def source(self, source_id: str = "fixture:guide") -> DocumentSource:
        return DocumentSource(
            source_id=source_id,
            source_uri="fixture://gtnh/guide.md",
            source_type="markdown",
            corpus_id="fixture-corpus",
            pack_id="fixture-pack",
            pack_version="fixture-1.0",
            game_version="1.7.10-fixture",
            source_revision="fixture-revision-1",
            license_id="fixture-only",
        )

    def test_markdown_sections_keep_heading_line_ranges_and_ignore_code_fences(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "documents" / "markdown_fixture.md"
        document = parse_markdown_document(fixture.read_bytes(), self.source())
        self.assertEqual(document.title, "guide")
        multiblock = next(section for section in document.sections if section.title == "Multiblock")
        self.assertEqual(multiblock.section_path, ("Fixture Pack Knowledge", "Multiblock"))
        self.assertEqual(multiblock.start_line, 5)
        self.assertIn("The fixture voltage tier", multiblock.content)
        self.assertFalse(any("This heading is code" in section.section_path for section in document.sections))
        chunks = chunk_markdown_document(document, max_chars=128, overlap_chars=16)
        located = next(chunk for chunk in chunks if chunk.title == "Energy")
        self.assertEqual(located.locator.kind, LocatorKind.LINE_RANGE)
        self.assertLessEqual(located.locator.start_line, located.locator.end_line)
        self.assertEqual(located.section_path, ("Fixture Pack Knowledge", "Multiblock", "Energy"))

    def test_markdown_ingest_persists_version_source_locator_and_stable_ids(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "documents" / "markdown_fixture.md"
        with tempfile.TemporaryDirectory() as tmpdir:
            store = RagStore(Path(tmpdir) / "bot.db")
            source = self.source("fixture-source")
            document_id = "fixture:guide/page-1"
            first_count = ingest_markdown_file(
                fixture, source, store, document_id=document_id, max_chars=128, overlap_chars=16
            )
            with store._connect() as conn:
                first_ids = [
                    row[0]
                    for row in conn.execute(
                        "SELECT chunk_id FROM rag_chunks WHERE document_name = ? ORDER BY chunk_id",
                        (document_id,),
                    )
                ]
            second_count = ingest_markdown_file(
                fixture, source, store, document_id=document_id, max_chars=128, overlap_chars=16
            )
            with store._connect() as conn:
                second_ids = [
                    row[0]
                    for row in conn.execute(
                        "SELECT chunk_id FROM rag_chunks WHERE document_name = ? ORDER BY chunk_id",
                        (document_id,),
                    )
                ]

            self.assertGreater(first_count, 0)
            self.assertEqual(second_count, first_count)
            self.assertEqual(second_ids, first_ids)
            status = store.index_status(document_id)
            self.assertEqual(status.state, IndexState.INDEXED)
            self.assertEqual(status.chunk_count, first_count)
            self.assertEqual(status.source.source_id, "fixture-source")
            self.assertEqual(status.source.pack_version, "fixture-1.0")
            self.assertEqual(status.source.game_version, "1.7.10-fixture")
            self.assertEqual(status.parser_version, "markdown-v1")
            self.assertEqual(status.chunker_version, "markdown-lines-v1")
            result = store.search("voltage alpha", document_name=document_id, limit=4)
            self.assertTrue(result)
            matching = next(chunk for chunk in result if "voltage" in chunk.content)
            self.assertEqual(matching.pack_id, "fixture-pack")
            self.assertEqual(matching.pack_version, "fixture-1.0")
            self.assertEqual(matching.section_path, ("Fixture Pack Knowledge", "Multiblock"))
            self.assertIsNotNone(matching.locator)
            self.assertEqual(matching.locator.kind, LocatorKind.LINE_RANGE)
            self.assertIn("parser_version", matching.metadata)

    def test_invalid_encoding_empty_document_and_missing_index_are_explicit(self) -> None:
        source = self.source("fixture:empty")
        with self.assertRaises(MarkdownParseError):
            parse_markdown_document(b"\xff\xfe\xff", source)
        empty = parse_markdown_document(b"", source)
        self.assertEqual(empty.sections, ())
        with tempfile.TemporaryDirectory() as tmpdir:
            store = RagStore(Path(tmpdir) / "bot.db")
            self.assertEqual(store.index_status("missing").state, IndexState.NOT_INDEXED)
            empty_path = Path(tmpdir) / "empty.md"
            empty_path.write_bytes(b"")
            self.assertEqual(ingest_markdown_file(empty_path, source, store), 0)
            self.assertEqual(store.index_status(source.source_id).state, IndexState.EMPTY)
            self.assertEqual(store.search("anything", document_name=source.source_id), [])

    def test_long_single_line_chunks_advance_and_have_unique_repeatable_ids(self) -> None:
        content = "abcdefgh " * 100
        parsed = parse_markdown_document(content, self.source("fixture:long"))
        first = chunk_markdown_document(parsed, max_chars=64, overlap_chars=50)
        second = chunk_markdown_document(parsed, max_chars=64, overlap_chars=50)
        self.assertGreater(len(first), 1)
        self.assertTrue(all(len(chunk.content) <= 64 for chunk in first))
        self.assertEqual(len({chunk.chunk_id for chunk in first}), len(first))
        self.assertEqual([chunk.chunk_id for chunk in first], [chunk.chunk_id for chunk in second])
        self.assertTrue(all(chunk.locator.start_line == chunk.locator.end_line == 1 for chunk in first))


if __name__ == "__main__":
    unittest.main()

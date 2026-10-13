from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.knowledge.documents import (
    DocumentChunk,
    DocumentLocator,
    DocumentSource,
    IndexState,
    LocatorKind,
)
from app.knowledge.index_jobs import DocumentIndexLifecycle, _generation_id
from app.knowledge.retrieval import DocumentKnowledgeService
from app.rag import RagStore
from app.vector_rag import VectorRagStore, _generation_collection_name


def _matches(metadata: dict[str, object], where: dict[str, object] | None) -> bool:
    if not where:
        return True
    if "$and" in where:
        return all(_matches(metadata, item) for item in where["$and"])
    for key, expected in where.items():
        if isinstance(expected, dict) and isinstance(expected.get("$in"), list):
            if metadata.get(key) not in expected["$in"]:
                return False
        elif metadata.get(key) != expected:
            return False
    return True


class _FakeEmbeddings(list):
    def tolist(self):
        return list(self)


class _FakeEmbedder:
    def encode(self, texts, show_progress_bar=False):
        return _FakeEmbeddings([[float(len(text))] for text in texts])


class _FakeCollection:
    def __init__(self, client, name):
        self.client = client
        self.name = name
        self.rows: dict[str, tuple[list[float], str, dict[str, object]]] = {}

    def get(self, include=None, where=None):
        selected = [(key, row) for key, row in self.rows.items() if _matches(row[2], where)]
        result = {"ids": [key for key, _row in selected]}
        if include and "metadatas" in include:
            result["metadatas"] = [row[2] for _key, row in selected]
        return result

    def count(self):
        return len(self.rows)

    def delete(self, ids):
        if self.name in self.client.fail_collection_delete:
            raise RuntimeError("fake Chroma delete failure")
        for chunk_id in ids:
            self.rows.pop(chunk_id, None)

    def add(self, *, ids, embeddings, documents, metadatas):
        if self.name in self.client.fail_collection_add:
            raise RuntimeError("fake Chroma write failure")
        for chunk_id, embedding, document, metadata in zip(ids, embeddings, documents, metadatas):
            self.rows[chunk_id] = (embedding, document, metadata)

    def query(self, *, query_embeddings, n_results, include, where=None):
        target = query_embeddings[0][0]
        rows = [
            (key, row) for key, row in self.rows.items()
            if _matches(row[2], where)
        ]
        rows.sort(key=lambda item: (abs(item[1][0][0] - target), item[0]))
        selected = rows[:n_results]
        return {
            "ids": [[key for key, _row in selected]],
            "metadatas": [[row[2] for _key, row in selected]],
            "distances": [[abs(row[0][0] - target) for _key, row in selected]],
        }


class _FakeChromaClient:
    def __init__(self):
        self.collections: dict[str, _FakeCollection] = {}
        self.fail_collection_add: set[str] = set()
        self.fail_collection_delete: set[str] = set()

    def get_or_create_collection(self, *, name, metadata):
        return self.collections.setdefault(name, _FakeCollection(self, name))

    def get_collection(self, *, name):
        if name not in self.collections:
            raise KeyError(name)
        return self.collections[name]

    def delete_collection(self, *, name):
        if name in self.fail_collection_delete:
            raise RuntimeError("fake Chroma collection deletion failure")
        self.collections.pop(name, None)


class IndexLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.rag = RagStore(root / "fixture.sqlite3")
        self.vector = VectorRagStore(root / "chroma")
        self.client = _FakeChromaClient()
        self.vector._client = self.client
        self.vector._embedder = _FakeEmbedder()
        self.lifecycle = DocumentIndexLifecycle(self.rag, self.vector)
        self.document_id = "fixture-guide"

    def tearDown(self):
        self.tmpdir.cleanup()

    def source(
        self, revision: str = "fixture-r1", source_type: str = "markdown"
    ) -> DocumentSource:
        return DocumentSource(
            source_id="synthetic-guide",
            source_uri="fixture://documents/guide.md",
            source_type=source_type,
            corpus_id="synthetic-corpus",
            pack_id="synthetic-pack",
            pack_version="synthetic-1",
            source_revision=revision,
        )

    def chunk(self, chunk_id: str, content: str, line: int) -> DocumentChunk:
        return DocumentChunk(
            chunk_id=chunk_id,
            document_id=self.document_id,
            title="Synthetic guide",
            content=content,
            section_path=("Fixture", "Lifecycle"),
            locator=DocumentLocator(LocatorKind.LINE_RANGE, start_line=line, end_line=line + 1),
            metadata={"fixture": True},
        )

    def replace(self, chunks, revision="fixture-r1"):
        return self.lifecycle.replace_generation(
            self.document_id,
            "fixture://local/guide.md",
            "synthetic-content-hash-" + revision + "-" + str(len(chunks)),
            chunks,
            document_source=self.source(revision),
            parser_version="fixture-parser-v1",
            chunker_version="fixture-chunker-v1",
        )

    def test_replacement_uses_active_generation_and_retains_previous_for_rollback(self):
        first_chunks = [
            self.chunk("old-prefix", "old prefix material", 10),
            self.chunk("old-tail", "old trailing material", 20),
        ]
        first = self.replace(first_chunks)
        self.assertEqual(first.status, "active")
        self.assertEqual({item.chunk_id for item in self.rag.search("trailing", self.document_id)}, {"old-tail"})

        second = self.replace([self.chunk("new-only", "new replacement material", 5)], "fixture-r2")
        self.assertEqual(second.previous_generation, first.generation_id)
        self.assertEqual(second.pending_cleanup, (first.generation_id,))
        self.assertEqual(self.rag.index_status(self.document_id).generation, second.generation_id)
        self.assertEqual(self.rag.search("trailing", self.document_id), [])
        self.assertEqual({item.chunk_id for item in self.rag.search("replacement material", self.document_id)}, {"new-only"})
        vector_result = DocumentKnowledgeService(self.rag, self.vector)._vector_search_sync(
            "replacement material", self.rag.document_catalog(), None, 8
        )
        self.assertEqual({item.chunk_id for item in vector_result.chunks}, {"new-only"})
        self.assertEqual(self.lifecycle.index_status(self.document_id).pending_cleanup, (first.generation_id,))

        self.assertEqual(self.lifecycle.rollback(self.document_id, first.generation_id), second.generation_id)
        self.assertEqual(self.rag.index_status(self.document_id).generation, first.generation_id)
        self.assertEqual({item.chunk_id for item in self.rag.search("trailing", self.document_id)}, {"old-tail"})

    def test_chroma_write_failure_keeps_previous_active_and_is_observable(self):
        old = self.replace([self.chunk("old", "old active content", 1)])
        generation = _generation_id(
            self.document_id,
            "synthetic-content-hash-fixture-r2-1",
            [self.chunk("new", "new staged content", 2)],
            self.source("fixture-r2"),
            "fixture-parser-v1",
            "fixture-chunker-v1",
            None,
        )
        self.client.fail_collection_add.add(_generation_collection_name(self.document_id, generation))
        with self.assertRaisesRegex(RuntimeError, "fake Chroma write failure"):
            self.replace([self.chunk("new", "new staged content", 2)], "fixture-r2")

        self.assertEqual(self.rag.index_status(self.document_id).generation, old.generation_id)
        self.assertEqual({item.chunk_id for item in self.rag.search("active content", self.document_id)}, {"old"})
        status = self.lifecycle.index_status(self.document_id)
        self.assertIn(generation, status.pending_cleanup)
        self.assertTrue(any(item["status"] == "failed" for item in status.operations))

    def test_interrupted_staging_recovers_and_switches_only_after_vector_verification(self):
        chunks = [self.chunk("recovered", "recovered staging content", 3)]
        generation = _generation_id(
            self.document_id,
            "manual-recovery-hash",
            chunks,
            self.source(),
            "fixture-parser-v1",
            "fixture-chunker-v1",
            None,
        )
        operation = self.rag.stage_generation(
            self.document_id,
            generation,
            "fixture://local/guide.md",
            "manual-recovery-hash",
            chunks,
            document_source=self.source(),
            parser_version="fixture-parser-v1",
            chunker_version="fixture-chunker-v1",
        )
        self.assertIsNotNone(operation)
        self.assertIsNone(self.rag.generation_head(self.document_id))
        self.assertEqual(self.rag.search("recovered staging", self.document_id), [])

        recovered = self.lifecycle.recover_pending_operations()
        self.assertEqual(recovered[0].status, "recovered_active")
        self.assertEqual(self.rag.index_status(self.document_id).generation, generation)
        self.assertEqual({item.chunk_id for item in self.rag.search("recovered staging", self.document_id)}, {"recovered"})

    def test_delete_is_tombstoned_immediately_and_cleanup_is_retryable(self):
        first = self.replace([self.chunk("one", "first generation content", 1)])
        second = self.replace([self.chunk("two", "second generation content", 2)], "fixture-r2")
        names = {
            _generation_collection_name(self.document_id, generation)
            for generation in (first.generation_id, second.generation_id)
        }
        self.client.fail_collection_delete.update(names)
        deleted = self.lifecycle.delete_document(self.document_id)
        self.assertEqual(deleted.status, "tombstoned_pending_cleanup")
        self.assertTrue(self.lifecycle.index_status(self.document_id).tombstoned)
        self.assertEqual(self.rag.search("generation content", self.document_id), [])

        first_recovery = self.lifecycle.recover_pending_operations()
        self.assertTrue(any(item.status == "cleanup_pending" for item in first_recovery))
        self.assertTrue(self.lifecycle.index_status(self.document_id).pending_cleanup)
        self.client.fail_collection_delete.clear()
        second_recovery = self.lifecycle.recover_pending_operations()
        self.assertTrue(any(item.status == "cleanup_complete" for item in second_recovery))
        self.assertEqual(self.lifecycle.index_status(self.document_id).pending_cleanup, ())
        self.assertIsNone(self.rag.index_status(self.document_id).generation)

        reimported = self.replace([self.chunk("again", "reimported after deletion", 4)], "fixture-r3")
        self.assertEqual(self.rag.index_status(self.document_id).generation, reimported.generation_id)
        self.assertFalse(self.lifecycle.index_status(self.document_id).tombstoned)

    def test_vector_rebuild_is_dry_runnable_and_idempotent(self):
        active = self.replace([self.chunk("active", "active rebuild content", 1)])
        collection_name = _generation_collection_name(self.document_id, active.generation_id)
        self.assertEqual(self.lifecycle.rebuild_index(dry_run=True)[0].status, "would_rebuild")
        self.assertIn(collection_name, self.client.collections)
        # Leave the active physical collection in place but incomplete. The
        # rebuild must publish a shadow collection before queuing old cleanup.
        self.client.collections[collection_name].delete(ids=["active"])

        rebuilt = self.lifecycle.rebuild_index(self.document_id)
        self.assertEqual(rebuilt[0].status, "verified")
        revision = self.rag.generation_vector_revision(self.document_id, active.generation_id)
        self.assertNotEqual(revision, active.generation_id)
        self.assertEqual(
            self.vector.generation_chunk_ids(
                self.document_id, active.generation_id, collection_revision=revision
            ),
            frozenset({"active"}),
        )
        self.assertIn(collection_name, self.client.collections)
        recovered = self.lifecycle.recover_pending_operations()
        self.assertTrue(any(item.status == "vector_cleanup_complete" for item in recovered))
        self.assertNotIn(collection_name, self.client.collections)
        self.assertEqual(self.lifecycle.rebuild_index(self.document_id)[0].status, "verified")

    def test_failed_shadow_rebuild_keeps_active_revision_and_old_collection(self):
        chunks = [
            self.chunk("active-a", "first active rebuild content", 1),
            self.chunk("active-b", "second active rebuild content", 3),
        ]
        active = self.replace(chunks)
        original_revision = self.rag.generation_vector_revision(
            self.document_id, active.generation_id
        )
        collection = self.client.get_collection(
            name=_generation_collection_name(self.document_id, original_revision)
        )
        collection.delete(ids=["active-b"])
        failed_revision = f"{active.generation_id}-r2"
        failed_collection = _generation_collection_name(self.document_id, failed_revision)
        self.client.fail_collection_add.add(failed_collection)

        with self.assertRaisesRegex(RuntimeError, "fake Chroma write failure"):
            self.lifecycle.rebuild_index(self.document_id)

        self.assertEqual(
            self.rag.generation_vector_revision(self.document_id, active.generation_id),
            original_revision,
        )
        self.assertEqual(
            self.vector.generation_chunk_ids(
                self.document_id, active.generation_id, collection_revision=original_revision
            ),
            frozenset({"active-a"}),
        )
        self.assertEqual(self.rag.search("first active rebuild", self.document_id)[0].chunk_id, "active-a")
        self.assertIn(failed_collection, self.client.collections)

    def test_vector_rebuild_shadow_recovers_after_ready_before_activation(self):
        chunks = [self.chunk("recovered-vector", "vector crash recovery content", 1)]
        active = self.replace(chunks)
        original_revision = self.rag.generation_vector_revision(
            self.document_id, active.generation_id
        )
        self.vector.delete_generation(
            self.document_id, active.generation_id, collection_revision=original_revision
        )
        revision, operation_id, expected_revision = self.rag.begin_vector_rebuild(
            self.document_id, active.generation_id
        )
        self.assertEqual(
            self.rag.begin_vector_rebuild(self.document_id, active.generation_id),
            (revision, operation_id, expected_revision),
        )
        self.assertEqual(expected_revision, original_revision)
        self.vector.upsert_generation_chunks(
            self.source(), self.document_id, active.generation_id, chunks,
            collection_revision=revision,
        )
        self.rag.mark_vector_rebuild_ready(operation_id)

        recovered = self.lifecycle.recover_pending_operations()

        self.assertTrue(any(item.status == "vector_rebuild_recovered" for item in recovered))
        self.assertEqual(
            self.rag.generation_vector_revision(self.document_id, active.generation_id), revision
        )
        self.assertEqual(
            self.vector.generation_chunk_ids(
                self.document_id, active.generation_id, collection_revision=revision
            ),
            frozenset({"recovered-vector"}),
        )

    def test_late_vector_rebuild_failure_cannot_cleanup_published_revision(self):
        chunks = [self.chunk("late-failure", "active vector revision content", 1)]
        active = self.replace(chunks)
        second_rag = RagStore(self.rag.sqlite_path)
        revision, operation_id, _expected = self.rag.begin_vector_rebuild(
            self.document_id, active.generation_id
        )
        self.vector.upsert_generation_chunks(
            self.source(), self.document_id, active.generation_id, chunks,
            collection_revision=revision,
        )
        self.rag.mark_vector_rebuild_ready(operation_id)
        self.rag.activate_vector_revision(operation_id)

        # Model another worker finishing late after the first worker published.
        second_rag.mark_vector_rebuild_ready(operation_id)
        second_rag.mark_vector_rebuild_failed(operation_id, "LateWorkerError")

        operation = next(
            item for item in second_rag.index_lifecycle_rows(self.document_id)
            if item["operation_id"] == operation_id
        )
        self.assertEqual(operation["status"], "complete")
        self.assertEqual(
            second_rag.generation_vector_revision(self.document_id, active.generation_id), revision
        )
        self.assertNotIn(
            (self.document_id, active.generation_id, revision),
            second_rag.pending_vector_cleanup(),
        )
        self.lifecycle.recover_pending_operations()
        self.assertEqual(
            self.vector.generation_chunk_ids(
                self.document_id, active.generation_id, collection_revision=revision
            ),
            frozenset({"late-failure"}),
        )

    def test_generation_with_legacy_source_type_uses_generation_collection(self):
        old = self.chunk("old-legacy-vector", "retired legacy source marker", 1)
        self.rag.replace_document(
            self.document_id,
            "fixture://documents/guide.md",
            "legacy-hash",
            [old],
            document_source=self.source(source_type="legacy"),
        )
        self.vector.upsert_chunks(
            self.document_id, [(old.chunk_id, 1, old.title, old.content)]
        )
        current = self.chunk("new-generation-vector", "current managed source passage", 20)
        self.lifecycle.replace_generation(
            self.document_id,
            "fixture://documents/guide.md",
            "managed-hash",
            [current],
            document_source=self.source("fixture-r2", source_type="legacy"),
            parser_version="fixture-parser-v1",
            chunker_version="fixture-chunker-v1",
        )

        outcome = DocumentKnowledgeService(self.rag, self.vector)._vector_search_sync(
            "retired legacy source marker", self.rag.document_catalog(), None, 8
        )

        self.assertNotIn("old-legacy-vector", {item.chunk_id for item in outcome.chunks})

    def test_vector_query_rejects_same_count_but_wrong_active_chunk_ids(self):
        self.replace([self.chunk("active-exact-id", "SQLite active source content", 1)])
        stale = self.chunk("stale-same-count-id", "stale vector source content", 10)
        self.vector.upsert_generation_chunks(
            self.source(), self.document_id,
            self.rag.index_status(self.document_id).generation,
            [stale],
        )

        outcome = DocumentKnowledgeService(self.rag, self.vector)._vector_search_sync(
            "stale vector source", self.rag.document_catalog(), None, 8
        )

        self.assertEqual(outcome.chunks, ())
        self.assertEqual(outcome.missing_document_count, 1)

    def test_compare_and_swap_rejects_stale_activation_after_concurrent_update(self):
        original = self.replace([self.chunk("base", "base active content", 1)])
        staged_a = [self.chunk("candidate-a", "candidate a content", 2)]
        staged_b = [self.chunk("candidate-b", "candidate b content", 3)]
        operation_a = self.rag.stage_generation(
            self.document_id, "candidate-generation-a", "fixture://a", "hash-a", staged_a,
            document_source=self.source("candidate-a"), parser_version="p1", chunker_version="c1",
        )
        operation_b = self.rag.stage_generation(
            self.document_id, "candidate-generation-b", "fixture://b", "hash-b", staged_b,
            document_source=self.source("candidate-b"), parser_version="p1", chunker_version="c1",
        )
        for operation_id, generation_id, chunks in (
            (operation_a, "candidate-generation-a", staged_a),
            (operation_b, "candidate-generation-b", staged_b),
        ):
            self.vector.upsert_generation_chunks(
                self.source(), self.document_id, generation_id, chunks
            )
            self.rag.mark_generation_vector_ready(operation_id)

        self.rag.activate_generation(operation_a)
        with self.assertRaisesRegex(RuntimeError, "active generation changed"):
            self.rag.activate_generation(operation_b)
        self.assertEqual(self.rag.generation_head(self.document_id), ("candidate-generation-a", False))
        self.assertNotEqual(self.rag.index_status(self.document_id).generation, original.generation_id)

    def test_staged_activation_cannot_clear_a_concurrent_delete_tombstone(self):
        self.replace([self.chunk("base", "base active content", 1)])
        chunks = [self.chunk("staged", "staged after deletion content", 2)]
        operation = self.rag.stage_generation(
            self.document_id, "staged-generation", "fixture://staged", "staged-hash", chunks,
            document_source=self.source("staged"), parser_version="p1", chunker_version="c1",
        )
        self.vector.upsert_generation_chunks(
            self.source(), self.document_id, "staged-generation", chunks
        )
        self.rag.mark_generation_vector_ready(operation)
        self.rag.tombstone_generation_document(self.document_id, "delete-concurrent")

        with self.assertRaisesRegex(RuntimeError, "activatable state"):
            self.rag.activate_generation(operation)
        self.assertEqual(self.rag.generation_head(self.document_id), (None, True))
        self.assertEqual(self.rag.search("staged after deletion", self.document_id), [])

    def test_repeated_delete_invalidates_staged_reimport_while_tombstoned(self):
        self.rag.tombstone_generation_document(self.document_id, "initial-delete")
        chunks = [self.chunk("staged-after-delete", "staged reimport content", 2)]
        operation = self.rag.stage_generation(
            self.document_id, "staged-after-delete-generation", "fixture://staged", "staged-hash", chunks,
            document_source=self.source("staged-after-delete"), parser_version="p1", chunker_version="c1",
        )
        self.vector.upsert_generation_chunks(
            self.source(), self.document_id, "staged-after-delete-generation", chunks
        )
        self.rag.mark_generation_vector_ready(operation)
        self.rag.tombstone_generation_document(self.document_id, "repeated-delete")

        with self.assertRaisesRegex(RuntimeError, "not in an activatable state"):
            self.rag.activate_generation(operation)
        self.assertEqual(self.rag.generation_head(self.document_id), (None, True))
        self.assertTrue(any(
            item.status == "cleanup_complete"
            for item in self.lifecycle.recover_pending_operations()
        ))
        self.assertIsNone(
            self.rag.generation_snapshot(self.document_id, "staged-after-delete-generation")
        )

    def test_cleanup_claim_prevents_other_store_rollback_or_restage(self):
        old_chunks = [self.chunk("old-cleanup", "old retained rollback content", 1)]
        old = self.replace(old_chunks)
        new = self.replace([self.chunk("new-cleanup", "new active content", 2)], "fixture-r2")
        second_rag = RagStore(self.rag.sqlite_path)
        second_lifecycle = DocumentIndexLifecycle(second_rag, self.vector)

        self.assertTrue(self.rag.claim_generation_cleanup(self.document_id, old.generation_id))
        self.assertIn(
            (self.document_id, old.generation_id), self.rag.pending_generation_cleanup()
        )
        with self.assertRaisesRegex(ValueError, "not retained"):
            second_lifecycle.rollback(self.document_id, old.generation_id)
        with self.assertRaisesRegex(RuntimeError, "cleanup has started"):
            second_lifecycle.replace_generation(
                self.document_id,
                "fixture://local/guide.md",
                "synthetic-content-hash-fixture-r1-1",
                old_chunks,
                document_source=self.source(),
                parser_version="fixture-parser-v1",
                chunker_version="fixture-chunker-v1",
            )
        second_rag.tombstone_generation_document(self.document_id, "delete-during-cleanup")
        second_rag.mark_generation_failed(f"replace:{old.generation_id}", "RuntimeError")
        connection = sqlite3.connect(self.rag.sqlite_path)
        try:
            state = connection.execute(
                "SELECT state FROM rag_index_generations WHERE document_name = ? AND generation_id = ?",
                (self.document_id, old.generation_id),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(state, "cleaning")
        self.assertEqual(second_rag.generation_head(self.document_id), (None, True))
        self.assertTrue(any(
            item.status == "cleanup_complete"
            for item in self.lifecycle.recover_pending_operations()
        ))
        self.assertIsNone(self.rag.generation_snapshot(self.document_id, old.generation_id))

    def test_rollback_with_missing_nondefault_vector_revision_fails_closed(self):
        old = self.replace([self.chunk("old-revision", "retained old revision content", 1)])
        self.client.collections[
            _generation_collection_name(self.document_id, old.generation_id)
        ].delete(ids=["old-revision"])
        self.lifecycle.rebuild_index(self.document_id)
        self.lifecycle.recover_pending_operations()
        old_revision = self.rag.generation_vector_revision(self.document_id, old.generation_id)
        self.assertNotEqual(old_revision, old.generation_id)

        new = self.replace([self.chunk("new-revision", "new current revision content", 2)], "fixture-r2")
        self.vector.delete_generation(
            self.document_id, old.generation_id, collection_revision=old_revision
        )
        with self.assertRaisesRegex(RuntimeError, "Chroma IDs do not match"):
            self.lifecycle.rollback(self.document_id, old.generation_id)

        self.assertEqual(self.rag.generation_head(self.document_id), (new.generation_id, False))
        self.assertEqual(
            self.rag.generation_vector_revision(self.document_id, old.generation_id), old_revision
        )
        self.assertNotIn(
            _generation_collection_name(self.document_id, old.generation_id), self.client.collections
        )

    def test_generation_filter_and_empty_generation_status(self):
        active = self.replace([])
        status = self.rag.index_status(self.document_id)
        self.assertEqual(status.state, IndexState.EMPTY)
        self.assertEqual(status.generation, active.generation_id)
        self.assertEqual(self.rag.document_catalog()[0].document_id, self.document_id)
        self.assertEqual(self.rag.search("anything", self.document_id), [])

    def test_active_generation_query_excludes_stale_shared_corpus_vectors(self):
        old = self.chunk("old-shared-id", "retired revision marker", 1)
        self.rag.replace_document(
            self.document_id,
            "fixture://documents/guide.md",
            "old-shared-hash",
            [old],
            document_source=self.source(),
            parser_version="fixture-parser-v1",
            chunker_version="fixture-chunker-v1",
        )
        self.vector.upsert_corpus_chunks(self.source(), self.document_id, [old])

        other_id = "fixture-peer"
        peer = DocumentChunk(
            chunk_id="peer-id",
            document_id=other_id,
            title="Peer guide",
            content="peer unrelated passage",
            section_path=("Fixture",),
            locator=DocumentLocator(LocatorKind.LINE_RANGE, start_line=1, end_line=1),
        )
        self.rag.replace_document(
            other_id,
            "fixture://documents/peer.md",
            "peer-hash",
            [peer],
            document_source=self.source(),
            parser_version="fixture-parser-v1",
            chunker_version="fixture-chunker-v1",
        )
        self.vector.upsert_corpus_chunks(self.source(), other_id, [peer])

        self.replace([self.chunk("current-id", "current active passage", 20)], "fixture-r2")
        service = DocumentKnowledgeService(self.rag, self.vector)
        outcome = service._vector_search_sync("retired revision marker", self.rag.document_catalog(), None, 8)
        self.assertNotIn("old-shared-id", {item.chunk_id for item in outcome.chunks})

    def test_dry_run_does_not_create_or_delete_index_state(self):
        chunk = self.chunk("dry", "dry run content", 1)
        planned = self.lifecycle.replace_generation(
            self.document_id,
            "fixture://local/guide.md",
            "dry-hash",
            [chunk],
            document_source=self.source(),
            parser_version="fixture-parser-v1",
            chunker_version="fixture-chunker-v1",
            dry_run=True,
        )
        self.assertEqual(planned.status, "would_replace")
        self.assertEqual(self.rag.index_status(self.document_id).state, IndexState.NOT_INDEXED)
        self.assertEqual(self.lifecycle.rebuild_index(dry_run=True), ())


if __name__ == "__main__":
    unittest.main()

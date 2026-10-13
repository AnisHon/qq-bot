from __future__ import annotations

import asyncio
from dataclasses import replace as dataclass_replace
from pathlib import Path
import tempfile
import unittest

from app.config import Settings
from app.handlers.llm_handler import LLMHandler
from app.llm import ChatWithToolsResponse, ToolCallRequest
from app.knowledge.documents import (
    DocumentChunk,
    DocumentFilter,
    DocumentLocator,
    DocumentSource,
    IndexState,
    LocatorKind,
)
from app.knowledge.index_jobs import DocumentIndexLifecycle
from app.knowledge.ingestion import (
    MARKDOWN_CHUNKER_VERSION,
    chunk_markdown_document,
    parse_markdown_document,
)
from app.knowledge.retrieval import DocumentKnowledgeService, reciprocal_rank_fusion
from app.memory import MemoryStore
from app.rag import RagChunk, RagStore
from app.storage_executor import StorageExecutor
from app.tools.agent import TOOL_REGISTRY, run_agent_loop
from app.tools.contracts import Evidence, PolicySnapshot, ToolContext, ToolError, ToolResult, ToolStatus
from app.vector_rag import VectorRagStore


class _FakeEmbeddings(list):
    def tolist(self):
        return list(self)


class _FakeEmbedder:
    def encode(self, texts, show_progress_bar=False):
        return _FakeEmbeddings([[float(len(text))] for text in texts])


def _matches(metadata: dict[str, object], where: dict[str, object] | None) -> bool:
    if not where:
        return True
    if "$and" in where:
        return all(_matches(metadata, condition) for condition in where["$and"])
    for key, expected in where.items():
        if isinstance(expected, dict) and isinstance(expected.get("$in"), list):
            if metadata.get(key) not in expected["$in"]:
                return False
        elif metadata.get(key) != expected:
            return False
    return True


class _FakeCollection:
    def __init__(self):
        self.rows: dict[str, tuple[list[float], str, dict[str, object]]] = {}

    def get(self, include=None, where=None):
        selected = [
            (key, value) for key, value in self.rows.items() if _matches(value[2], where)
        ]
        result = {"ids": [key for key, _ in selected]}
        if include and "metadatas" in include:
            result["metadatas"] = [value[2] for _, value in selected]
        return result

    def count(self):
        return len(self.rows)

    def delete(self, ids):
        for chunk_id in ids:
            self.rows.pop(chunk_id, None)

    def add(self, *, ids, embeddings, documents, metadatas):
        for chunk_id, embedding, document, metadata in zip(ids, embeddings, documents, metadatas):
            self.rows[chunk_id] = (embedding, document, metadata)

    def query(self, *, query_embeddings, n_results, include, where=None):
        query_value = query_embeddings[0][0]
        candidates = [
            (chunk_id, row)
            for chunk_id, row in self.rows.items()
            if _matches(row[2], where)
        ]
        candidates.sort(key=lambda item: (abs(item[1][0][0] - query_value), item[0]))
        selected = candidates[:n_results]
        return {
            "ids": [[chunk_id for chunk_id, _ in selected]],
            "metadatas": [[row[2] for _, row in selected]],
            "distances": [
                [abs(row[0][0] - query_value) / max(query_value, row[0][0], 1.0) for _, row in selected]
            ],
        }


class _FakeChromaClient:
    def __init__(self):
        self.collections: dict[str, _FakeCollection] = {}

    def get_or_create_collection(self, *, name, metadata):
        return self.collections.setdefault(name, _FakeCollection())

    def get_collection(self, *, name):
        if name not in self.collections:
            raise KeyError(name)
        return self.collections[name]

    def delete_collection(self, *, name):
        self.collections.pop(name, None)


class _FailingChromaClient(_FakeChromaClient):
    def get_collection(self, *, name):
        raise RuntimeError("fixture backend failure")


class _KnowledgeAgentLLM:
    async def chat_with_tools(self, messages, tools, **kwargs):
        if not any(message.get("role") == "tool" for message in messages):
            return ChatWithToolsResponse(
                text=None,
                tool_calls=[ToolCallRequest("call-knowledge", "knowledge_search", '{"query":"alpha"}')],
            )
        return ChatWithToolsResponse(text="基于资料的回答")

    async def chat(self, messages, **kwargs):
        return "降级回答"


def _source(version: str) -> DocumentSource:
    return DocumentSource(
        source_id=f"fixture-source-{version}",
        source_uri=f"fixture://knowledge/{version}.md",
        source_type="markdown",
        corpus_id="synthetic-corpus",
        pack_id="fixture-pack",
        pack_version=version,
        game_version="1.7.10-fixture",
        source_revision=f"revision-{version}",
        license_id="fixture-only",
    )


def _chunk(document_id: str, chunk_id: str, content: str, line: int) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        title="Synthetic technical notes",
        content=content,
        section_path=("Synthetic guide", "Power"),
        locator=DocumentLocator(LocatorKind.LINE_RANGE, start_line=line, end_line=line + 2),
        metadata={"fixture": True},
    )


class HybridRetrievalTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage_executor = StorageExecutor(sqlite_workers=1, vector_workers=1, queue_capacity=4)

    async def asyncTearDown(self):
        await self.storage_executor.aclose(timeout=2)

    def make_stores(self, root: Path):
        rag = RagStore(root / "bot.db")
        vector = VectorRagStore(root / "chromadb")
        vector._client = _FakeChromaClient()
        vector._embedder = _FakeEmbedder()
        return rag, vector

    def add_document(self, rag, vector, version: str, content: str):
        source = _source(version)
        document_id = f"guide-{version}"
        chunk = _chunk(document_id, f"chunk-{version}", content, 10)
        rag.replace_document(
            document_id,
            source.source_uri,
            f"hash-{version}",
            [chunk],
            document_source=source,
            parser_version="fixture-parser",
            chunker_version="fixture-chunker",
        )
        vector.upsert_corpus_chunks(source, document_id, [chunk])
        return document_id

    def index_markdown_fixture(self, lifecycle, path: Path, source: DocumentSource, document_id: str):
        parsed = parse_markdown_document(path.read_bytes(), source, document_id=document_id)
        chunks = chunk_markdown_document(parsed, max_chars=1200, overlap_chars=0)
        lifecycle.replace_generation(
            document_id,
            source.source_uri,
            parsed.content_hash,
            chunks,
            document_source=source,
            parser_version=parsed.parser_version,
            chunker_version=MARKDOWN_CHUNKER_VERSION,
        )
        return parsed, chunks

    async def test_two_pack_markdown_fixtures_cover_hybrid_citations_and_exact_version_filter(self):
        """Human-authored synthetic fixtures exercise the complete local retrieval path."""
        fixture_dir = Path(__file__).parent / "fixtures" / "documents"
        alpha_source = DocumentSource(
            source_id="synthetic-alpha-guide",
            source_uri="fixture://synthetic/alpha-guide.md",
            source_type="markdown",
            corpus_id="synthetic-two-pack-corpus",
            pack_id="synthetic-pack-alpha",
            pack_version="fixture-alpha-v1",
            game_version="fixture-game-v1",
            source_revision="alpha-r1",
            license_id="fixture-only",
        )
        beta_source = DocumentSource(
            source_id="synthetic-beta-guide",
            source_uri="fixture://synthetic/beta-guide.md",
            source_type="markdown",
            corpus_id="synthetic-two-pack-corpus",
            pack_id="synthetic-pack-beta",
            pack_version="fixture-beta-v2",
            game_version="fixture-game-v2",
            source_revision="beta-r1",
            license_id="fixture-only",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            rag, vector = self.make_stores(Path(tmpdir))
            lifecycle = DocumentIndexLifecycle(rag, vector)
            alpha_doc = "synthetic-alpha-doc"
            beta_doc = "synthetic-beta-doc"
            alpha_parsed, alpha_chunks = self.index_markdown_fixture(
                lifecycle, fixture_dir / "markdown_fixture.md", alpha_source, alpha_doc
            )
            _beta_parsed, _beta_chunks = self.index_markdown_fixture(
                lifecycle, fixture_dir / "second_pack_fixture.md", beta_source, beta_doc
            )
            service = DocumentKnowledgeService(rag, vector, self.storage_executor)

            alpha_filter = DocumentFilter(
                corpus_id="synthetic-two-pack-corpus",
                pack_id="synthetic-pack-alpha",
                pack_version="fixture-alpha-v1",
                game_version="fixture-game-v1",
                source_revision="alpha-r1",
            )
            beta_filter = DocumentFilter(
                corpus_id="synthetic-two-pack-corpus",
                pack_id="synthetic-pack-beta",
                pack_version="fixture-beta-v2",
                game_version="fixture-game-v2",
                source_revision="beta-r1",
            )
            shared_query = "synthetic EU tick"
            first = await service.search(shared_query, alpha_filter)
            repeated = await service.search(shared_query, alpha_filter)
            beta = await service.search(shared_query, beta_filter)

            self.assertEqual(first.status, ToolStatus.OK)
            self.assertEqual(first.content["ranking"], "rrf")
            self.assertTrue(first.content["results"])
            self.assertEqual({row["document_id"] for row in first.content["results"]}, {alpha_doc})
            self.assertEqual({row["pack_id"] for row in first.content["results"]}, {"synthetic-pack-alpha"})
            self.assertEqual({row["pack_version"] for row in first.content["results"]}, {"fixture-alpha-v1"})
            result = next(row for row in first.content["results"] if "32 EU per tick" in row["content"])
            self.assertIn("Energy", result["section_path"])
            self.assertTrue(result["locator"].startswith("fixture://synthetic/alpha-guide.md#L"))
            self.assertIsNotNone(result["fts_rank"])
            self.assertIsNotNone(result["vector_rank"])
            self.assertEqual(first.content["results"], repeated.content["results"])
            citation = next(item for item in first.evidence if item.locator == result["locator"])
            self.assertEqual(citation.pack_version, "fixture-alpha-v1")
            self.assertEqual(
                alpha_parsed.sections[-1].section_path,
                ("Fixture Pack Knowledge", "Multiblock", "Energy"),
            )
            self.assertTrue(any("32 EU per tick" in chunk.content for chunk in alpha_chunks))
            self.assertEqual(beta.status, ToolStatus.OK)
            self.assertEqual({row["document_id"] for row in beta.content["results"]}, {beta_doc})
            self.assertEqual({row["pack_id"] for row in beta.content["results"]}, {"synthetic-pack-beta"})
            self.assertEqual({row["pack_version"] for row in beta.content["results"]}, {"fixture-beta-v2"})
            beta_result = next(
                row for row in beta.content["results"] if "48 synthetic EU per tick" in row["content"]
            )
            self.assertIn("Power", beta_result["section_path"])
            self.assertIsNotNone(beta_result["fts_rank"])
            self.assertIsNotNone(beta_result["vector_rank"])
            beta_citation = next(item for item in beta.evidence if item.locator == beta_result["locator"])
            self.assertEqual(beta_citation.pack_id, "synthetic-pack-beta")
            self.assertEqual(beta_citation.pack_version, "fixture-beta-v2")

            mismatch = await service.search(
                shared_query,
                DocumentFilter(
                    corpus_id="synthetic-two-pack-corpus",
                    pack_id="synthetic-pack-alpha",
                    pack_version="fixture-alpha-v2",
                ),
            )
            self.assertEqual(mismatch.status, ToolStatus.VERSION_MISMATCH)
            self.assertEqual(mismatch.content["results"], [])
            self.assertNotEqual(alpha_source.pack_id, beta_source.pack_id)

    async def test_markdown_fixture_replacement_and_tombstone_hide_old_chunks(self):
        fixture_dir = Path(__file__).parent / "fixtures" / "documents"
        source = DocumentSource(
            source_id="synthetic-alpha-guide",
            source_uri="fixture://synthetic/alpha-guide.md",
            source_type="markdown",
            corpus_id="synthetic-update-corpus",
            pack_id="synthetic-pack-alpha",
            pack_version="fixture-alpha-v1",
            source_revision="alpha-r1",
            license_id="fixture-only",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            rag, vector = self.make_stores(Path(tmpdir))
            lifecycle = DocumentIndexLifecycle(rag, vector)
            document_id = "synthetic-update-doc"
            self.index_markdown_fixture(
                lifecycle, fixture_dir / "markdown_fixture.md", source, document_id
            )
            service = DocumentKnowledgeService(rag, vector, self.storage_executor)
            old = await service.search("voltage tier", DocumentFilter(document_id=document_id))
            self.assertEqual(old.status, ToolStatus.OK)

            revised_source = dataclass_replace(source, source_revision="alpha-r2")
            revised_text = """# Synthetic Pack Alpha Manual

## Multiblock

### Energy

The revised test cell uses 96 synthetic EU per tick and produces a sample alloy.
The keyword amberrevision appears only in this artificial update fixture.
"""
            revised = parse_markdown_document(
                revised_text, revised_source, document_id=document_id
            )
            revised_chunks = chunk_markdown_document(revised, max_chars=1200, overlap_chars=0)
            lifecycle.replace_generation(
                document_id,
                revised_source.source_uri,
                revised.content_hash,
                revised_chunks,
                document_source=revised_source,
                parser_version=revised.parser_version,
                chunker_version=MARKDOWN_CHUNKER_VERSION,
            )

            stale = await service.search("voltage tier", DocumentFilter(document_id=document_id))
            current = await service.search("amberrevision", DocumentFilter(document_id=document_id))
            self.assertEqual(stale.status, ToolStatus.OK)
            self.assertTrue(stale.content["results"])
            self.assertTrue(all("voltage tier" not in row["content"] for row in stale.content["results"]))
            self.assertTrue(all(row["fts_rank"] is None for row in stale.content["results"]))
            self.assertEqual(current.status, ToolStatus.OK)
            self.assertIn("96 synthetic EU", current.content["text"])
            self.assertTrue(current.evidence[0].locator.startswith(revised_source.source_uri + "#L"))

            lifecycle.delete_document(document_id)
            deleted = await service.search("amberrevision", DocumentFilter(document_id=document_id))
            self.assertEqual(deleted.status, ToolStatus.NOT_INDEXED)

    async def test_empty_document_catalog_reports_data_unavailable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rag, vector = self.make_stores(Path(tmpdir))
            result = await DocumentKnowledgeService(rag, vector, self.storage_executor).search(
                "any fixture term", DocumentFilter(corpus_id="empty-synthetic-corpus")
            )
            self.assertEqual(result.status, ToolStatus.DATA_UNAVAILABLE)

    async def test_exact_version_filter_is_shared_by_fts_and_chroma_and_returns_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rag, vector = self.make_stores(Path(tmpdir))
            first_id = self.add_document(rag, vector, "1.0", "alpha transformer requires 32 EU per tick")
            self.add_document(rag, vector, "2.0", "alpha transformer requires 128 EU per tick")
            service = DocumentKnowledgeService(rag, vector, self.storage_executor)

            result = await service.search(
                "alpha transformer",
                DocumentFilter(
                    corpus_id="synthetic-corpus",
                    pack_id="fixture-pack",
                    pack_version="1.0",
                    game_version="1.7.10-fixture",
                ),
            )

            self.assertEqual(result.status, ToolStatus.OK)
            self.assertEqual(len(result.content["results"]), 1)
            self.assertEqual(result.content["results"][0]["document_id"], first_id)
            self.assertIn("32 EU", result.content["text"])
            self.assertNotIn("128 EU", result.content["text"])
            self.assertEqual(len(result.evidence), 1)
            self.assertEqual(result.evidence[0].kind, "document")
            self.assertEqual(result.evidence[0].pack_version, "1.0")
            self.assertEqual(result.evidence[0].locator, "fixture://knowledge/1.0.md#L10-L12")

    async def test_missing_index_version_and_unknown_corpus_have_distinct_statuses(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rag, vector = self.make_stores(Path(tmpdir))
            self.add_document(rag, vector, "1.0", "alpha transformer fixture")
            service = DocumentKnowledgeService(rag, vector, self.storage_executor)

            mismatch = await service.search(
                "alpha",
                DocumentFilter(corpus_id="synthetic-corpus", pack_id="fixture-pack", pack_version="9.9"),
            )
            unavailable = await service.search(
                "alpha",
                DocumentFilter(corpus_id="not-imported-corpus"),
            )
            not_indexed = await service.search("alpha", DocumentFilter(document_id="not-yet-indexed"))

            self.assertEqual(mismatch.status, ToolStatus.VERSION_MISMATCH)
            self.assertEqual(mismatch.content["results"], [])
            self.assertEqual(unavailable.status, ToolStatus.DATA_UNAVAILABLE)
            self.assertEqual(not_indexed.status, ToolStatus.NOT_INDEXED)

    async def test_empty_document_and_empty_index_are_not_reported_as_no_results(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rag, vector = self.make_stores(Path(tmpdir))
            source = _source("1.0")
            rag.replace_document("empty-doc", source.source_uri, "empty-hash", [], document_source=source)
            status = rag.index_status("empty-doc")
            result = await DocumentKnowledgeService(rag, vector, self.storage_executor).search(
                "alpha", DocumentFilter(document_id="empty-doc")
            )
            self.assertEqual(status.state, IndexState.EMPTY)
            self.assertEqual(result.status, ToolStatus.NOT_INDEXED)

    async def test_fts_only_results_are_marked_partial_and_still_cite_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rag = RagStore(Path(tmpdir) / "bot.db")
            source = _source("1.0")
            chunk = _chunk("fts-only-doc", "fts-only-chunk", "alpha FTS-only fixture evidence", 21)
            rag.replace_document(
                "fts-only-doc", source.source_uri, "fixture-hash", [chunk], document_source=source
            )

            result = await DocumentKnowledgeService(rag, None, self.storage_executor).search(
                "alpha", DocumentFilter(document_id="fts-only-doc")
            )

            self.assertEqual(result.status, ToolStatus.PARTIAL)
            self.assertEqual(len(result.content["results"]), 1)
            self.assertIn("向量索引", result.content["text"])
            self.assertEqual(result.evidence[0].locator, "fixture://knowledge/1.0.md#L21-L23")

    async def test_partial_vector_coverage_is_reported_for_mixed_document_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rag, vector = self.make_stores(Path(tmpdir))
            indexed_id = self.add_document(rag, vector, "1.0", "alpha transformer indexed fixture")
            missing_source = _source("2.0")
            missing_id = "guide-2.0"
            rag.replace_document(
                missing_id,
                missing_source.source_uri,
                "hash-2.0",
                [_chunk(missing_id, "chunk-2.0", "alpha transformer without vectors", 30)],
                document_source=missing_source,
            )

            result = await DocumentKnowledgeService(rag, vector, self.storage_executor).search(
                "alpha transformer", DocumentFilter(corpus_id="synthetic-corpus")
            )

            self.assertEqual(result.status, ToolStatus.PARTIAL)
            self.assertIn(indexed_id, [row["document_id"] for row in result.content["results"]])
            self.assertIn(missing_id, [row["document_id"] for row in result.content["results"]])
            self.assertIn("1 个目标文档尚无向量索引", result.content["text"])

    async def test_stale_version_vectors_do_not_count_as_exact_version_coverage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rag, vector = self.make_stores(Path(tmpdir))
            source = _source("2.0")
            document_id = "guide-current"
            rag.replace_document(
                document_id,
                source.source_uri,
                "current-hash",
                [_chunk(document_id, "current-chunk", "alpha current version evidence", 40)],
                document_source=source,
            )
            stale_collection = vector._corpus_collection(source.corpus_id, create=True)
            stale_collection.add(
                ids=["stale-version-chunk"],
                embeddings=[[3.0]],
                documents=["Synthetic technical notes alpha stale version"],
                metadatas=[
                    {
                        "document_id": document_id,
                        "source_id": source.source_id,
                        "source_uri": "fixture://knowledge/1.0.md",
                        "source_type": source.source_type,
                        "corpus_id": source.corpus_id,
                        "pack_id": source.pack_id,
                        "pack_version": "1.0",
                        "game_version": source.game_version,
                        "source_revision": "revision-1.0",
                        "title": "Synthetic technical notes",
                        "content": "alpha stale version evidence",
                    }
                ],
            )

            result = await DocumentKnowledgeService(rag, vector, self.storage_executor).search(
                "alpha",
                DocumentFilter(
                    corpus_id=source.corpus_id,
                    pack_id=source.pack_id,
                    pack_version="2.0",
                    game_version=source.game_version,
                    source_revision=source.source_revision,
                ),
            )

            self.assertEqual(result.status, ToolStatus.PARTIAL)
            self.assertEqual(result.content["results"][0]["document_id"], document_id)
            self.assertIn("current version evidence", result.content["text"])
            self.assertNotIn("stale version evidence", result.content["text"])

    async def test_legacy_vector_without_source_uri_does_not_create_fake_citation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rag, vector = self.make_stores(Path(tmpdir))
            rag.replace_document(
                "old-sfm",
                "old-sfm.pdf",
                "legacy-hash",
                [("legacy-sql-chunk", 1, "Legacy PDF", "unrelated legacy text")],
            )
            collection = vector._collection("old-sfm", create=True)
            collection.add(
                ids=["legacy-vector-chunk"],
                embeddings=[[3.0]],
                documents=["Legacy PDF alpha vector-only text"],
                metadatas=[
                    {
                        "document_id": "old-sfm",
                        "page": 1,
                        "title": "Legacy PDF",
                        "content": "alpha vector-only text",
                    }
                ],
            )

            result = await DocumentKnowledgeService(rag, vector, self.storage_executor).search(
                "alpha", DocumentFilter(document_id="old-sfm")
            )

            self.assertEqual(result.status, ToolStatus.OK)
            self.assertEqual(result.content["results"][0]["locator"], None)
            self.assertIsNone(result.content["results"][0]["source_uri"])
            self.assertEqual(result.evidence, ())

    async def test_chroma_backend_failure_is_partial_not_misreported_as_missing_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rag = RagStore(Path(tmpdir) / "bot.db")
            source = _source("1.0")
            chunk = _chunk("fts-only-doc", "fts-only-chunk", "alpha FTS evidence", 21)
            rag.replace_document(
                "fts-only-doc", source.source_uri, "fixture-hash", [chunk], document_source=source
            )
            vector = VectorRagStore(Path(tmpdir) / "chromadb")
            vector._client = _FailingChromaClient()
            vector._embedder = _FakeEmbedder()

            result = await DocumentKnowledgeService(rag, vector, self.storage_executor).search(
                "alpha", DocumentFilter(document_id="fts-only-doc")
            )

            self.assertEqual(result.status, ToolStatus.PARTIAL)
            self.assertIn("向量检索暂不可用", result.content["text"])
            self.assertNotIn("尚无向量索引", result.content["text"])

    async def test_agent_registry_preserves_service_toolresult_and_document_evidence(self):
        expected = ToolResult(
            status=ToolStatus.OK,
            content={"text": "fixture result", "results": []},
            evidence=(Evidence("document", "fixture-source", "Fixture", "fixture://doc.md#L1-L2"),),
        )
        context = ToolContext(
            request_id="hybrid-test",
            policy_snapshot=PolicySnapshot(roles=frozenset({"group_member"})),
            extensions={"local_knowledge_search": lambda query: _return_result(expected)},
        )
        result = await TOOL_REGISTRY.dispatch("knowledge_search", {"query": "fixture"}, context)
        self.assertEqual(result, expected)

    async def test_agent_observation_keeps_partial_fts_evidence_and_citation(self):
        partial = ToolResult(
            status=ToolStatus.PARTIAL,
            content={"text": "向量检索暂不可用。\n1. Power Guide（fixture://doc.md#L2-L3）\n   alpha evidence"},
            evidence=(Evidence("document", "fixture-doc", "Power Guide", "fixture://doc.md#L2-L3"),),
            error=ToolError("partial_document_retrieval", "向量检索暂不可用。", retryable=True),
        )
        messages = [{"role": "user", "content": "alpha"}]
        answer = await run_agent_loop(
            _KnowledgeAgentLLM(),
            messages,
            max_rounds=2,
            max_calls_per_round=1,
            max_total_calls=1,
            total_timeout=5,
            tool_timeout=1,
            results_max_chars=2000,
            local_knowledge_search=lambda query: _return_result(partial),
        )

        tool_message = next(message for message in messages if message["role"] == "tool")
        self.assertIn("[partial] 向量检索暂不可用。", tool_message["content"])
        self.assertIn("alpha evidence", tool_message["content"])
        self.assertIn("fixture://doc.md#L2-L3", answer)

    async def test_llm_handler_uses_document_service_for_existing_sfm_entry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            rag = RagStore(root / "bot.db")
            source = DocumentSource(
                source_id="sfm-source",
                source_uri="fixture://sfm/reference.md",
                source_type="markdown",
                corpus_id="fixture-sfm",
            )
            chunk = _chunk("sfm", "sfm-chunk", "SFM alpha input sends items to machine", 4)
            rag.replace_document("sfm", source.source_uri, "sfm-hash", [chunk], document_source=source)
            settings = Settings(sqlite_path=root / "bot.db", rag_document_name="sfm")
            handler = LLMHandler(
                settings,
                MemoryStore(settings.sqlite_path),
                llm=type("FakeLLM", (), {"api_key": "fixture"})(),
                rag=rag,
                vector_rag=None,
                llms={},
                storage_executor=self.storage_executor,
            )

            result = await handler._search_local_knowledge("SFM alpha input")

            self.assertEqual(result.status, ToolStatus.PARTIAL)
            self.assertIn("SFM alpha", result.content["text"])
            self.assertEqual(result.evidence[0].kind, "document")

    def test_rrf_combines_rank_positions_not_raw_scores(self):
        fts = [
            RagChunk("a", "fixture", 0, "A", "alpha", 99.0, document_id="doc-a"),
            RagChunk("b", "fixture", 0, "B", "beta", 0.1, document_id="doc-b"),
        ]
        vector = [
            RagChunk("b", "fixture", 0, "B", "beta", 0.01, document_id="doc-b"),
            RagChunk("c", "fixture", 0, "C", "gamma", 0.99, document_id="doc-c"),
        ]

        fused = reciprocal_rank_fusion(fts, vector, limit=3, k=60)

        self.assertEqual([item.chunk.chunk_id for item in fused], ["b", "a", "c"])
        self.assertEqual(fused[0].fts_rank, 2)
        self.assertEqual(fused[0].vector_rank, 1)


async def _return_result(result: ToolResult) -> ToolResult:
    return result


if __name__ == "__main__":
    unittest.main()

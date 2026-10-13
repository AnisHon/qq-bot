from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from app.knowledge.documents import (
    DocumentFilter,
    DocumentIndexStatus,
    DocumentLocator,
    IndexState,
    LocatorKind,
)
from app.rag import RagChunk, RagStore
from app.storage_executor import StorageExecutor
from app.tools.contracts import Evidence, ToolError, ToolResult, ToolStatus
from app.vector_rag import VectorRagStore

if TYPE_CHECKING:
    from app.knowledge.documents import DocumentSource


LOGGER = logging.getLogger(__name__)
RRF_K = 60
MAX_CORPUS_COLLECTIONS_PER_QUERY = 16
MAX_LEGACY_COLLECTIONS_PER_QUERY = 16
MAX_DOCUMENTS_PER_COVERAGE_CHECK = 128


@dataclass(frozen=True, slots=True)
class RankedDocumentChunk:
    chunk: RagChunk
    score: float
    fts_rank: int | None
    vector_rank: int | None


@dataclass(frozen=True, slots=True)
class _VectorSearchOutcome:
    chunks: tuple[RagChunk, ...]
    available: bool
    truncated: bool = False
    missing_document_count: int = 0
    unavailable_reason: str | None = None


class DocumentKnowledgeService:
    """Version-aware FTS / Chroma retrieval with stable source evidence."""

    def __init__(
        self,
        rag_store: RagStore | None,
        vector_store: VectorRagStore | None,
        storage_executor: StorageExecutor | None = None,
    ) -> None:
        self.rag_store = rag_store
        self.vector_store = vector_store
        self.storage_executor = storage_executor

    async def catalog(self) -> list[DocumentIndexStatus] | None:
        """Return the current local source catalog, or None when no FTS store is configured."""
        if self.rag_store is None:
            return None
        return await self._run_sqlite(self.rag_store.document_catalog)

    async def search(
        self,
        query: str,
        filters: DocumentFilter | None = None,
        *,
        limit: int = 8,
        evidence_limit: int = 8,
    ) -> ToolResult:
        query = query.strip()
        if not query or limit <= 0 or evidence_limit < 0:
            return ToolResult(
                status=ToolStatus.INVALID_ARGUMENT,
                content={"text": "文档检索参数无效。", "results": []},
                error=ToolError("invalid_document_query", "文档检索参数无效。"),
            )
        if self.rag_store is None:
            return _status_result(
                ToolStatus.DATA_UNAVAILABLE,
                "当前没有配置本地文档索引。",
                "document_index_unavailable",
            )

        try:
            catalog = await self._run_sqlite(self.rag_store.document_catalog)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("document catalog lookup failed (%s)", type(exc).__name__)
            return _status_result(ToolStatus.ERROR, "读取文档索引状态失败。", "document_catalog_error")

        target_documents, availability = _select_documents(catalog, filters)
        if availability is not None:
            return availability

        assert target_documents
        indexed_documents = [item for item in target_documents if item.state == IndexState.INDEXED]
        if not indexed_documents:
            return _status_result(
                ToolStatus.NOT_INDEXED,
                "目标来源存在，但尚无可检索的文档片段。",
                "document_not_indexed",
            )

        fts_task = asyncio.create_task(
            self._run_sqlite(
                self.rag_store.search,
                query,
                filters.document_id if filters else None,
                limit * 2,
                filters=filters,
            )
        )
        generation_documents = [item for item in indexed_documents if item.generation is not None]
        expected_ids_task = asyncio.create_task(
            self._run_sqlite(_generation_id_snapshot, self.rag_store, generation_documents)
        )
        try:
            fts_value, expected_ids_value = await asyncio.gather(
                fts_task, expected_ids_task, return_exceptions=True
            )
        except BaseException:
            fts_task.cancel()
            expected_ids_task.cancel()
            await asyncio.gather(fts_task, expected_ids_task, return_exceptions=True)
            raise
        if isinstance(expected_ids_value, BaseException):
            vector_value = expected_ids_value
        else:
            try:
                expected_generation_ids = expected_ids_value
                vector_value = await self._run_vector_search(
                    query, indexed_documents, filters, limit * 2, expected_generation_ids
                )
            except Exception as exc:  # noqa: BLE001
                vector_value = exc

        fts_error = fts_value if isinstance(fts_value, BaseException) else None
        vector_error = vector_value if isinstance(vector_value, BaseException) else None
        fts_chunks = [] if fts_error is not None else list(fts_value)
        vector_outcome = (
            _VectorSearchOutcome((), available=False)
            if vector_error is not None
            else vector_value
        )
        assert isinstance(vector_outcome, _VectorSearchOutcome)

        if fts_error is not None:
            LOGGER.warning("document FTS search failed (%s)", type(fts_error).__name__)
        if vector_error is not None:
            LOGGER.warning("document vector search failed (%s)", type(vector_error).__name__)
        if fts_error is not None and vector_error is not None:
            return _status_result(ToolStatus.ERROR, "文档检索执行失败。", "document_retrieval_error")

        ranked = reciprocal_rank_fusion(fts_chunks, list(vector_outcome.chunks), limit=limit)
        partial_reasons: list[str] = []
        if fts_error is not None:
            partial_reasons.append("FTS5 暂不可用")
        if vector_error is not None:
            partial_reasons.append("向量检索暂不可用")
        else:
            if vector_outcome.unavailable_reason:
                partial_reasons.append(vector_outcome.unavailable_reason)
            if vector_outcome.missing_document_count:
                partial_reasons.append(
                    f"{vector_outcome.missing_document_count} 个目标文档尚无向量索引"
                )
            elif not vector_outcome.available:
                partial_reasons.append("目标文档尚无向量索引")
        if vector_outcome.truncated:
            partial_reasons.append("向量检索来源数量达到本次上限")

        status = ToolStatus.PARTIAL if partial_reasons else (ToolStatus.NO_RESULTS if not ranked else ToolStatus.OK)
        results: list[dict[str, object]] = []
        evidence: list[Evidence] = []
        text_lines: list[str] = []
        fts_ranks = {chunk.chunk_id: rank for rank, chunk in enumerate(fts_chunks, start=1)}
        vector_ranks = {chunk.chunk_id: rank for rank, chunk in enumerate(vector_outcome.chunks, start=1)}
        for rank, item in enumerate(ranked, start=1):
            chunk = item.chunk
            locator = _evidence_locator(chunk)
            section = " / ".join(chunk.section_path)
            results.append(
                {
                    "rank": rank,
                    "document_id": chunk.document_id,
                    "chunk_id": chunk.chunk_id,
                    "title": chunk.title,
                    "section_path": list(chunk.section_path),
                    "content": chunk.content,
                    "locator": locator,
                    "source_uri": chunk.source_uri or (chunk.source if chunk.source != "vector" else None),
                    "source_id": chunk.source_id,
                    "pack_id": chunk.pack_id,
                    "pack_version": chunk.pack_version,
                    "game_version": chunk.game_version,
                    "source_revision": chunk.source_revision,
                    "rrf_score": round(item.score, 8),
                    "fts_rank": item.fts_rank,
                    "vector_rank": item.vector_rank,
                }
            )
            title = f"{section} / {chunk.title}" if section and section != chunk.title else chunk.title
            location_note = f"（{locator}）" if locator else ""
            text_lines.append(f"{rank}. {title}{location_note}\n   {chunk.content}")
            if locator and len(evidence) < evidence_limit:
                evidence.append(
                    Evidence(
                        kind="document",
                        source_id=chunk.source_id or chunk.document_id or chunk.source,
                        title=chunk.title,
                        locator=locator,
                        revision=chunk.source_revision or chunk.content_hash,
                        pack_id=chunk.pack_id,
                        pack_version=chunk.pack_version,
                        confidence=f"rrf:{item.score:.8f}",
                    )
                )

        if partial_reasons:
            text_lines.insert(0, "部分检索后端不可用：" + "；".join(partial_reasons) + "。")
        if not text_lines:
            text_lines.append("当前索引中没有匹配的文档片段。")
        error = None
        if partial_reasons:
            error = ToolError("partial_document_retrieval", "；".join(partial_reasons), retryable=True)
        return ToolResult(
            status=status,
            content={"text": "\n".join(text_lines), "results": results, "ranking": "rrf"},
            evidence=tuple(evidence),
            error=error,
        )

    async def _run_sqlite(self, function, /, *args, **kwargs):
        if self.storage_executor is not None:
            return await self.storage_executor.run_sqlite(function, *args, **kwargs)
        return await asyncio.to_thread(function, *args, **kwargs)

    async def _run_vector_search(
        self,
        query: str,
        documents: list[DocumentIndexStatus],
        filters: DocumentFilter | None,
        limit: int,
        expected_generation_ids: dict[str, frozenset[str]],
    ) -> _VectorSearchOutcome:
        if self.vector_store is None:
            return _VectorSearchOutcome(
                (), available=False, unavailable_reason="未配置向量检索后端"
            )
        if self.storage_executor is not None:
            return await self.storage_executor.run_vector(
                self._vector_search_sync,
                query,
                documents,
                filters,
                limit,
                expected_generation_ids,
            )
        return await asyncio.to_thread(
            self._vector_search_sync,
            query,
            documents,
            filters,
            limit,
            expected_generation_ids,
        )

    def _vector_search_sync(
        self,
        query: str,
        documents: list[DocumentIndexStatus],
        filters: DocumentFilter | None,
        limit: int,
        expected_generation_ids: dict[str, frozenset[str]] | None = None,
    ) -> _VectorSearchOutcome:
        assert self.vector_store is not None
        legacy_documents = [
            item
            for item in documents
            if item.generation is None
            and (item.source is None or item.source.source_type == "legacy")
        ]
        generation_documents = [
            item for item in documents
            if item.generation is not None
        ]
        generic_by_corpus: dict[str, list[DocumentIndexStatus]] = {}
        for item in documents:
            if (
                item.source is not None
                and item.source.source_type != "legacy"
                and item.generation is None
            ):
                generic_by_corpus.setdefault(item.source.corpus_id, []).append(item)

        truncated = False
        chunks: list[RagChunk] = []
        missing_document_count = 0
        legacy_documents = sorted(legacy_documents, key=lambda item: item.document_id)
        if len(legacy_documents) > MAX_LEGACY_COLLECTIONS_PER_QUERY:
            legacy_documents = legacy_documents[:MAX_LEGACY_COLLECTIONS_PER_QUERY]
            truncated = True
        available = False
        generation_documents = sorted(generation_documents, key=lambda item: item.document_id)
        if len(generation_documents) > MAX_LEGACY_COLLECTIONS_PER_QUERY:
            generation_documents = generation_documents[:MAX_LEGACY_COLLECTIONS_PER_QUERY]
            truncated = True
        for item in generation_documents:
            assert item.source is not None and item.generation is not None
            actual_ids = self.vector_store.generation_chunk_ids(
                item.document_id,
                item.generation,
                collection_revision=item.vector_revision or item.generation,
                raise_on_error=True,
            )
            expected_ids = (
                expected_generation_ids.get(item.document_id)
                if expected_generation_ids is not None
                else self.rag_store.generation_chunk_ids(item.document_id, item.generation)
                if self.rag_store is not None
                else None
            )
            if actual_ids is None or expected_ids is None or actual_ids != expected_ids:
                missing_document_count += 1
                continue
            available = available or bool(actual_ids)
            chunks.extend(
                self.vector_store.search_generation(
                    query,
                    item.document_id,
                    item.generation,
                    limit=limit,
                    filters=filters,
                    collection_revision=item.vector_revision or item.generation,
                    raise_on_error=True,
                )
            )

        for item in legacy_documents:
            if not self.vector_store.has_document(item.document_id, raise_on_error=True):
                missing_document_count += 1
                continue
            available = True
            chunks.extend(
                self.vector_store.search(
                    query,
                    document_name=item.document_id,
                    limit=limit,
                    filters=filters,
                    raise_on_error=True,
                )
            )

        corpus_ids = sorted(generic_by_corpus)
        if len(corpus_ids) > MAX_CORPUS_COLLECTIONS_PER_QUERY:
            corpus_ids = corpus_ids[:MAX_CORPUS_COLLECTIONS_PER_QUERY]
            truncated = True
        for corpus_id in corpus_ids:
            corpus_documents = sorted(generic_by_corpus[corpus_id], key=lambda item: item.document_id)
            coverage_documents = corpus_documents[:MAX_DOCUMENTS_PER_COVERAGE_CHECK]
            if len(corpus_documents) > MAX_DOCUMENTS_PER_COVERAGE_CHECK:
                truncated = True
            indexed_documents = self.vector_store.indexed_corpus_documents(
                corpus_id,
                [item.document_id for item in coverage_documents],
                filters=filters,
                raise_on_error=True,
            )
            missing_document_count += sum(
                item.document_id not in indexed_documents for item in coverage_documents
            )
            if not indexed_documents:
                continue
            available = True
            chunks.extend(
                self.vector_store.search_corpus(
                    query,
                    corpus_id,
                    limit=limit,
                    filters=filters,
                    document_ids=[item.document_id for item in coverage_documents],
                    raise_on_error=True,
                )
            )
        unique: dict[str, RagChunk] = {}
        for chunk in chunks:
            unique.setdefault(chunk.chunk_id, chunk)
        return _VectorSearchOutcome(
            tuple(sorted(unique.values(), key=lambda chunk: (-chunk.score, chunk.chunk_id))[:limit]),
            available,
            truncated,
            missing_document_count,
        )


def reciprocal_rank_fusion(
    fts_chunks: list[RagChunk],
    vector_chunks: list[RagChunk],
    *,
    limit: int = 8,
    k: int = RRF_K,
) -> list[RankedDocumentChunk]:
    """Combine rank positions, without mixing BM25 and cosine score scales."""
    if limit <= 0 or k < 1:
        return []
    scores: dict[str, float] = {}
    chunks: dict[str, RagChunk] = {}
    fts_ranks: dict[str, int] = {}
    vector_ranks: dict[str, int] = {}
    for rank, chunk in enumerate(fts_chunks, start=1):
        chunks.setdefault(chunk.chunk_id, chunk)
        fts_ranks.setdefault(chunk.chunk_id, rank)
        scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (k + rank)
    for rank, chunk in enumerate(vector_chunks, start=1):
        chunks.setdefault(chunk.chunk_id, chunk)
        vector_ranks.setdefault(chunk.chunk_id, rank)
        scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (k + rank)
    ordered = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))[:limit]
    return [
        RankedDocumentChunk(
            chunk=chunks[chunk_id],
            score=scores[chunk_id],
            fts_rank=fts_ranks.get(chunk_id),
            vector_rank=vector_ranks.get(chunk_id),
        )
        for chunk_id in ordered
    ]


def _select_documents(
    catalog: list[DocumentIndexStatus],
    filters: DocumentFilter | None,
) -> tuple[list[DocumentIndexStatus], ToolResult | None]:
    filters = filters or DocumentFilter()
    all_base = [item for item in catalog if _matches_filter(item, filters, include_version=False)]
    if not all_base:
        if filters.document_id is not None:
            return [], _status_result(
                ToolStatus.NOT_INDEXED,
                "指定文档尚未建立索引。",
                "document_not_indexed",
            )
        return [], _status_result(
            ToolStatus.DATA_UNAVAILABLE,
            "指定文档来源或语料尚未配置。",
            "document_source_unavailable",
        )

    exact = [item for item in all_base if _matches_filter(item, filters, include_version=True)]
    if not exact and filters.version_constrained:
        known = any(
            item.source is not None
            and any(
                requested is not None
                and available is not None
                and requested != available
                for requested, available in (
                    (filters.pack_version, item.source.pack_version),
                    (filters.game_version, item.source.game_version),
                    (filters.source_revision, item.source.source_revision),
                )
            )
            for item in all_base
        )
        if known:
            return [], _status_result(
                ToolStatus.VERSION_MISMATCH,
                "本地资料存在，但没有与请求版本完全匹配的索引；未使用其他版本替代。",
                "document_version_mismatch",
            )
        return [], _status_result(
            ToolStatus.NOT_INDEXED,
            "目标来源存在，但请求的版本元数据尚未建立索引。",
            "document_version_not_indexed",
        )
    if not any(item.state == IndexState.INDEXED for item in exact):
        return [], _status_result(
            ToolStatus.NOT_INDEXED,
            "目标来源存在，但尚无可检索的文档片段。",
            "document_not_indexed",
        )
    return exact, None


def _generation_id_snapshot(
    rag_store: RagStore, documents: list[DocumentIndexStatus]
) -> dict[str, frozenset[str]]:
    return {
        item.document_id: rag_store.generation_chunk_ids(item.document_id, item.generation)
        for item in documents
        if item.generation is not None
    }


def _matches_filter(
    item: DocumentIndexStatus,
    filters: DocumentFilter,
    *,
    include_version: bool,
) -> bool:
    if filters.document_id is not None and item.document_id != filters.document_id:
        return False
    source = item.source
    for key in ("source_id", "source_type", "corpus_id", "pack_id"):
        wanted = getattr(filters, key)
        if wanted is not None and (source is None or getattr(source, key) != wanted):
            return False
    if include_version:
        for key in ("pack_version", "game_version", "source_revision"):
            wanted = getattr(filters, key)
            if wanted is not None and (source is None or getattr(source, key) != wanted):
                return False
    return True


def _evidence_locator(chunk: RagChunk) -> str | None:
    if chunk.source_uri:
        base = chunk.source_uri
    elif chunk.source and chunk.source != "vector":
        base = chunk.source
    else:
        return None
    if not base:
        return None
    base = base.split("#", 1)[0]
    locator = chunk.locator
    if locator is None and chunk.page > 0:
        locator = DocumentLocator(kind=LocatorKind.PAGE, page=chunk.page)
    if locator is None:
        return base
    if locator.kind == LocatorKind.LINE_RANGE:
        fragment = locator.display()
    elif locator.kind == LocatorKind.PAGE:
        fragment = f"page={locator.page}"
    else:
        fragment = str(locator.anchor)
    return f"{base}#{fragment}"


def _status_result(status: ToolStatus, message: str, code: str) -> ToolResult:
    return ToolResult(
        status=status,
        content={"text": message, "results": []},
        error=ToolError(code, message),
    )

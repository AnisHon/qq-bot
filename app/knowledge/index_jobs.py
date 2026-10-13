from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import logging
from threading import RLock
import uuid
from typing import Mapping

from app.knowledge.documents import DocumentChunk, DocumentIndexStatus, DocumentSource
from app.rag import RagStore
from app.vector_rag import VectorRagStore


LOGGER = logging.getLogger(__name__)


class IndexLifecycleError(RuntimeError):
    """An index generation could not be safely activated or cleaned up."""


@dataclass(frozen=True, slots=True)
class GenerationOperationResult:
    document_id: str
    generation_id: str | None
    status: str
    chunk_count: int = 0
    previous_generation: str | None = None
    pending_cleanup: tuple[str, ...] = ()
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class IndexLifecycleStatus:
    document: DocumentIndexStatus
    active_generation: str | None
    tombstoned: bool
    pending_cleanup: tuple[str, ...]
    pending_vector_cleanup: tuple[str, ...]
    operations: tuple[Mapping[str, object], ...]


class DocumentIndexLifecycle:
    """Coordinate SQLite/FTS and Chroma generation lifecycle operations.

    The SQLite pointer is authoritative for queries. New content is staged in
    query-invisible SQLite tables and an isolated Chroma collection, verified,
    then activated in one SQLite transaction. Cross-store operations are not
    described as atomic: cleanup is explicit and idempotently recoverable.
    """

    def __init__(self, rag_store: RagStore, vector_store: VectorRagStore) -> None:
        self.rag_store = rag_store
        self.vector_store = vector_store
        # Serializes jobs using this lifecycle object. SQLite compare-and-swap
        # still protects active-pointer changes made by another process.
        self._lock = RLock()

    def replace_generation(
        self,
        document_id: str,
        source_path: str,
        content_hash: str,
        chunks: list[DocumentChunk],
        *,
        document_source: DocumentSource,
        parser_version: str,
        chunker_version: str,
        metadata: Mapping[str, object] | None = None,
        dry_run: bool = False,
    ) -> GenerationOperationResult:
        generation_id = _generation_id(
            document_id,
            content_hash,
            chunks,
            document_source,
            parser_version,
            chunker_version,
            metadata,
        )
        if dry_run:
            previous = self.rag_store.generation_head(document_id)
            return GenerationOperationResult(
                document_id=document_id,
                generation_id=generation_id,
                status="would_replace",
                chunk_count=len(chunks),
                previous_generation=previous[0] if previous and not previous[1] else None,
                dry_run=True,
            )

        with self._lock:
            operation_id = self.rag_store.stage_generation(
                document_id,
                generation_id,
                source_path,
                content_hash,
                chunks,
                document_source=document_source,
                parser_version=parser_version,
                chunker_version=chunker_version,
                metadata=metadata,
            )
            if operation_id is None:
                return GenerationOperationResult(
                    document_id=document_id,
                    generation_id=generation_id,
                    status="already_active",
                    chunk_count=len(chunks),
                )
            try:
                self._build_and_activate(operation_id, document_id, generation_id)
            except Exception as exc:
                self.rag_store.mark_generation_failed(operation_id, type(exc).__name__)
                LOGGER.warning("document generation replacement failed (%s)", type(exc).__name__)
                raise
            return GenerationOperationResult(
                document_id=document_id,
                generation_id=generation_id,
                status="active",
                chunk_count=len(chunks),
                previous_generation=self._previous_generation(operation_id),
                pending_cleanup=tuple(
                    generation for doc, generation in self.rag_store.pending_generation_cleanup()
                    if doc == document_id
                ),
            )

    def delete_document(
        self, document_id: str, *, dry_run: bool = False
    ) -> GenerationOperationResult:
        """Tombstone a managed document; cleanup remains explicitly retryable."""
        head = self.rag_store.generation_head(document_id)
        if head is None:
            raise ValueError("document is not managed by the generation index")
        generations = tuple(
            generation for doc, generation in self.rag_store.pending_generation_cleanup()
            if doc == document_id
        )
        if not generations and head[0] is not None:
            generations = (head[0],)
        if dry_run:
            return GenerationOperationResult(
                document_id=document_id,
                generation_id=head[0],
                status="would_tombstone",
                previous_generation=head[0],
                pending_cleanup=generations,
                dry_run=True,
            )
        with self._lock:
            operation_id = f"delete:{document_id}:{uuid.uuid4().hex}"
            all_generations = self.rag_store.tombstone_generation_document(document_id, operation_id)
            return GenerationOperationResult(
                document_id=document_id,
                generation_id=None,
                status="tombstoned_pending_cleanup",
                previous_generation=head[0],
                pending_cleanup=tuple(all_generations),
            )

    def rebuild_index(
        self, document_id: str | None = None, *, dry_run: bool = False
    ) -> tuple[GenerationOperationResult, ...]:
        """Recreate/verify vectors for active SQLite generations without switching them."""
        active = self.rag_store.active_generation_ids(document_id)
        if dry_run:
            return tuple(
                GenerationOperationResult(
                    document_id=doc,
                    generation_id=generation,
                    status="would_rebuild",
                    dry_run=True,
                )
                for doc, generation in active
            )
        results: list[GenerationOperationResult] = []
        with self._lock:
            for doc, generation in active:
                snapshot = self.rag_store.generation_snapshot(doc, generation)
                if snapshot is None:
                    raise IndexLifecycleError("active SQLite generation is missing")
                source, _content_hash, _source_path, _parser, _chunker, chunks = snapshot
                expected = self.rag_store.generation_chunk_ids(doc, generation)
                if not self.rag_store.validate_generation(doc, generation, expected):
                    raise IndexLifecycleError("active SQLite/FTS chunk IDs do not match")
                active_revision = self.rag_store.generation_vector_revision(doc, generation) or generation
                actual = self.vector_store.generation_chunk_ids(
                    doc, generation, collection_revision=active_revision, raise_on_error=True
                )
                if actual != expected:
                    revision, operation_id, _expected_revision = self.rag_store.begin_vector_rebuild(
                        doc, generation
                    )
                    try:
                        actual = self.vector_store.upsert_generation_chunks(
                            source, doc, generation, chunks, collection_revision=revision
                        )
                        if actual != expected:
                            raise IndexLifecycleError("shadow Chroma IDs do not match active SQLite generation")
                        self.rag_store.mark_vector_rebuild_ready(operation_id)
                        self.rag_store.activate_vector_revision(operation_id)
                    except Exception as exc:
                        self.rag_store.mark_vector_rebuild_failed(operation_id, type(exc).__name__)
                        raise
                results.append(
                    GenerationOperationResult(
                        document_id=doc,
                        generation_id=generation,
                        status="verified",
                        chunk_count=len(expected),
                    )
                )
        return tuple(results)

    def recover_pending_operations(self) -> tuple[GenerationOperationResult, ...]:
        """Resume staged builds and pending cleanup; safe to invoke repeatedly."""
        recovered: list[GenerationOperationResult] = []
        with self._lock:
            operations = self.rag_store.pending_generation_operations()
            for operation in operations:
                if operation["operation"] != "replace":
                    if operation["operation"] == "vector_rebuild":
                        operation_id = str(operation["operation_id"])
                        document_id = str(operation["document_name"])
                        generation_id = str(operation["generation_id"])
                        revision = str(operation["vector_revision"])
                        try:
                            snapshot = self.rag_store.generation_snapshot(document_id, generation_id)
                            if snapshot is None:
                                raise IndexLifecycleError("vector rebuild source generation is missing")
                            source, _hash, _path, _parser, _chunker, chunks = snapshot
                            expected = self.rag_store.generation_chunk_ids(document_id, generation_id)
                            if not self.rag_store.validate_generation(document_id, generation_id, expected):
                                raise IndexLifecycleError("vector rebuild SQLite/FTS IDs do not match")
                            actual = self.vector_store.upsert_generation_chunks(
                                source, document_id, generation_id, chunks,
                                collection_revision=revision,
                            )
                            if actual != expected:
                                raise IndexLifecycleError("recovered shadow Chroma IDs do not match")
                            self.rag_store.mark_vector_rebuild_ready(operation_id)
                            self.rag_store.activate_vector_revision(operation_id)
                            recovered.append(
                                GenerationOperationResult(
                                    document_id=document_id,
                                    generation_id=generation_id,
                                    status="vector_rebuild_recovered",
                                    chunk_count=len(expected),
                                )
                            )
                        except Exception as exc:
                            self.rag_store.mark_vector_rebuild_failed(operation_id, type(exc).__name__)
                            LOGGER.warning("vector revision recovery failed (%s)", type(exc).__name__)
                    continue
                operation_id = str(operation["operation_id"])
                document_id = str(operation["document_name"])
                generation_id = str(operation["generation_id"])
                try:
                    count = self._build_and_activate(operation_id, document_id, generation_id)
                    recovered.append(
                        GenerationOperationResult(
                            document_id=document_id,
                            generation_id=generation_id,
                            status="recovered_active",
                            chunk_count=count,
                            previous_generation=self._previous_generation(operation_id),
                        )
                    )
                except Exception as exc:
                    self.rag_store.mark_generation_failed(operation_id, type(exc).__name__)
                    LOGGER.warning("document generation recovery failed (%s)", type(exc).__name__)

            pending_before = set(self.rag_store.pending_generation_cleanup())
            for doc, generation in sorted(pending_before):
                cleanup_id = f"cleanup:{doc}:{generation}"
                try:
                    if not self.rag_store.claim_generation_cleanup(doc, generation):
                        continue
                    revisions = self.rag_store.vector_revisions_for_generation(doc, generation) or [generation]
                    for revision in revisions:
                        self.vector_store.delete_generation(
                            doc, generation, collection_revision=revision
                        )
                    self.rag_store.cleanup_generation_rows(doc, generation)
                    self.rag_store.mark_cleanup_complete(doc, generation)
                    recovered.append(
                        GenerationOperationResult(
                            document_id=doc,
                            generation_id=generation,
                            status="cleanup_complete",
                        )
                    )
                except Exception as exc:
                    self.rag_store.mark_operation_error(cleanup_id, type(exc).__name__)
                    LOGGER.warning("document generation cleanup failed (%s)", type(exc).__name__)
                    recovered.append(
                        GenerationOperationResult(
                            document_id=doc,
                            generation_id=generation,
                            status="cleanup_pending",
                        )
                    )
            for doc, generation, revision in self.rag_store.pending_vector_cleanup():
                cleanup_id = f"vector-cleanup:{doc}:{generation}:{revision}"
                try:
                    self.vector_store.delete_generation(
                        doc, generation, collection_revision=revision
                    )
                    self.rag_store.cleanup_vector_revision_row(doc, generation, revision)
                    recovered.append(
                        GenerationOperationResult(
                            document_id=doc,
                            generation_id=generation,
                            status="vector_cleanup_complete",
                        )
                    )
                except Exception as exc:
                    self.rag_store.mark_operation_error(cleanup_id, type(exc).__name__)
                    LOGGER.warning("vector revision cleanup failed (%s)", type(exc).__name__)
                    recovered.append(
                        GenerationOperationResult(
                            document_id=doc,
                            generation_id=generation,
                            status="vector_cleanup_pending",
                        )
                    )
            self.rag_store.complete_deletes_without_pending_generations()
            # If the SQLite rows were removed immediately before a crash, the
            # deterministic cleanup operation is complete by construction.
            remaining = set(self.rag_store.pending_generation_cleanup())
            for operation in self.rag_store.pending_generation_operations():
                if operation["operation"] == "cleanup":
                    key = (str(operation["document_name"]), str(operation["generation_id"]))
                    if key not in remaining:
                        self.rag_store.mark_cleanup_complete(*key)
        return tuple(recovered)

    def rollback(self, document_id: str, generation_id: str) -> str | None:
        """Restore a retained generation before cleanup removes its rollback data."""
        with self._lock:
            head = self.rag_store.generation_head(document_id)
            if head is not None and head[1]:
                raise RuntimeError("cannot roll back a tombstoned document")
            expected_current = head[0] if head else None
            snapshot = self.rag_store.generation_snapshot(document_id, generation_id)
            if snapshot is None:
                raise ValueError("rollback generation is not retained")
            _source, _content_hash, _path, _parser, _chunker, _chunks = snapshot
            expected = self.rag_store.generation_chunk_ids(document_id, generation_id)
            if not self.rag_store.validate_generation(document_id, generation_id, expected):
                raise IndexLifecycleError("rollback generation SQLite/FTS IDs do not match")
            actual = self.vector_store.generation_chunk_ids(
                document_id,
                generation_id,
                collection_revision=self.rag_store.generation_vector_revision(document_id, generation_id)
                or generation_id,
                raise_on_error=True,
            )
            if actual != expected:
                raise IndexLifecycleError("rollback generation Chroma IDs do not match")
            return self.rag_store.rollback_generation(
                document_id,
                generation_id,
                expected_current_generation=expected_current,
                expected_tombstone=False,
            )

    def index_status(self, document_id: str) -> IndexLifecycleStatus:
        head = self.rag_store.generation_head(document_id)
        pending = tuple(
            generation for doc, generation in self.rag_store.pending_generation_cleanup()
            if doc == document_id
        )
        operations = tuple(self.rag_store.index_lifecycle_rows(document_id))
        return IndexLifecycleStatus(
            document=self.rag_store.index_status(document_id),
            active_generation=head[0] if head and not head[1] else None,
            tombstoned=bool(head and head[1]),
            pending_cleanup=pending,
            pending_vector_cleanup=tuple(
                f"{gen}:{revision}"
                for doc, gen, revision in self.rag_store.pending_vector_cleanup()
                if doc == document_id
            ),
            operations=operations,
        )

    def _build_and_activate(
        self, operation_id: str, document_id: str, generation_id: str
    ) -> int:
        snapshot = self.rag_store.generation_snapshot(document_id, generation_id)
        if snapshot is None:
            raise IndexLifecycleError("staged SQLite generation is missing")
        source, _content_hash, _source_path, _parser, _chunker, chunks = snapshot
        expected = frozenset(chunk.chunk_id for chunk in chunks)
        if not self.rag_store.validate_generation(document_id, generation_id, expected):
            raise IndexLifecycleError("staged SQLite/FTS chunk IDs do not match")
        actual = self.vector_store.upsert_generation_chunks(
            source, document_id, generation_id, chunks
        )
        if actual != expected:
            raise IndexLifecycleError("staged Chroma chunk IDs do not match SQLite")
        self.rag_store.mark_generation_vector_ready(operation_id)
        self.rag_store.activate_generation(operation_id)
        return len(expected)

    def _previous_generation(self, operation_id: str) -> str | None:
        return self.rag_store.operation_expected_generation(operation_id)


def _generation_id(
    document_id: str,
    content_hash: str,
    chunks: list[DocumentChunk],
    source: DocumentSource,
    parser_version: str,
    chunker_version: str,
    metadata: Mapping[str, object] | None,
) -> str:
    payload = {
        "document_id": document_id,
        "content_hash": content_hash,
        "source": asdict(source),
        "parser_version": parser_version,
        "chunker_version": chunker_version,
        "metadata": dict(metadata or {}),
        "chunks": [
            {
                "chunk_id": chunk.chunk_id,
                "title": chunk.title,
                "content": chunk.content,
                "section_path": list(chunk.section_path),
                "locator": asdict(chunk.locator),
                "metadata": dict(chunk.metadata),
            }
            for chunk in chunks
        ],
    }
    canonical = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return "g-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:40]

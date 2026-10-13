from __future__ import annotations

"""Semantic vector RAG using ChromaDB + sentence-transformers.

This store sits alongside the existing SQLite FTS5 RagStore.
The FTS5 store handles exact/keyword matching; this one handles
semantic similarity for natural-language queries.

The embedding model (paraphrase-multilingual-MiniLM-L12-v2) is loaded
lazily on first use so service startup is not blocked.  The model is
~120 MB and fits comfortably on CPU; first load takes ~2s.
"""

import logging
import json
from pathlib import Path
import hashlib
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chromadb import Collection
    from sentence_transformers import SentenceTransformer

from app.rag import RagChunk
from app.knowledge.documents import (
    DocumentChunk,
    DocumentFilter,
    DocumentLocator,
    DocumentSource,
    LocatorKind,
)


LOGGER = logging.getLogger(__name__)
_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
_COLLECTION_PREFIX = "rag_"
_CORPUS_COLLECTION_PREFIX = "rag_corpus_"
_GENERATION_COLLECTION_PREFIX = "rag_gen_"


class VectorRagStore:
    """ChromaDB-backed semantic search for RAG chunks.

    Chunks are stored per document name in separate ChromaDB collections.
    The store is initialised with a path to a persistent ChromaDB directory.
    """

    def __init__(self, chroma_path: Path) -> None:
        self._path = Path(chroma_path)
        self._path.mkdir(parents=True, exist_ok=True)
        self._client = None   # lazy
        self._embedder: SentenceTransformer | None = None
        self._lock = Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def upsert_chunks(
        self,
        document_name: str,
        chunks: list[tuple[str, int, str, str] | DocumentChunk],  # legacy tuple or provenance-aware chunk
        *,
        document_source: DocumentSource | None = None,
        index_generation: str | None = None,
    ) -> int:
        """Replace all vector entries for one document with the supplied chunks."""
        normalized: list[tuple[str, int, str, str, DocumentChunk | None]] = []
        for chunk in chunks:
            if isinstance(chunk, DocumentChunk):
                if chunk.document_id != document_name:
                    raise ValueError("document chunk belongs to a different vector collection")
                page = chunk.locator.page or 0
                normalized.append((chunk.chunk_id, page, chunk.title, chunk.content, chunk))
            else:
                chunk_id, page, title, content = chunk
                normalized.append((chunk_id, page, title, content, None))
        collection = self._collection(document_name, create=True)
        if collection is None:
            raise RuntimeError(f"could not open vector collection for {document_name}")
        texts = [f"{title} {content}" for _, _, title, content, _ in normalized]
        chunk_ids = [chunk_id for chunk_id, _, _, _, _ in normalized]
        embeddings = None
        metadatas = []
        for _, page, title, content, chunk in normalized:
            metadata: dict[str, str | int | float | bool] = {
                "document_id": document_name,
                "page": page,
                "title": title,
                "content": content,
            }
            if document_source is not None:
                for key in (
                    "source_id",
                    "source_uri",
                    "source_type",
                    "corpus_id",
                    "pack_id",
                    "pack_version",
                    "game_version",
                    "source_revision",
                    "license_id",
                ):
                    value = getattr(document_source, key)
                    if value is not None:
                        metadata[key] = value
            if chunk is not None:
                metadata.update(
                    {
                        "locator_kind": chunk.locator.kind.value,
                        "section_path_json": json.dumps(list(chunk.section_path), ensure_ascii=False),
                        "chunk_metadata_json": json.dumps(
                            dict(chunk.metadata), ensure_ascii=False, allow_nan=False, sort_keys=True
                        ),
                    }
                )
                locator = chunk.locator
                if locator.start_line is not None:
                    metadata["start_line"] = locator.start_line
                if locator.end_line is not None:
                    metadata["end_line"] = locator.end_line
                if locator.page is not None:
                    metadata["page"] = locator.page
                if locator.anchor is not None:
                    metadata["anchor"] = locator.anchor
                generation = chunk.generation or index_generation
            else:
                generation = index_generation
            if generation is not None:
                metadata["generation"] = generation
            metadatas.append(metadata)
        # Prepare the complete replacement before deleting any live vectors.
        if normalized:
            embedder = self._get_embedder()
            try:
                embeddings = embedder.encode(texts, show_progress_bar=False).tolist()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("VectorRagStore.upsert_chunks embedding failed (%s)", type(exc).__name__)
                raise

        try:
            existing = collection.get(include=[])
            old_ids = existing.get("ids") or []
            if old_ids:
                collection.delete(ids=old_ids)
            if normalized:
                collection.add(
                    ids=chunk_ids,
                    embeddings=embeddings,
                    documents=texts,
                    metadatas=metadatas,
                )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("VectorRagStore.upsert_chunks replacement failed (%s)", type(exc).__name__)
            raise
        return len(normalized)

    def upsert_corpus_chunks(
        self,
        document_source: DocumentSource,
        document_id: str,
        chunks: list[DocumentChunk],
        *,
        index_generation: str | None = None,
    ) -> int:
        """Replace one document's vectors in its shared corpus collection.

        This additive API leaves the P0-07 document collections untouched. It
        is the multi-document path used by the new Knowledge layer; P2-03 will
        add staged generation switching and cross-store recovery.
        """
        if not document_id or any(chunk.document_id != document_id for chunk in chunks):
            raise ValueError("all vector chunks must belong to the requested document")
        collection = self._corpus_collection(document_source.corpus_id, create=True)
        if collection is None:
            raise RuntimeError("could not open corpus vector collection")
        texts = [f"{chunk.title} {chunk.content}" for chunk in chunks]
        embeddings = None
        metadatas = [
            _document_chunk_metadata(document_source, document_id, chunk, index_generation)
            for chunk in chunks
        ]
        if chunks:
            embedder = self._get_embedder()
            embeddings = embedder.encode(texts, show_progress_bar=False).tolist()
        try:
            existing = collection.get(where={"document_id": document_id}, include=[])
            old_ids = existing.get("ids") or []
            if old_ids:
                collection.delete(ids=old_ids)
            if chunks:
                collection.add(
                    ids=[chunk.chunk_id for chunk in chunks],
                    embeddings=embeddings,
                    documents=texts,
                    metadatas=metadatas,
                )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("VectorRagStore.upsert_corpus_chunks failed (%s)", type(exc).__name__)
            raise
        return len(chunks)

    def upsert_generation_chunks(
        self,
        document_source: DocumentSource,
        document_id: str,
        generation_id: str,
        chunks: list[DocumentChunk],
        *,
        collection_revision: str | None = None,
    ) -> frozenset[str]:
        """Build a generation-isolated collection and verify its exact IDs.

        A generation has a deterministic, private collection. Replacing rows
        here cannot disturb the currently active generation or the legacy SFM
        collection. The caller switches the SQLite active pointer only after
        this method returns a matching ID set.
        """
        if not document_id or not generation_id:
            raise ValueError("document and generation IDs are required")
        if any(chunk.document_id != document_id for chunk in chunks):
            raise ValueError("all vector chunks must belong to the requested document")
        chunk_ids = [chunk.chunk_id for chunk in chunks]
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("generation vector chunk IDs must be unique")
        collection_revision = collection_revision or generation_id
        collection = self._generation_collection(document_id, collection_revision, create=True)
        if collection is None:
            raise RuntimeError("could not open generation vector collection")
        texts = [f"{chunk.title} {chunk.content}" for chunk in chunks]
        metadatas = [
            _document_chunk_metadata(document_source, document_id, chunk, generation_id)
            for chunk in chunks
        ]
        embeddings = None
        if chunks:
            embeddings = self._get_embedder().encode(texts, show_progress_bar=False).tolist()
        try:
            previous_ids = collection.get(include=[]).get("ids") or []
            if previous_ids:
                collection.delete(ids=previous_ids)
            if chunks:
                collection.add(
                    ids=chunk_ids,
                    embeddings=embeddings,
                    documents=texts,
                    metadatas=metadatas,
                )
            actual_ids = frozenset(collection.get(include=[]).get("ids") or [])
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("VectorRagStore generation write failed (%s)", type(exc).__name__)
            raise
        expected_ids = frozenset(chunk_ids)
        if actual_ids != expected_ids:
            raise RuntimeError("generation vector IDs do not match the staged SQLite generation")
        return actual_ids

    def generation_chunk_ids(
        self, document_id: str, generation_id: str, *,
        collection_revision: str | None = None, raise_on_error: bool = False
    ) -> frozenset[str] | None:
        collection = self._generation_collection(
            document_id, collection_revision or generation_id,
            create=False, raise_on_error=raise_on_error
        )
        if collection is None:
            return None
        try:
            return frozenset(collection.get(include=[]).get("ids") or [])
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("VectorRagStore generation lookup failed (%s)", type(exc).__name__)
            if raise_on_error:
                raise
            return None

    def search_generation(
        self,
        query: str,
        document_id: str,
        generation_id: str,
        limit: int = 8,
        *,
        filters: DocumentFilter | None = None,
        collection_revision: str | None = None,
        raise_on_error: bool = False,
    ) -> list[RagChunk]:
        query = query.strip()
        if not query or limit <= 0:
            return []
        if filters is not None and filters.document_id not in {None, document_id}:
            return []
        collection = self._generation_collection(
            document_id, collection_revision or generation_id,
            create=False, raise_on_error=raise_on_error
        )
        if collection is None or collection.count() <= 0:
            return []
        conditions: list[dict[str, object]] = [
            {"document_id": document_id},
            {"generation": generation_id},
        ]
        metadata_filter = _chroma_where(filters)
        if metadata_filter:
            conditions.append(metadata_filter)
        where: dict[str, object] = conditions[0] if len(conditions) == 1 else {"$and": conditions}
        try:
            query_vec = self._get_embedder().encode([query], show_progress_bar=False).tolist()
            results = collection.query(
                query_embeddings=query_vec,
                n_results=min(limit, collection.count()),
                include=["metadatas", "distances"],
                where=where,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("VectorRagStore generation search failed (%s)", type(exc).__name__)
            if raise_on_error:
                raise
            return []
        ids = results.get("ids", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]
        chunks = [
            _vector_chunk(str(chunk_id), metadata or {}, 1.0 / (1.0 + float(distance)), document_id)
            for chunk_id, metadata, distance in zip(ids, metadatas, distances)
        ]
        return sorted(chunks, key=lambda chunk: chunk.score, reverse=True)

    def delete_generation(
        self, document_id: str, generation_id: str, *, collection_revision: str | None = None
    ) -> None:
        """Delete one generation collection; absent collections are success."""
        name = _generation_collection_name(document_id, collection_revision or generation_id)
        try:
            self._get_client().delete_collection(name=name)
        except Exception as exc:  # noqa: BLE001
            if _is_collection_not_found(exc):
                return
            LOGGER.warning("VectorRagStore generation delete failed (%s)", type(exc).__name__)
            raise

    def delete_document(self, document_name: str) -> None:
        """Delete the document's dedicated collection; repeated deletion is safe."""
        collection = self._collection(document_name, create=True)
        if collection is None:
            raise RuntimeError(f"could not open vector collection for {document_name}")
        name = f"{_COLLECTION_PREFIX}{document_name}"
        try:
            self._get_client().delete_collection(name=name)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "VectorRagStore.delete_document failed for document %s (%s)",
                document_name,
                type(exc).__name__,
            )
            raise

    def search(
        self,
        query: str,
        document_name: str | None = None,
        limit: int = 4,
        *,
        filters: DocumentFilter | None = None,
        raise_on_error: bool = False,
    ) -> list[RagChunk]:
        """Return up to limit semantically similar chunks."""
        query = query.strip()
        if not query:
            return []
        if limit <= 0:
            return []
        if filters is not None and filters.document_id is not None:
            if document_name is not None and document_name != filters.document_id:
                return []
            document_name = filters.document_id
        collection = self._collection(
            document_name or "sfm", create=False, raise_on_error=raise_on_error
        )
        if collection is None:
            return []
        count = collection.count()
        if count <= 0:
            return []
        embedder = self._get_embedder()
        try:
            query_vec = embedder.encode([query], show_progress_bar=False).tolist()
            query_args = {
                "query_embeddings": query_vec,
                "n_results": min(limit, count),
                "include": ["metadatas", "distances"],
            }
            where = _chroma_where(filters)
            if where:
                query_args["where"] = where
            results = collection.query(
                **query_args,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("VectorRagStore.search failed: %s", exc)
            if raise_on_error:
                raise
            return []
        chunks = []
        ids = results.get("ids", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]
        for chunk_id, meta, dist in zip(ids, metas, distances):
            score = 1.0 / (1.0 + float(dist))
            chunks.append(_vector_chunk(str(chunk_id), meta or {}, score, document_name or "sfm"))
        return sorted(chunks, key=lambda c: c.score, reverse=True)

    def search_corpus(
        self,
        query: str,
        corpus_id: str,
        limit: int = 8,
        *,
        filters: DocumentFilter | None = None,
        document_ids: list[str] | None = None,
        raise_on_error: bool = False,
    ) -> list[RagChunk]:
        """Search one shared corpus collection with exact metadata filters."""
        query = query.strip()
        if not query or limit <= 0:
            return []
        if filters is not None and filters.corpus_id not in {None, corpus_id}:
            return []
        collection = self._corpus_collection(
            corpus_id, create=False, raise_on_error=raise_on_error
        )
        if collection is None:
            return []
        count = collection.count()
        if count <= 0:
            return []
        embedder = self._get_embedder()
        query_vec = embedder.encode([query], show_progress_bar=False).tolist()
        where = _chroma_where(
            filters,
            required_corpus_id=corpus_id,
            include_document_id=True,
        )
        if document_ids is not None:
            active_document_filter: dict[str, object] = {
                "document_id": {"$in": list(dict.fromkeys(document_ids))}
            }
            where = (
                {"$and": [where, active_document_filter]}
                if where is not None
                else active_document_filter
            )
        results = collection.query(
            query_embeddings=query_vec,
            n_results=min(limit, count),
            include=["metadatas", "distances"],
            where=where,
        )
        chunks = []
        ids = results.get("ids", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]
        for chunk_id, meta, dist in zip(ids, metas, distances):
            chunks.append(
                _vector_chunk(str(chunk_id), meta or {}, 1.0 / (1.0 + float(dist)), "")
            )
        return sorted(chunks, key=lambda chunk: chunk.score, reverse=True)

    def has_document(self, document_name: str, *, raise_on_error: bool = False) -> bool:
        col = self._collection(
            document_name, create=False, raise_on_error=raise_on_error
        )
        return col is not None and col.count() > 0

    def has_corpus(self, corpus_id: str, *, raise_on_error: bool = False) -> bool:
        collection = self._corpus_collection(
            corpus_id, create=False, raise_on_error=raise_on_error
        )
        return collection is not None and collection.count() > 0

    def indexed_corpus_documents(
        self,
        corpus_id: str,
        document_ids: list[str],
        *,
        filters: DocumentFilter | None = None,
        raise_on_error: bool = False,
    ) -> frozenset[str]:
        """Return which requested documents have vectors in this corpus."""
        if not document_ids:
            return frozenset()
        collection = self._corpus_collection(
            corpus_id, create=False, raise_on_error=raise_on_error
        )
        if collection is None:
            return frozenset()
        document_filter: dict[str, object] = {
            "document_id": {"$in": list(dict.fromkeys(document_ids))}
        }
        metadata_filter = _chroma_where(filters, required_corpus_id=corpus_id)
        if metadata_filter:
            if "$and" in metadata_filter:
                where = {"$and": [document_filter, *metadata_filter["$and"]]}
            else:
                where = {"$and": [document_filter, metadata_filter]}
        else:
            where = document_filter
        rows = collection.get(
            where=where,
            include=["metadatas"],
        )
        indexed = set()
        for metadata in rows.get("metadatas") or []:
            if isinstance(metadata, dict) and isinstance(metadata.get("document_id"), str):
                indexed.add(metadata["document_id"])
        return frozenset(indexed)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_client(self):
        if self._client is None:
            with self._lock:
                if self._client is None:
                    import chromadb
                    self._client = chromadb.PersistentClient(path=str(self._path))
        return self._client

    def _collection(
        self,
        document_name: str,
        *,
        create: bool,
        raise_on_error: bool = False,
    ) -> "Collection | None":
        name = f"{_COLLECTION_PREFIX}{document_name}"
        client = self._get_client()
        try:
            if create:
                return client.get_or_create_collection(
                    name=name,
                    metadata={"hnsw:space": "cosine"},
                )
            return client.get_collection(name=name)
        except Exception as exc:  # noqa: BLE001
            if _is_collection_not_found(exc):
                return None
            LOGGER.warning("VectorRagStore collection lookup failed (%s)", type(exc).__name__)
            if raise_on_error:
                raise
            return None

    def _corpus_collection(
        self,
        corpus_id: str,
        *,
        create: bool,
        raise_on_error: bool = False,
    ) -> "Collection | None":
        suffix = hashlib.sha256(corpus_id.encode("utf-8")).hexdigest()[:32]
        name = f"{_CORPUS_COLLECTION_PREFIX}{suffix}"
        client = self._get_client()
        try:
            if create:
                return client.get_or_create_collection(
                    name=name,
                    metadata={"hnsw:space": "cosine", "corpus_id": corpus_id},
                )
            return client.get_collection(name=name)
        except Exception as exc:  # noqa: BLE001
            if _is_collection_not_found(exc):
                return None
            LOGGER.warning("VectorRagStore corpus lookup failed (%s)", type(exc).__name__)
            if raise_on_error:
                raise
            return None

    def _generation_collection(
        self,
        document_id: str,
        generation_id: str,
        *,
        create: bool,
        raise_on_error: bool = False,
    ) -> "Collection | None":
        name = _generation_collection_name(document_id, generation_id)
        client = self._get_client()
        try:
            if create:
                return client.get_or_create_collection(
                    name=name,
                    metadata={"hnsw:space": "cosine", "document_id": document_id, "generation": generation_id},
                )
            return client.get_collection(name=name)
        except Exception as exc:  # noqa: BLE001
            if _is_collection_not_found(exc):
                return None
            LOGGER.warning("VectorRagStore generation collection lookup failed (%s)", type(exc).__name__)
            if raise_on_error:
                raise
            return None

    def _get_embedder(self) -> "SentenceTransformer":
        if self._embedder is None:
            with self._lock:
                if self._embedder is None:
                    LOGGER.info("Loading embedding model %s …", _MODEL_NAME)
                    from sentence_transformers import SentenceTransformer
                    self._embedder = SentenceTransformer(_MODEL_NAME)
                    LOGGER.info("Embedding model loaded.")
        return self._embedder


def _chroma_where(
    filters: DocumentFilter | None,
    *,
    required_corpus_id: str | None = None,
    include_document_id: bool = False,
) -> dict[str, object] | None:
    conditions: list[dict[str, str]] = []
    if required_corpus_id is not None:
        conditions.append({"corpus_id": required_corpus_id})
    keys = (
        "source_id",
        "source_type",
        "corpus_id",
        "pack_id",
        "pack_version",
        "game_version",
        "source_revision",
    )
    if include_document_id:
        keys = ("document_id", *keys)
    for key in keys:
        value = getattr(filters, key) if filters is not None else None
        if value is not None:
            if key == "corpus_id" and required_corpus_id is not None:
                continue
            conditions.append({key: value})
    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def _generation_collection_name(document_id: str, generation_id: str) -> str:
    identity = f"{document_id}\0{generation_id}".encode("utf-8")
    return _GENERATION_COLLECTION_PREFIX + hashlib.sha256(identity).hexdigest()[:40]


def _document_chunk_metadata(
    source: DocumentSource,
    document_id: str,
    chunk: DocumentChunk,
    generation: str | None,
) -> dict[str, str | int | float | bool]:
    metadata: dict[str, str | int | float | bool] = {
        "document_id": document_id,
        "source_id": source.source_id,
        "source_uri": source.source_uri,
        "source_type": source.source_type,
        "corpus_id": source.corpus_id,
        "title": chunk.title,
        "content": chunk.content,
        "locator_kind": chunk.locator.kind.value,
        "section_path_json": json.dumps(list(chunk.section_path), ensure_ascii=False),
        "chunk_metadata_json": json.dumps(
            dict(chunk.metadata), ensure_ascii=False, allow_nan=False, sort_keys=True
        ),
    }
    for key in ("pack_id", "pack_version", "game_version", "source_revision", "license_id"):
        value = getattr(source, key)
        if value is not None:
            metadata[key] = value
    for key in ("start_line", "end_line", "page"):
        value = getattr(chunk.locator, key)
        if value is not None:
            metadata[key] = value
    if chunk.locator.anchor is not None:
        metadata["anchor"] = chunk.locator.anchor
    if chunk.generation or generation:
        metadata["generation"] = chunk.generation or generation
    return metadata


def _vector_chunk(chunk_id: str, metadata: dict[str, object], score: float, document_name: str) -> RagChunk:
    locator = None
    locator_kind = metadata.get("locator_kind")
    if isinstance(locator_kind, str):
        try:
            locator = DocumentLocator(
                kind=LocatorKind(locator_kind),
                start_line=_optional_int(metadata.get("start_line")),
                end_line=_optional_int(metadata.get("end_line")),
                page=_optional_int(metadata.get("page")),
                anchor=metadata.get("anchor") if isinstance(metadata.get("anchor"), str) else None,
            )
        except ValueError:
            locator = None
    try:
        raw_path = metadata.get("section_path_json", "[]")
        path_data = json.loads(raw_path) if isinstance(raw_path, str) else []
        section_path = tuple(str(item) for item in path_data) if isinstance(path_data, list) else ()
    except json.JSONDecodeError:
        section_path = ()
    try:
        raw_chunk_metadata = metadata.get("chunk_metadata_json", "{}")
        chunk_metadata = json.loads(raw_chunk_metadata) if isinstance(raw_chunk_metadata, str) else {}
    except json.JSONDecodeError:
        chunk_metadata = {}
    return RagChunk(
        chunk_id=chunk_id,
        document_id=str(metadata.get("document_id") or document_name),
        source=str(metadata.get("source_uri") or "vector"),
        page=_optional_int(metadata.get("page")) or 0,
        title=str(metadata.get("title") or ""),
        content=str(metadata.get("content") or ""),
        score=score,
        source_id=_optional_str(metadata.get("source_id")),
        source_uri=_optional_str(metadata.get("source_uri")),
        source_type=_optional_str(metadata.get("source_type")),
        corpus_id=_optional_str(metadata.get("corpus_id")),
        pack_id=_optional_str(metadata.get("pack_id")),
        pack_version=_optional_str(metadata.get("pack_version")),
        game_version=_optional_str(metadata.get("game_version")),
        source_revision=_optional_str(metadata.get("source_revision")),
        license_id=_optional_str(metadata.get("license_id")),
        generation=_optional_str(metadata.get("generation")),
        section_path=section_path,
        locator=locator,
        metadata=chunk_metadata if isinstance(chunk_metadata, dict) else {},
    )


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _is_collection_not_found(exc: Exception) -> bool:
    # KeyError is used by local fake clients; Chroma raises this concrete type.
    return isinstance(exc, KeyError) or (
        type(exc).__name__ == "NotFoundError"
        and type(exc).__module__.startswith("chromadb.")
    )

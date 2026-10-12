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
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chromadb import Collection
    from sentence_transformers import SentenceTransformer

from app.rag import RagChunk


LOGGER = logging.getLogger(__name__)
_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
_COLLECTION_PREFIX = "rag_"


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
        chunks: list[tuple[str, int, str, str]],  # (chunk_id, page, title, content)
    ) -> int:
        """Embed and store/replace chunks for a document."""
        if not chunks:
            return 0
        collection = self._collection(document_name, create=True)
        embedder = self._get_embedder()
        texts = [f"{title} {content}" for _, _, title, content in chunks]
        chunk_ids = [chunk_id for chunk_id, _, _, _ in chunks]
        # Delete existing entries to allow full replace
        try:
            existing = collection.get(ids=chunk_ids)
            if existing["ids"]:
                collection.delete(ids=existing["ids"])
        except Exception:  # noqa: BLE001
            pass
        try:
            embeddings = embedder.encode(texts, show_progress_bar=False).tolist()
            metadatas = [
                {"page": page, "title": title, "content": content}
                for _, page, title, content in chunks
            ]
            collection.add(
                ids=chunk_ids,
                embeddings=embeddings,
                documents=texts,
                metadatas=metadatas,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("VectorRagStore.upsert_chunks failed: %s", exc)
            return 0
        return len(chunks)

    def search(
        self,
        query: str,
        document_name: str | None = None,
        limit: int = 4,
    ) -> list[RagChunk]:
        """Return up to limit semantically similar chunks."""
        query = query.strip()
        if not query:
            return []
        collection = self._collection(document_name or "sfm", create=False)
        if collection is None:
            return []
        embedder = self._get_embedder()
        try:
            query_vec = embedder.encode([query], show_progress_bar=False).tolist()
            results = collection.query(
                query_embeddings=query_vec,
                n_results=min(limit, collection.count()),
                include=["metadatas", "distances"],
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("VectorRagStore.search failed: %s", exc)
            return []
        chunks = []
        ids = results.get("ids", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]
        for chunk_id, meta, dist in zip(ids, metas, distances):
            score = 1.0 / (1.0 + float(dist))
            chunks.append(
                RagChunk(
                    chunk_id=str(chunk_id),
                    source="vector",
                    page=int(meta.get("page", 0)),
                    title=str(meta.get("title", "")),
                    content=str(meta.get("content", "")),
                    score=score,
                )
            )
        return sorted(chunks, key=lambda c: c.score, reverse=True)

    def has_document(self, document_name: str) -> bool:
        col = self._collection(document_name, create=False)
        return col is not None and col.count() > 0

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_client(self):
        if self._client is None:
            with self._lock:
                if self._client is None:
                    import chromadb
                    self._client = chromadb.PersistentClient(path=str(self._path))
        return self._client

    def _collection(self, document_name: str, *, create: bool) -> "Collection | None":
        name = f"{_COLLECTION_PREFIX}{document_name}"
        client = self._get_client()
        try:
            if create:
                return client.get_or_create_collection(
                    name=name,
                    metadata={"hnsw:space": "cosine"},
                )
            return client.get_collection(name=name)
        except Exception:  # noqa: BLE001
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

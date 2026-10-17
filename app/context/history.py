from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import re
import time
from typing import Any, Callable, Literal, Protocol

from app.context.contracts import ContextSnapshot
from app.memory import MemoryStore


HistorySearchStatus = Literal["results", "no_results", "not_indexed", "timed_out"]
SQLiteCall = Callable[..., Any]
VectorCall = Callable[..., Any]


class HistoryVectorIndex(Protocol):
    """Optional semantic index contract; implementations receive an explicit embedder."""

    def upsert(
        self, *, group_id: str, row_id: int, epoch: int, content: str,
        content_hash: str, embedding_version: str,
    ) -> None: ...

    def delete(self, *, group_id: str, row_id: int) -> None: ...

    def query(
        self, *, query: str, group_id: str, epoch: int,
        embedding_version: str, limit: int,
    ) -> list[dict[str, object]]: ...


@dataclass(frozen=True, slots=True)
class HistorySearchResult:
    status: HistorySearchStatus
    rows: tuple[dict[str, object], ...] = ()
    backfill_complete: bool = False
    vector_status: Literal["disabled", "results", "no_results", "unavailable", "timed_out"] = "disabled"


class ChromaHistoryVectorIndex:
    """Duck-typed Chroma adapter with an injected embedder and a private collection.

    This class never downloads or constructs an embedding model. The caller must
    explicitly provide a Chroma collection and a locally available encoder.
    """

    def __init__(self, collection: Any, embedder: Any) -> None:
        if collection is None or embedder is None:
            raise ValueError("history Chroma collection and local embedder are required")
        self.collection = collection
        self.embedder = embedder

    @staticmethod
    def _vector_id(group_id: str, row_id: int, embedding_version: str) -> str:
        scope = hashlib.sha256(str(group_id).encode("utf-8")).hexdigest()[:20]
        version = hashlib.sha256(str(embedding_version).encode("utf-8")).hexdigest()[:12]
        return f"chat-{scope}-{int(row_id)}-{version}"

    def _encode(self, texts: list[str]) -> list[list[float]]:
        encoded = self.embedder.encode(texts, show_progress_bar=False)
        if hasattr(encoded, "tolist"):
            encoded = encoded.tolist()
        if not isinstance(encoded, list) or len(encoded) != len(texts):
            raise ValueError("history embedder returned an invalid vector batch")
        vectors: list[list[float]] = []
        for vector in encoded:
            if hasattr(vector, "tolist"):
                vector = vector.tolist()
            if not isinstance(vector, list) or not vector:
                raise ValueError("history embedder returned an invalid vector")
            vectors.append([float(value) for value in vector])
        return vectors

    def upsert(
        self, *, group_id: str, row_id: int, epoch: int, content: str,
        content_hash: str, embedding_version: str,
    ) -> None:
        self.collection.upsert(
            ids=[self._vector_id(group_id, row_id, embedding_version)], embeddings=self._encode([content]),
            documents=[content], metadatas=[{
                "group_id": str(group_id), "row_id": int(row_id), "epoch": int(epoch),
                "content_hash": str(content_hash), "embedding_version": str(embedding_version),
            }],
        )

    def delete(self, *, group_id: str, row_id: int) -> None:
        result = self.collection.get(
            where={"$and": [{"group_id": str(group_id)}, {"row_id": int(row_id)}]},
            include=[],
        )
        ids = result.get("ids") or []
        if ids:
            self.collection.delete(ids=ids)

    def query(
        self, *, query: str, group_id: str, epoch: int,
        embedding_version: str, limit: int,
    ) -> list[dict[str, object]]:
        if not 1 <= limit <= 100:
            raise ValueError("history vector query limit is outside the supported bounds")
        result = self.collection.query(
            query_embeddings=self._encode([query]), n_results=limit,
            where={"$and": [
                {"group_id": str(group_id)}, {"epoch": int(epoch)},
                {"embedding_version": str(embedding_version)},
            ]},
            include=["metadatas", "distances"],
        )
        ids = (result.get("ids") or [[]])[0]
        metadata = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        hits: list[dict[str, object]] = []
        for index, item in enumerate(metadata):
            try:
                hits.append({
                    "id": int(item["row_id"]), "content_hash": str(item["content_hash"]),
                    "rank": float(distances[index]) if index < len(distances) else float(index),
                    "vector_id": str(ids[index]) if index < len(ids) else "",
                })
            except (KeyError, TypeError, ValueError):
                continue
        return hits


class HistoryVectorSync:
    """Drain SQLite's idempotent outbox with SQLite revalidation before each write."""

    def __init__(self, memory: MemoryStore, sqlite_call: SQLiteCall,
                 vector_index: HistoryVectorIndex, vector_call: VectorCall) -> None:
        self.memory = memory
        self.sqlite_call = sqlite_call
        self.vector_index = vector_index
        self.vector_call = vector_call

    async def drain_once(self, *, limit: int = 32) -> dict[str, int]:
        operations = await self.sqlite_call(self.memory.pending_history_vector_operations, limit)
        completed = failed = 0
        for operation in operations:
            operation_id = str(operation["operation_id"])
            group_id = str(operation["group_id"])
            row_id = int(operation["row_id"])
            try:
                source = await self.sqlite_call(self.memory.get_history_vector_source, group_id, row_id)
                if source is not None:
                    # Older pending work converges to the current row value.
                    await self.vector_call(
                        self.vector_index.upsert,
                        group_id=group_id, row_id=row_id, epoch=int(source["epoch"]),
                        content=str(source["content"]), content_hash=str(source["content_hash"]),
                        embedding_version=str(operation["embedding_version"]),
                    )
                else:
                    await self.vector_call(self.vector_index.delete, group_id=group_id, row_id=row_id)
                await self.sqlite_call(self.memory.finish_history_vector_operation, operation_id)
                completed += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the FTS path remains available
                await self.sqlite_call(
                    self.memory.finish_history_vector_operation, operation_id, type(exc).__name__
                )
                failed += 1
        return {"processed": len(operations), "completed": completed, "failed": failed}


class HistoryRetriever:
    """Bounded SQLite FTS adapter. It never interprets query text as FTS syntax."""

    def __init__(
        self,
        memory: MemoryStore,
        sqlite_call: SQLiteCall,
        *,
        vector_index: HistoryVectorIndex | None = None,
        vector_call: VectorCall | None = None,
        vector_embedding_version: str = "chat-default-v1",
    ) -> None:
        if vector_index is not None and vector_call is None:
            raise ValueError("history vector access must use a bounded offload callback")
        self.memory = memory
        self.sqlite_call = sqlite_call
        self.vector_index = vector_index
        self.vector_call = vector_call
        self.vector_embedding_version = vector_embedding_version

    async def search(
        self,
        query: str,
        snapshot: ContextSnapshot,
        *,
        limit: int = 24,
        deadline: float | None = None,
    ) -> HistorySearchResult:
        if not snapshot.persisted:
            return HistorySearchResult("not_indexed")
        try:
            state_call = self.sqlite_call(self.memory.chat_history_index_status)
            if deadline is None:
                state = await state_call
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return HistorySearchResult("timed_out")
                state = await asyncio.wait_for(state_call, timeout=remaining)
        except TimeoutError:
            return HistorySearchResult("timed_out")
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            return HistorySearchResult("timed_out", backfill_complete=bool(state["backfill_complete"]))
        try:
            operation = self.sqlite_call(
                self.memory.search_history_fts, query, snapshot, limit=limit
            )
            rows = await operation if remaining is None else await asyncio.wait_for(operation, timeout=remaining)
        except TimeoutError:
            return HistorySearchResult("timed_out", backfill_complete=bool(state["backfill_complete"]))
        vector_status: Literal["disabled", "results", "no_results", "unavailable", "timed_out"] = "disabled"
        vector_rows: list[dict[str, object]] = []
        if self.vector_index is not None and self.vector_call is not None:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                vector_status = "timed_out"
            else:
                try:
                    operation = self.vector_call(
                        self.vector_index.query,
                        query=query[:512], group_id=snapshot.group_id, epoch=snapshot.epoch,
                        embedding_version=self.vector_embedding_version, limit=min(100, limit),
                    )
                    raw_hits = (
                        await operation
                        if remaining is None
                        else await asyncio.wait_for(operation, timeout=remaining)
                    )
                    vector_rows = await self.sqlite_call(
                        self.memory.validate_history_vector_hits, snapshot, raw_hits, limit=limit
                    )
                    vector_status = "results" if vector_rows else "no_results"
                except TimeoutError:
                    vector_status = "timed_out"
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - semantic retrieval is optional; SQLite remains authoritative
                    vector_status = "unavailable"
        if rows and vector_rows:
            merged = reciprocal_rank_fusion([list(rows), vector_rows])[:limit]
            rows = merged
        elif vector_rows:
            rows = vector_rows[:limit]
        # A valid miss while a legacy FTS backfill is incomplete is not proof of no matches.
        if not rows and not bool(state["backfill_complete"]):
            return HistorySearchResult(
                "not_indexed", backfill_complete=False, vector_status=vector_status
            )
        return HistorySearchResult(
            "results" if rows else "no_results",
            tuple(rows),
            backfill_complete=bool(state["backfill_complete"]),
            vector_status=vector_status,
        )

    async def reply_chain(self, snapshot: ContextSnapshot, message_id: str | None, *, depth: int = 3):
        if not snapshot.persisted or depth <= 0 or not message_id:
            return []
        return await self.sqlite_call(
            self.memory.get_history_reply_chain, snapshot, message_id, max_depth=depth
        )

    async def neighborhood(
        self,
        snapshot: ContextSnapshot,
        anchors: list[dict[str, object]],
        *,
        max_total: int = 32,
    ) -> list[dict[str, object]]:
        if not snapshot.persisted or not anchors:
            return []
        return await self.sqlite_call(
            self.memory.get_history_neighborhood, snapshot, anchors, radius=1, max_total=max_total
        )


def should_search_history(query: str, recent_count: int, recent_limit: int) -> bool:
    """Avoid retrieval for greetings; search old history when recent context is thin or recalled."""
    text = query.strip()
    if len(text) < 4 or not re.search(r"[\w\u3400-\u9fff]", text, flags=re.UNICODE):
        return False
    lowered = text.casefold()
    recall_markers = (
        "之前", "上次", "以前", "刚才", "前面", "我们说", "讨论过", "记得", "还记得",
        "earlier", "last time", "before", "we discussed", "do you remember", "previously",
    )
    if any(marker in lowered for marker in recall_markers):
        return True
    return recent_count < recent_limit


def reciprocal_rank_fusion(
    ranked_sources: list[list[dict[str, object]]], *, k: int = 60
) -> list[dict[str, object]]:
    """Fuse ranked local retrieval sources by row ID while preserving source provenance."""
    if k < 1:
        raise ValueError("RRF k must be positive")
    fused: dict[int, dict[str, object]] = {}
    for source_index, rows in enumerate(ranked_sources):
        source_name = ("fts", "vector", "history")[source_index] if source_index < 3 else f"source{source_index}"
        for rank, row in enumerate(rows, start=1):
            try:
                row_id = int(row["id"])
            except (KeyError, TypeError, ValueError):
                continue
            entry = fused.setdefault(row_id, {**row, "rrf_score": 0.0, "retrieval_sources": []})
            entry["rrf_score"] = float(entry["rrf_score"]) + 1.0 / (k + rank)
            sources = entry["retrieval_sources"]
            if isinstance(sources, list) and source_name not in sources:
                sources.append(source_name)
            entry[f"{source_name}_rank"] = rank
    return sorted(
        fused.values(),
        key=lambda row: (
            -float(row["rrf_score"]),
            0 if row.get("role") == "user" else 1,
            -int(row.get("id") or 0),
        ),
    )

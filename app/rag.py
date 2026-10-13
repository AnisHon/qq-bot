from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import json
import math
import re
import sqlite3
from typing import Iterable, Mapping

from app.knowledge.documents import (
    DocumentChunk,
    DocumentFilter,
    DocumentIndexStatus,
    DocumentLocator,
    DocumentSource,
    IndexState,
    LocatorKind,
)


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+.#:-]*|[0-9]+|[\u4e00-\u9fff]{2,}")


@dataclass(slots=True)
class RagChunk:
    chunk_id: str
    source: str
    page: int
    title: str
    content: str
    score: float
    document_id: str | None = None
    source_id: str | None = None
    source_uri: str | None = None
    source_type: str | None = None
    corpus_id: str | None = None
    pack_id: str | None = None
    pack_version: str | None = None
    game_version: str | None = None
    source_revision: str | None = None
    license_id: str | None = None
    content_hash: str | None = None
    generation: str | None = None
    section_path: tuple[str, ...] = ()
    locator: DocumentLocator | None = None
    metadata: Mapping[str, object] | None = None


def normalize_query(text: str) -> str:
    terms = _tokenize(text)
    return " ".join(f'"{term}"' for term in terms)


def _tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens: list[str] = []
    for match in _WORD_RE.finditer(text):
        token = match.group(0).strip()
        if not token:
            continue
        tokens.append(token)
        if re.fullmatch(r"[\u4e00-\u9fff]{3,}", token):
            for idx in range(0, len(token) - 1):
                tokens.append(token[idx : idx + 2])
        elif re.fullmatch(r"[\u4e00-\u9fff]{2}", token):
            tokens.extend(token)
    return tokens


def _clean_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class RagStore:
    def __init__(self, sqlite_path: Path):
        self.sqlite_path = Path(sqlite_path)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.sqlite_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            if immediate:
                # State transitions that read a pointer and then update it must
                # acquire SQLite's writer reservation before the first read.
                # Otherwise two processes can both pass a deferred read check.
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    source_path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_name TEXT NOT NULL,
                    chunk_id TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    page INTEGER NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS rag_chunks_fts USING fts5(
                    chunk_id UNINDEXED,
                    document_name UNINDEXED,
                    title,
                    content,
                    tokenize = 'unicode61 remove_diacritics 2'
                )
                """
            )
            # Additive metadata tables keep the legacy PDF schema and API
            # readable while new Markdown documents retain provenance.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_document_sources (
                    document_name TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    source_uri TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    corpus_id TEXT NOT NULL,
                    pack_id TEXT,
                    pack_version TEXT,
                    game_version TEXT,
                    source_revision TEXT,
                    license_id TEXT,
                    parser_version TEXT NOT NULL DEFAULT 'legacy',
                    chunker_version TEXT NOT NULL DEFAULT 'legacy',
                    index_generation TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_chunk_locators (
                    chunk_id TEXT PRIMARY KEY,
                    locator_kind TEXT NOT NULL,
                    start_line INTEGER,
                    end_line INTEGER,
                    page INTEGER,
                    anchor TEXT,
                    section_path_json TEXT NOT NULL DEFAULT '[]',
                    generation TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            # Generation-backed documents are stored separately so a staged
            # revision cannot overwrite rows that are still serving queries.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_index_generations (
                    document_name TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    parser_version TEXT NOT NULL,
                    chunker_version TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_uri TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    corpus_id TEXT NOT NULL,
                    pack_id TEXT,
                    pack_version TEXT,
                    game_version TEXT,
                    source_revision TEXT,
                    license_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    active_vector_revision TEXT,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (document_name, generation_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_index_generation_chunks (
                    document_name TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    chunk_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    page INTEGER NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    locator_kind TEXT,
                    start_line INTEGER,
                    end_line INTEGER,
                    locator_page INTEGER,
                    anchor TEXT,
                    section_path_json TEXT NOT NULL DEFAULT '[]',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (document_name, generation_id, chunk_id)
                )
                """
            )
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS rag_index_generation_fts USING fts5(
                    chunk_id UNINDEXED,
                    document_name UNINDEXED,
                    generation_id UNINDEXED,
                    title,
                    content,
                    tokenize = 'unicode61 remove_diacritics 2'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_index_heads (
                    document_name TEXT PRIMARY KEY,
                    active_generation TEXT,
                    tombstoned INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_index_vector_revisions (
                    document_name TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    vector_revision TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (document_name, generation_id, vector_revision)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_index_operations (
                    operation_id TEXT PRIMARY KEY,
                    document_name TEXT NOT NULL,
                    generation_id TEXT,
                    operation TEXT NOT NULL,
                    expected_generation TEXT,
                    expected_tombstone INTEGER NOT NULL DEFAULT 0,
                    vector_revision TEXT,
                    expected_vector_revision TEXT,
                    status TEXT NOT NULL,
                    error_code TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )

    def stage_generation(
        self,
        document_name: str,
        generation_id: str,
        source_path: Path | str,
        content_hash: str,
        chunks: Iterable[DocumentChunk],
        *,
        document_source: DocumentSource,
        parser_version: str,
        chunker_version: str,
        metadata: Mapping[str, object] | None = None,
    ) -> str | None:
        """Persist an immutable, query-invisible generation and its operation."""
        encoded_document_metadata = json.dumps(
            dict(metadata or {}), ensure_ascii=False, allow_nan=False, sort_keys=True
        )
        normalized: list[tuple[DocumentChunk, str]] = []
        seen: set[str] = set()
        for chunk in chunks:
            if chunk.document_id != document_name:
                raise ValueError("document chunk belongs to a different document")
            if not chunk.chunk_id or chunk.chunk_id in seen:
                raise ValueError("generation chunk IDs must be non-empty and unique")
            seen.add(chunk.chunk_id)
            content = _clean_text(chunk.content)
            if content:
                normalized.append((chunk, content))
        operation_id = f"replace:{generation_id}"
        with self._connect(immediate=True) as conn:
            head = conn.execute(
                "SELECT active_generation, tombstoned FROM rag_index_heads WHERE document_name = ?",
                (document_name,),
            ).fetchone()
            current = head["active_generation"] if head and not head["tombstoned"] else None
            if current == generation_id:
                return None
            existing = conn.execute(
                "SELECT state FROM rag_index_generations WHERE document_name = ? AND generation_id = ?",
                (document_name, generation_id),
            ).fetchone()
            if existing is not None and existing["state"] == "cleaning":
                raise RuntimeError("generation cleanup has started and cannot be restaged")
            if existing is None:
                conn.execute(
                    """INSERT INTO rag_index_generations (
                           document_name, generation_id, source_path, content_hash,
                           parser_version, chunker_version, source_id, source_uri,
                           source_type, corpus_id, pack_id, pack_version, game_version,
                           source_revision, license_id, metadata_json, state
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'building')""",
                    (
                        document_name, generation_id, str(source_path), content_hash,
                        parser_version, chunker_version, document_source.source_id,
                        document_source.source_uri, document_source.source_type,
                        document_source.corpus_id, document_source.pack_id,
                        document_source.pack_version, document_source.game_version,
                        document_source.source_revision, document_source.license_id,
                        encoded_document_metadata,
                    ),
                )
            else:
                # A failed deterministic generation may be retried safely. Its
                # rows remain invisible until the active pointer is switched.
                conn.execute(
                    "DELETE FROM rag_index_generation_fts WHERE document_name = ? AND generation_id = ?",
                    (document_name, generation_id),
                )
                conn.execute(
                    "DELETE FROM rag_index_generation_chunks WHERE document_name = ? AND generation_id = ?",
                    (document_name, generation_id),
                )
                conn.execute(
                    "UPDATE rag_index_generations SET source_path = ?, content_hash = ?, parser_version = ?, "
                    "chunker_version = ?, source_id = ?, source_uri = ?, source_type = ?, corpus_id = ?, "
                    "pack_id = ?, pack_version = ?, game_version = ?, source_revision = ?, license_id = ?, "
                    "metadata_json = ?, state = 'building' WHERE document_name = ? AND generation_id = ?",
                    (
                        str(source_path), content_hash, parser_version, chunker_version,
                        document_source.source_id, document_source.source_uri,
                        document_source.source_type, document_source.corpus_id,
                        document_source.pack_id, document_source.pack_version,
                        document_source.game_version, document_source.source_revision,
                        document_source.license_id, encoded_document_metadata,
                        document_name, generation_id,
                    ),
                )
                conn.execute(
                    "UPDATE rag_index_operations SET status = 'complete', error_code = NULL, "
                    "updated_at = datetime('now') WHERE operation_id = ? "
                    "AND operation = 'cleanup' AND status = 'pending_cleanup'",
                    (f"cleanup:{document_name}:{generation_id}",),
                )
            for chunk, content in normalized:
                locator = chunk.locator
                chunk_metadata = json.dumps(
                    dict(chunk.metadata), ensure_ascii=False, allow_nan=False, sort_keys=True
                )
                section_path = json.dumps(list(chunk.section_path), ensure_ascii=False)
                conn.execute(
                    """INSERT INTO rag_index_generation_chunks (
                           document_name, generation_id, chunk_id, source, page, title,
                           content, locator_kind, start_line, end_line, locator_page,
                           anchor, section_path_json, metadata_json
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        document_name, generation_id, chunk.chunk_id,
                        document_source.source_uri, locator.page or 0, chunk.title,
                        content, locator.kind.value, locator.start_line, locator.end_line,
                        locator.page, locator.anchor, section_path, chunk_metadata,
                    ),
                )
                conn.execute(
                    """INSERT INTO rag_index_generation_fts (
                           chunk_id, document_name, generation_id, title, content
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (chunk.chunk_id, document_name, generation_id, chunk.title, content),
                )
            conn.execute(
                """INSERT INTO rag_index_operations (
                       operation_id, document_name, generation_id, operation,
                       expected_generation, expected_tombstone, status, error_code
                   ) VALUES (?, ?, ?, 'replace', ?, ?, 'building', NULL)
                   ON CONFLICT(operation_id) DO UPDATE SET
                       expected_generation = excluded.expected_generation,
                       expected_tombstone = excluded.expected_tombstone,
                       status = 'building', error_code = NULL, updated_at = datetime('now')""",
                (operation_id, document_name, generation_id, current, int(bool(head and head["tombstoned"]))),
            )
        return operation_id

    def generation_snapshot(
        self, document_name: str, generation_id: str
    ) -> tuple[DocumentSource, str, str, str, str, list[DocumentChunk]] | None:
        """Load one staged generation for vector build/recovery."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM rag_index_generations WHERE document_name = ? AND generation_id = ?",
                (document_name, generation_id),
            ).fetchone()
            if row is None:
                return None
            chunk_rows = conn.execute(
                "SELECT * FROM rag_index_generation_chunks WHERE document_name = ? AND generation_id = ? ORDER BY chunk_id",
                (document_name, generation_id),
            ).fetchall()
        source = DocumentSource(
            source_id=row["source_id"], source_uri=row["source_uri"],
            source_type=row["source_type"], corpus_id=row["corpus_id"],
            pack_id=row["pack_id"], pack_version=row["pack_version"],
            game_version=row["game_version"], source_revision=row["source_revision"],
            license_id=row["license_id"],
        )
        chunks = [_generation_chunk_from_row(item) for item in chunk_rows]
        return (
            source, row["content_hash"], row["source_path"],
            row["parser_version"], row["chunker_version"], chunks,
        )

    def generation_chunk_ids(self, document_name: str, generation_id: str) -> frozenset[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT chunk_id FROM rag_index_generation_chunks WHERE document_name = ? AND generation_id = ?",
                (document_name, generation_id),
            ).fetchall()
        return frozenset(str(row["chunk_id"]) for row in rows)

    def validate_generation(
        self, document_name: str, generation_id: str, expected_ids: frozenset[str]
    ) -> bool:
        """Verify staged rows and FTS contain exactly the expected chunk IDs."""
        with self._connect() as conn:
            chunk_ids = frozenset(
                str(row["chunk_id"])
                for row in conn.execute(
                    "SELECT chunk_id FROM rag_index_generation_chunks "
                    "WHERE document_name = ? AND generation_id = ?",
                    (document_name, generation_id),
                ).fetchall()
            )
            fts_ids = frozenset(
                str(row["chunk_id"])
                for row in conn.execute(
                    "SELECT chunk_id FROM rag_index_generation_fts "
                    "WHERE document_name = ? AND generation_id = ?",
                    (document_name, generation_id),
                ).fetchall()
            )
        return chunk_ids == expected_ids and fts_ids == expected_ids

    def mark_generation_vector_ready(self, operation_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE rag_index_operations SET status = 'vector_ready', error_code = NULL, "
                "updated_at = datetime('now') WHERE operation_id = ? AND operation = 'replace'",
                (operation_id,),
            )

    def mark_generation_failed(self, operation_id: str, error_code: str) -> None:
        with self._connect() as conn:
            operation = conn.execute(
                "SELECT document_name, generation_id FROM rag_index_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            conn.execute(
                "UPDATE rag_index_operations SET status = 'failed', error_code = ?, "
                "updated_at = datetime('now') WHERE operation_id = ? AND status != 'complete'",
                (error_code[:80], operation_id),
            )
            if operation is not None and operation["generation_id"] is not None:
                doc = str(operation["document_name"])
                generation = str(operation["generation_id"])
                conn.execute(
                    "UPDATE rag_index_generations SET state = 'pending_cleanup' "
                    "WHERE document_name = ? AND generation_id = ? "
                    "AND state NOT IN ('active', 'cleaning')",
                    (doc, generation),
                )
                conn.execute(
                    """INSERT INTO rag_index_operations (
                           operation_id, document_name, generation_id, operation,
                           status, error_code
                       ) VALUES (?, ?, ?, 'cleanup', 'pending_cleanup', ?)
                       ON CONFLICT(operation_id) DO UPDATE SET status = 'pending_cleanup',
                           error_code = excluded.error_code, updated_at = datetime('now')""",
                    (f"cleanup:{doc}:{generation}", doc, generation, error_code[:80]),
                )

    def activate_generation(self, operation_id: str) -> str | None:
        """Atomically switch the active pointer after vector validation."""
        with self._connect(immediate=True) as conn:
            op = conn.execute(
                "SELECT * FROM rag_index_operations WHERE operation_id = ? AND operation = 'replace'",
                (operation_id,),
            ).fetchone()
            if op is None or op["status"] not in {"vector_ready", "cleanup_pending", "complete"}:
                raise ValueError("generation operation is not ready to activate")
            doc = op["document_name"]
            generation = op["generation_id"]
            generation_row = conn.execute(
                "SELECT state FROM rag_index_generations WHERE document_name = ? AND generation_id = ?",
                (doc, generation),
            ).fetchone()
            if generation_row is None:
                raise ValueError("generation is no longer retained")
            head = conn.execute(
                "SELECT active_generation, tombstoned FROM rag_index_heads WHERE document_name = ?",
                (doc,),
            ).fetchone()
            current = head["active_generation"] if head and not head["tombstoned"] else None
            if current == generation:
                if generation_row["state"] != "active":
                    raise RuntimeError("generation is not in an activatable state")
                return op["expected_generation"]
            if generation_row["state"] != "building":
                raise RuntimeError("generation is not in an activatable state")
            was_tombstoned = bool(head and head["tombstoned"])
            if (
                current != op["expected_generation"]
                or was_tombstoned != bool(op["expected_tombstone"])
            ):
                raise RuntimeError("active generation changed while replacement was staged")
            if current is not None:
                conn.execute(
                    "UPDATE rag_index_generations SET state = 'pending_cleanup' "
                    "WHERE document_name = ? AND generation_id = ?",
                    (doc, current),
                )
            conn.execute(
                "UPDATE rag_index_generations SET state = 'active' WHERE document_name = ? AND generation_id = ?",
                (doc, generation),
            )
            conn.execute(
                "UPDATE rag_index_generations SET active_vector_revision = ? "
                "WHERE document_name = ? AND generation_id = ?",
                (generation, doc, generation),
            )
            conn.execute(
                """INSERT INTO rag_index_vector_revisions (
                       document_name, generation_id, vector_revision, state
                   ) VALUES (?, ?, ?, 'active')
                   ON CONFLICT(document_name, generation_id, vector_revision)
                   DO UPDATE SET state = 'active'""",
                (doc, generation, generation),
            )
            conn.execute(
                """INSERT INTO rag_index_heads (document_name, active_generation, tombstoned)
                   VALUES (?, ?, 0) ON CONFLICT(document_name) DO UPDATE SET
                       active_generation = excluded.active_generation, tombstoned = 0,
                       updated_at = datetime('now')""",
                (doc, generation),
            )
            conn.execute(
                "UPDATE rag_index_operations SET status = 'complete', error_code = NULL, updated_at = datetime('now') "
                "WHERE operation_id = ?",
                (operation_id,),
            )
            if current is not None:
                cleanup_id = f"cleanup:{doc}:{current}"
                conn.execute(
                    """INSERT INTO rag_index_operations (
                           operation_id, document_name, generation_id, operation,
                           expected_generation, status
                       ) VALUES (?, ?, ?, 'cleanup', ?, 'pending_cleanup')
                       ON CONFLICT(operation_id) DO UPDATE SET status = 'pending_cleanup',
                           error_code = NULL, updated_at = datetime('now')""",
                    (cleanup_id, doc, current, generation),
                )
            return current

    def tombstone_generation_document(self, document_name: str, operation_id: str) -> list[str]:
        """Hide a document immediately while retaining generations for cleanup/rollback."""
        with self._connect(immediate=True) as conn:
            head = conn.execute(
                "SELECT active_generation FROM rag_index_heads WHERE document_name = ?",
                (document_name,),
            ).fetchone()
            active = head["active_generation"] if head else None
            generations = [
                str(row["generation_id"])
                for row in conn.execute(
                    "SELECT generation_id FROM rag_index_generations WHERE document_name = ? "
                    "AND state != 'deleted' ORDER BY generation_id",
                    (document_name,),
                ).fetchall()
            ]
            conn.execute(
                """INSERT INTO rag_index_heads (document_name, active_generation, tombstoned)
                   VALUES (?, NULL, 1) ON CONFLICT(document_name) DO UPDATE SET
                       active_generation = NULL, tombstoned = 1, updated_at = datetime('now')""",
                (document_name,),
            )
            conn.execute(
                "UPDATE rag_index_generations SET state = 'pending_cleanup' WHERE document_name = ? "
                "AND state NOT IN ('deleted', 'cleaning')",
                (document_name,),
            )
            conn.execute(
                "UPDATE rag_index_vector_revisions SET state = 'pending_cleanup' "
                "WHERE document_name = ? AND state != 'deleted'",
                (document_name,),
            )
            for generation in generations:
                conn.execute(
                    """INSERT INTO rag_index_operations (
                           operation_id, document_name, generation_id, operation,
                           expected_generation, status
                       ) VALUES (?, ?, ?, 'cleanup', ?, 'pending_cleanup')
                       ON CONFLICT(operation_id) DO UPDATE SET status = 'pending_cleanup',
                           error_code = NULL, updated_at = datetime('now')""",
                    (f"cleanup:{document_name}:{generation}", document_name, generation, active),
                )
            conn.execute(
                """INSERT INTO rag_index_operations (
                       operation_id, document_name, generation_id, operation,
                       expected_generation, status
                   ) VALUES (?, ?, NULL, 'delete', ?, 'pending_cleanup')""",
                (operation_id, document_name, active),
            )
        return generations

    def pending_generation_operations(self) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT operation_id, document_name, generation_id, operation, "
                "expected_generation, vector_revision, expected_vector_revision, status "
                "FROM rag_index_operations "
                "WHERE status IN ('building', 'vector_ready', 'pending_cleanup') "
                "ORDER BY created_at, operation_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_generation_cleanup(self) -> list[tuple[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT document_name, generation_id FROM rag_index_generations "
                "WHERE state IN ('pending_cleanup', 'cleaning') "
                "ORDER BY document_name, generation_id"
            ).fetchall()
        return [(str(row["document_name"]), str(row["generation_id"])) for row in rows]

    def claim_generation_cleanup(self, document_name: str, generation_id: str) -> bool:
        """Persist cleanup intent before deleting a retained vector collection.

        Once cleanup is claimed, rollback refuses to reactivate this generation.
        A crash is recoverable because `cleaning` generations remain in the
        pending cleanup scan and all physical deletes are idempotent.
        """
        with self._connect(immediate=True) as conn:
            row = conn.execute(
                "SELECT state FROM rag_index_generations "
                "WHERE document_name = ? AND generation_id = ?",
                (document_name, generation_id),
            ).fetchone()
            if row is None or row["state"] not in {"pending_cleanup", "cleaning"}:
                return False
            head = conn.execute(
                "SELECT active_generation, tombstoned FROM rag_index_heads WHERE document_name = ?",
                (document_name,),
            ).fetchone()
            if head is not None and not head["tombstoned"] and head["active_generation"] == generation_id:
                return False
            conn.execute(
                "UPDATE rag_index_generations SET state = 'cleaning' "
                "WHERE document_name = ? AND generation_id = ? "
                "AND state IN ('pending_cleanup', 'cleaning')",
                (document_name, generation_id),
            )
            return True

    def cleanup_generation_rows(self, document_name: str, generation_id: str) -> None:
        with self._connect(immediate=True) as conn:
            generation = conn.execute(
                "SELECT state FROM rag_index_generations "
                "WHERE document_name = ? AND generation_id = ?",
                (document_name, generation_id),
            ).fetchone()
            if generation is None:
                return
            if generation["state"] not in {"pending_cleanup", "cleaning"}:
                raise RuntimeError("generation is no longer eligible for cleanup")
            head = conn.execute(
                "SELECT active_generation, tombstoned FROM rag_index_heads WHERE document_name = ?",
                (document_name,),
            ).fetchone()
            if head is not None and not head["tombstoned"] and head["active_generation"] == generation_id:
                raise RuntimeError("cannot clean an active generation")
            conn.execute(
                "DELETE FROM rag_index_generation_fts WHERE document_name = ? AND generation_id = ?",
                (document_name, generation_id),
            )
            conn.execute(
                "DELETE FROM rag_index_generation_chunks WHERE document_name = ? AND generation_id = ?",
                (document_name, generation_id),
            )
            conn.execute(
                "DELETE FROM rag_index_generations WHERE document_name = ? AND generation_id = ? "
                "AND state IN ('pending_cleanup', 'cleaning')",
                (document_name, generation_id),
            )
            conn.execute(
                "DELETE FROM rag_index_vector_revisions WHERE document_name = ? AND generation_id = ?",
                (document_name, generation_id),
            )
            conn.execute(
                "UPDATE rag_index_operations SET status = 'complete', error_code = NULL, "
                "updated_at = datetime('now') WHERE document_name = ? AND generation_id = ? "
                "AND operation = 'vector_cleanup' AND status = 'pending_cleanup'",
                (document_name, generation_id),
            )

    def mark_cleanup_complete(self, document_name: str, generation_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE rag_index_operations SET status = 'complete', error_code = NULL, "
                "updated_at = datetime('now') WHERE operation_id = ? "
                "AND operation = 'cleanup' AND status = 'pending_cleanup'",
                (f"cleanup:{document_name}:{generation_id}",),
            )

    def mark_delete_complete(self, operation_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE rag_index_operations SET status = 'complete', error_code = NULL, "
                "updated_at = datetime('now') WHERE operation_id = ? AND operation = 'delete'",
                (operation_id,),
            )

    def mark_operation_error(self, operation_id: str, error_code: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE rag_index_operations SET error_code = ?, updated_at = datetime('now') "
                "WHERE operation_id = ? AND status != 'complete'",
                (error_code[:80], operation_id),
            )

    def complete_deletes_without_pending_generations(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE rag_index_operations SET status = 'complete', error_code = NULL,
                          updated_at = datetime('now')
                   WHERE operation = 'delete' AND status = 'pending_cleanup'
                     AND NOT EXISTS (
                         SELECT 1 FROM rag_index_generations g
                         WHERE g.document_name = rag_index_operations.document_name
                           AND g.state IN ('pending_cleanup', 'cleaning')
                     )"""
            )

    def index_lifecycle_rows(self, document_name: str | None = None) -> list[dict[str, object]]:
        clauses = "WHERE document_name = ?" if document_name is not None else ""
        params = (document_name,) if document_name is not None else ()
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT operation_id, document_name, generation_id, operation, status, error_code, "
                f"vector_revision, expected_vector_revision, created_at, updated_at "
                f"FROM rag_index_operations {clauses} ORDER BY created_at, operation_id",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def operation_expected_generation(self, operation_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT expected_generation FROM rag_index_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return row["expected_generation"] if row is not None else None

    def generation_vector_revision(self, document_name: str, generation_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT active_vector_revision FROM rag_index_generations "
                "WHERE document_name = ? AND generation_id = ?",
                (document_name, generation_id),
            ).fetchone()
        return row["active_vector_revision"] if row is not None else None

    def vector_revisions_for_generation(
        self, document_name: str, generation_id: str
    ) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT vector_revision FROM rag_index_vector_revisions "
                "WHERE document_name = ? AND generation_id = ? ORDER BY vector_revision",
                (document_name, generation_id),
            ).fetchall()
        return [str(row["vector_revision"]) for row in rows]

    def begin_vector_rebuild(
        self, document_name: str, generation_id: str
    ) -> tuple[str, str, str | None]:
        """Create or resume an isolated shadow-vector rebuild for an active generation."""
        with self._connect(immediate=True) as conn:
            head = conn.execute(
                "SELECT active_generation, tombstoned FROM rag_index_heads WHERE document_name = ?",
                (document_name,),
            ).fetchone()
            if head is None or head["tombstoned"] or head["active_generation"] != generation_id:
                raise RuntimeError("vector rebuild target is no longer active")
            current_row = conn.execute(
                "SELECT active_vector_revision FROM rag_index_generations "
                "WHERE document_name = ? AND generation_id = ?",
                (document_name, generation_id),
            ).fetchone()
            if current_row is None:
                raise ValueError("active generation metadata is missing")
            current = current_row["active_vector_revision"]
            pending = conn.execute(
                """SELECT operation_id, vector_revision, expected_vector_revision FROM rag_index_operations
                   WHERE document_name = ? AND generation_id = ? AND operation = 'vector_rebuild'
                     AND status IN ('building', 'vector_ready')
                   ORDER BY created_at DESC LIMIT 1""",
                (document_name, generation_id),
            ).fetchone()
            if pending is not None and pending["expected_vector_revision"] == current:
                return str(pending["vector_revision"]), str(pending["operation_id"]), current
            count_row = conn.execute(
                "SELECT COUNT(*) AS revision_count FROM rag_index_vector_revisions "
                "WHERE document_name = ? AND generation_id = ?",
                (document_name, generation_id),
            ).fetchone()
            revision = f"{generation_id}-r{int(count_row['revision_count']) + 1}"
            operation_id = f"vector-rebuild:{document_name}:{generation_id}:{revision}"
            conn.execute(
                """INSERT INTO rag_index_vector_revisions (
                       document_name, generation_id, vector_revision, state
                   ) VALUES (?, ?, ?, 'building')""",
                (document_name, generation_id, revision),
            )
            conn.execute(
                """INSERT INTO rag_index_operations (
                       operation_id, document_name, generation_id, operation,
                       vector_revision, expected_vector_revision, status
                   ) VALUES (?, ?, ?, 'vector_rebuild', ?, ?, 'building')""",
                (operation_id, document_name, generation_id, revision, current),
            )
        return revision, operation_id, current

    def mark_vector_rebuild_ready(self, operation_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE rag_index_operations SET status = 'vector_ready', error_code = NULL, "
                "updated_at = datetime('now') WHERE operation_id = ? AND operation = 'vector_rebuild' "
                "AND status IN ('building', 'vector_ready')",
                (operation_id,),
            )

    def activate_vector_revision(self, operation_id: str) -> str | None:
        """Atomically publish a verified shadow collection for the active document generation."""
        with self._connect(immediate=True) as conn:
            op = conn.execute(
                "SELECT * FROM rag_index_operations WHERE operation_id = ? AND operation = 'vector_rebuild'",
                (operation_id,),
            ).fetchone()
            if op is None or op["status"] not in {"vector_ready", "complete"}:
                raise ValueError("vector rebuild operation is not ready to activate")
            document_name = str(op["document_name"])
            generation_id = str(op["generation_id"])
            revision = str(op["vector_revision"])
            head = conn.execute(
                "SELECT active_generation, tombstoned FROM rag_index_heads WHERE document_name = ?",
                (document_name,),
            ).fetchone()
            if head is None or head["tombstoned"] or head["active_generation"] != generation_id:
                raise RuntimeError("active document generation changed during vector rebuild")
            current_row = conn.execute(
                "SELECT active_vector_revision FROM rag_index_generations "
                "WHERE document_name = ? AND generation_id = ?",
                (document_name, generation_id),
            ).fetchone()
            current = current_row["active_vector_revision"] if current_row else None
            if current == revision:
                return current
            if current != op["expected_vector_revision"]:
                raise RuntimeError("active vector revision changed during rebuild")
            if current is not None:
                conn.execute(
                    "UPDATE rag_index_vector_revisions SET state = 'pending_cleanup' "
                    "WHERE document_name = ? AND generation_id = ? AND vector_revision = ?",
                    (document_name, generation_id, current),
                )
                cleanup_id = f"vector-cleanup:{document_name}:{generation_id}:{current}"
                conn.execute(
                    """INSERT INTO rag_index_operations (
                           operation_id, document_name, generation_id, operation,
                           vector_revision, expected_vector_revision, status
                       ) VALUES (?, ?, ?, 'vector_cleanup', ?, ?, 'pending_cleanup')
                       ON CONFLICT(operation_id) DO UPDATE SET status = 'pending_cleanup',
                           error_code = NULL, updated_at = datetime('now')""",
                    (cleanup_id, document_name, generation_id, current, revision),
                )
            conn.execute(
                "UPDATE rag_index_vector_revisions SET state = 'active' "
                "WHERE document_name = ? AND generation_id = ? AND vector_revision = ?",
                (document_name, generation_id, revision),
            )
            conn.execute(
                "UPDATE rag_index_generations SET active_vector_revision = ? "
                "WHERE document_name = ? AND generation_id = ?",
                (revision, document_name, generation_id),
            )
            conn.execute(
                "UPDATE rag_index_operations SET status = 'complete', error_code = NULL, "
                "updated_at = datetime('now') WHERE operation_id = ?",
                (operation_id,),
            )
            return current

    def mark_vector_rebuild_failed(self, operation_id: str, error_code: str) -> None:
        with self._connect(immediate=True) as conn:
            op = conn.execute(
                "SELECT document_name, generation_id, vector_revision, status FROM rag_index_operations "
                "WHERE operation_id = ? AND operation = 'vector_rebuild'",
                (operation_id,),
            ).fetchone()
            if op is None or op["status"] == "complete":
                return
            active = conn.execute(
                "SELECT active_vector_revision FROM rag_index_generations "
                "WHERE document_name = ? AND generation_id = ?",
                (op["document_name"], op["generation_id"]),
            ).fetchone()
            if active is not None and active["active_vector_revision"] == op["vector_revision"]:
                # Another recovery worker may already have published this
                # collection. A late failure must not queue the live vectors.
                return
            conn.execute(
                "UPDATE rag_index_operations SET status = 'failed', error_code = ?, "
                "updated_at = datetime('now') WHERE operation_id = ?",
                (error_code[:80], operation_id),
            )
            conn.execute(
                "UPDATE rag_index_vector_revisions SET state = 'pending_cleanup' "
                "WHERE document_name = ? AND generation_id = ? AND vector_revision = ?",
                (op["document_name"], op["generation_id"], op["vector_revision"]),
            )
            cleanup_id = (
                f"vector-cleanup:{op['document_name']}:{op['generation_id']}:{op['vector_revision']}"
            )
            conn.execute(
                """INSERT INTO rag_index_operations (
                       operation_id, document_name, generation_id, operation,
                       vector_revision, status, error_code
                   ) VALUES (?, ?, ?, 'vector_cleanup', ?, 'pending_cleanup', ?)
                   ON CONFLICT(operation_id) DO UPDATE SET status = 'pending_cleanup',
                       error_code = excluded.error_code, updated_at = datetime('now')""",
                (cleanup_id, op["document_name"], op["generation_id"], op["vector_revision"], error_code[:80]),
            )

    def pending_vector_cleanup(self) -> list[tuple[str, str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT document_name, generation_id, vector_revision "
                "FROM rag_index_vector_revisions WHERE state = 'pending_cleanup' "
                "ORDER BY document_name, generation_id, vector_revision"
            ).fetchall()
        return [
            (str(row["document_name"]), str(row["generation_id"]), str(row["vector_revision"]))
            for row in rows
        ]

    def cleanup_vector_revision_row(
        self, document_name: str, generation_id: str, vector_revision: str
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM rag_index_vector_revisions WHERE document_name = ? "
                "AND generation_id = ? AND vector_revision = ? AND state = 'pending_cleanup'",
                (document_name, generation_id, vector_revision),
            )
            conn.execute(
                "UPDATE rag_index_operations SET status = 'complete', error_code = NULL, "
                "updated_at = datetime('now') WHERE operation_id = ? "
                "AND operation = 'vector_cleanup' AND status = 'pending_cleanup'",
                (f"vector-cleanup:{document_name}:{generation_id}:{vector_revision}",),
            )

    def active_generation_ids(self, document_name: str | None = None) -> list[tuple[str, str]]:
        clauses = "WHERE h.tombstoned = 0 AND h.active_generation IS NOT NULL"
        params: tuple[object, ...] = ()
        if document_name is not None:
            clauses += " AND h.document_name = ?"
            params = (document_name,)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT h.document_name, h.active_generation FROM rag_index_heads h {clauses} ORDER BY h.document_name",
                params,
            ).fetchall()
        return [(str(row["document_name"]), str(row["active_generation"])) for row in rows]

    def generation_head(self, document_name: str) -> tuple[str | None, bool] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT active_generation, tombstoned FROM rag_index_heads WHERE document_name = ?",
                (document_name,),
            ).fetchone()
        if row is None:
            return None
        return row["active_generation"], bool(row["tombstoned"])

    def rollback_generation(
        self,
        document_name: str,
        generation_id: str,
        *,
        expected_current_generation: str | None,
        expected_tombstone: bool = False,
    ) -> str | None:
        """Point to a retained generation; callers must verify its vector set first."""
        with self._connect(immediate=True) as conn:
            target = conn.execute(
                "SELECT state FROM rag_index_generations WHERE document_name = ? AND generation_id = ?",
                (document_name, generation_id),
            ).fetchone()
            if target is None or target["state"] in {"deleted", "cleaning"}:
                raise ValueError("requested rollback generation is not retained")
            head = conn.execute(
                "SELECT active_generation, tombstoned FROM rag_index_heads WHERE document_name = ?",
                (document_name,),
            ).fetchone()
            if head is not None and head["tombstoned"]:
                raise RuntimeError("cannot roll back a tombstoned document")
            previous = head["active_generation"] if head and not head["tombstoned"] else None
            was_tombstoned = bool(head and head["tombstoned"])
            if previous != expected_current_generation or was_tombstoned != expected_tombstone:
                raise RuntimeError("active generation changed during rollback validation")
            if previous == generation_id:
                return previous
            if previous is not None:
                conn.execute(
                    "UPDATE rag_index_generations SET state = 'pending_cleanup' "
                    "WHERE document_name = ? AND generation_id = ?",
                    (document_name, previous),
                )
                conn.execute(
                    """INSERT INTO rag_index_operations (
                           operation_id, document_name, generation_id, operation,
                           expected_generation, status
                       ) VALUES (?, ?, ?, 'cleanup', ?, 'pending_cleanup')
                       ON CONFLICT(operation_id) DO UPDATE SET status = 'pending_cleanup',
                           error_code = NULL, updated_at = datetime('now')""",
                    (f"cleanup:{document_name}:{previous}", document_name, previous, generation_id),
                )
            conn.execute(
                "UPDATE rag_index_generations SET state = 'active' WHERE document_name = ? AND generation_id = ?",
                (document_name, generation_id),
            )
            conn.execute(
                """INSERT INTO rag_index_heads (document_name, active_generation, tombstoned)
                   VALUES (?, ?, 0) ON CONFLICT(document_name) DO UPDATE SET
                       active_generation = excluded.active_generation, tombstoned = 0,
                       updated_at = datetime('now')""",
                (document_name, generation_id),
            )
            return previous

    def replace_document(
        self,
        name: str,
        source_path: Path | str,
        content_hash: str,
        chunks: Iterable[tuple[str, int, str, str] | DocumentChunk],
        *,
        document_source: DocumentSource | None = None,
        parser_version: str = "legacy",
        chunker_version: str = "legacy",
        index_generation: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> int:
        source = document_source or DocumentSource(
            source_id=name,
            source_uri=str(source_path),
            source_type="legacy",
            corpus_id="legacy",
        )
        encoded_document_metadata = json.dumps(
            dict(metadata or {}), ensure_ascii=False, allow_nan=False, sort_keys=True
        )
        with self._connect() as conn:
            conn.execute("DELETE FROM rag_chunks_fts WHERE document_name = ?", (name,))
            conn.execute(
                "DELETE FROM rag_chunk_locators WHERE chunk_id IN "
                "(SELECT chunk_id FROM rag_chunks WHERE document_name = ?)",
                (name,),
            )
            conn.execute("DELETE FROM rag_chunks WHERE document_name = ?", (name,))
            conn.execute(
                """
                INSERT INTO rag_documents (name, source_path, content_hash)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    source_path = excluded.source_path,
                    content_hash = excluded.content_hash,
                    created_at = datetime('now')
                """,
                (name, str(source_path), content_hash),
            )
            conn.execute(
                """
                INSERT INTO rag_document_sources (
                    document_name, source_id, source_uri, source_type, corpus_id,
                    pack_id, pack_version, game_version, source_revision, license_id,
                    parser_version, chunker_version, index_generation, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_name) DO UPDATE SET
                    source_id = excluded.source_id,
                    source_uri = excluded.source_uri,
                    source_type = excluded.source_type,
                    corpus_id = excluded.corpus_id,
                    pack_id = excluded.pack_id,
                    pack_version = excluded.pack_version,
                    game_version = excluded.game_version,
                    source_revision = excluded.source_revision,
                    license_id = excluded.license_id,
                    parser_version = excluded.parser_version,
                    chunker_version = excluded.chunker_version,
                    index_generation = excluded.index_generation,
                    metadata_json = excluded.metadata_json
                """,
                (
                    name,
                    source.source_id,
                    source.source_uri,
                    source.source_type,
                    source.corpus_id,
                    source.pack_id,
                    source.pack_version,
                    source.game_version,
                    source.source_revision,
                    source.license_id,
                    parser_version,
                    chunker_version,
                    index_generation,
                    encoded_document_metadata,
                ),
            )
            count = 0
            for chunk in chunks:
                if isinstance(chunk, DocumentChunk):
                    if chunk.document_id != name:
                        raise ValueError("document chunk belongs to a different document")
                    chunk_id = chunk.chunk_id
                    page = chunk.locator.page or 0
                    title = chunk.title
                    content = chunk.content
                    locator = chunk.locator
                    section_path = chunk.section_path
                    chunk_generation = chunk.generation
                    chunk_metadata = chunk.metadata
                else:
                    chunk_id, page, title, content = chunk
                    locator = None
                    section_path = ()
                    chunk_generation = None
                    chunk_metadata = {}
                content = _clean_text(content)
                if not content:
                    continue
                conn.execute(
                    """
                    INSERT INTO rag_chunks (document_name, chunk_id, source, page, title, content)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (name, chunk_id, source.source_uri, page, title, content),
                )
                conn.execute(
                    """
                    INSERT INTO rag_chunks_fts (chunk_id, document_name, title, content)
                    VALUES (?, ?, ?, ?)
                    """,
                    (chunk_id, name, title, content),
                )
                if locator is not None:
                    conn.execute(
                        """
                        INSERT INTO rag_chunk_locators (
                            chunk_id, locator_kind, start_line, end_line, page, anchor,
                            section_path_json, generation, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            chunk_id,
                            locator.kind.value,
                            locator.start_line,
                            locator.end_line,
                            locator.page,
                            locator.anchor,
                            json.dumps(list(section_path), ensure_ascii=False),
                            chunk_generation or index_generation,
                            json.dumps(dict(chunk_metadata), ensure_ascii=False, allow_nan=False, sort_keys=True),
                        ),
                    )
                count += 1
            return count

    def delete_document(self, name: str) -> int:
        """Delete one document's SQLite rows and FTS entries in one transaction."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS chunk_count FROM rag_chunks WHERE document_name = ?",
                (name,),
            ).fetchone()
            conn.execute("DELETE FROM rag_chunks_fts WHERE document_name = ?", (name,))
            conn.execute(
                "DELETE FROM rag_chunk_locators WHERE chunk_id IN "
                "(SELECT chunk_id FROM rag_chunks WHERE document_name = ?)",
                (name,),
            )
            conn.execute("DELETE FROM rag_chunks WHERE document_name = ?", (name,))
            conn.execute("DELETE FROM rag_documents WHERE name = ?", (name,))
            conn.execute("DELETE FROM rag_document_sources WHERE document_name = ?", (name,))
            return int(row["chunk_count"])

    def index_status(self, document_name: str) -> DocumentIndexStatus:
        """Report whether a document is absent, present-but-empty, or indexed."""
        with self._connect() as conn:
            active = conn.execute(
                """SELECT g.document_name AS document_id, g.generation_id AS index_generation,
                          g.active_vector_revision AS vector_revision,
                          g.content_hash, g.parser_version, g.chunker_version,
                          g.source_id, g.source_uri, g.source_type, g.corpus_id, g.pack_id,
                          g.pack_version, g.game_version, g.source_revision, g.license_id,
                          COUNT(c.chunk_id) AS chunk_count
                   FROM rag_index_heads h
                   JOIN rag_index_generations g
                     ON g.document_name = h.document_name AND g.generation_id = h.active_generation
                   LEFT JOIN rag_index_generation_chunks c
                     ON c.document_name = g.document_name AND c.generation_id = g.generation_id
                   WHERE h.document_name = ? AND h.tombstoned = 0
                   GROUP BY g.document_name, g.generation_id""",
                (document_name,),
            ).fetchone()
            if active is not None:
                return _document_status_from_row(active)
            managed = conn.execute(
                "SELECT 1 FROM rag_index_heads WHERE document_name = ?",
                (document_name,),
            ).fetchone()
            if managed is not None:
                return DocumentIndexStatus(document_id=document_name, state=IndexState.NOT_INDEXED)
            row = conn.execute(
                """
                SELECT d.name AS document_id, d.content_hash, COUNT(c.chunk_id) AS chunk_count,
                       s.source_id, s.source_uri, s.source_type, s.corpus_id,
                       s.pack_id, s.pack_version, s.game_version, s.source_revision,
                       s.license_id, s.parser_version, s.chunker_version, s.index_generation
                FROM rag_documents d
                LEFT JOIN rag_chunks c ON c.document_name = d.name
                LEFT JOIN rag_document_sources s ON s.document_name = d.name
                WHERE d.name = ? AND NOT EXISTS (
                    SELECT 1 FROM rag_index_heads h WHERE h.document_name = d.name
                )
                GROUP BY d.name
                """,
                (document_name,),
            ).fetchone()
        if row is None:
            return DocumentIndexStatus(document_id=document_name, state=IndexState.NOT_INDEXED)
        return _document_status_from_row(row)

    def document_catalog(self) -> list[DocumentIndexStatus]:
        """Return indexed document metadata for availability / version checks."""
        with self._connect() as conn:
            legacy_rows = conn.execute(
                """
                SELECT d.name AS document_id, d.content_hash, COUNT(c.chunk_id) AS chunk_count,
                       s.source_id, s.source_uri, s.source_type, s.corpus_id,
                       s.pack_id, s.pack_version, s.game_version, s.source_revision,
                       s.license_id, s.parser_version, s.chunker_version, s.index_generation
                FROM rag_documents d
                LEFT JOIN rag_chunks c ON c.document_name = d.name
                LEFT JOIN rag_document_sources s ON s.document_name = d.name
                WHERE NOT EXISTS (
                    SELECT 1 FROM rag_index_heads h WHERE h.document_name = d.name
                )
                GROUP BY d.name
                ORDER BY d.name
                """
            ).fetchall()
            generation_rows = conn.execute(
                """SELECT g.document_name AS document_id, g.generation_id AS index_generation,
                          g.active_vector_revision AS vector_revision,
                          g.content_hash, g.parser_version, g.chunker_version,
                          g.source_id, g.source_uri, g.source_type, g.corpus_id, g.pack_id,
                          g.pack_version, g.game_version, g.source_revision, g.license_id,
                          COUNT(c.chunk_id) AS chunk_count
                   FROM rag_index_heads h
                   JOIN rag_index_generations g
                     ON g.document_name = h.document_name AND g.generation_id = h.active_generation
                   LEFT JOIN rag_index_generation_chunks c
                     ON c.document_name = g.document_name AND c.generation_id = g.generation_id
                   WHERE h.tombstoned = 0
                   GROUP BY g.document_name, g.generation_id
                   ORDER BY g.document_name"""
            ).fetchall()
        return [_document_status_from_row(row) for row in (*legacy_rows, *generation_rows)]

    def search(
        self,
        query: str,
        document_name: str | None = None,
        limit: int = 4,
        *,
        filters: DocumentFilter | None = None,
    ) -> list[RagChunk]:
        query = query.strip()
        if not query or limit <= 0:
            return []
        if filters is not None and filters.document_id is not None:
            if document_name is not None and document_name != filters.document_id:
                return []
            document_name = filters.document_id
        terms = normalize_query(query)
        results: list[RagChunk] = []
        seen: set[str] = set()
        if terms:
            try:
                results.extend(
                    self._search_fts(terms, document_name=document_name, limit=limit * 2, filters=filters)
                )
                seen.update(item.chunk_id for item in results)
            except sqlite3.Error:
                results = []
                seen = set()
        if len(results) < limit:
            for item in self._search_fallback(
                query, document_name=document_name, limit=limit * 4, filters=filters
            ):
                if item.chunk_id not in seen:
                    results.append(item)
                    seen.add(item.chunk_id)
                if len(results) >= limit:
                    break
        generation_results = self._search_active_generations(
            query, document_name=document_name, limit=limit, filters=filters
        )
        results.extend(generation_results)
        return sorted(results, key=lambda item: item.score, reverse=True)[:limit]

    def _search_active_generations(
        self,
        query: str,
        *,
        document_name: str | None,
        limit: int,
        filters: DocumentFilter | None,
    ) -> list[RagChunk]:
        terms = normalize_query(query)
        results: list[RagChunk] = []
        if terms:
            try:
                results = self._search_generation_fts(
                    terms, document_name=document_name, limit=limit * 2, filters=filters
                )
            except sqlite3.Error:
                results = []
        if len(results) < limit:
            seen = {item.chunk_id for item in results}
            for item in self._search_generation_fallback(
                query, document_name=document_name, limit=limit * 4, filters=filters
            ):
                if item.chunk_id not in seen:
                    results.append(item)
                    seen.add(item.chunk_id)
                if len(results) >= limit:
                    break
        return sorted(results, key=lambda item: item.score, reverse=True)[:limit]

    def _search_fts(
        self,
        terms: str,
        document_name: str | None,
        limit: int,
        filters: DocumentFilter | None = None,
    ) -> list[RagChunk]:
        clauses, filter_params = _document_filter_sql(document_name, filters)
        where = " AND " + " AND ".join(clauses) if clauses else ""
        params: list[object] = [terms, *filter_params, limit]
        sql = f"""
            SELECT c.chunk_id, c.document_name AS document_id, c.source, c.page,
                   c.title, c.content, bm25(f) AS rank,
                   d.content_hash, s.source_id, s.source_uri, s.source_type, s.corpus_id,
                   s.pack_id, s.pack_version, s.game_version, s.source_revision, s.license_id,
                   s.index_generation, s.parser_version, s.chunker_version,
                   s.metadata_json AS source_metadata_json,
                   l.locator_kind, l.start_line, l.end_line, l.page AS locator_page,
                   l.anchor, l.section_path_json, l.generation AS chunk_generation,
                   l.metadata_json AS chunk_metadata_json
            FROM rag_chunks_fts f
            JOIN rag_chunks c ON c.chunk_id = f.chunk_id
            JOIN rag_documents d ON d.name = c.document_name
            LEFT JOIN rag_document_sources s ON s.document_name = c.document_name
            LEFT JOIN rag_chunk_locators l ON l.chunk_id = c.chunk_id
            WHERE rag_chunks_fts MATCH ? {where}
            ORDER BY rank
            LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            _rag_chunk_from_row(row, 1.0 / (1.0 + max(float(row["rank"]), 0.0)))
            for row in rows
        ]

    def _search_generation_fts(
        self,
        terms: str,
        *,
        document_name: str | None,
        limit: int,
        filters: DocumentFilter | None,
    ) -> list[RagChunk]:
        clauses, filter_params = _generation_filter_sql(document_name, filters)
        where = " AND " + " AND ".join(clauses) if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT c.chunk_id, c.document_name AS document_id, c.source, c.page,
                           c.title, c.content, bm25(f) AS rank,
                           g.content_hash, g.source_id, g.source_uri, g.source_type, g.corpus_id,
                           g.pack_id, g.pack_version, g.game_version, g.source_revision, g.license_id,
                           g.generation_id AS index_generation, g.parser_version, g.chunker_version,
                           g.metadata_json AS source_metadata_json,
                           c.locator_kind, c.start_line, c.end_line,
                           c.locator_page, c.anchor, c.section_path_json,
                           c.generation_id AS chunk_generation,
                           c.metadata_json AS chunk_metadata_json
                    FROM rag_index_generation_fts f
                    JOIN rag_index_generation_chunks c
                      ON c.chunk_id = f.chunk_id AND c.document_name = f.document_name
                     AND c.generation_id = f.generation_id
                    JOIN rag_index_heads h
                      ON h.document_name = c.document_name AND h.active_generation = c.generation_id
                     AND h.tombstoned = 0
                    JOIN rag_index_generations g
                      ON g.document_name = c.document_name AND g.generation_id = c.generation_id
                    WHERE rag_index_generation_fts MATCH ? {where}
                    ORDER BY rank LIMIT ?""",
                (terms, *filter_params, limit),
            ).fetchall()
        return [
            _rag_chunk_from_row(row, 1.0 / (1.0 + max(float(row["rank"]), 0.0)))
            for row in rows
        ]

    def _search_generation_fallback(
        self,
        query: str,
        *,
        document_name: str | None,
        limit: int,
        filters: DocumentFilter | None,
    ) -> list[RagChunk]:
        terms = _tokenize(query)
        if not terms:
            return []
        clauses, params = _generation_filter_sql(document_name, filters)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT c.chunk_id, c.document_name AS document_id, c.source, c.page,
                           c.title, c.content, g.content_hash,
                           g.source_id, g.source_uri, g.source_type, g.corpus_id,
                           g.pack_id, g.pack_version, g.game_version, g.source_revision,
                           g.license_id, g.generation_id AS index_generation,
                           g.parser_version, g.chunker_version,
                           g.metadata_json AS source_metadata_json,
                           c.locator_kind, c.start_line, c.end_line,
                           c.locator_page, c.anchor, c.section_path_json,
                           c.generation_id AS chunk_generation,
                           c.metadata_json AS chunk_metadata_json
                    FROM rag_index_generation_chunks c
                    JOIN rag_index_heads h
                      ON h.document_name = c.document_name AND h.active_generation = c.generation_id
                     AND h.tombstoned = 0
                    JOIN rag_index_generations g
                      ON g.document_name = c.document_name AND g.generation_id = c.generation_id
                    {where}""",
                params,
            ).fetchall()
        scored: list[RagChunk] = []
        for row in rows:
            haystack = f"{row['title']}\n{row['content']}".lower()
            score = sum(1.0 + math.log(haystack.count(term)) for term in terms if term in haystack)
            if score > 0:
                scored.append(_rag_chunk_from_row(row, score))
        return sorted(scored, key=lambda item: item.score, reverse=True)[:limit]

    def _search_fallback(
        self,
        query: str,
        document_name: str | None,
        limit: int,
        filters: DocumentFilter | None = None,
    ) -> list[RagChunk]:
        terms = _tokenize(query)
        if not terms:
            return []
        clauses, params = _document_filter_sql(document_name, filters)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT c.chunk_id, c.document_name AS document_id, c.source,
                       c.page, c.title, c.content,
                       d.content_hash, s.source_id, s.source_uri, s.source_type, s.corpus_id,
                       s.pack_id, s.pack_version, s.game_version, s.source_revision, s.license_id,
                       s.index_generation, s.parser_version, s.chunker_version,
                       s.metadata_json AS source_metadata_json,
                       l.locator_kind, l.start_line, l.end_line, l.page AS locator_page,
                       l.anchor, l.section_path_json, l.generation AS chunk_generation,
                       l.metadata_json AS chunk_metadata_json
                FROM rag_chunks c
                JOIN rag_documents d ON d.name = c.document_name
                LEFT JOIN rag_document_sources s ON s.document_name = c.document_name
                LEFT JOIN rag_chunk_locators l ON l.chunk_id = c.chunk_id
                {where}
                """,
                params,
            ).fetchall()
        scored: list[RagChunk] = []
        for row in rows:
            haystack = f"{row['title']}\n{row['content']}".lower()
            score = 0.0
            for term in terms:
                occurrences = haystack.count(term)
                if occurrences:
                    score += 1.0 + math.log(occurrences)
            if score <= 0:
                continue
            scored.append(_rag_chunk_from_row(row, score))
        return sorted(scored, key=lambda item: item.score, reverse=True)[:limit]


def _rag_chunk_from_row(row: sqlite3.Row, score: float) -> RagChunk:
    locator = None
    locator_kind = row["locator_kind"]
    if locator_kind:
        try:
            locator = DocumentLocator(
                kind=LocatorKind(str(locator_kind)),
                start_line=row["start_line"],
                end_line=row["end_line"],
                page=row["locator_page"],
                anchor=row["anchor"],
            )
        except (ValueError, TypeError):
            # Malformed optional metadata must not make legacy text unavailable.
            locator = None
    try:
        section_data = json.loads(row["section_path_json"] or "[]")
        section_path = tuple(str(value) for value in section_data) if isinstance(section_data, list) else ()
    except (json.JSONDecodeError, TypeError):
        section_path = ()
    try:
        source_metadata = json.loads(row["source_metadata_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        source_metadata = {}
    try:
        chunk_metadata = json.loads(row["chunk_metadata_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        chunk_metadata = {}
    metadata = {
        **(source_metadata if isinstance(source_metadata, dict) else {}),
        **(chunk_metadata if isinstance(chunk_metadata, dict) else {}),
    }
    if row["parser_version"] is not None:
        metadata.setdefault("parser_version", str(row["parser_version"]))
    if row["chunker_version"] is not None:
        metadata.setdefault("chunker_version", str(row["chunker_version"]))
    return RagChunk(
        chunk_id=str(row["chunk_id"]),
        document_id=str(row["document_id"]),
        source=str(row["source"]),
        page=int(row["page"]),
        title=str(row["title"]),
        content=str(row["content"]),
        score=score,
        source_id=row["source_id"],
        source_uri=row["source_uri"],
        source_type=row["source_type"],
        corpus_id=row["corpus_id"],
        pack_id=row["pack_id"],
        pack_version=row["pack_version"],
        game_version=row["game_version"],
        source_revision=row["source_revision"],
        license_id=row["license_id"],
        content_hash=row["content_hash"],
        generation=row["chunk_generation"] or row["index_generation"],
        section_path=section_path,
        locator=locator,
        metadata=metadata,
    )


def _generation_chunk_from_row(row: sqlite3.Row) -> DocumentChunk:
    metadata = json.loads(row["metadata_json"] or "{}")
    section_data = json.loads(row["section_path_json"] or "[]")
    return DocumentChunk(
        chunk_id=str(row["chunk_id"]),
        document_id=str(row["document_name"]),
        title=str(row["title"]),
        content=str(row["content"]),
        section_path=tuple(str(item) for item in section_data) if isinstance(section_data, list) else (),
        locator=DocumentLocator(
            kind=LocatorKind(str(row["locator_kind"] or LocatorKind.PAGE.value)),
            start_line=row["start_line"],
            end_line=row["end_line"],
            page=row["locator_page"],
            anchor=row["anchor"],
        ),
        generation=str(row["generation_id"]),
        metadata=metadata if isinstance(metadata, dict) else {},
    )


def _document_filter_sql(
    document_name: str | None,
    filters: DocumentFilter | None,
) -> tuple[list[str], list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    if document_name:
        clauses.append("c.document_name = ?")
        params.append(document_name)
    clauses.append(
        "NOT EXISTS (SELECT 1 FROM rag_index_heads h WHERE h.document_name = c.document_name)"
    )
    if filters is None:
        return clauses, params
    columns = (
        ("source_id", "s.source_id"),
        ("source_type", "s.source_type"),
        ("corpus_id", "s.corpus_id"),
        ("pack_id", "s.pack_id"),
        ("pack_version", "s.pack_version"),
        ("game_version", "s.game_version"),
        ("source_revision", "s.source_revision"),
    )
    for attribute, column in columns:
        value = getattr(filters, attribute)
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    return clauses, params


def _generation_filter_sql(
    document_name: str | None,
    filters: DocumentFilter | None,
) -> tuple[list[str], list[object]]:
    clauses: list[str] = ["h.tombstoned = 0", "h.active_generation = c.generation_id"]
    params: list[object] = []
    if document_name:
        clauses.append("c.document_name = ?")
        params.append(document_name)
    if filters is not None:
        columns = (
            ("source_id", "g.source_id"),
            ("source_type", "g.source_type"),
            ("corpus_id", "g.corpus_id"),
            ("pack_id", "g.pack_id"),
            ("pack_version", "g.pack_version"),
            ("game_version", "g.game_version"),
            ("source_revision", "g.source_revision"),
        )
        for attribute, column in columns:
            value = getattr(filters, attribute)
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
    return clauses, params


def _document_status_from_row(row: sqlite3.Row) -> DocumentIndexStatus:
    chunk_count = int(row["chunk_count"])
    source = None
    if row["source_id"] is not None:
        source = DocumentSource(
            source_id=str(row["source_id"]),
            source_uri=str(row["source_uri"]),
            source_type=str(row["source_type"]),
            corpus_id=str(row["corpus_id"]),
            pack_id=row["pack_id"],
            pack_version=row["pack_version"],
            game_version=row["game_version"],
            source_revision=row["source_revision"],
            license_id=row["license_id"],
        )
    return DocumentIndexStatus(
        document_id=str(row["document_id"]),
        state=IndexState.INDEXED if chunk_count else IndexState.EMPTY,
        chunk_count=chunk_count,
        content_hash=str(row["content_hash"]),
        source=source,
        parser_version=row["parser_version"],
        chunker_version=row["chunker_version"],
        generation=row["index_generation"],
        vector_revision=(
            row["vector_revision"] if "vector_revision" in row.keys() else None
        ),
    )

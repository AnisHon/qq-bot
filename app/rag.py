from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import math
import re
import sqlite3
from typing import Iterable


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+.#:-]*|[0-9]+|[\u4e00-\u9fff]{2,}")


@dataclass(slots=True)
class RagChunk:
    chunk_id: str
    source: str
    page: int
    title: str
    content: str
    score: float


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

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.sqlite_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

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

    def replace_document(
        self,
        name: str,
        source_path: Path,
        content_hash: str,
        chunks: Iterable[tuple[str, int, str, str]],
    ) -> int:
        with self._connect() as conn:
            conn.execute("DELETE FROM rag_chunks_fts WHERE document_name = ?", (name,))
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
            count = 0
            for chunk_id, page, title, content in chunks:
                content = _clean_text(content)
                if not content:
                    continue
                conn.execute(
                    """
                    INSERT INTO rag_chunks (document_name, chunk_id, source, page, title, content)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (name, chunk_id, str(source_path), page, title, content),
                )
                conn.execute(
                    """
                    INSERT INTO rag_chunks_fts (chunk_id, document_name, title, content)
                    VALUES (?, ?, ?, ?)
                    """,
                    (chunk_id, name, title, content),
                )
                count += 1
            return count

    def search(self, query: str, document_name: str | None = None, limit: int = 4) -> list[RagChunk]:
        query = query.strip()
        if not query:
            return []
        terms = normalize_query(query)
        results: list[RagChunk] = []
        seen: set[str] = set()
        if terms:
            try:
                results.extend(self._search_fts(terms, document_name=document_name, limit=limit * 2))
                seen.update(item.chunk_id for item in results)
            except sqlite3.Error:
                results = []
                seen = set()
        if len(results) < limit:
            for item in self._search_fallback(query, document_name=document_name, limit=limit * 4):
                if item.chunk_id not in seen:
                    results.append(item)
                    seen.add(item.chunk_id)
                if len(results) >= limit:
                    break
        return sorted(results, key=lambda item: item.score, reverse=True)[:limit]

    def _search_fts(self, terms: str, document_name: str | None, limit: int) -> list[RagChunk]:
        filters = []
        params: list[object] = [terms]
        if document_name:
            filters.append("c.document_name = ?")
            params.append(document_name)
        where = " AND " + " AND ".join(filters) if filters else ""
        sql = f"""
            SELECT c.chunk_id, c.source, c.page, c.title, c.content, bm25(f) AS rank
            FROM rag_chunks_fts f
            JOIN rag_chunks c ON c.chunk_id = f.chunk_id
            WHERE rag_chunks_fts MATCH ? {where}
            ORDER BY rank
            LIMIT ?
        """
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        chunks = []
        for row in rows:
            rank = float(row["rank"])
            chunks.append(
                RagChunk(
                    chunk_id=row["chunk_id"],
                    source=row["source"],
                    page=int(row["page"]),
                    title=row["title"],
                    content=row["content"],
                    score=1.0 / (1.0 + max(rank, 0.0)),
                )
            )
        return chunks

    def _search_fallback(self, query: str, document_name: str | None, limit: int) -> list[RagChunk]:
        terms = _tokenize(query)
        if not terms:
            return []
        params: list[object] = []
        where = ""
        if document_name:
            where = "WHERE document_name = ?"
            params.append(document_name)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT chunk_id, source, page, title, content
                FROM rag_chunks
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
            scored.append(
                RagChunk(
                    chunk_id=row["chunk_id"],
                    source=row["source"],
                    page=int(row["page"]),
                    title=row["title"],
                    content=row["content"],
                    score=score,
                )
            )
        return sorted(scored, key=lambda item: item.score, reverse=True)[:limit]

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from threading import RLock
import time
from uuid import uuid4

from app.context.contracts import ContextSnapshot, SummarySnapshot


_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]+")
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _history_search_tokens(text: str) -> tuple[str, ...]:
    """Build predictable FTS terms, including overlapping bigrams for CJK text."""
    tokens: list[str] = []
    for run in _CJK_RUN_RE.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index:index + 2] for index in range(len(run) - 1))
    tokens.extend(match.casefold() for match in _LATIN_TOKEN_RE.findall(text))
    return tuple(dict.fromkeys(token for token in tokens if token))


def _history_trigram_terms(text: str) -> tuple[str, ...]:
    terms = [run[index:index + 3] for run in _CJK_RUN_RE.findall(text) for index in range(len(run) - 2)]
    terms.extend(token.casefold() for token in _LATIN_TOKEN_RE.findall(text) if len(token) >= 3)
    return tuple(dict.fromkeys(terms))


def _fts_or_expression(terms: tuple[str, ...], limit: int = 32) -> str:
    # Terms are produced from local regex tokenizers, never copied as FTS syntax.
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms[:limit])


class MemoryStore:
    def __init__(
        self,
        sqlite_path: Path,
        history_limit: int = 20,
        *,
        history_max_messages_per_group: int = 20_000,
        history_max_bytes_per_group: int = 512 * 1024 * 1024,
        history_vector_enabled: bool = False,
        history_vector_embedding_version: str = "chat-default-v1",
    ):
        if history_max_messages_per_group < 1 or history_max_bytes_per_group < 1:
            raise ValueError("history capacity limits must be positive")
        self.sqlite_path = Path(sqlite_path)
        self.history_limit = history_limit
        self.history_max_messages_per_group = history_max_messages_per_group
        self.history_max_bytes_per_group = history_max_bytes_per_group
        self.history_vector_enabled = bool(history_vector_enabled)
        self.history_vector_embedding_version = history_vector_embedding_version
        self.chat_trigram_available = False
        self._lock = RLock()
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.sqlite_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS message_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    nickname TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            memory_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(memories)").fetchall()
            }
            for name, definition in (
                ("kind", "TEXT NOT NULL DEFAULT 'fact'"),
                ("status", "TEXT NOT NULL DEFAULT 'active'"),
                ("revision", "INTEGER NOT NULL DEFAULT 1"),
                ("verification", "TEXT NOT NULL DEFAULT 'legacy_unknown'"),
                ("confidence", "REAL"),
                ("normalized_key", "TEXT NOT NULL DEFAULT ''"),
                ("visibility", "TEXT NOT NULL DEFAULT 'legacy_unscoped'"),
                ("group_id", "TEXT"),
                ("owner_user_id", "TEXT"),
                ("valid_from", "TEXT"),
                ("valid_until", "TEXT"),
                ("supersedes_id", "INTEGER"),
                ("updated_at", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in memory_columns:
                    conn.execute(f"ALTER TABLE memories ADD COLUMN {name} {definition}")
            conn.execute(
                "UPDATE memories SET group_id=scope_id,visibility='group' "
                "WHERE scope='group' AND group_id IS NULL"
            )
            conn.execute(
                "UPDATE memories SET owner_user_id=scope_id,visibility='legacy_unscoped' "
                "WHERE scope='user' AND owner_user_id IS NULL"
            )
            conn.execute(
                "UPDATE memories SET updated_at=created_at WHERE updated_at=''"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_visibility_status_group "
                "ON memories(visibility,status,group_id,owner_user_id,id)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS memory_revisions ("
                "memory_id INTEGER NOT NULL, revision INTEGER NOT NULL, content TEXT NOT NULL, "
                "metadata_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')), "
                "PRIMARY KEY(memory_id,revision))"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS memory_sources ("
                "source_id TEXT PRIMARY KEY, memory_id INTEGER NOT NULL, revision INTEGER NOT NULL, "
                "source_kind TEXT NOT NULL, source_ref TEXT, source_hash TEXT, locator TEXT, "
                "verification TEXT NOT NULL, source_state TEXT NOT NULL DEFAULT 'active', "
                "observed_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_sources_memory_revision "
                "ON memory_sources(memory_id,revision,source_state)"
            )
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(search_text, tokenize='unicode61')"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS memory_index_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT OR IGNORE INTO memory_index_meta(key,value) VALUES('backfill_last_id','0')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO memory_index_meta(key,value) VALUES('backfill_complete','0')"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_summaries (
                    group_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    last_message_id INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 0,
                    epoch INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS episodic_memory ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,group_id TEXT NOT NULL,epoch INTEGER NOT NULL,"
                "start_message_id INTEGER NOT NULL,end_message_id INTEGER NOT NULL,schema_version INTEGER NOT NULL,"
                "topic TEXT NOT NULL,summary TEXT NOT NULL,decisions_json TEXT NOT NULL,"
                "open_questions_json TEXT NOT NULL,source_refs_json TEXT NOT NULL,source_hashes_json TEXT NOT NULL,"
                "revision INTEGER NOT NULL DEFAULT 1,state TEXT NOT NULL DEFAULT 'active',"
                "created_at TEXT NOT NULL DEFAULT (datetime('now')),"
                "UNIQUE(group_id,epoch,start_message_id,end_message_id))"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_episodic_memory_group_epoch_range "
                "ON episodic_memory(group_id,epoch,state,end_message_id)"
            )
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS episode_fts USING fts5(search_text, tokenize='unicode61')"
            )
            message_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(message_history)").fetchall()
            }
            for name, definition in (
                ("epoch", "INTEGER NOT NULL DEFAULT 0"),
                ("onebot_message_id", "TEXT"),
                ("reply_message_id", "TEXT"),
                ("request_id", "TEXT"),
                ("event_timestamp", "INTEGER"),
                ("history_visible", "INTEGER NOT NULL DEFAULT 1"),
                ("content_hash", "TEXT NOT NULL DEFAULT ''"),
                ("estimated_tokens", "INTEGER NOT NULL DEFAULT 0"),
                ("deleted_at", "TEXT"),
                ("history_vector_revision", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in message_columns:
                    conn.execute(f"ALTER TABLE message_history ADD COLUMN {name} {definition}")
            summary_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(conversation_summaries)").fetchall()
            }
            if "revision" not in summary_columns:
                conn.execute("ALTER TABLE conversation_summaries ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
            if "epoch" not in summary_columns:
                conn.execute("ALTER TABLE conversation_summaries ADD COLUMN epoch INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS context_groups (
                    group_id TEXT PRIMARY KEY,
                    epoch INTEGER NOT NULL DEFAULT 0,
                    summary_revision INTEGER NOT NULL DEFAULT 0,
                    summary_covered_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO context_groups(group_id, summary_revision, summary_covered_id)
                SELECT group_id, revision, last_message_id FROM conversation_summaries
                """
            )
            conn.execute(
                """
                UPDATE context_groups
                SET summary_revision = (
                        SELECT revision FROM conversation_summaries
                        WHERE conversation_summaries.group_id = context_groups.group_id
                    ),
                    summary_covered_id = (
                        SELECT last_message_id FROM conversation_summaries
                        WHERE conversation_summaries.group_id = context_groups.group_id
                    )
                WHERE EXISTS (
                    SELECT 1 FROM conversation_summaries
                    WHERE conversation_summaries.group_id = context_groups.group_id
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_message_history_group_onebot_id
                ON message_history(group_id, onebot_message_id)
                WHERE onebot_message_id IS NOT NULL AND onebot_message_id != ''
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    schedule_type TEXT NOT NULL,
                    interval_seconds INTEGER NOT NULL DEFAULT 0,
                    next_run_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_run_at INTEGER,
                    run_count INTEGER NOT NULL DEFAULT 0,
                    running_since INTEGER,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_message_history_group_id_id ON message_history(group_id, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_message_history_group_epoch_id "
                "ON message_history(group_id, epoch, history_visible, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_scope_scope_id_id ON memories(scope, scope_id, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_due ON scheduled_tasks(enabled, next_run_at)"
            )
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(scheduled_tasks)").fetchall()
            }
            for name, definition in (
                ("running_since", "INTEGER"),
                ("failure_count", "INTEGER NOT NULL DEFAULT 0"),
                ("last_error", "TEXT"),
            ):
                if name not in columns:
                    conn.execute(f"ALTER TABLE scheduled_tasks ADD COLUMN {name} {definition}")
            conn.execute(
                """
                UPDATE scheduled_tasks
                SET enabled = 0, running_since = NULL,
                    failure_count = failure_count + 1,
                    last_error = 'Bot restarted while this task was running; automatic retry is disabled.'
                WHERE enabled = 1 AND running_since IS NOT NULL
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS group_settings (
                    group_id TEXT NOT NULL,
                    setting_key TEXT NOT NULL,
                    setting_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (group_id, setting_key)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS history_purge_jobs (
                    group_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'pending',
                    hidden_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS history_vector_outbox (
                    operation_id TEXT PRIMARY KEY,
                    group_id TEXT NOT NULL,
                    row_id INTEGER NOT NULL,
                    epoch INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    embedding_version TEXT NOT NULL,
                    operation TEXT NOT NULL CHECK(operation IN ('upsert','delete')),
                    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','complete')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_vector_outbox_state "
                "ON history_vector_outbox(state,created_at)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS agent_tasks ("
                "task_id TEXT PRIMARY KEY,request_id TEXT NOT NULL,group_id TEXT NOT NULL,user_id TEXT NOT NULL,"
                "epoch INTEGER NOT NULL,goal TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN "
                "('running','completed','failed','cancelled','interrupted')),revision INTEGER NOT NULL DEFAULT 0,"
                "state_json TEXT NOT NULL,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,expires_at INTEGER NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_tasks_group_status "
                "ON agent_tasks(group_id,status,updated_at DESC)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS agent_checkpoints ("
                "task_id TEXT NOT NULL,revision INTEGER NOT NULL,state_json TEXT NOT NULL,"
                "created_at INTEGER NOT NULL,PRIMARY KEY(task_id,revision),"
                "FOREIGN KEY(task_id) REFERENCES agent_tasks(task_id) ON DELETE CASCADE)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS context_compaction_metrics ("
                "group_id TEXT NOT NULL,epoch INTEGER NOT NULL,compaction_count INTEGER NOT NULL DEFAULT 0,"
                "archived_tool_blocks INTEGER NOT NULL DEFAULT 0,dropped_context_items INTEGER NOT NULL DEFAULT 0,"
                "updated_at INTEGER NOT NULL,PRIMARY KEY(group_id,epoch))"
            )
            # A process restart is a recovery boundary, never an instruction to replay work.
            conn.execute(
                "UPDATE agent_tasks SET status='interrupted',updated_at=unixepoch() WHERE status='running'"
            )
            self._initialize_chat_search(conn)

    def _initialize_chat_search(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS context_index_meta "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chat_history_fts "
            "USING fts5(search_text, tokenize='unicode61')"
        )
        try:
            conn.execute("CREATE VIRTUAL TABLE temp.context_trigram_probe USING fts5(value, tokenize='trigram')")
            conn.execute("DROP TABLE temp.context_trigram_probe")
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chat_history_trigram "
                "USING fts5(content, tokenize='trigram')"
            )
            self.chat_trigram_available = True
        except sqlite3.OperationalError as exc:
            detail = str(exc).casefold()
            if "tokenizer" not in detail and "trigram" not in detail:
                raise
            self.chat_trigram_available = False
        conn.execute(
            "INSERT INTO context_index_meta(key,value) VALUES('trigram_available', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("1" if self.chat_trigram_available else "0",),
        )
        conn.execute(
            "INSERT OR IGNORE INTO context_index_meta(key,value) VALUES('backfill_last_id','0')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO context_index_meta(key,value) VALUES('backfill_complete','0')"
        )
        any_history = conn.execute("SELECT 1 FROM message_history LIMIT 1").fetchone()
        cursor = conn.execute(
            "SELECT value FROM context_index_meta WHERE key='backfill_last_id'"
        ).fetchone()
        if any_history is None and cursor is not None and str(cursor["value"]) == "0":
            conn.execute(
                "UPDATE context_index_meta SET value='1' WHERE key='backfill_complete'"
            )

    def _set_history_index(self, conn: sqlite3.Connection, row_id: int, content: str, visible: bool = True) -> None:
        conn.execute("DELETE FROM chat_history_fts WHERE rowid = ?", (int(row_id),))
        if self.chat_trigram_available:
            conn.execute("DELETE FROM chat_history_trigram WHERE rowid = ?", (int(row_id),))
        if not visible:
            return
        tokens = _history_search_tokens(content)
        if tokens:
            conn.execute(
                "INSERT INTO chat_history_fts(rowid, search_text) VALUES (?, ?)",
                (int(row_id), " ".join(tokens)),
            )
        if self.chat_trigram_available and content.strip():
            conn.execute(
                "INSERT INTO chat_history_trigram(rowid, content) VALUES (?, ?)",
                (int(row_id), content),
            )

    @staticmethod
    def _set_memory_index(conn: sqlite3.Connection, memory_id: int, content: str, visible: bool = True) -> None:
        conn.execute("DELETE FROM memory_fts WHERE rowid=?", (int(memory_id),))
        if not visible:
            return
        tokens = _history_search_tokens(content)
        if tokens:
            conn.execute(
                "INSERT INTO memory_fts(rowid,search_text) VALUES(?,?)",
                (int(memory_id), " ".join(tokens)),
            )

    @staticmethod
    def _invalidate_episodes_for_row(
        conn: sqlite3.Connection, group_id: str, epoch: int, row_id: int,
    ) -> None:
        rows = conn.execute(
            "SELECT id FROM episodic_memory WHERE group_id=? AND epoch=? AND state='active' "
            "AND start_message_id<=? AND end_message_id>=?",
            (str(group_id), int(epoch), int(row_id), int(row_id)),
        ).fetchall()
        for row in rows:
            conn.execute("UPDATE episodic_memory SET state='source_deleted' WHERE id=?", (int(row["id"]),))
            conn.execute("DELETE FROM episode_fts WHERE rowid=?", (int(row["id"]),))

    def _queue_history_vector(
        self,
        conn: sqlite3.Connection,
        *,
        group_id: str,
        row_id: int,
        epoch: int,
        content_hash: str,
        operation: str,
    ) -> None:
        """Record an optional Chroma mutation; SQLite remains the authority."""
        if not self.history_vector_enabled:
            return
        if operation not in {"upsert", "delete"}:
            raise ValueError("unsupported history vector operation")
        conn.execute(
            "UPDATE message_history SET history_vector_revision=history_vector_revision+1 WHERE id=?",
            (int(row_id),),
        )
        conn.execute(
            "INSERT INTO history_vector_outbox(operation_id,group_id,row_id,epoch,content_hash,"
            "embedding_version,operation,state,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,'pending',datetime('now'),datetime('now'))",
            (uuid4().hex, str(group_id), int(row_id), int(epoch), str(content_hash),
             self.history_vector_embedding_version, operation),
        )

    def pending_history_vector_operations(self, limit: int = 32) -> list[dict[str, object]]:
        if not 1 <= limit <= 32:
            raise ValueError("vector outbox batch must contain 1 to 32 rows")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT operation_id,group_id,row_id,epoch,content_hash,embedding_version,operation,attempts "
                "FROM history_vector_outbox WHERE state='pending' ORDER BY created_at,operation_id LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_history_vector_source(self, group_id: str, row_id: int) -> dict[str, object] | None:
        """Read one active source for indexing; callers must not treat it as verified data."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT m.id,m.group_id,m.user_id,m.role,m.content,m.content_hash,m.epoch "
                "FROM message_history AS m JOIN context_groups AS g ON g.group_id=m.group_id "
                "AND g.epoch=m.epoch WHERE m.group_id=? AND m.id=? AND m.history_visible=1",
                (str(group_id), int(row_id)),
            ).fetchone()
        return dict(row) if row else None

    def finish_history_vector_operation(self, operation_id: str, error: str | None = None) -> bool:
        """Complete an idempotent operation or leave it pending for bounded retry."""
        with self._connect() as conn:
            if error is None:
                cursor = conn.execute(
                    "UPDATE history_vector_outbox SET state='complete',last_error=NULL,"
                    "updated_at=datetime('now') WHERE operation_id=? AND state='pending'",
                    (str(operation_id),),
                )
            else:
                cursor = conn.execute(
                    "UPDATE history_vector_outbox SET attempts=attempts+1,last_error=?,"
                    "updated_at=datetime('now') WHERE operation_id=? AND state='pending'",
                    (str(error)[:80], str(operation_id)),
                )
        return cursor.rowcount > 0

    def validate_history_vector_hits(
        self,
        snapshot: ContextSnapshot,
        hits: list[dict[str, object]],
        *,
        limit: int = 24,
    ) -> list[dict[str, object]]:
        """Re-read vector IDs from SQLite and reject stale, hidden, future, or cross-scope hits."""
        if not snapshot.persisted or snapshot.current_row_id is None:
            return []
        if not 1 <= limit <= 100:
            raise ValueError("vector hit limit is outside the supported bounds")
        validated: list[dict[str, object]] = []
        seen: set[int] = set()
        with self._connect() as conn:
            conn.execute("BEGIN")
            active = conn.execute(
                "SELECT 1 FROM context_groups WHERE group_id=? AND epoch=?",
                (snapshot.group_id, snapshot.epoch),
            ).fetchone()
            if active is None:
                return []
            for hit in hits[:100]:
                try:
                    row_id = int(hit["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                if row_id in seen:
                    continue
                seen.add(row_id)
                row = conn.execute(
                    "SELECT id,group_id,user_id,nickname,role,content,created_at,epoch,"
                    "onebot_message_id,reply_message_id,content_hash FROM message_history "
                    "WHERE id=? AND group_id=? AND epoch=? AND history_visible=1 AND id<?",
                    (row_id, snapshot.group_id, snapshot.epoch, snapshot.current_row_id),
                ).fetchone()
                if row is None:
                    continue
                expected_hash = str(hit.get("content_hash") or "")
                actual_hash = str(row["content_hash"] or "")
                if not expected_hash or expected_hash != actual_hash:
                    continue
                result = dict(row)
                result["rank"] = float(hit.get("rank") or 0.0)
                result["content_hash"] = actual_hash
                result["completeness"] = "complete"
                result["source_ref"] = f"history:{snapshot.group_id}:{snapshot.epoch}:{row_id}"
                validated.append(result)
                if len(validated) >= limit:
                    break
        return validated

    @staticmethod
    def _set_history_metadata(conn: sqlite3.Connection, row_id: int, content: str) -> None:
        raw = content.encode("utf-8")
        conn.execute(
            "UPDATE message_history SET content_hash = ?, estimated_tokens = ? WHERE id = ?",
            (hashlib.sha256(raw).hexdigest(), max(1, (len(raw) * 2 + 4) // 5), int(row_id)),
        )

    def _history_capacity_available(
        self, conn: sqlite3.Connection, group_id: str, *, incoming_rows: int, incoming_bytes: int
    ) -> bool:
        usage = conn.execute(
            "SELECT COUNT(*) AS count, COALESCE(SUM(length(CAST(content AS BLOB))), 0) AS bytes "
            "FROM message_history WHERE group_id = ? AND history_visible = 1",
            (str(group_id),),
        ).fetchone()
        return (
            int(usage["count"]) + incoming_rows <= self.history_max_messages_per_group
            and int(usage["bytes"]) + incoming_bytes <= self.history_max_bytes_per_group
        )

    def rebuild_chat_history_index_batch(self, limit: int = 500) -> dict[str, int | bool | str]:
        """Idempotently index at most 500 legacy rows in one local transaction."""
        if not 1 <= limit <= 500:
            raise ValueError("history index batches must contain 1 to 500 rows")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor_row = conn.execute(
                "SELECT value FROM context_index_meta WHERE key='backfill_last_id'"
            ).fetchone()
            cursor = int(cursor_row["value"]) if cursor_row else 0
            rows = conn.execute(
                "SELECT id, content, history_visible, content_hash FROM message_history "
                "WHERE id > ? ORDER BY id ASC LIMIT ?",
                (cursor, int(limit)),
            ).fetchall()
            if not rows:
                conn.execute(
                    "INSERT INTO context_index_meta(key,value) VALUES('backfill_complete','1') "
                    "ON CONFLICT(key) DO UPDATE SET value='1'"
                )
                return {
                    "indexed": 0, "last_id": cursor, "complete": True,
                    "mode": "trigram+bigram" if self.chat_trigram_available else "bigram_fallback",
                }
            for row in rows:
                row_id = int(row["id"])
                content = str(row["content"] or "")
                if bool(row["history_visible"]):
                    if not str(row["content_hash"] or ""):
                        self._set_history_metadata(conn, row_id, content)
                    self._set_history_index(conn, row_id, content, True)
                else:
                    self._set_history_index(conn, row_id, "", False)
            last_id = int(rows[-1]["id"])
            more = conn.execute("SELECT 1 FROM message_history WHERE id > ? LIMIT 1", (last_id,)).fetchone()
            conn.execute(
                "INSERT INTO context_index_meta(key,value) VALUES('backfill_last_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(last_id),),
            )
            conn.execute(
                "INSERT INTO context_index_meta(key,value) VALUES('backfill_complete', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("0" if more else "1",),
            )
            return {
                "indexed": len(rows), "last_id": last_id, "complete": not bool(more),
                "mode": "trigram+bigram" if self.chat_trigram_available else "bigram_fallback",
            }

    def chat_history_index_status(self) -> dict[str, int | bool | str]:
        with self._connect() as conn:
            metadata = {
                row["key"]: row["value"]
                for row in conn.execute(
                    "SELECT key,value FROM context_index_meta WHERE key IN "
                    "('backfill_last_id','backfill_complete','trigram_available')"
                ).fetchall()
            }
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM message_history WHERE history_visible=1"
            ).fetchone()
            indexed = conn.execute("SELECT COUNT(*) AS n FROM chat_history_fts").fetchone()
        return {
            "visible_messages": int(count["n"]),
            "indexed_messages": int(indexed["n"]),
            "backfill_last_id": int(metadata.get("backfill_last_id", "0")),
            "backfill_complete": metadata.get("backfill_complete") == "1",
            "trigram_available": metadata.get("trigram_available") == "1",
            "mode": "trigram+bigram" if metadata.get("trigram_available") == "1" else "bigram_fallback",
        }

    def optimize_context_search_indexes(self) -> dict[str, int]:
        """Merge SQLite FTS segments for the bounded context-search indexes.

        FTS5's incremental segment merges are not a reliable latency target after
        many individually committed chat writes. This maintenance operation is
        intentionally explicit so callers can run it off the event loop.
        """
        tables = ["chat_history_fts", "memory_fts", "episode_fts"]
        if self.chat_trigram_available:
            tables.insert(1, "chat_history_trigram")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for table in tables:
                # Static allowlist above; table identifiers cannot come from input.
                conn.execute(f"INSERT INTO {table}({table}) VALUES('optimize')")
        return {"optimized_indexes": len(tables)}

    def search_history_fts(
        self,
        query: str,
        snapshot: ContextSnapshot,
        *,
        limit: int = 24,
        max_chars: int = 1800,
    ) -> list[dict[str, object]]:
        """Search only visible rows inside a persisted group/epoch/request snapshot."""
        if not snapshot.persisted or snapshot.current_row_id is None:
            return []
        if not 1 <= limit <= 100 or not 1 <= max_chars <= 8192:
            raise ValueError("history search limits are outside the supported bounds")
        if len(query) > 512:
            query = query[:512]
        fallback_expression = _fts_or_expression(_history_search_tokens(query))
        if not fallback_expression:
            return []
        candidates: dict[int, tuple[float, dict[str, object]]] = {}
        with self._connect() as conn:
            conn.execute("BEGIN")
            current = conn.execute(
                "SELECT 1 FROM context_groups cg JOIN message_history m ON m.group_id=cg.group_id "
                "AND m.epoch=cg.epoch WHERE cg.group_id=? AND cg.epoch=? AND m.id=? AND m.history_visible=1",
                (snapshot.group_id, snapshot.epoch, snapshot.current_row_id),
            ).fetchone()
            if current is None:
                return []
            fallback_rows = conn.execute(
                """
                SELECT m.id, m.group_id, m.user_id, m.nickname, m.role, m.content,
                       m.created_at, m.epoch, m.onebot_message_id, m.reply_message_id,
                       m.content_hash, bm25(chat_history_fts) AS rank
                FROM chat_history_fts
                JOIN message_history AS m ON m.id = chat_history_fts.rowid
                WHERE chat_history_fts MATCH ? AND m.group_id = ? AND m.epoch = ?
                  AND m.id < ? AND m.history_visible = 1
                ORDER BY rank ASC, m.id DESC LIMIT ?
                """,
                (fallback_expression, snapshot.group_id, snapshot.epoch, snapshot.current_row_id, limit),
            ).fetchall()
            for row in fallback_rows:
                candidates[int(row["id"])] = (float(row["rank"]), dict(row))

            if self.chat_trigram_available:
                trigram_expression = _fts_or_expression(_history_trigram_terms(query))
                if trigram_expression:
                    trigram_rows = conn.execute(
                        """
                        SELECT m.id, m.group_id, m.user_id, m.nickname, m.role, m.content,
                               m.created_at, m.epoch, m.onebot_message_id, m.reply_message_id,
                               m.content_hash, bm25(chat_history_trigram) AS rank
                        FROM chat_history_trigram
                        JOIN message_history AS m ON m.id = chat_history_trigram.rowid
                        WHERE chat_history_trigram MATCH ? AND m.group_id = ? AND m.epoch = ?
                          AND m.id < ? AND m.history_visible = 1
                        ORDER BY rank ASC, m.id DESC LIMIT ?
                        """,
                        (trigram_expression, snapshot.group_id, snapshot.epoch, snapshot.current_row_id, limit),
                    ).fetchall()
                    for row in trigram_rows:
                        row_id = int(row["id"])
                        current = candidates.get(row_id)
                        if current is None or float(row["rank"]) < current[0]:
                            candidates[row_id] = (float(row["rank"]), dict(row))

        rows: list[dict[str, object]] = []
        for rank, row in sorted(candidates.values(), key=lambda item: (item[0], -int(item[1]["id"])))[:limit]:
            content = str(row["content"] or "")
            partial = len(content) > max_chars
            row["content"] = content[:max_chars] if partial else content
            row["rank"] = rank
            row["completeness"] = "partial" if partial else "complete"
            row["source_ref"] = f"history:{snapshot.group_id}:{snapshot.epoch}:{row['id']}"
            rows.append(row)
        return rows

    def get_history_reply_chain(
        self, snapshot: ContextSnapshot, reply_message_id: str | None, max_depth: int = 3
    ) -> list[dict[str, object]]:
        """Follow stored reply-to links within one group's visible snapshot boundary."""
        if not snapshot.persisted or snapshot.current_row_id is None or not reply_message_id:
            return []
        if not 0 <= max_depth <= 3:
            raise ValueError("reply chain depth must be between 0 and 3")
        rows: list[dict[str, object]] = []
        seen: set[str] = set()
        current_message_id = str(reply_message_id)
        with self._connect() as conn:
            conn.execute("BEGIN")
            for _ in range(max_depth):
                if not current_message_id or current_message_id in seen:
                    break
                seen.add(current_message_id)
                row = conn.execute(
                    """
                    SELECT id,group_id,user_id,nickname,role,content,created_at,epoch,
                           onebot_message_id,reply_message_id,request_id,content_hash
                    FROM message_history
                    WHERE group_id=? AND epoch=? AND history_visible=1 AND id<? AND onebot_message_id=?
                    LIMIT 1
                    """,
                    (snapshot.group_id, snapshot.epoch, snapshot.current_row_id, current_message_id),
                ).fetchone()
                if row is None:
                    break
                record = dict(row)
                rows.append(record)
                current_message_id = str(record.get("reply_message_id") or "")
        return list(reversed(rows))

    def get_history_neighborhood(
        self,
        snapshot: ContextSnapshot,
        anchor_rows: list[dict[str, object]],
        *,
        radius: int = 1,
        max_total: int = 32,
    ) -> list[dict[str, object]]:
        """Expand validated same-epoch search anchors without crossing snapshot bounds."""
        if not snapshot.persisted or snapshot.current_row_id is None or not anchor_rows:
            return []
        if not 0 <= radius <= 3 or not 1 <= max_total <= 100:
            raise ValueError("history neighborhood limits are outside the supported bounds")
        selected: dict[int, dict[str, object]] = {}
        with self._connect() as conn:
            conn.execute("BEGIN")
            active = conn.execute(
                "SELECT epoch FROM context_groups WHERE group_id=?", (snapshot.group_id,)
            ).fetchone()
            if active is None or int(active["epoch"]) != snapshot.epoch:
                return []
            for anchor in anchor_rows:
                anchor_id = int(anchor.get("id", 0))
                exact = conn.execute(
                    """
                    SELECT id,group_id,user_id,nickname,role,content,created_at,epoch,
                           onebot_message_id,reply_message_id,request_id,content_hash
                    FROM message_history WHERE id=? AND group_id=? AND epoch=?
                      AND history_visible=1 AND id<?
                    """,
                    (anchor_id, snapshot.group_id, snapshot.epoch, snapshot.current_row_id),
                ).fetchone()
                if exact is None:
                    continue
                selected[anchor_id] = dict(exact)
            anchors = sorted(selected)
            for anchor_id in anchors:
                for before in (True, False):
                    if before:
                        sql = (
                            "SELECT id,group_id,user_id,nickname,role,content,created_at,epoch,"
                            "onebot_message_id,reply_message_id,request_id,content_hash FROM message_history "
                            "WHERE group_id=? AND epoch=? AND history_visible=1 AND id<? AND id<? "
                            "ORDER BY id DESC LIMIT ?"
                        )
                        args = (snapshot.group_id, snapshot.epoch, snapshot.current_row_id, anchor_id, radius)
                    else:
                        sql = (
                            "SELECT id,group_id,user_id,nickname,role,content,created_at,epoch,"
                            "onebot_message_id,reply_message_id,request_id,content_hash FROM message_history "
                            "WHERE group_id=? AND epoch=? AND history_visible=1 AND id>? AND id<? "
                            "ORDER BY id ASC LIMIT ?"
                        )
                        args = (snapshot.group_id, snapshot.epoch, anchor_id, snapshot.current_row_id, radius)
                    for row in conn.execute(sql, args).fetchall():
                        row_id = int(row["id"])
                        if row_id not in selected and len(selected) < max_total:
                            selected[row_id] = dict(row)
                if len(selected) >= max_total:
                    break
        return [selected[row_id] for row_id in sorted(selected)]

    def add_message(
        self,
        group_id: str,
        user_id: str,
        nickname: str,
        role: str,
        content: str,
        *,
        onebot_message_id: str | None = None,
        reply_message_id: str | None = None,
        request_id: str | None = None,
        event_timestamp: int | None = None,
    ) -> int:
        with self._lock, self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO context_groups(group_id) VALUES (?)", (str(group_id),))
            group = conn.execute(
                "SELECT epoch FROM context_groups WHERE group_id = ?", (str(group_id),)
            ).fetchone()
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO message_history
                    (group_id, user_id, nickname, role, content, epoch,
                     onebot_message_id, reply_message_id, request_id, event_timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(group_id), str(user_id), nickname or "", role, content,
                    int(group["epoch"]),
                    str(onebot_message_id) if onebot_message_id else None,
                    str(reply_message_id) if reply_message_id else None,
                    str(request_id) if request_id else None,
                    int(event_timestamp) if event_timestamp is not None else None,
                ),
            )
            if cursor.rowcount == 1:
                row_id = int(cursor.lastrowid)
                self._set_history_metadata(conn, row_id, content)
                self._set_history_index(conn, row_id, content)
                self._queue_history_vector(
                    conn, group_id=str(group_id), row_id=row_id, epoch=int(group["epoch"]),
                    content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(), operation="upsert",
                )
                return row_id
            row = conn.execute(
                "SELECT id,content,history_visible FROM message_history WHERE group_id = ? AND onebot_message_id = ?",
                (str(group_id), str(onebot_message_id)),
            ).fetchone()
            if row is None:
                raise sqlite3.IntegrityError("message insert was ignored without an idempotency match")
            self._set_history_index(conn, int(row["id"]), str(row["content"]), bool(row["history_visible"]))
            if bool(row["history_visible"]):
                self._queue_history_vector(
                    conn, group_id=str(group_id), row_id=int(row["id"]), epoch=int(group["epoch"]),
                    content_hash=hashlib.sha256(str(row["content"]).encode("utf-8")).hexdigest(),
                    operation="upsert",
                )
            return int(row["id"])

    def record_message_snapshot(
        self,
        *,
        group_id: str,
        user_id: str,
        nickname: str,
        content: str,
        request_id: str,
        onebot_message_id: str | None = None,
        reply_message_id: str | None = None,
        event_timestamp: int | None = None,
    ) -> ContextSnapshot:
        """Atomically write a user row and capture its group epoch/revision boundary."""
        group_id = str(group_id)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO context_groups(group_id) VALUES (?)", (group_id,))
            current = conn.execute(
                "SELECT epoch, summary_revision FROM context_groups WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            if onebot_message_id:
                existing = conn.execute(
                    """
                    SELECT id, epoch, request_id, content, history_visible FROM message_history
                    WHERE group_id = ? AND onebot_message_id = ?
                    """,
                    (group_id, str(onebot_message_id)),
                ).fetchone()
                if existing is not None:
                    if str(existing["request_id"] or "") != str(request_id):
                        raise sqlite3.IntegrityError("OneBot message id was reused for a different request")
                    if not bool(existing["history_visible"]):
                        raise sqlite3.IntegrityError("OneBot message was already retracted")
                    row_id = int(existing["id"])
                    self._set_history_index(conn, row_id, str(existing["content"]))
                    self._queue_history_vector(
                        conn, group_id=group_id, row_id=row_id, epoch=int(existing["epoch"]),
                        content_hash=hashlib.sha256(str(existing["content"]).encode("utf-8")).hexdigest(),
                        operation="upsert",
                    )
                    return ContextSnapshot(
                        group_id=group_id,
                        user_id=str(user_id),
                        request_id=str(request_id),
                        epoch=int(existing["epoch"]),
                        current_row_id=row_id,
                        summary_revision=int(current["summary_revision"]),
                    )
            incoming_bytes = len(content.encode("utf-8"))
            if not self._history_capacity_available(
                conn, group_id, incoming_rows=1, incoming_bytes=incoming_bytes
            ):
                return ContextSnapshot(
                    group_id=group_id,
                    user_id=str(user_id),
                    request_id=str(request_id),
                    epoch=int(current["epoch"]),
                    current_row_id=None,
                    summary_revision=int(current["summary_revision"]),
                    persisted=False,
                    persistence_status="capacity_degraded",
                )
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO message_history
                    (group_id, user_id, nickname, role, content, epoch,
                     onebot_message_id, reply_message_id, request_id, event_timestamp)
                VALUES (?, ?, ?, 'user', ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id, str(user_id), nickname or "", content, int(current["epoch"]),
                    str(onebot_message_id) if onebot_message_id else None,
                    str(reply_message_id) if reply_message_id else None,
                    str(request_id), int(event_timestamp) if event_timestamp is not None else None,
                ),
            )
            if cursor.rowcount == 1:
                row_id = int(cursor.lastrowid)
                self._set_history_metadata(conn, row_id, content)
                self._set_history_index(conn, row_id, content)
                self._queue_history_vector(
                    conn, group_id=group_id, row_id=row_id, epoch=int(current["epoch"]),
                    content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(), operation="upsert",
                )
            elif onebot_message_id:
                existing = conn.execute(
                    """
                    SELECT id, epoch, request_id, content, history_visible FROM message_history
                    WHERE group_id = ? AND onebot_message_id = ?
                    """,
                    (group_id, str(onebot_message_id)),
                ).fetchone()
                if existing is None or str(existing["request_id"] or "") != str(request_id):
                    raise sqlite3.IntegrityError("OneBot message id was reused for a different request")
                row_id = int(existing["id"])
                current = dict(current)
                current["epoch"] = existing["epoch"]
                if bool(existing["history_visible"]):
                    self._set_history_index(conn, row_id, str(existing["content"]))
                    self._queue_history_vector(
                        conn, group_id=group_id, row_id=row_id, epoch=int(existing["epoch"]),
                        content_hash=hashlib.sha256(str(existing["content"]).encode("utf-8")).hexdigest(),
                        operation="upsert",
                    )
            else:
                raise sqlite3.IntegrityError("message insert was ignored without an idempotency key")
            return ContextSnapshot(
                group_id=group_id,
                user_id=str(user_id),
                request_id=str(request_id),
                epoch=int(current["epoch"]),
                current_row_id=row_id,
                summary_revision=int(current["summary_revision"]),
            )

    def record_assistant_if_epoch(
        self, snapshot: ContextSnapshot, user_id: str, nickname: str, content: str
    ) -> bool:
        if not snapshot.persisted:
            return False
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT epoch FROM context_groups WHERE group_id = ?", (snapshot.group_id,)
            ).fetchone()
            if current is None or int(current["epoch"]) != snapshot.epoch:
                return False
            if not self._history_capacity_available(
                conn,
                snapshot.group_id,
                incoming_rows=1,
                incoming_bytes=len(content.encode("utf-8")),
            ):
                return False
            conn.execute(
                """
                INSERT INTO message_history
                    (group_id, user_id, nickname, role, content, epoch, request_id)
                VALUES (?, ?, ?, 'assistant', ?, ?, ?)
                """,
                (snapshot.group_id, str(user_id), nickname or "", content,
                 snapshot.epoch, snapshot.request_id),
            )
            row_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            self._set_history_metadata(conn, row_id, content)
            self._set_history_index(conn, row_id, content)
            self._queue_history_vector(
                conn, group_id=snapshot.group_id, row_id=row_id, epoch=snapshot.epoch,
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(), operation="upsert",
            )
            return True

    def context_snapshot_is_current(self, snapshot: ContextSnapshot) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT epoch FROM context_groups WHERE group_id = ?", (snapshot.group_id,)
            ).fetchone()
        return bool(row and int(row["epoch"]) == snapshot.epoch)

    def get_recent_messages_before(self, snapshot: ContextSnapshot, limit: int) -> list[dict[str, str]]:
        if not snapshot.persisted or snapshot.current_row_id is None or limit <= 0:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, group_id, user_id, nickname, role, content, created_at,
                       epoch, onebot_message_id, reply_message_id, request_id
                FROM message_history
                WHERE group_id = ? AND epoch = ? AND history_visible = 1 AND id < ?
                ORDER BY id DESC LIMIT ?
                """,
                (snapshot.group_id, snapshot.epoch, snapshot.current_row_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def get_context_epoch(self, group_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT epoch FROM context_groups WHERE group_id = ?", (str(group_id),)
            ).fetchone()
        return int(row["epoch"]) if row else 0

    def get_recent_messages(self, group_id: str, limit: int | None = None) -> list[dict[str, str]]:
        limit = limit or self.history_limit
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, group_id, user_id, nickname, role, content, created_at
                FROM message_history
                WHERE group_id = ? AND history_visible = 1
                  AND epoch = COALESCE((SELECT epoch FROM context_groups WHERE group_id = ?), 0)
                ORDER BY id DESC
                LIMIT ?
                """,
                (str(group_id), str(group_id), limit),
            ).fetchall()
        result = []
        for row in reversed(rows):
            result.append(dict(row))
        return result

    def clear_group_history(self, group_id: str) -> int:
        return self.purge_group_history(group_id)

    def clear_group_context(self, group_id: str) -> int:
        """Legacy destructive clear API; new context reset uses reset_group_context."""
        return self.purge_group_history(group_id)

    def reset_group_context(self, group_id: str) -> int:
        """Start a fresh epoch while retaining the old raw history outside new retrieval."""
        group_id = str(group_id)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO context_groups(group_id) VALUES (?)", (str(group_id),)
            )
            active = conn.execute(
                "SELECT COUNT(*) AS count FROM message_history WHERE group_id = ? AND history_visible = 1 "
                "AND epoch = (SELECT epoch FROM context_groups WHERE group_id = ?)",
                (group_id, group_id),
            ).fetchone()
            conn.execute(
                "UPDATE context_groups SET epoch = epoch + 1, summary_revision = summary_revision + 1, "
                "summary_covered_id = 0, updated_at = datetime('now') WHERE group_id = ?",
                (group_id,),
            )
            conn.execute(
                "DELETE FROM conversation_summaries WHERE group_id = ?",
                (group_id,),
            )
            self._delete_agent_tasks(conn, group_id)
            if self.history_vector_enabled:
                rows = conn.execute(
                    "SELECT id,epoch,content_hash FROM message_history WHERE group_id=? AND history_visible=1",
                    (group_id,),
                ).fetchall()
                for row in rows:
                    self._queue_history_vector(
                        conn, group_id=group_id, row_id=int(row["id"]), epoch=int(row["epoch"]),
                        content_hash=str(row["content_hash"] or ""), operation="delete",
                    )
            return int(active["count"])

    def begin_group_history_purge(self, group_id: str) -> int:
        """Invalidate and tombstone a group's retained history before batched erasure."""
        group_id = str(group_id)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute(
                "SELECT status,hidden_count FROM history_purge_jobs WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            if job is not None and job["status"] == "pending":
                return int(job["hidden_count"])
            conn.execute("INSERT OR IGNORE INTO context_groups(group_id) VALUES (?)", (group_id,))
            count = conn.execute(
                "SELECT COUNT(*) AS count FROM message_history WHERE group_id = ? AND history_visible = 1",
                (group_id,),
            ).fetchone()
            conn.execute(
                "UPDATE context_groups SET epoch=epoch+1,summary_revision=summary_revision+1,"
                "summary_covered_id=0,updated_at=datetime('now') WHERE group_id=?",
                (group_id,),
            )
            conn.execute(
                "DELETE FROM conversation_summaries WHERE group_id = ?", (group_id,)
            )
            self._delete_agent_tasks(conn, group_id)
            conn.execute("DELETE FROM context_compaction_metrics WHERE group_id=?", (group_id,))
            conn.execute(
                "UPDATE memory_sources SET source_state='source_deleted' WHERE source_state='active' "
                "AND memory_id IN (SELECT id FROM memories WHERE group_id=?)",
                (group_id,),
            )
            episode_ids = conn.execute(
                "SELECT id FROM episodic_memory WHERE group_id=?", (group_id,)
            ).fetchall()
            for episode in episode_ids:
                conn.execute("DELETE FROM episode_fts WHERE rowid=?", (int(episode["id"]),))
            conn.execute("DELETE FROM episodic_memory WHERE group_id=?", (group_id,))
            if self.history_vector_enabled:
                vector_rows = conn.execute(
                    "SELECT id,epoch,content_hash FROM message_history WHERE group_id=? AND history_visible=1",
                    (group_id,),
                ).fetchall()
                for row in vector_rows:
                    self._queue_history_vector(
                        conn, group_id=group_id, row_id=int(row["id"]), epoch=int(row["epoch"]),
                        content_hash=str(row["content_hash"] or ""), operation="delete",
                    )
            conn.execute(
                "UPDATE message_history SET history_visible=0,deleted_at=datetime('now') WHERE group_id=?",
                (group_id,),
            )
            # FTS rows are removed in bounded chunks in the same transaction as the tombstones.
            last_id = 0
            while True:
                ids = conn.execute(
                    "SELECT id FROM message_history WHERE group_id=? AND id>? ORDER BY id LIMIT 500",
                    (group_id, last_id),
                ).fetchall()
                if not ids:
                    break
                for row in ids:
                    self._set_history_index(conn, int(row["id"]), "", False)
                last_id = int(ids[-1]["id"])
            conn.execute(
                "INSERT INTO history_purge_jobs(group_id,status,hidden_count,updated_at) "
                "VALUES(?,'pending',?,datetime('now')) ON CONFLICT(group_id) DO UPDATE SET "
                "status='pending',hidden_count=excluded.hidden_count,updated_at=datetime('now')",
                (group_id, int(count["count"])),
            )
            return int(count["count"])

    def purge_group_history_batch(self, group_id: str, limit: int = 500) -> dict[str, int | bool]:
        if not 1 <= limit <= 500:
            raise ValueError("history purge batches must contain 1 to 500 rows")
        group_id = str(group_id)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute(
                "SELECT status FROM history_purge_jobs WHERE group_id = ?", (group_id,)
            ).fetchone()
            if job is None or job["status"] == "complete":
                return {"deleted": 0, "remaining": 0, "complete": True}
            ids = conn.execute(
                "SELECT id FROM message_history WHERE group_id=? AND history_visible=0 "
                "ORDER BY id LIMIT ?",
                (group_id, int(limit)),
            ).fetchall()
            for row in ids:
                row_id = int(row["id"])
                self._set_history_index(conn, row_id, "", False)
                conn.execute("DELETE FROM message_history WHERE id=? AND group_id=?", (row_id, group_id))
            remaining_row = conn.execute(
                "SELECT COUNT(*) AS count FROM message_history WHERE group_id=? AND history_visible=0",
                (group_id,),
            ).fetchone()
            remaining = int(remaining_row["count"])
            complete = remaining == 0
            conn.execute(
                "UPDATE history_purge_jobs SET status=?,updated_at=datetime('now') WHERE group_id=?",
                ("complete" if complete else "pending", group_id),
            )
            return {"deleted": len(ids), "remaining": remaining, "complete": complete}

    def purge_group_history(self, group_id: str, batch_size: int = 500) -> int:
        total = self.begin_group_history_purge(group_id)
        while True:
            state = self.purge_group_history_batch(group_id, batch_size)
            if state["complete"]:
                return total

    @staticmethod
    def _delete_agent_tasks(conn: sqlite3.Connection, group_id: str) -> None:
        conn.execute("DELETE FROM agent_checkpoints WHERE task_id IN (SELECT task_id FROM agent_tasks WHERE group_id=?)", (group_id,))
        conn.execute("DELETE FROM agent_tasks WHERE group_id=?", (group_id,))

    def create_agent_task(
        self, *, task_id: str, request_id: str, group_id: str, user_id: str,
        epoch: int, goal: str, state_json: str, ttl_seconds: int = 86_400,
    ) -> bool:
        if not 60 <= ttl_seconds <= 7 * 86_400 or len(state_json.encode("utf-8")) > 32_768:
            raise ValueError("working state limits are invalid")
        try:
            decoded = json.loads(state_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("working state must be valid JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("working state must be a JSON object")
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM agent_checkpoints WHERE task_id IN (SELECT task_id FROM agent_tasks WHERE expires_at<=?)", (now,))
            conn.execute("DELETE FROM agent_tasks WHERE expires_at<=?", (now,))
            try:
                conn.execute(
                    "INSERT INTO agent_tasks(task_id,request_id,group_id,user_id,epoch,goal,status,revision,state_json,"
                    "created_at,updated_at,expires_at) VALUES(?,?,?,?,?,?,'running',0,?,?,?,?)",
                    (task_id, request_id, str(group_id), str(user_id), int(epoch), goal, state_json,
                     now, now, now + int(ttl_seconds)),
                )
            except sqlite3.IntegrityError:
                return False
            conn.execute(
                "INSERT INTO agent_checkpoints(task_id,revision,state_json,created_at) VALUES(?,0,?,?)",
                (task_id, state_json, now),
            )
            return True

    def save_agent_checkpoint(
        self, *, task_id: str, group_id: str, user_id: str, epoch: int,
        expected_revision: int, state_json: str, status: str = "running",
    ) -> int | None:
        if status not in {"running", "completed", "failed", "cancelled"}:
            raise ValueError("invalid working task status")
        if len(state_json.encode("utf-8")) > 32_768:
            raise ValueError("working state exceeds the persistence limit")
        decoded = json.loads(state_json)
        if not isinstance(decoded, dict):
            raise ValueError("working state must be a JSON object")
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE agent_tasks SET status=?,revision=revision+1,state_json=?,updated_at=? "
                "WHERE task_id=? AND group_id=? AND user_id=? AND epoch=? AND revision=? "
                "AND status='running' AND expires_at>?",
                (status, state_json, now, task_id, str(group_id), str(user_id), int(epoch),
                 int(expected_revision), now),
            )
            if cursor.rowcount != 1:
                return None
            next_revision = int(expected_revision) + 1
            conn.execute(
                "INSERT INTO agent_checkpoints(task_id,revision,state_json,created_at) VALUES(?,?,?,?)",
                (task_id, next_revision, state_json, now),
            )
            return next_revision

    def get_agent_task(
        self, task_id: str, *, group_id: str, user_id: str, epoch: int | None = None,
    ) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT task_id,request_id,group_id,user_id,epoch,goal,status,revision,state_json,"
                "created_at,updated_at,expires_at FROM agent_tasks WHERE task_id=? AND group_id=? "
                "AND user_id=? AND expires_at>? AND (? IS NULL OR epoch=?)",
                (task_id, str(group_id), str(user_id), int(time.time()), epoch, epoch),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_agent_tasks(self, *, group_id: str, limit: int = 50) -> list[dict[str, object]]:
        if not 1 <= limit <= 100:
            raise ValueError("working task list limit is invalid")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id,request_id,group_id,user_id,epoch,goal,status,revision,updated_at,expires_at "
                "FROM agent_tasks WHERE group_id=? AND expires_at>? ORDER BY updated_at DESC LIMIT ?",
                (str(group_id), int(time.time()), int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def retract_history_message(self, group_id: str, message_id: int) -> bool:
        """Hide and erase one message while keeping a tombstone row ID for provenance checks."""
        group_id = str(group_id)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id,epoch FROM message_history WHERE group_id=? AND id=? AND history_visible=1",
                (group_id, int(message_id)),
            ).fetchone()
            if row is None:
                return False
            self._invalidate_episodes_for_row(
                conn, group_id, int(row["epoch"]), int(message_id)
            )
            self._set_history_index(conn, int(row["id"]), "", False)
            source = conn.execute(
                "SELECT epoch,content_hash FROM message_history WHERE group_id=? AND id=?",
                (group_id, int(message_id)),
            ).fetchone()
            if source is not None:
                self._queue_history_vector(
                    conn, group_id=group_id, row_id=int(message_id), epoch=int(source["epoch"]),
                    content_hash=str(source["content_hash"] or ""), operation="delete",
                )
            conn.execute(
                "UPDATE message_history SET history_visible=0,content='',content_hash='',estimated_tokens=0,"
                "deleted_at=datetime('now') WHERE group_id=? AND id=?",
                (group_id, int(message_id)),
            )
            return True

    def update_history_message_content(self, group_id: str, message_id: int, content: str) -> bool:
        """Update a visible source and its search/hash metadata in one SQLite transaction."""
        group_id = str(group_id)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id,epoch FROM message_history WHERE group_id=? AND id=? AND history_visible=1",
                (group_id, int(message_id)),
            ).fetchone()
            if row is None:
                return False
            self._invalidate_episodes_for_row(
                conn, group_id, int(row["epoch"]), int(message_id)
            )
            conn.execute(
                "UPDATE context_groups SET summary_revision=summary_revision+1,summary_covered_id=0,"
                "updated_at=datetime('now') WHERE group_id=?",
                (group_id,),
            )
            conn.execute("DELETE FROM conversation_summaries WHERE group_id=?", (group_id,))
            conn.execute(
                "UPDATE message_history SET content=? WHERE group_id=? AND id=?",
                (content, group_id, int(message_id)),
            )
            self._set_history_metadata(conn, int(message_id), content)
            self._set_history_index(conn, int(message_id), content)
            source = conn.execute(
                "SELECT epoch FROM message_history WHERE group_id=? AND id=?",
                (group_id, int(message_id)),
            ).fetchone()
            self._queue_history_vector(
                conn, group_id=group_id, row_id=int(message_id), epoch=int(source["epoch"]),
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(), operation="upsert",
            )
            return True

    def prune_history_before(self, cutoff_created_at: str, batch_size: int = 500) -> int:
        """Explicit, bounded retention deletion. It is never run automatically by default."""
        if not 1 <= batch_size <= 500:
            raise ValueError("history retention batches must contain 1 to 500 rows")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id,group_id,epoch,content_hash FROM message_history "
                "WHERE history_visible=1 AND created_at<=? "
                "ORDER BY id LIMIT ?",
                (str(cutoff_created_at), int(batch_size)),
            ).fetchall()
            if not rows:
                return 0
            affected_groups = {str(row["group_id"]) for row in rows}
            for group_id in affected_groups:
                conn.execute(
                    "UPDATE context_groups SET epoch=epoch+1,summary_revision=summary_revision+1,"
                    "summary_covered_id=0,updated_at=datetime('now') WHERE group_id=?",
                    (group_id,),
                )
                conn.execute("DELETE FROM conversation_summaries WHERE group_id=?", (group_id,))
            for row in rows:
                self._queue_history_vector(
                    conn, group_id=str(row["group_id"]), row_id=int(row["id"]),
                    epoch=int(row["epoch"]), content_hash=str(row["content_hash"] or ""),
                    operation="delete",
                )
                self._set_history_index(conn, int(row["id"]), "", False)
                conn.execute("DELETE FROM message_history WHERE id=?", (int(row["id"]),))
            return len(rows)

    def get_group_summary(self, group_id: str) -> dict[str, str] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT group_id, summary, last_message_id, revision, epoch, updated_at
                FROM conversation_summaries
                WHERE group_id = ?
                """,
                (str(group_id),),
            ).fetchone()
        return dict(row) if row else None

    def get_group_summary_for_snapshot(
        self, snapshot: ContextSnapshot
    ) -> dict[str, str] | None:
        if not snapshot.persisted or snapshot.current_row_id is None:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT group_id, summary, last_message_id, revision, epoch, updated_at
                FROM conversation_summaries
                WHERE group_id = ? AND epoch = ? AND revision <= ? AND last_message_id < ?
                """,
                (
                    snapshot.group_id,
                    snapshot.epoch,
                    snapshot.summary_revision,
                    snapshot.current_row_id,
                ),
            ).fetchone()
        return dict(row) if row else None

    def upsert_group_summary(self, group_id: str, summary: str, last_message_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO context_groups(group_id) VALUES (?)", (str(group_id),))
            state = conn.execute(
                "SELECT epoch, summary_revision FROM context_groups WHERE group_id = ?",
                (str(group_id),),
            ).fetchone()
            next_revision = int(state["summary_revision"]) + 1
            conn.execute(
                """
                INSERT INTO conversation_summaries
                    (group_id, summary, last_message_id, revision, epoch, updated_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(group_id) DO UPDATE SET
                    summary = excluded.summary,
                    last_message_id = excluded.last_message_id,
                    revision = excluded.revision,
                    epoch = excluded.epoch,
                    updated_at = datetime('now')
                """,
                (str(group_id), summary, int(last_message_id), next_revision, int(state["epoch"])),
            )
            conn.execute(
                "UPDATE context_groups SET summary_revision = ?, summary_covered_id = ?, "
                "updated_at = datetime('now') WHERE group_id = ?",
                (next_revision, int(last_message_id), str(group_id)),
            )

    def clear_group_summary(self, group_id: str) -> bool:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO context_groups(group_id) VALUES (?)", (str(group_id),))
            conn.execute(
                "UPDATE context_groups SET summary_revision = summary_revision + 1, "
                "summary_covered_id = 0, updated_at = datetime('now') WHERE group_id = ?",
                (str(group_id),),
            )
            cursor = conn.execute(
                "DELETE FROM conversation_summaries WHERE group_id = ?",
                (str(group_id),),
            )
            return cursor.rowcount > 0

    def begin_group_summary_snapshot(
        self,
        group_id: str,
        *,
        keep_recent: int,
        trigger_count: int,
        trigger_tokens: int = 3000,
        max_input_bytes: int = 262_144,
        max_rows: int = 100,
    ) -> SummarySnapshot | None:
        if keep_recent < 0 or trigger_count < 1 or trigger_tokens < 1:
            raise ValueError("summary limits must be non-negative and trigger_count positive")
        if not 1 <= max_input_bytes <= 2_000_000 or not 1 <= max_rows <= 500:
            raise ValueError("summary input limits are outside the supported bounds")
        group_id = str(group_id)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN")
            conn.execute("INSERT OR IGNORE INTO context_groups(group_id) VALUES (?)", (group_id,))
            state = conn.execute(
                "SELECT epoch, summary_revision, summary_covered_id FROM context_groups WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            epoch = int(state["epoch"])
            revision = int(state["summary_revision"])
            covered_id = int(state["summary_covered_id"])
            summary_row = conn.execute(
                "SELECT summary FROM conversation_summaries WHERE group_id = ? AND epoch = ?",
                (group_id, epoch),
            ).fetchone()
            rows = conn.execute(
                """
                SELECT id, group_id, user_id, nickname, role, content, created_at,
                       onebot_message_id, reply_message_id, request_id, event_timestamp, content_hash
                FROM message_history
                WHERE group_id = ? AND epoch = ? AND history_visible = 1 AND id > ?
                ORDER BY id ASC
                """,
                (group_id, epoch, covered_id),
            ).fetchall()
            if len(rows) <= keep_recent:
                return None
            to_summarize = rows[:-keep_recent] if keep_recent else rows
            if not to_summarize:
                return None
            estimated_tokens = sum(max(1, int(len(str(row["content"]).encode("utf-8")) * 0.4)) for row in to_summarize)
            if len(rows) < trigger_count and estimated_tokens < trigger_tokens:
                return None
            copied: list[dict[str, object]] = []
            accumulated_bytes = 0
            for row in to_summarize:
                row_bytes = len(str(row["content"]).encode("utf-8"))
                if len(copied) >= max_rows or accumulated_bytes + row_bytes > max_input_bytes:
                    break
                copied.append(dict(row))
                accumulated_bytes += row_bytes
            if not copied:
                return None
            copied_rows = tuple(copied)
            return SummarySnapshot(
                group_id=group_id,
                epoch=epoch,
                revision=revision,
                previously_covered_message_id=covered_id,
                through_message_id=int(copied_rows[-1]["id"]),
                previous_summary=str(summary_row["summary"]) if summary_row else "",
                rows=copied_rows,
            )

    def commit_group_summary(
        self, snapshot: SummarySnapshot, summary: str, episode: dict[str, object] | None = None,
    ) -> bool:
        if not summary.strip():
            return False
        if episode is not None:
            allowed_ids = {int(row["id"]) for row in snapshot.rows}
            if (
                episode.get("schema_version") != 1
                or str(episode.get("summary") or "").strip() != summary.strip()
                or not isinstance(episode.get("topic"), str)
                or len(str(episode.get("topic") or "")) > 160
            ):
                raise ValueError("structured episode does not match the summary snapshot")
            for field in ("decisions", "open_questions"):
                entries = episode.get(field)
                if not isinstance(entries, list) or len(entries) > 12:
                    raise ValueError("structured episode entries are invalid")
                for entry in entries:
                    if not isinstance(entry, dict) or not isinstance(entry.get("text"), str):
                        raise ValueError("structured episode item is invalid")
                    source_ids = entry.get("source_row_ids")
                    if (
                        not isinstance(source_ids, list) or not source_ids or len(source_ids) > 8
                        or any(isinstance(item, bool) or not isinstance(item, int) or item not in allowed_ids
                               for item in source_ids)
                    ):
                        raise ValueError("structured episode source is outside the summary snapshot")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = conn.execute(
                "SELECT epoch, summary_revision, summary_covered_id FROM context_groups WHERE group_id = ?",
                (snapshot.group_id,),
            ).fetchone()
            if (
                state is None
                or int(state["epoch"]) != snapshot.epoch
                or int(state["summary_revision"]) != snapshot.revision
                or int(state["summary_covered_id"]) != snapshot.previously_covered_message_id
            ):
                return False
            # Message row IDs are global, so validate the exact coverage boundary directly.
            expected_rows = {int(row["id"]): str(row.get("content") or "") for row in snapshot.rows}
            current_rows = conn.execute(
                "SELECT id,content,history_visible FROM message_history WHERE group_id=? AND epoch=? "
                "AND id>? AND id<=? ORDER BY id",
                (snapshot.group_id, snapshot.epoch, snapshot.previously_covered_message_id,
                 snapshot.through_message_id),
            ).fetchall()
            if not expected_rows or len(current_rows) != len(expected_rows):
                return False
            for row in current_rows:
                row_id = int(row["id"])
                if not bool(row["history_visible"]) or expected_rows.get(row_id) != str(row["content"]):
                    return False
            next_revision = snapshot.revision + 1
            conn.execute(
                """
                INSERT INTO conversation_summaries
                    (group_id, summary, last_message_id, revision, epoch, updated_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(group_id) DO UPDATE SET
                    summary = excluded.summary,
                    last_message_id = excluded.last_message_id,
                    revision = excluded.revision,
                    epoch = excluded.epoch,
                    updated_at = datetime('now')
                """,
                (snapshot.group_id, summary, snapshot.through_message_id,
                 next_revision, snapshot.epoch),
            )
            conn.execute(
                "UPDATE context_groups SET summary_revision = ?, summary_covered_id = ?, "
                "updated_at = datetime('now') WHERE group_id = ? AND epoch = ?",
                (next_revision, snapshot.through_message_id, snapshot.group_id, snapshot.epoch),
            )
            if episode is not None:
                from json import dumps

                source_refs = [
                    f"history:{snapshot.group_id}:{snapshot.epoch}:{int(row['id'])}"
                    for row in snapshot.rows
                ]
                source_hashes = {
                    str(row["id"]): str(row.get("content_hash") or hashlib.sha256(
                        str(row.get("content") or "").encode("utf-8")
                    ).hexdigest())
                    for row in snapshot.rows
                }
                cursor = conn.execute(
                    "INSERT INTO episodic_memory(group_id,epoch,start_message_id,end_message_id,schema_version,"
                    "topic,summary,decisions_json,open_questions_json,source_refs_json,source_hashes_json,revision,state) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,1,'active')",
                    (snapshot.group_id, snapshot.epoch, int(snapshot.rows[0]["id"]),
                     snapshot.through_message_id, int(episode.get("schema_version", 1)),
                     str(episode.get("topic") or ""), summary,
                     dumps(episode.get("decisions", []), ensure_ascii=False, separators=(",", ":")),
                     dumps(episode.get("open_questions", []), ensure_ascii=False, separators=(",", ":")),
                     dumps(source_refs, ensure_ascii=False, separators=(",", ":")),
                     dumps(source_hashes, ensure_ascii=False, separators=(",", ":"))),
                )
                episode_id = int(cursor.lastrowid)
                episode_text = " ".join([
                    str(episode.get("topic") or ""), summary,
                    " ".join(str(item.get("text") or "") for item in episode.get("decisions", []) if isinstance(item, dict)),
                    " ".join(str(item.get("text") or "") for item in episode.get("open_questions", []) if isinstance(item, dict)),
                ])
                terms = _history_search_tokens(episode_text)
                if terms:
                    conn.execute(
                        "INSERT INTO episode_fts(rowid,search_text) VALUES(?,?)",
                        (episode_id, " ".join(terms)),
                    )
            return True

    def search_episodes(
        self, query: str, *, group_id: str, epoch: int, limit: int = 12,
    ) -> list[dict[str, object]]:
        if not 1 <= limit <= 50:
            raise ValueError("episode retrieval limit is outside the supported bounds")
        expression = _fts_or_expression(_history_search_tokens(query[:512]))
        if not expression:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT e.*,bm25(episode_fts) AS rank FROM episode_fts "
                "JOIN episodic_memory e ON e.id=episode_fts.rowid "
                "WHERE episode_fts MATCH ? AND e.group_id=? AND e.epoch=? AND e.state='active' "
                "ORDER BY rank ASC,e.end_message_id DESC LIMIT ?",
                (expression, str(group_id), int(epoch), int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def summary_snapshot_is_current(self, snapshot: SummarySnapshot) -> bool:
        with self._connect() as conn:
            state = conn.execute(
                "SELECT epoch, summary_revision, summary_covered_id FROM context_groups WHERE group_id = ?",
                (snapshot.group_id,),
            ).fetchone()
        return bool(
            state is not None
            and int(state["epoch"]) == snapshot.epoch
            and int(state["summary_revision"]) == snapshot.revision
            and int(state["summary_covered_id"]) == snapshot.previously_covered_message_id
        )

    def get_messages_for_summary(self, group_id: str, keep_recent: int) -> list[dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, group_id, user_id, nickname, role, content, created_at
                FROM message_history
                WHERE group_id = ? AND history_visible = 1
                  AND epoch = COALESCE((SELECT epoch FROM context_groups WHERE group_id = ?), 0)
                ORDER BY id ASC
                """,
                (str(group_id), str(group_id)),
            ).fetchall()
        if len(rows) <= keep_recent:
            return []
        return [dict(row) for row in rows[:-keep_recent]]

    def delete_messages_through(self, group_id: str, max_message_id: int) -> int:
        with self._lock, self._connect() as conn:
            ids = conn.execute(
                "SELECT id FROM message_history WHERE group_id=? AND id<=?",
                (str(group_id), int(max_message_id)),
            ).fetchall()
            for row in ids:
                self._set_history_index(conn, int(row["id"]), "", False)
            cursor = conn.execute(
                "DELETE FROM message_history WHERE group_id = ? AND id <= ?",
                (str(group_id), int(max_message_id)),
            )
            return int(cursor.rowcount)

    def message_exists(self, group_id: str, message_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM message_history
                WHERE group_id = ? AND id = ?
                LIMIT 1
                """,
                (str(group_id), int(message_id)),
            ).fetchone()
        return row is not None

    def count_group_history(self, group_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM message_history WHERE group_id = ?",
                (str(group_id),),
            ).fetchone()
        return int(row["count"] if row else 0)

    def get_context_epoch(self, group_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT epoch FROM context_groups WHERE group_id=?", (str(group_id),)
            ).fetchone()
        return int(row["epoch"]) if row is not None else 0

    def get_context_stats(self, group_id: str) -> dict[str, object]:
        group_id = str(group_id)
        with self._connect() as conn:
            state = conn.execute(
                "SELECT epoch,summary_revision,summary_covered_id FROM context_groups WHERE group_id=?",
                (group_id,),
            ).fetchone()
            epoch = int(state["epoch"]) if state is not None else 0
            history = conn.execute(
                "SELECT COUNT(*) AS count,COALESCE(SUM(length(content)),0) AS bytes,"
                "COALESCE(SUM(estimated_tokens),0) AS estimated_tokens FROM message_history "
                "WHERE group_id=? AND epoch=? AND history_visible=1",
                (group_id, epoch),
            ).fetchone()
            fts = conn.execute(
                "SELECT COUNT(*) AS count FROM chat_history_fts f JOIN message_history m ON m.id=f.rowid "
                "WHERE m.group_id=? AND m.epoch=? AND m.history_visible=1",
                (group_id, epoch),
            ).fetchone()
            episodes = conn.execute(
                "SELECT COUNT(*) AS count FROM episodic_memory WHERE group_id=? AND epoch=? AND state='active'",
                (group_id, epoch),
            ).fetchone()
            facts = conn.execute(
                "SELECT COUNT(*) AS count FROM memories WHERE group_id=? AND status='active' "
                "AND verification IN ('admin_confirmed','tool_observed') AND kind='fact'",
                (group_id,),
            ).fetchone()
            pending_vector = conn.execute(
                "SELECT COUNT(*) AS count FROM history_vector_outbox WHERE group_id=? AND state='pending'",
                (group_id,),
            ).fetchone()
            purge = conn.execute(
                "SELECT status,hidden_count FROM history_purge_jobs WHERE group_id=?", (group_id,)
            ).fetchone()
            summary = conn.execute(
                "SELECT summary,last_message_id,revision FROM conversation_summaries "
                "WHERE group_id=? AND epoch=?", (group_id, epoch),
            ).fetchone()
            compactions = conn.execute(
                "SELECT compaction_count,archived_tool_blocks,dropped_context_items FROM "
                "context_compaction_metrics WHERE group_id=? AND epoch=?",
                (group_id, epoch),
            ).fetchone()
        return {
            "epoch": epoch,
            "summary_revision": int(state["summary_revision"]) if state is not None else 0,
            "summary_covered_id": int(state["summary_covered_id"]) if state is not None else 0,
            "history_messages": int(history["count"]),
            "history_bytes": int(history["bytes"]),
            "history_estimated_tokens": int(history["estimated_tokens"]),
            "history_fts_rows": int(fts["count"]),
            "active_episodes": int(episodes["count"]),
            "verified_facts": int(facts["count"]),
            "pending_vector_operations": int(pending_vector["count"]),
            "purge_status": str(purge["status"]) if purge is not None else "none",
            "purge_hidden_count": int(purge["hidden_count"]) if purge is not None else 0,
            "summary_available": summary is not None,
            "summary_last_message_id": int(summary["last_message_id"]) if summary is not None else None,
            "compaction_count": int(compactions["compaction_count"]) if compactions is not None else 0,
            "archived_tool_blocks": int(compactions["archived_tool_blocks"]) if compactions is not None else 0,
            "dropped_context_items": int(compactions["dropped_context_items"]) if compactions is not None else 0,
        }

    def record_context_compaction(
        self, *, group_id: str, epoch: int, archived_tool_blocks: int, dropped_context_items: int,
    ) -> bool:
        if archived_tool_blocks < 0 or dropped_context_items < 0:
            raise ValueError("compaction metrics cannot be negative")
        if archived_tool_blocks == 0 and dropped_context_items == 0:
            return False
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                "SELECT epoch FROM context_groups WHERE group_id=?", (str(group_id),)
            ).fetchone()
            if active is None or int(active["epoch"]) != int(epoch):
                return False
            conn.execute(
                "INSERT INTO context_compaction_metrics(group_id,epoch,compaction_count,archived_tool_blocks,"
                "dropped_context_items,updated_at) VALUES(?,?,1,?,?,?) ON CONFLICT(group_id,epoch) DO UPDATE SET "
                "compaction_count=compaction_count+1,archived_tool_blocks=archived_tool_blocks+excluded.archived_tool_blocks,"
                "dropped_context_items=dropped_context_items+excluded.dropped_context_items,updated_at=excluded.updated_at",
                (str(group_id), int(epoch), int(archived_tool_blocks), int(dropped_context_items), now),
            )
            return True

    def get_group_setting(self, group_id: str, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT setting_value FROM group_settings WHERE group_id = ? AND setting_key = ?",
                (str(group_id), key),
            ).fetchone()
        return str(row["setting_value"]) if row else None

    def set_group_setting(self, group_id: str, key: str, value: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO group_settings (group_id, setting_key, setting_value, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(group_id, setting_key) DO UPDATE SET
                    setting_value = excluded.setting_value,
                    updated_at = datetime('now')
                """,
                (str(group_id), key, value),
            )

    def add_memory(self, scope: str, scope_id: str, content: str, source_user_id: str) -> int:
        """Legacy memory entry point: preserve it as unverified and never auto-promote it."""
        scope_id = str(scope_id)
        visibility = "group" if scope == "group" else "legacy_unscoped"
        group_id = scope_id if scope == "group" else None
        owner_user_id = scope_id if scope == "user" else None
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO memories
                    (scope,scope_id,content,source_user_id,kind,status,revision,verification,
                     normalized_key,visibility,group_id,owner_user_id,updated_at)
                VALUES (?, ?, ?, ?, 'fact','active',1,'legacy_unknown',?,?,?,?,datetime('now'))
                """,
                (scope, scope_id, content, str(source_user_id),
                 hashlib.sha256(" ".join(content.casefold().split()).encode("utf-8")).hexdigest(),
                 visibility, group_id, owner_user_id),
            )
            memory_id = int(cursor.lastrowid)
            self._set_memory_index(conn, memory_id, content)
            self._record_memory_revision(conn, memory_id, 1, content, "legacy_unknown", "active")
            return memory_id

    @staticmethod
    def _record_memory_revision(
        conn: sqlite3.Connection, memory_id: int, revision: int, content: str,
        verification: str, status: str,
    ) -> None:
        import json

        metadata = json.dumps(
            {"verification": verification, "status": status},
            ensure_ascii=False, separators=(",", ":"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO memory_revisions(memory_id,revision,content,metadata_json) VALUES(?,?,?,?)",
            (int(memory_id), int(revision), content, metadata),
        )

    def add_structured_memory(
        self,
        *,
        group_id: str | None,
        owner_user_id: str | None,
        content: str,
        actor_user_id: str,
        verification: str,
        visibility: str = "group",
        kind: str = "fact",
        source_kind: str,
        source_id: str | None = None,
        source_hash: str | None = None,
        locator: str | None = None,
        normalized_key: str | None = None,
        confidence: float | None = None,
        supersedes_id: int | None = None,
    ) -> tuple[int, bool]:
        """Insert or explicitly revise a structured fact, keeping every prior version/source."""
        content = " ".join(str(content).split())
        if not content or len(content.encode("utf-8")) > 8192:
            raise ValueError("memory content must be non-empty and at most 8 KiB")
        if verification not in {"user_asserted", "admin_confirmed", "tool_observed", "inferred_candidate", "legacy_unknown"}:
            raise ValueError("unknown memory verification state")
        if kind not in {"fact", "episode"}:
            raise ValueError("unknown memory kind")
        if visibility not in {"group", "user", "legacy_unscoped"}:
            raise ValueError("unknown memory visibility")
        if visibility == "group" and not group_id:
            raise ValueError("group memory requires a group id")
        if visibility == "user" and (not group_id or not owner_user_id):
            raise ValueError("user memory requires group and owner ids")
        if confidence is not None and not 0.0 <= float(confidence) <= 1.0:
            raise ValueError("memory confidence must be between zero and one")
        group_id = str(group_id) if group_id is not None else None
        owner_user_id = str(owner_user_id) if owner_user_id is not None else None
        source_user_id = str(actor_user_id)
        content_key = hashlib.sha256(content.casefold().encode("utf-8")).hexdigest()
        key = (str(normalized_key).strip().casefold()[:256] if normalized_key else content_key)
        if not key:
            key = content_key
        source_hash = source_hash or hashlib.sha256(content.encode("utf-8")).hexdigest()
        scope = "group" if visibility == "group" else "user"
        scope_id = group_id if visibility == "group" else owner_user_id
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            clauses = ["normalized_key=?", "status IN ('active','conflict')", "visibility=?"]
            params: list[object] = [key, visibility]
            if group_id is not None:
                clauses.append("group_id=?")
                params.append(group_id)
            if owner_user_id is not None:
                clauses.append("owner_user_id=?")
                params.append(owner_user_id)
            existing_rows = conn.execute(
                "SELECT id,content,revision,verification,status FROM memories WHERE " + " AND ".join(clauses),
                params,
            ).fetchall()
            for existing in existing_rows:
                if str(existing["content"]).casefold() == content.casefold():
                    memory_id = int(existing["id"])
                    # An explicit admin confirmation may promote the exact legacy row, never by model input.
                    if verification == "admin_confirmed" and existing["verification"] == "legacy_unknown":
                        revision = int(existing["revision"]) + 1
                        conn.execute(
                            "UPDATE memories SET verification=?,revision=?,source_user_id=?,updated_at=datetime('now') "
                            "WHERE id=?",
                            (verification, revision, source_user_id, memory_id),
                        )
                        self._record_memory_revision(conn, memory_id, revision, content, verification, "active")
                    self._insert_memory_source(
                        conn, memory_id, int(conn.execute("SELECT revision FROM memories WHERE id=?", (memory_id,)).fetchone()[0]),
                        source_kind, source_id, source_hash, locator, verification,
                    )
                    return memory_id, False

            status = "conflict" if existing_rows else "active"
            if existing_rows:
                for existing in existing_rows:
                    revision = int(existing["revision"]) + 1
                    conn.execute(
                        "UPDATE memories SET status='conflict',revision=?,updated_at=datetime('now') WHERE id=?",
                        (revision, int(existing["id"])),
                    )
                    self._record_memory_revision(
                        conn, int(existing["id"]), revision, str(existing["content"]),
                        str(existing["verification"]), "conflict",
                    )
            cursor = conn.execute(
                "INSERT INTO memories(scope,scope_id,content,source_user_id,kind,status,revision,verification,"
                "confidence,normalized_key,visibility,group_id,owner_user_id,supersedes_id,updated_at) "
                "VALUES(?,?,?,?,?,?,1,?,?,?,?,?,?,?,datetime('now'))",
                (scope, str(scope_id), content, source_user_id, kind, status, verification,
                 confidence, key, visibility, group_id, owner_user_id, supersedes_id),
            )
            memory_id = int(cursor.lastrowid)
            self._set_memory_index(conn, memory_id, content)
            self._record_memory_revision(conn, memory_id, 1, content, verification, status)
            self._insert_memory_source(conn, memory_id, 1, source_kind, source_id, source_hash, locator, verification)
            return memory_id, True

    @staticmethod
    def _insert_memory_source(
        conn: sqlite3.Connection, memory_id: int, revision: int, source_kind: str,
        source_id: str | None, source_hash: str | None, locator: str | None, verification: str,
    ) -> str:
        source_ref_id = uuid4().hex
        conn.execute(
            "INSERT INTO memory_sources(source_id,memory_id,revision,source_kind,source_ref,source_hash,locator,verification) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (source_ref_id, int(memory_id), int(revision), str(source_kind), source_id,
             source_hash, locator, verification),
        )
        return source_ref_id

    def retract_structured_memory(self, memory_id: int, *, group_id: str) -> bool:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT content,revision,verification FROM memories WHERE id=? AND group_id=? AND status!='retracted'",
                (int(memory_id), str(group_id)),
            ).fetchone()
            if row is None:
                return False
            revision = int(row["revision"]) + 1
            conn.execute(
                "UPDATE memories SET status='retracted',revision=?,updated_at=datetime('now') WHERE id=?",
                (revision, int(memory_id)),
            )
            self._set_memory_index(conn, int(memory_id), "", False)
            self._record_memory_revision(
                conn, int(memory_id), revision, str(row["content"]), str(row["verification"]), "retracted"
            )
            return True

    def revise_structured_memory(
        self, memory_id: int, *, group_id: str, actor_user_id: str, content: str,
        source_kind: str, source_id: str | None = None, locator: str | None = None,
    ) -> bool:
        content = " ".join(str(content).split())
        if not content or len(content.encode("utf-8")) > 8192:
            raise ValueError("memory content must be non-empty and at most 8 KiB")
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        key = hashlib.sha256(content.casefold().encode("utf-8")).hexdigest()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT content,revision,verification,visibility,owner_user_id FROM memories "
                "WHERE id=? AND group_id=? AND status!='retracted'",
                (int(memory_id), str(group_id)),
            ).fetchone()
            if row is None or row["visibility"] != "group":
                return False
            revision = int(row["revision"]) + 1
            conn.execute(
                "UPDATE memories SET content=?,normalized_key=?,verification='admin_confirmed',"
                "source_user_id=?,revision=?,status='active',updated_at=datetime('now') WHERE id=?",
                (content, key, str(actor_user_id), revision, int(memory_id)),
            )
            self._set_memory_index(conn, int(memory_id), content)
            self._record_memory_revision(conn, int(memory_id), revision, content, "admin_confirmed", "active")
            self._insert_memory_source(
                conn, int(memory_id), revision, source_kind, source_id, content_hash, locator, "admin_confirmed"
            )
            return True

    def mark_group_memory_sources_deleted(self, group_id: str) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE memory_sources SET source_state='source_deleted' WHERE memory_id IN "
                "(SELECT id FROM memories WHERE group_id=?) AND source_state='active'",
                (str(group_id),),
            )
            return int(cursor.rowcount)

    def search_structured_memories(
        self,
        query: str,
        *,
        group_id: str,
        user_id: str,
        limit: int = 10,
        allowed_verifications: tuple[str, ...] = ("admin_confirmed", "tool_observed"),
        fallback_group_preferences: bool = True,
    ) -> list[dict[str, object]]:
        if not 1 <= limit <= 100:
            raise ValueError("memory retrieval limit is outside the supported bounds")
        terms = _history_search_tokens(query[:512])
        now_clause = "(valid_from IS NULL OR valid_from<=datetime('now')) AND (valid_until IS NULL OR valid_until>datetime('now'))"
        verification_sql = ",".join("?" for _ in allowed_verifications) or "''"
        with self._connect() as conn:
            if terms:
                expression = _fts_or_expression(terms)
                rows = conn.execute(
                    "SELECT m.id,m.content,m.kind,m.status,m.revision,m.verification,m.confidence,"
                    "m.visibility,m.group_id,m.owner_user_id,m.updated_at,bm25(memory_fts) AS rank "
                    "FROM memory_fts JOIN memories m ON m.id=memory_fts.rowid "
                    "WHERE memory_fts MATCH ? AND m.status='active' AND m.kind='fact' "
                    f"AND m.verification IN ({verification_sql}) AND {now_clause} AND "
                    "((m.visibility='group' AND m.group_id=?) OR "
                    "(m.visibility='user' AND m.group_id=? AND m.owner_user_id=?)) "
                    "ORDER BY rank ASC,m.id DESC LIMIT ?",
                    (expression, *allowed_verifications, str(group_id), str(group_id), str(user_id), int(limit)),
                ).fetchall()
            else:
                rows = []
            if not rows and fallback_group_preferences:
                rows = conn.execute(
                    "SELECT id,content,kind,status,revision,verification,confidence,visibility,group_id,"
                    "owner_user_id,updated_at,0.0 AS rank FROM memories WHERE status='active' AND kind='fact' "
                    f"AND verification IN ({verification_sql}) AND visibility='group' AND group_id=? AND {now_clause} "
                    "ORDER BY id DESC LIMIT 2",
                    (*allowed_verifications, str(group_id)),
                ).fetchall()
        return [dict(row) for row in rows]

    def search_memories(self, query: str, *, group_id: str, user_id: str, limit: int = 20) -> list[dict[str, object]]:
        """Admin command search includes legacy rows, but remains strictly scope-bound."""
        terms = _history_search_tokens(query[:512])
        if not terms:
            return []
        expression = _fts_or_expression(terms)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT m.id,m.scope,m.scope_id,m.group_id,m.owner_user_id,m.content,m.kind,m.status,"
                "m.revision,m.verification,m.confidence,m.visibility,m.created_at,bm25(memory_fts) AS rank "
                "FROM memory_fts JOIN memories m ON m.id=memory_fts.rowid "
                "WHERE memory_fts MATCH ? AND m.status!='retracted' AND "
                "((m.group_id=? AND m.visibility IN ('group','legacy_unscoped')) OR "
                "(m.group_id=? AND m.owner_user_id=? AND m.visibility='user')) "
                "ORDER BY rank ASC,m.id DESC LIMIT ?",
                (expression, str(group_id), str(group_id), str(user_id), min(max(1, limit), 100)),
            ).fetchall()
            result = [dict(row) for row in rows]
            for item in result:
                item["sources"] = [dict(source) for source in conn.execute(
                    "SELECT source_kind,source_ref,source_hash,locator,verification,source_state,revision "
                    "FROM memory_sources WHERE memory_id=? ORDER BY revision DESC,observed_at DESC LIMIT 8",
                    (int(item["id"]),),
                ).fetchall()]
        return result

    def rebuild_memory_search_batch(self, limit: int = 500) -> dict[str, int | bool]:
        if not 1 <= limit <= 500:
            raise ValueError("memory index batches must contain 1 to 500 rows")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor_row = conn.execute(
                "SELECT value FROM memory_index_meta WHERE key='backfill_last_id'"
            ).fetchone()
            cursor = int(cursor_row["value"]) if cursor_row else 0
            rows = conn.execute(
                "SELECT id,content,status FROM memories WHERE id>? ORDER BY id LIMIT ?",
                (cursor, int(limit)),
            ).fetchall()
            if not rows:
                conn.execute("UPDATE memory_index_meta SET value='1' WHERE key='backfill_complete'")
                return {"indexed": 0, "last_id": cursor, "complete": True}
            for row in rows:
                self._set_memory_index(
                    conn, int(row["id"]), str(row["content"]), str(row["status"]) != "retracted"
                )
            last_id = int(rows[-1]["id"])
            more = conn.execute("SELECT 1 FROM memories WHERE id>? LIMIT 1", (last_id,)).fetchone()
            conn.execute("UPDATE memory_index_meta SET value=? WHERE key='backfill_last_id'", (str(last_id),))
            conn.execute(
                "UPDATE memory_index_meta SET value=? WHERE key='backfill_complete'", ("0" if more else "1",)
            )
            return {"indexed": len(rows), "last_id": last_id, "complete": not bool(more)}

    def list_memories(self, scope: str | None = None, scope_id: str | None = None, limit: int = 20) -> list[dict[str, str]]:
        clauses = []
        params: list[str | int] = []
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        if scope_id is not None:
            clauses.append("scope_id = ?")
            params.append(str(scope_id))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, scope, scope_id, content, source_user_id, created_at,kind,status,revision,
                       verification,confidence,visibility,group_id,owner_user_id,normalized_key
                FROM memories
                {where}
                  {'AND' if where else 'WHERE'} status != 'retracted'
                ORDER BY id DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_memory(self, memory_id: int, scope: str | None = None, scope_id: str | None = None) -> bool:
        clauses = ["id = ?"]
        params: list[str | int] = [memory_id]
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        if scope_id is not None:
            clauses.append("scope_id = ?")
            params.append(str(scope_id))
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"SELECT id,content,revision,verification FROM memories WHERE {' AND '.join(clauses)} "
                "AND status!='retracted'",
                params,
            ).fetchone()
            if row is None:
                return False
            revision = int(row["revision"]) + 1
            conn.execute(
                "UPDATE memories SET status='retracted',revision=?,updated_at=datetime('now') WHERE id=?",
                (revision, int(memory_id)),
            )
            self._set_memory_index(conn, int(memory_id), "", False)
            self._record_memory_revision(
                conn, int(memory_id), revision, str(row["content"]), str(row["verification"]), "retracted"
            )
            return True

    def delete_memories_by_keyword(
        self,
        keyword: str,
        scope: str | None = None,
        scope_id: str | None = None,
    ) -> int:
        clauses = ["content LIKE ?"]
        params: list[str] = [f"%{keyword}%"]
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        if scope_id is not None:
            clauses.append("scope_id = ?")
            params.append(str(scope_id))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT id,content,revision,verification FROM memories WHERE {' AND '.join(clauses)} "
                "AND status!='retracted'",
                params,
            ).fetchall()
            for row in rows:
                memory_id = int(row["id"])
                revision = int(row["revision"]) + 1
                conn.execute(
                    "UPDATE memories SET status='retracted',revision=?,updated_at=datetime('now') WHERE id=?",
                    (revision, memory_id),
                )
                self._set_memory_index(conn, memory_id, "", False)
                self._record_memory_revision(
                    conn, memory_id, revision, str(row["content"]), str(row["verification"]), "retracted"
                )
            return len(rows)

    def add_scheduled_task(
        self,
        group_id: str,
        created_by: str,
        prompt: str,
        schedule_type: str,
        next_run_at: int,
        interval_seconds: int = 0,
        created_at: int | None = None,
    ) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO scheduled_tasks (
                    group_id, created_by, prompt, schedule_type,
                    interval_seconds, next_run_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(group_id),
                    str(created_by),
                    prompt,
                    schedule_type,
                    int(interval_seconds),
                    int(next_run_at),
                    int(created_at or next_run_at),
                ),
            )
            return int(cursor.lastrowid)

    def list_scheduled_tasks(self, group_id: str, limit: int = 50) -> list[dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, group_id, created_by, prompt, schedule_type, interval_seconds,
                       next_run_at, created_at, last_run_at, run_count, enabled
                FROM scheduled_tasks
                WHERE group_id = ? AND enabled = 1
                ORDER BY next_run_at ASC, id ASC
                LIMIT ?
                """,
                (str(group_id), int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_scheduled_task(self, task_id: int, group_id: str | None = None) -> bool:
        clauses = ["id = ?"]
        params: list[str | int] = [int(task_id)]
        if group_id is not None:
            clauses.append("group_id = ?")
            params.append(str(group_id))
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM scheduled_tasks WHERE {' AND '.join(clauses)}",
                params,
            )
            return cursor.rowcount > 0

    def clear_scheduled_tasks(self, group_id: str) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM scheduled_tasks WHERE group_id = ? AND enabled = 1",
                (str(group_id),),
            )
            return int(cursor.rowcount)

    def claim_due_scheduled_tasks(self, now: int, limit: int = 5) -> list[dict[str, str]]:
        """Atomically claim due tasks so one scheduled occurrence runs only once."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, group_id, created_by, prompt, schedule_type, interval_seconds,
                       next_run_at, created_at, last_run_at, run_count, enabled
                FROM scheduled_tasks
                WHERE enabled = 1 AND running_since IS NULL AND next_run_at <= ?
                ORDER BY next_run_at ASC, id ASC
                LIMIT ?
                """,
                (int(now), int(limit)),
            ).fetchall()
            claimed = []
            for row in rows:
                cursor = conn.execute(
                    """
                    UPDATE scheduled_tasks SET running_since = ?
                    WHERE id = ? AND enabled = 1 AND running_since IS NULL AND next_run_at <= ?
                    """,
                    (int(now), int(row["id"]), int(now)),
                )
                if cursor.rowcount:
                    claimed.append(dict(row))
        return claimed

    def get_due_scheduled_tasks(self, now: int, limit: int = 5) -> list[dict[str, str]]:
        """Compatibility read used by older callers and tests; does not claim tasks."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, group_id, created_by, prompt, schedule_type, interval_seconds,
                       next_run_at, created_at, last_run_at, run_count, enabled
                FROM scheduled_tasks
                WHERE enabled = 1 AND running_since IS NULL AND next_run_at <= ?
                ORDER BY next_run_at ASC, id ASC LIMIT ?
                """,
                (int(now), int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def finish_scheduled_task(self, task_id: int, now: int) -> None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT interval_seconds, next_run_at FROM scheduled_tasks WHERE id = ?",
                (int(task_id),),
            ).fetchone()
            if not row:
                return
            interval_seconds = int(row["interval_seconds"] or 0)
            if interval_seconds > 0:
                next_run_at = int(row["next_run_at"])
                while next_run_at <= int(now):
                    next_run_at += interval_seconds
                conn.execute(
                    """
                    UPDATE scheduled_tasks
                    SET next_run_at = ?, last_run_at = ?, run_count = run_count + 1,
                        running_since = NULL, last_error = NULL
                    WHERE id = ?
                    """,
                    (next_run_at, int(now), int(task_id)),
                )
            else:
                conn.execute(
                    """
                    UPDATE scheduled_tasks
                    SET enabled = 0, last_run_at = ?, run_count = run_count + 1,
                        running_since = NULL
                    WHERE id = ?
                    """,
                    (int(now), int(task_id)),
                )

    def reschedule_scheduled_task(self, task_id: int, next_run_at: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE scheduled_tasks SET next_run_at = ? WHERE id = ? AND enabled = 1",
                (int(next_run_at), int(task_id)),
            )

    def release_scheduled_task(self, task_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE scheduled_tasks SET running_since = NULL WHERE id = ? AND enabled = 1",
                (int(task_id),),
            )

    def fail_scheduled_task(self, task_id: int, now: int, error: str) -> None:
        """Stop a failed task. Scheduled jobs are never retried implicitly."""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE scheduled_tasks
                SET enabled = 0, running_since = NULL, last_run_at = ?,
                    failure_count = failure_count + 1, last_error = ?
                WHERE id = ?
                """,
                (int(now), str(error)[:1000], int(task_id)),
            )

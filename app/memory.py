from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from threading import RLock


class MemoryStore:
    def __init__(self, sqlite_path: Path, history_limit: int = 20):
        self.sqlite_path = Path(sqlite_path)
        self.history_limit = history_limit
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_summaries (
                    group_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    last_message_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
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

    def add_message(self, group_id: str, user_id: str, nickname: str, role: str, content: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO message_history (group_id, user_id, nickname, role, content)
                VALUES (?, ?, ?, ?, ?)
                """,
                (str(group_id), str(user_id), nickname or "", role, content),
            )
            self._trim_history(conn, str(group_id))

    def _trim_history(self, conn: sqlite3.Connection, group_id: str) -> None:
        ids = [
            row["id"]
            for row in conn.execute(
                """
                SELECT id FROM message_history
                WHERE group_id = ?
                ORDER BY id DESC
                """,
                (group_id,),
            ).fetchall()
        ]
        if len(ids) <= self.history_limit:
            return
        for stale_id in ids[self.history_limit :]:
            conn.execute("DELETE FROM message_history WHERE id = ?", (stale_id,))

    def get_recent_messages(self, group_id: str, limit: int | None = None) -> list[dict[str, str]]:
        limit = limit or self.history_limit
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, group_id, user_id, nickname, role, content, created_at
                FROM message_history
                WHERE group_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (str(group_id), limit),
            ).fetchall()
        result = []
        for row in reversed(rows):
            result.append(dict(row))
        return result

    def clear_group_history(self, group_id: str) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM message_history WHERE group_id = ?",
                (str(group_id),),
            )
            return int(cursor.rowcount)

    def clear_group_context(self, group_id: str) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM message_history WHERE group_id = ?",
                (str(group_id),),
            )
            conn.execute(
                "DELETE FROM conversation_summaries WHERE group_id = ?",
                (str(group_id),),
            )
            return int(cursor.rowcount)

    def get_group_summary(self, group_id: str) -> dict[str, str] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT group_id, summary, last_message_id, updated_at
                FROM conversation_summaries
                WHERE group_id = ?
                """,
                (str(group_id),),
            ).fetchone()
        return dict(row) if row else None

    def upsert_group_summary(self, group_id: str, summary: str, last_message_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_summaries (group_id, summary, last_message_id, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(group_id) DO UPDATE SET
                    summary = excluded.summary,
                    last_message_id = excluded.last_message_id,
                    updated_at = datetime('now')
                """,
                (str(group_id), summary, int(last_message_id)),
            )

    def clear_group_summary(self, group_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM conversation_summaries WHERE group_id = ?",
                (str(group_id),),
            )
            return cursor.rowcount > 0

    def get_messages_for_summary(self, group_id: str, keep_recent: int) -> list[dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, group_id, user_id, nickname, role, content, created_at
                FROM message_history
                WHERE group_id = ?
                ORDER BY id ASC
                """,
                (str(group_id),),
            ).fetchall()
        if len(rows) <= keep_recent:
            return []
        return [dict(row) for row in rows[:-keep_recent]]

    def delete_messages_through(self, group_id: str, max_message_id: int) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM message_history
                WHERE group_id = ? AND id <= ?
                """,
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
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO memories (scope, scope_id, content, source_user_id)
                VALUES (?, ?, ?, ?)
                """,
                (scope, str(scope_id), content, str(source_user_id)),
            )
            return int(cursor.lastrowid)

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
                SELECT id, scope, scope_id, content, source_user_id, created_at
                FROM memories
                {where}
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
            cursor = conn.execute(
                f"DELETE FROM memories WHERE {' AND '.join(clauses)}",
                params,
            )
            return cursor.rowcount > 0

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
            cursor = conn.execute(
                f"DELETE FROM memories WHERE {' AND '.join(clauses)}",
                params,
            )
            return int(cursor.rowcount)

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

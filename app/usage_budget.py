from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
import uuid

from app.storage_executor import StorageExecutor


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BudgetScope:
    group_id: str | None = None
    user_id: str | None = None
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))


_budget_scope: ContextVar[BudgetScope | None] = ContextVar("llm_budget_scope", default=None)


def set_budget_scope(scope: BudgetScope) -> Token:
    return _budget_scope.set(scope)


def reset_budget_scope(token: Token) -> None:
    _budget_scope.reset(token)


def current_budget_scope() -> BudgetScope | None:
    return _budget_scope.get()


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @classmethod
    def from_payload(cls, payload: object) -> ProviderUsage | None:
        if not isinstance(payload, dict):
            return None
        input_tokens = payload.get("prompt_tokens", payload.get("input_tokens"))
        output_tokens = payload.get("completion_tokens", payload.get("output_tokens"))
        if (
            isinstance(input_tokens, bool)
            or isinstance(output_tokens, bool)
            or not isinstance(input_tokens, int)
            or not isinstance(output_tokens, int)
            or input_tokens < 0
            or output_tokens < 0
        ):
            return None
        return cls(input_tokens=input_tokens, output_tokens=output_tokens)


@dataclass(frozen=True, slots=True)
class ToolUsage:
    tool_call_count: int = 0
    model_request_count: int = 0
    input_tokens_reported: int | None = None
    output_tokens_reported: int | None = None
    tokens_reserved_estimate: int | None = None
    estimate_method: str | None = None
    cost_minor_units: int | None = None
    cost_source: str = "unknown"
    cost_status: str = "unknown"
    usage_status: str = "unknown"


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    global_requests_per_day: int | None = None
    group_requests_per_day: int | None = None
    user_requests_per_day: int | None = None
    global_tokens_per_day: int | None = None
    group_tokens_per_day: int | None = None
    user_tokens_per_day: int | None = None
    default_max_completion_tokens: int | None = None
    reservation_lease_seconds: int = 300

    def __post_init__(self) -> None:
        for value in (
            self.global_requests_per_day,
            self.group_requests_per_day,
            self.user_requests_per_day,
            self.global_tokens_per_day,
            self.group_tokens_per_day,
            self.user_tokens_per_day,
        ):
            if value is not None and value < 1:
                raise ValueError("configured budget limits must be positive")
        if self.default_max_completion_tokens is not None and self.default_max_completion_tokens < 1:
            raise ValueError("default completion token limit must be positive")
        if self.reservation_lease_seconds < 1:
            raise ValueError("reservation lease must be positive")
        if self.has_token_limits and self.default_max_completion_tokens is None:
            raise ValueError("token budgets require a default max completion token limit")

    @property
    def has_token_limits(self) -> bool:
        return any(
            value is not None
            for value in (
                self.global_tokens_per_day,
                self.group_tokens_per_day,
                self.user_tokens_per_day,
            )
        )


class BudgetExceeded(RuntimeError):
    def __init__(self, dimension: str, scope: str) -> None:
        self.dimension = dimension
        self.scope = scope
        super().__init__(f"{dimension} budget exceeded for {scope}")


class BudgetConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    reservation_id: str
    request_id: str
    lease_expires_at: int
    reserved_tokens_estimate: int | None
    estimate_method: str
    scope_keys: tuple[str, ...] = ()
    reserved_model_requests: int = 1
    reserved_cost_minor_units: int | None = None


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    scope_kind: str
    scope_id: str | None
    request_count: int
    reported_requests: int
    reserved_requests: int
    unknown_requests: int
    reported_input_tokens: int | None
    reported_output_tokens: int | None
    estimated_reserved_tokens: int
    estimated_unknown_tokens: int
    cost_minor_units: int | None


def estimate_input_tokens(request_payload: dict[str, object]) -> int:
    """Estimate the whole request prompt, including tool schemas, not just messages."""
    serialized = json.dumps(request_payload, ensure_ascii=False, separators=(",", ":"))
    byte_count = len(serialized.encode("utf-8"))
    messages = request_payload.get("messages")
    message_count = len(messages) if isinstance(messages, list) else 0
    # This deliberately remains an estimate: UTF-8 bytes do not reproduce a
    # provider tokenizer and can still differ from its accounting.
    return max(1, byte_count + 8 * message_count + 16)


class UsageBudgetStore:
    """SQLite reservation ledger. It stores IDs and counts, never prompts or credentials."""

    def __init__(self, sqlite_path: Path) -> None:
        self.sqlite_path = Path(sqlite_path)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS model_usage_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    group_id TEXT,
                    user_id TEXT,
                    created_at INTEGER NOT NULL,
                    lease_expires_at INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    reserved_input_tokens INTEGER,
                    reserved_output_tokens INTEGER,
                    reserved_total_tokens INTEGER,
                    estimate_method TEXT NOT NULL DEFAULT 'legacy-unknown',
                    reported_input_tokens INTEGER,
                    reported_output_tokens INTEGER,
                    cost_minor_units INTEGER,
                    cost_status TEXT NOT NULL DEFAULT 'unknown',
                    usage_status TEXT NOT NULL,
                    outcome TEXT NOT NULL DEFAULT ''
                )
                """
            )
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(model_usage_reservations)").fetchall()
            }
            if "estimate_method" not in columns:
                conn.execute(
                    "ALTER TABLE model_usage_reservations ADD COLUMN "
                    "estimate_method TEXT NOT NULL DEFAULT 'legacy-unknown'"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_usage_created_status "
                "ON model_usage_reservations(created_at, status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_usage_group_created "
                "ON model_usage_reservations(group_id, created_at, status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_usage_user_created "
                "ON model_usage_reservations(user_id, created_at, status)"
            )
        finally:
            conn.close()

    def _connect(self):
        conn = sqlite3.connect(self.sqlite_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _utc_day_start(now: int) -> int:
        moment = datetime.fromtimestamp(now, timezone.utc)
        return int(moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    @staticmethod
    def _usage_totals(conn: sqlite3.Connection, scope_kind: str, scope_id: str | None, day_start: int):
        status_clause = "status IN ('reserved', 'settled', 'unknown_settled')"
        if scope_kind == "global":
            scope_clause = "1 = 1"
            params: tuple[object, ...] = (day_start,)
        elif scope_kind == "group":
            scope_clause = "group_id = ?"
            params = (day_start, scope_id)
        else:
            scope_clause = "user_id = ?"
            params = (day_start, scope_id)
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS request_count,
                   COALESCE(SUM(
                       CASE WHEN status = 'settled' AND usage_status = 'reported'
                            THEN COALESCE(reported_input_tokens, 0) + COALESCE(reported_output_tokens, 0)
                            ELSE COALESCE(reserved_total_tokens, 0) END
                   ), 0) AS token_count
            FROM model_usage_reservations
            WHERE created_at >= ? AND {status_clause} AND {scope_clause}
            """,
            params,
        ).fetchone()
        return int(row["request_count"]), int(row["token_count"])

    def reserve(
        self,
        reservation_id: str,
        request_id: str,
        model: str,
        scope: BudgetScope | None,
        input_estimate: int,
        max_output_tokens: int | None,
        now: int,
        lease_seconds: int,
        limits: BudgetLimits,
    ) -> BudgetReservation:
        group_id = str(scope.group_id) if scope and scope.group_id else None
        user_id = str(scope.user_id) if scope and scope.user_id else None
        request_id = request_id[:120]
        model = model[:200]
        total_estimate = input_estimate + max_output_tokens if max_output_tokens is not None else None
        scopes = (
            ("global", None, limits.global_requests_per_day, limits.global_tokens_per_day),
            ("group", group_id, limits.group_requests_per_day, limits.group_tokens_per_day),
            ("user", user_id, limits.user_requests_per_day, limits.user_tokens_per_day),
        )
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            day_start = self._utc_day_start(now)
            for kind, scope_id, request_limit, token_limit in scopes:
                if kind != "global" and scope_id is None:
                    continue
                request_count, token_count = self._usage_totals(conn, kind, scope_id, day_start)
                if request_limit is not None and request_count >= request_limit:
                    raise BudgetExceeded("requests_per_day", kind)
                if token_limit is not None:
                    if total_estimate is None:
                        raise BudgetConfigurationError(
                            "token budget requires a provider-enforced max completion token limit"
                        )
                    if token_count + total_estimate > token_limit:
                        raise BudgetExceeded("tokens_per_day", kind)
            lease_expires_at = now + lease_seconds
            conn.execute(
                """
                INSERT INTO model_usage_reservations (
                    reservation_id, request_id, model, group_id, user_id, created_at,
                    lease_expires_at, status, reserved_input_tokens, reserved_output_tokens,
                    reserved_total_tokens, estimate_method, usage_status, outcome
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?, 'unknown', '')
                """,
                (
                    reservation_id,
                    request_id,
                    model,
                    group_id,
                    user_id,
                    now,
                    lease_expires_at,
                    input_estimate,
                    max_output_tokens,
                    total_estimate,
                    "utf8-bytes-plus-message-overhead-v1",
                ),
            )
            conn.execute("COMMIT")
            return BudgetReservation(
                reservation_id=reservation_id,
                request_id=request_id,
                lease_expires_at=lease_expires_at,
                reserved_tokens_estimate=total_estimate,
                estimate_method="utf8-bytes-plus-message-overhead-v1",
                scope_keys=("global",)
                + ((f"group:{group_id}",) if group_id is not None else ())
                + ((f"user:{user_id}",) if user_id is not None else ()),
            )
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def settle(self, reservation_id: str, usage: ProviderUsage | None, outcome: str) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if usage is None:
                conn.execute(
                    """
                    UPDATE model_usage_reservations
                    SET status = 'unknown_settled', usage_status = 'unknown', outcome = ?
                    WHERE reservation_id = ? AND status = 'reserved'
                    """,
                    (outcome[:80], reservation_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE model_usage_reservations
                    SET status = 'settled', usage_status = 'reported',
                        reported_input_tokens = ?, reported_output_tokens = ?, outcome = ?
                    WHERE reservation_id = ? AND status = 'reserved'
                    """,
                    (usage.input_tokens, usage.output_tokens, outcome[:80], reservation_id),
                )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def release(self, reservation_id: str, reason: str) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE model_usage_reservations
                SET status = 'released', usage_status = 'not_dispatched', outcome = ?
                WHERE reservation_id = ? AND status = 'reserved'
                """,
                (reason[:80], reservation_id),
            )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def renew(self, reservation_id: str, now: int, lease_seconds: int) -> bool:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE model_usage_reservations
                SET lease_expires_at = ?
                WHERE reservation_id = ? AND status = 'reserved' AND lease_expires_at > ?
                """,
                (now + lease_seconds, reservation_id, now),
            )
            conn.execute("COMMIT")
            return cursor.rowcount == 1
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def recover_expired(self, now: int) -> int:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE model_usage_reservations
                SET status = 'unknown_settled', usage_status = 'unknown',
                    outcome = 'expired_reservation_recovered'
                WHERE status = 'reserved' AND lease_expires_at <= ?
                """,
                (now,),
            )
            conn.execute("COMMIT")
            return int(cursor.rowcount)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def get_reservation(self, reservation_id: str) -> dict[str, object] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM model_usage_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def usage_snapshot(self, scope_kind: str, scope_id: str | None, now: int) -> UsageSnapshot:
        """Read one UTC-day usage summary without presenting estimates as actual usage."""
        if scope_kind not in {"global", "group", "user"}:
            raise ValueError("scope_kind must be global, group, or user")
        if scope_kind != "global" and not scope_id:
            raise ValueError("scoped usage requires a scope id")
        day_start = self._utc_day_start(now)
        day_end = day_start + 86400
        scope_clause = "1 = 1" if scope_kind == "global" else f"{scope_kind}_id = ?"
        params: tuple[object, ...] = (day_start, day_end) if scope_kind == "global" else (day_start, day_end, scope_id)
        conn = self._connect()
        try:
            row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS request_count,
                    SUM(CASE WHEN status = 'settled' AND usage_status = 'reported' THEN 1 ELSE 0 END) AS reported_requests,
                    SUM(CASE WHEN status = 'reserved' THEN 1 ELSE 0 END) AS reserved_requests,
                    SUM(CASE WHEN status = 'unknown_settled' THEN 1 ELSE 0 END) AS unknown_requests,
                    SUM(CASE WHEN status = 'settled' AND usage_status = 'reported' THEN COALESCE(reported_input_tokens, 0) ELSE 0 END) AS reported_input_tokens,
                    SUM(CASE WHEN status = 'settled' AND usage_status = 'reported' THEN COALESCE(reported_output_tokens, 0) ELSE 0 END) AS reported_output_tokens,
                    SUM(CASE WHEN status = 'reserved' THEN COALESCE(reserved_total_tokens, 0) ELSE 0 END) AS estimated_reserved_tokens,
                    SUM(CASE WHEN status = 'unknown_settled' THEN COALESCE(reserved_total_tokens, 0) ELSE 0 END) AS estimated_unknown_tokens,
                    SUM(CASE WHEN cost_status = 'reported' THEN cost_minor_units ELSE 0 END) AS cost_minor_units,
                    SUM(CASE WHEN cost_status = 'reported' THEN 1 ELSE 0 END) AS cost_rows
                FROM model_usage_reservations
                WHERE created_at >= ? AND created_at < ?
                  AND status IN ('reserved', 'settled', 'unknown_settled')
                  AND {scope_clause}
                """,
                params,
            ).fetchone()
        finally:
            conn.close()
        reported_count = int(row["reported_requests"] or 0)
        return UsageSnapshot(
            scope_kind=scope_kind,
            scope_id=scope_id,
            request_count=int(row["request_count"] or 0),
            reported_requests=reported_count,
            reserved_requests=int(row["reserved_requests"] or 0),
            unknown_requests=int(row["unknown_requests"] or 0),
            reported_input_tokens=int(row["reported_input_tokens"] or 0) if reported_count else None,
            reported_output_tokens=int(row["reported_output_tokens"] or 0) if reported_count else None,
            estimated_reserved_tokens=int(row["estimated_reserved_tokens"] or 0),
            estimated_unknown_tokens=int(row["estimated_unknown_tokens"] or 0),
            cost_minor_units=(int(row["cost_minor_units"] or 0) if int(row["cost_rows"] or 0) else None),
        )


class BudgetController:
    def __init__(
        self,
        store: UsageBudgetStore,
        storage_executor: StorageExecutor,
        limits: BudgetLimits | None = None,
        *,
        max_concurrent_model_requests: int = 4,
    ) -> None:
        if not 1 <= max_concurrent_model_requests <= 32:
            raise ValueError("model request concurrency must be between 1 and 32")
        self.store = store
        self.storage_executor = storage_executor
        self.limits = limits or BudgetLimits()
        self._model_request_slots = asyncio.Semaphore(max_concurrent_model_requests)

    @asynccontextmanager
    async def model_request_slot(self):
        """Share a process-wide concurrency cap across every configured model client."""
        await self._model_request_slots.acquire()
        try:
            yield
        finally:
            self._model_request_slots.release()

    def prepare_payload(self, payload: dict[str, object]) -> dict[str, object]:
        prepared = dict(payload)
        if self.limits.has_token_limits:
            configured_max = self.limits.default_max_completion_tokens
            current_max = prepared.get("max_tokens")
            if current_max is None:
                prepared["max_tokens"] = configured_max
            else:
                prepared["max_tokens"] = min(int(current_max), int(configured_max))
        return prepared

    async def reserve_budget(
        self,
        scope: BudgetScope | None,
        request_id: str,
        model: str,
        request_payload: dict[str, object],
        max_output_tokens: int | None,
        *,
        cost_limit_minor_units: int | None = None,
    ) -> BudgetReservation:
        if cost_limit_minor_units is not None:
            raise BudgetConfigurationError(
                "currency budget is unavailable without a configured, auditable provider rate card"
            )
        now = int(datetime.now(timezone.utc).timestamp())
        reservation_id = str(uuid.uuid4())
        reservation_task = asyncio.create_task(
            self.storage_executor.run_sqlite(
                self.store.reserve,
                reservation_id,
                request_id,
                model,
                scope,
                estimate_input_tokens(request_payload),
                max_output_tokens,
                now,
                self.limits.reservation_lease_seconds,
                self.limits,
            ),
            name="llm-budget-reservation",
        )
        try:
            return await asyncio.shield(reservation_task)
        except asyncio.CancelledError:
            # StorageExecutor intentionally lets an already-submitted sync
            # operation finish after its caller is cancelled. Recover its
            # result and release it so a pre-dispatch cancellation does not
            # leave a false reservation until lease recovery.
            try:
                reservation = await asyncio.shield(reservation_task)
            except Exception:  # noqa: BLE001
                LOGGER.exception("budget reservation failed while caller was cancelled")
            else:
                release_task = asyncio.create_task(
                    self.release_budget(reservation, "caller_cancelled_before_dispatch"),
                    name="llm-budget-release-cancelled",
                )
                try:
                    await asyncio.shield(release_task)
                except Exception:  # noqa: BLE001
                    LOGGER.exception("failed to release reservation after caller cancellation")
            raise

    async def settle_budget(
        self,
        reservation: BudgetReservation,
        usage: ProviderUsage | None,
        outcome: str,
    ) -> None:
        await self.storage_executor.run_sqlite(
            self.store.settle,
            reservation.reservation_id,
            usage,
            outcome,
        )

    async def release_budget(self, reservation: BudgetReservation, reason: str) -> None:
        await self.storage_executor.run_sqlite(
            self.store.release,
            reservation.reservation_id,
            reason,
        )

    async def renew_budget(self, reservation: BudgetReservation) -> bool:
        now = int(datetime.now(timezone.utc).timestamp())
        return await self.storage_executor.run_sqlite(
            self.store.renew,
            reservation.reservation_id,
            now,
            self.limits.reservation_lease_seconds,
        )

    async def recover_expired_reservations(self, now: int | None = None) -> int:
        timestamp = now or int(datetime.now(timezone.utc).timestamp())
        return await self.storage_executor.run_sqlite(self.store.recover_expired, timestamp)

    async def get_usage_snapshot(
        self,
        scope_kind: str,
        scope_id: str | None = None,
        now: int | None = None,
    ) -> UsageSnapshot:
        timestamp = now or int(datetime.now(timezone.utc).timestamp())
        return await self.storage_executor.run_sqlite(
            self.store.usage_snapshot, scope_kind, scope_id, timestamp
        )

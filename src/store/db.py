"""SQLite access and the row types that cross module boundaries.

Three rules this module exists to hold:

* Money is integer paise. There is no float arithmetic anywhere in here.
* Timestamps are integer unix seconds, always passed in. Nothing reads a clock.
* IDs are derived from stable keys, never from a counter, a UUID or the clock, so
  two runs with the same seed produce byte-identical rows.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "triage.db"

_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


class StoreError(Exception):
    """Base for store-level failures."""


class IdempotencyConflict(StoreError):
    """An attempt with this idempotency key already exists. (I-5)"""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"idempotency key {key!r} has already been used")


# -- identifiers --------------------------------------------------------------


def base62(value: int, length: int = 8) -> str:
    """Fixed-length base62 rendering of a non-negative integer."""
    out = []
    for _ in range(length):
        value, rem = divmod(value, 62)
        out.append(_BASE62[rem])
    return "".join(reversed(out))


def stable_id(prefix: str, *parts: Any, length: int = 8) -> str:
    """Deterministic Razorpay-style id derived from the parts that identify a thing.

    Same inputs, same id — across processes and across runs. That is what lets the
    simulator be byte-identical on a fixed seed, and what makes ``POST /cases``
    idempotent on ``payment_id`` without a lookup-then-insert race.
    """
    key = "|".join(str(p) for p in parts)
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    return f"{prefix}{base62(int.from_bytes(digest, 'big'), length)}"


# -- row types ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Payment:
    id: str
    customer_id: str
    merchant_id: str
    method: str
    rail: str
    amount_paise: int
    created_at: int
    first_outcome: str
    first_error_code: str | None


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    payment_id: str
    customer_id: str
    merchant_id: str
    method: str
    rail: str
    amount_paise: int
    error_code: str
    error_source: str | None
    failed_at: int
    city_tier: int | None
    vpa_handle: str | None
    payer_bank: str | None
    state: str
    arm: str | None
    max_attempts: int
    drop_dead_at: int
    next_attempt_at: int | None
    status_resolved_at: int | None
    nudge_sent_at: int | None
    recovered_at: int | None
    recovered_amount_paise: int | None
    created_at: int


@dataclass(frozen=True, slots=True)
class CaseView:
    """Exactly what the attempt oracle is allowed to see about a case.

    Observable fields only — what a real recovery system would have on hand. The
    world's latent state is keyed off ``customer_id``; it is never carried in here,
    and nothing latent ever travels back. (I-12)
    """

    id: str
    customer_id: str
    merchant_id: str
    method: str
    rail: str
    amount_paise: int
    error_code: str | None
    failed_at: int
    attempt_number: int


@dataclass(frozen=True, slots=True)
class Attempt:
    id: str
    case_id: str
    attempt_number: int
    idempotency_key: str
    action: str
    target_rail: str | None
    scheduled_at: int | None
    executed_at: int
    outcome: str
    error_code: str | None
    latency_ms: int


@dataclass(frozen=True, slots=True)
class AuditRow:
    id: int
    case_id: str
    at: int
    from_state: str | None
    to_state: str
    actor: str
    reason: str
    idempotency_key: str | None
    detail: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class Downtime:
    id: str
    method: str
    scope: str
    instrument: str | None
    severity: str
    status: str
    begin: int
    end: int | None

    def as_dict(self) -> dict[str, Any]:
        """The Razorpay Downtime API shape (research/05 §5.3)."""
        return {
            "id": self.id,
            "entity": "payment.downtime",
            "method": self.method,
            "scope": self.scope,
            "instrument": self.instrument,
            "severity": self.severity,
            "status": self.status,
            "begin": self.begin,
            "end": self.end,
        }


# -- connection ---------------------------------------------------------------


def connect(path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def reset_db(conn: sqlite3.Connection) -> None:
    """Drop every table and rebuild. Used by the simulator before a fresh run."""
    for table in ("audit", "attempts", "downtimes", "cases", "payments", "runs"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()
    init_db(conn)


def open_db(path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = connect(path)
    init_db(conn)
    return conn


# -- payments -----------------------------------------------------------------


def insert_payments(conn: sqlite3.Connection, payments: Iterable[Payment]) -> int:
    rows = [
        (
            p.id,
            p.customer_id,
            p.merchant_id,
            p.method,
            p.rail,
            p.amount_paise,
            p.created_at,
            p.first_outcome,
            p.first_error_code,
        )
        for p in payments
    ]
    conn.executemany(
        "INSERT INTO payments (id, customer_id, merchant_id, method, rail, "
        "amount_paise, created_at, first_outcome, first_error_code) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def get_payment(conn: sqlite3.Connection, payment_id: str) -> Payment | None:
    row = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    return Payment(**dict(row)) if row else None


def count_payments(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0])


# -- cases --------------------------------------------------------------------

_CASE_COLUMNS = (
    "id, payment_id, customer_id, merchant_id, method, rail, amount_paise, "
    "error_code, error_source, failed_at, city_tier, vpa_handle, payer_bank, "
    "state, arm, max_attempts, drop_dead_at, next_attempt_at, status_resolved_at, "
    "nudge_sent_at, recovered_at, recovered_amount_paise, created_at"
)


def _to_case(row: sqlite3.Row) -> Case:
    return Case(**dict(row))


def insert_case(conn: sqlite3.Connection, case: Case) -> Case:
    conn.execute(
        f"INSERT INTO cases ({_CASE_COLUMNS}) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            case.id,
            case.payment_id,
            case.customer_id,
            case.merchant_id,
            case.method,
            case.rail,
            case.amount_paise,
            case.error_code,
            case.error_source,
            case.failed_at,
            case.city_tier,
            case.vpa_handle,
            case.payer_bank,
            case.state,
            case.arm,
            case.max_attempts,
            case.drop_dead_at,
            case.next_attempt_at,
            case.status_resolved_at,
            case.nudge_sent_at,
            case.recovered_at,
            case.recovered_amount_paise,
            case.created_at,
        ),
    )
    return case


def get_case(conn: sqlite3.Connection, case_id: str) -> Case | None:
    row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    return _to_case(row) if row else None


def get_case_by_payment(conn: sqlite3.Connection, payment_id: str) -> Case | None:
    row = conn.execute(
        "SELECT * FROM cases WHERE payment_id = ?", (payment_id,)
    ).fetchone()
    return _to_case(row) if row else None


def update_case(conn: sqlite3.Connection, case_id: str, **fields: Any) -> None:
    """Partial update. ``state`` is deliberately not settable here — only the state
    machine writes it, and only after it has written an audit row. (I-8)"""
    if "state" in fields:
        raise StoreError("case state is written by src.executor.state, not by db.update_case")
    _assign(conn, case_id, fields)


def _assign(conn: sqlite3.Connection, case_id: str, fields: dict[str, Any]) -> None:
    if not fields:
        return
    clause = ", ".join(f"{name} = ?" for name in fields)
    conn.execute(
        f"UPDATE cases SET {clause} WHERE id = ?", (*fields.values(), case_id)
    )


def set_case_state(conn: sqlite3.Connection, case_id: str, state: str, **fields: Any) -> None:
    """Only ``src.executor.state.transition`` should reach this, after auditing."""
    _assign(conn, case_id, {"state": state, **fields})


def list_cases(
    conn: sqlite3.Connection,
    *,
    state: str | None = None,
    arm: str | None = None,
    error_code: str | None = None,
    method: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Case]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("state", state),
        ("arm", arm),
        ("error_code", error_code),
        ("method", method),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    rows = conn.execute(
        f"SELECT * FROM cases{where} ORDER BY created_at, id LIMIT ? OFFSET ?", params
    ).fetchall()
    return [_to_case(row) for row in rows]


def count_cases(conn: sqlite3.Connection, **filters: Any) -> int:
    clauses = [f"{k} = ?" for k, v in filters.items() if v is not None]
    params = [v for v in filters.values() if v is not None]
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return int(conn.execute(f"SELECT COUNT(*) FROM cases{where}", params).fetchone()[0])


def due_cases(conn: sqlite3.Connection, now: int, limit: int = 1000) -> list[Case]:
    """SCHEDULED cases whose next attempt time has arrived."""
    rows = conn.execute(
        "SELECT * FROM cases WHERE state = 'SCHEDULED' AND next_attempt_at IS NOT NULL "
        "AND next_attempt_at <= ? ORDER BY next_attempt_at, id LIMIT ?",
        (now, limit),
    ).fetchall()
    return [_to_case(row) for row in rows]


# -- attempts -----------------------------------------------------------------


def insert_attempt(conn: sqlite3.Connection, attempt: Attempt) -> Attempt:
    """Insert one attempt. A reused idempotency key raises IdempotencyConflict.

    The UNIQUE index is what actually rejects the duplicate; this only translates
    the constraint violation into a typed error. Bypassing this function does not
    bypass the guard.
    """
    try:
        conn.execute(
            "INSERT INTO attempts (id, case_id, attempt_number, idempotency_key, "
            "action, target_rail, scheduled_at, executed_at, outcome, error_code, "
            "latency_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attempt.id,
                attempt.case_id,
                attempt.attempt_number,
                attempt.idempotency_key,
                attempt.action,
                attempt.target_rail,
                attempt.scheduled_at,
                attempt.executed_at,
                attempt.outcome,
                attempt.error_code,
                attempt.latency_ms,
            ),
        )
    except sqlite3.IntegrityError as exc:
        if "idempotency_key" in str(exc):
            raise IdempotencyConflict(attempt.idempotency_key) from exc
        raise
    return attempt


def list_attempts(conn: sqlite3.Connection, case_id: str) -> list[Attempt]:
    rows = conn.execute(
        "SELECT * FROM attempts WHERE case_id = ? ORDER BY attempt_number", (case_id,)
    ).fetchall()
    return [Attempt(**dict(row)) for row in rows]


def attempt_count(conn: sqlite3.Connection, case_id: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE case_id = ?", (case_id,)
        ).fetchone()[0]
    )


def last_attempt(conn: sqlite3.Connection, case_id: str) -> Attempt | None:
    row = conn.execute(
        "SELECT * FROM attempts WHERE case_id = ? ORDER BY attempt_number DESC LIMIT 1",
        (case_id,),
    ).fetchone()
    return Attempt(**dict(row)) if row else None


def idempotency_key_exists(conn: sqlite3.Connection, key: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM attempts WHERE idempotency_key = ? LIMIT 1", (key,)
        ).fetchone()
        is not None
    )


# -- audit --------------------------------------------------------------------


def append_audit(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    at: int,
    from_state: str | None,
    to_state: str,
    actor: str,
    reason: str,
    idempotency_key: str | None = None,
    detail: dict[str, Any] | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO audit (case_id, at, from_state, to_state, actor, reason, "
        "idempotency_key, detail_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            case_id,
            at,
            from_state,
            to_state,
            actor,
            reason,
            idempotency_key,
            json.dumps(detail, sort_keys=True) if detail is not None else None,
        ),
    )
    return int(cursor.lastrowid or 0)


def list_audit(conn: sqlite3.Connection, case_id: str) -> list[AuditRow]:
    rows = conn.execute(
        "SELECT * FROM audit WHERE case_id = ? ORDER BY id", (case_id,)
    ).fetchall()
    return [
        AuditRow(
            id=row["id"],
            case_id=row["case_id"],
            at=row["at"],
            from_state=row["from_state"],
            to_state=row["to_state"],
            actor=row["actor"],
            reason=row["reason"],
            idempotency_key=row["idempotency_key"],
            detail=json.loads(row["detail_json"]) if row["detail_json"] else None,
        )
        for row in rows
    ]


# -- downtimes ----------------------------------------------------------------


def insert_downtimes(conn: sqlite3.Connection, events: Iterable[Downtime]) -> int:
    rows = [
        (d.id, d.method, d.scope, d.instrument, d.severity, d.status, d.begin, d.end)
        for d in events
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO downtimes (id, method, scope, instrument, severity, "
        "status, begin, end) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def list_downtimes(
    conn: sqlite3.Connection,
    *,
    at: int | None = None,
    method: str | None = None,
) -> list[Downtime]:
    """All downtimes, or only those active at ``at``."""
    clauses: list[str] = []
    params: list[Any] = []
    if method is not None:
        clauses.append("method = ?")
        params.append(method)
    if at is not None:
        clauses.append("begin <= ? AND (end IS NULL OR end > ?)")
        params.extend([at, at])
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM downtimes{where} ORDER BY begin, id", params
    ).fetchall()
    return [
        Downtime(
            id=row["id"],
            method=row["method"],
            scope=row["scope"],
            instrument=row["instrument"],
            severity=row["severity"],
            status=row["status"],
            begin=row["begin"],
            end=row["end"],
        )
        for row in rows
    ]


TERMINAL_SQL = "('RECOVERED', 'EXHAUSTED', 'STOPPED')"


def due_for_tick(conn: sqlite3.Connection, now: int, arm: str | None = None) -> list[Case]:
    """Non-terminal cases that need a decision at this tick.

    Filtered in SQL rather than in Python: a 37-day window at hourly ticks asks this
    question ~900 times, and most cases are simply waiting on a scheduled time.

    A case needs attention when it is newly received, its scheduled time has arrived,
    it is sitting in a state that is polled every tick (a nudge awaiting a response,
    an unresolved status), or its drop-dead time has passed.
    """
    clauses = [
        "state NOT IN " + TERMINAL_SQL,
        "("
        "state = 'RECEIVED'"
        " OR (next_attempt_at IS NOT NULL AND next_attempt_at <= ?)"
        " OR state IN ('ESCALATED', 'AWAITING_STATUS')"
        " OR drop_dead_at <= ?"
        ")",
    ]
    params: list[Any] = [now, now]
    if arm is not None:
        clauses.append("arm = ?")
        params.append(arm)
    rows = conn.execute(
        f"SELECT * FROM cases WHERE {' AND '.join(clauses)} ORDER BY id", params
    ).fetchall()
    return [_to_case(row) for row in rows]


def open_cases(conn: sqlite3.Connection, arm: str | None = None) -> list[Case]:
    """Every case not yet in a terminal state."""
    sql = f"SELECT * FROM cases WHERE state NOT IN {TERMINAL_SQL}"
    params: list[Any] = []
    if arm is not None:
        sql += " AND arm = ?"
        params.append(arm)
    return [_to_case(r) for r in conn.execute(sql + " ORDER BY id", params).fetchall()]


def assign_arms(conn: sqlite3.Connection, arms: Sequence[str]) -> dict[str, int]:
    """Partition the generated cases across arms by a stable hash of case_id.

    Assignment, not regeneration: every arm faces the same world, and which arm a
    case lands in does not depend on the order arms are listed or run. (I-13)
    """
    if not arms:
        raise StoreError("at least one arm is required")
    ordered = sorted(arms)
    counts = dict.fromkeys(ordered, 0)
    for case in conn.execute("SELECT id FROM cases ORDER BY id").fetchall():
        bucket = arm_for(case["id"], ordered)
        conn.execute("UPDATE cases SET arm = ? WHERE id = ?", (bucket, case["id"]))
        counts[bucket] += 1
    return counts


def arm_for(case_id: str, arms: Sequence[str]) -> str:
    """Which arm a case belongs to. Pure, stable, order-independent."""
    ordered = sorted(arms)
    digest = hashlib.blake2b(case_id.encode("utf-8"), digest_size=8).digest()
    return ordered[int.from_bytes(digest, "big") % len(ordered)]


# -- runs ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Run:
    run_id: str
    seed: int
    n_payments: int
    days: int
    scenario: str
    trailing_days: int
    tick_seconds: int
    arms: str
    start_ts: int
    created_at: int
    git_sha: str | None

    @property
    def arm_names(self) -> list[str]:
        return [a for a in self.arms.split(",") if a]


def insert_run(conn: sqlite3.Connection, run: Run) -> Run:
    conn.execute(
        "INSERT OR REPLACE INTO runs (run_id, seed, n_payments, days, scenario, "
        "trailing_days, tick_seconds, arms, start_ts, created_at, git_sha) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run.run_id, run.seed, run.n_payments, run.days, run.scenario,
            run.trailing_days, run.tick_seconds, run.arms, run.start_ts,
            run.created_at, run.git_sha,
        ),
    )
    return run


def get_run(conn: sqlite3.Connection, run_id: str) -> Run | None:
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return Run(**dict(row)) if row else None


def list_runs(conn: sqlite3.Connection) -> list[Run]:
    rows = conn.execute("SELECT * FROM runs ORDER BY run_id").fetchall()
    return [Run(**dict(r)) for r in rows]


def fetch_all(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    """Escape hatch for reporting queries. Returns plain dicts."""
    return [dict(row) for row in conn.execute(sql, params).fetchall()]

"""I-5 — every attempt carries an idempotency key, enforced by a UNIQUE index.

Not by application logic. Not by an ``if``. A UNIQUE constraint at the schema level,
so a race condition cannot produce a double charge. The API pre-checks for a clean
409, but the pre-check is a convenience: the tests below bypass it and show the
database still refuses.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from src.store import db
from tests.conftest import NOW, open_case

KEY = "case_DEMO:1"


def _attempt(case_id: str, number: int, key: str) -> db.Attempt:
    return db.Attempt(
        id=db.stable_id("att_", case_id, number, key),
        case_id=case_id,
        attempt_number=number,
        idempotency_key=key,
        action="RETRY_NOW",
        target_rail=None,
        scheduled_at=NOW,
        executed_at=NOW,
        outcome="failed",
        error_code="payment_failed",
        latency_ms=1200,
    )


# -- the constraint itself ----------------------------------------------------


def test_unique_index_exists_on_idempotency_key(conn: sqlite3.Connection) -> None:
    indexes = {
        row["name"]: row for row in conn.execute("PRAGMA index_list('attempts')")
    }
    assert "idx_attempts_idempotency_key" in indexes
    assert indexes["idx_attempts_idempotency_key"]["unique"] == 1


def test_raw_insert_with_a_duplicate_key_is_refused(
    conn: sqlite3.Connection, runner, world
) -> None:
    """The guard holds even when application logic is bypassed entirely."""
    case = _seed_case(conn, runner, "insufficient_funds")
    db.insert_attempt(conn, _attempt(case.id, 1, KEY))

    # Straight SQL. No helper, no pre-check, no typed exception in the way.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO attempts (id, case_id, attempt_number, idempotency_key, "
            "action, target_rail, scheduled_at, executed_at, outcome, error_code, "
            "latency_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("att_OTHER", case.id, 2, KEY, "RETRY_NOW", None, NOW, NOW, "failed", None, 5),
        )


def test_insert_attempt_translates_the_violation(
    conn: sqlite3.Connection, runner, world
) -> None:
    case = _seed_case(conn, runner, "insufficient_funds")
    db.insert_attempt(conn, _attempt(case.id, 1, KEY))
    with pytest.raises(db.IdempotencyConflict) as excinfo:
        db.insert_attempt(conn, _attempt(case.id, 2, KEY))
    assert excinfo.value.key == KEY


def test_distinct_keys_are_accepted(conn: sqlite3.Connection, runner, world) -> None:
    case = _seed_case(conn, runner, "insufficient_funds")
    db.insert_attempt(conn, _attempt(case.id, 1, f"{case.id}:1"))
    db.insert_attempt(conn, _attempt(case.id, 2, f"{case.id}:2"))
    assert db.attempt_count(conn, case.id) == 2


def _seed_case(conn: sqlite3.Connection, runner, error_code: str) -> db.Case:
    from src.executor.state import open_case as open_case_row

    payment = db.Payment(
        id="pay_SEED",
        customer_id="cust_SEED",
        merchant_id="mch_SEED",
        method="upi",
        rail="@oksbi",
        amount_paise=499000,
        created_at=NOW,
        first_outcome="failed",
        first_error_code=error_code,
    )
    db.insert_payments(conn, [payment])
    case = runner.build_case(
        payment=payment, error_code=error_code, error_source="bank", failed_at=NOW
    )
    open_case_row(conn, case, now=NOW)
    return case


# -- over HTTP ----------------------------------------------------------------


def test_reused_key_returns_409(client: TestClient) -> None:
    case = open_case(client, error_code="insufficient_funds")
    case_id = case["id"]
    at = case["next_attempt_at"]

    first = client.post(
        f"/v1/recovery/cases/{case_id}/attempts",
        json={"now": at},
        headers={"Idempotency-Key": "demo-key-1"},
    )
    assert first.status_code == 201, first.text

    second = client.post(
        f"/v1/recovery/cases/{case_id}/attempts",
        json={"now": at + 60},
        headers={"Idempotency-Key": "demo-key-1"},
    )
    assert second.status_code == 409, second.text
    error = second.json()["error"]
    assert error["code"] == "IDEMPOTENCY_CONFLICT"
    assert error["reason"] == "idempotency_key_reused"
    assert error["field"] == "Idempotency-Key"


def test_only_one_attempt_row_survives_a_duplicate(client: TestClient) -> None:
    case = open_case(client, error_code="insufficient_funds")
    case_id, at = case["id"], case["next_attempt_at"]
    headers = {"Idempotency-Key": "demo-key-2"}

    client.post(f"/v1/recovery/cases/{case_id}/attempts", json={"now": at}, headers=headers)
    client.post(
        f"/v1/recovery/cases/{case_id}/attempts", json={"now": at + 60}, headers=headers
    )

    detail = client.get(f"/v1/recovery/cases/{case_id}").json()
    assert len(detail["attempts"]) == 1


def test_missing_idempotency_key_header_is_rejected(client: TestClient) -> None:
    case = open_case(client, error_code="insufficient_funds")
    response = client.post(
        f"/v1/recovery/cases/{case['id']}/attempts",
        json={"now": case["next_attempt_at"]},
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["field"] == "Idempotency-Key"
    assert error["reason"] == "invalid_query_param"


def test_default_key_is_case_id_and_attempt_number(
    conn: sqlite3.Connection, runner, world
) -> None:
    # I-5 names the canonical form. The executor uses it when no key is supplied.
    case = _seed_case(conn, runner, "insufficient_funds")
    case = runner.diagnose(conn, case, now=NOW)
    attempt, _ = runner.execute_attempt(conn, case, world, now=case.next_attempt_at)
    assert attempt.idempotency_key == f"{case.id}:1"

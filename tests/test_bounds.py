"""I-7 — no action fires past max_attempts or drop_dead_at.

Both are enforced in the executor **before** any attempt is constructed, so a breach
costs nothing and touches nothing. Over HTTP a breach is a 422 POLICY_VIOLATION.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from src.executor import state as st
from src.executor.runner import (
    DEFAULT_DROP_DEAD_DAYS,
    MIN_INTERVAL_SECONDS,
    RETRY_NOW_FLOOR_SECONDS,
    PolicyViolation,
    Runner,
)
from src.store import db
from tests.conftest import DAY, NOW


class Exploding:
    """Any call means a bound was checked too late."""

    def attempt(self, *args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("the resolver ran despite a breached bound")


def _seed(conn: sqlite3.Connection, runner: Runner, code: str = "request_timed_out") -> db.Case:
    payment = db.Payment(
        id=f"pay_B_{code}",
        customer_id="cust_B",
        merchant_id="mch_B",
        method="upi",
        rail="@oksbi",
        amount_paise=499000,
        created_at=NOW,
        first_outcome="failed",
        first_error_code=code,
    )
    db.insert_payments(conn, [payment])
    case = runner.build_case(
        payment=payment, error_code=code, error_source="gateway", failed_at=NOW
    )
    st.open_case(conn, case, now=NOW)
    return runner.diagnose(conn, case, now=NOW)


# -- constants ----------------------------------------------------------------


def test_retry_now_floor_is_thirty_seconds() -> None:
    # error_policy.json stores min_wait_hours as an integer and holds 0 for RETRY_NOW.
    # "Re-attempt in seconds" is not expressible there, so the executor owns it.
    assert RETRY_NOW_FLOOR_SECONDS == 30


def test_drop_dead_defaults_to_a_week(conn, runner) -> None:
    case = _seed(conn, runner)
    assert case.drop_dead_at == NOW + DEFAULT_DROP_DEAD_DAYS * DAY


# -- max_attempts -------------------------------------------------------------


def test_max_attempts_is_refused(conn, runner) -> None:
    case = _seed(conn, runner)
    for number in range(1, case.max_attempts + 1):
        db.insert_attempt(
            conn,
            db.Attempt(
                id=db.stable_id("att_", case.id, number),
                case_id=case.id,
                attempt_number=number,
                idempotency_key=f"{case.id}:{number}",
                action="RETRY_NOW",
                target_rail=None,
                scheduled_at=NOW,
                executed_at=NOW,
                outcome="failed",
                error_code="request_timed_out",
                latency_ms=900,
            ),
        )
    with pytest.raises(PolicyViolation) as excinfo:
        runner.execute_attempt(conn, case, Exploding(), now=NOW + DAY)
    assert excinfo.value.reason == "max_attempts_exceeded"


def test_max_attempts_over_http(client: TestClient, db_path) -> None:
    """Deterministic: the attempt budget is spent up front, then one more is asked for.

    Spending it by looping real attempts would depend on how the world resolved each
    one, which is not what this test is about.
    """
    from tests.conftest import open_case

    case = open_case(client, error_code="insufficient_funds", payment_id="pay_MAXATT")
    case_id = case["id"]

    conn = db.connect(db_path)
    try:
        for number in range(1, case["max_attempts"] + 1):
            db.insert_attempt(
                conn,
                db.Attempt(
                    id=db.stable_id("att_", case_id, number),
                    case_id=case_id,
                    attempt_number=number,
                    idempotency_key=f"{case_id}:{number}",
                    action="RETRY_SCHEDULED",
                    target_rail=None,
                    scheduled_at=NOW,
                    executed_at=NOW,
                    outcome="failed",
                    error_code="insufficient_funds",
                    latency_ms=900,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    response = client.post(
        f"/v1/recovery/cases/{case_id}/attempts",
        json={"now": case["next_attempt_at"]},
        headers={"Idempotency-Key": "one-too-many"},
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "POLICY_VIOLATION"
    assert error["reason"] == "max_attempts_exceeded"

    final = client.get(f"/v1/recovery/cases/{case_id}").json()
    assert final["attempt_count"] == final["max_attempts"]


def test_a_case_never_outlives_its_attempt_budget(client: TestClient) -> None:
    """Drive a case to wherever it ends up and check the budget held.

    The end state is not asserted: a case may recover, exhaust, escalate or stop
    depending on what came back. What must hold in every one of those is that no more
    attempts were made than allowed, and that the case is no longer attemptable.
    """
    from tests.conftest import open_case

    case = open_case(client, error_code="request_timed_out", payment_id="pay_BUDGET")
    case_id, at = case["id"], case["next_attempt_at"]

    for index in range(case["max_attempts"] + 4):
        current = client.get(f"/v1/recovery/cases/{case_id}").json()
        if current["status"] != st.SCHEDULED:
            break
        at = max(at, current["next_attempt_at"]) + MIN_INTERVAL_SECONDS
        if at > current["drop_dead_at"]:
            break
        response = client.post(
            f"/v1/recovery/cases/{case_id}/attempts",
            json={"now": at},
            headers={"Idempotency-Key": f"budget-{index}"},
        )
        assert response.status_code in (201, 422), response.text
        if response.status_code == 422:
            break

    final = client.get(f"/v1/recovery/cases/{case_id}").json()
    assert final["attempt_count"] <= final["max_attempts"]
    assert final["status"] != st.ATTEMPTING


def test_never_more_attempt_rows_than_max_attempts(conn, runner, world) -> None:
    case = _seed(conn, runner)
    at = case.next_attempt_at or NOW
    for _ in range(12):
        current = db.get_case(conn, case.id)
        assert current is not None
        if current.state != st.SCHEDULED:
            break
        at = max(at + MIN_INTERVAL_SECONDS, current.next_attempt_at or at)
        if at > current.drop_dead_at:
            break
        runner.execute_attempt(conn, current, world, now=at)
    assert db.attempt_count(conn, case.id) <= case.max_attempts


# -- drop_dead_at -------------------------------------------------------------


def test_drop_dead_is_refused(conn, runner) -> None:
    case = _seed(conn, runner)
    with pytest.raises(PolicyViolation) as excinfo:
        runner.execute_attempt(
            conn, case, Exploding(), now=case.drop_dead_at + 1
        )
    assert excinfo.value.reason == "drop_dead_at_passed"


def test_drop_dead_over_http(client: TestClient) -> None:
    from tests.conftest import open_case

    case = open_case(client, error_code="insufficient_funds", payment_id="pay_DROPDEAD")
    response = client.post(
        f"/v1/recovery/cases/{case['id']}/attempts",
        json={"now": case["drop_dead_at"] + 1},
        headers={"Idempotency-Key": "dropdead-1"},
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "POLICY_VIOLATION"
    assert error["reason"] == "drop_dead_at_passed"


def test_scheduling_past_drop_dead_exhausts_instead(conn, runner) -> None:
    # insufficient_funds waits 72h; a case opened six days before its cutoff cannot
    # fit another wait, so diagnosis exhausts it rather than scheduling a no-op.
    payment = db.Payment(
        id="pay_TIGHT",
        customer_id="cust_B",
        merchant_id="mch_B",
        method="upi",
        rail="@oksbi",
        amount_paise=499000,
        created_at=NOW,
        first_outcome="failed",
        first_error_code="insufficient_funds",
    )
    db.insert_payments(conn, [payment])
    tight = Runner(runner.engine, drop_dead_days=1)
    case = tight.build_case(
        payment=payment,
        error_code="insufficient_funds",
        error_source="bank",
        failed_at=NOW,
    )
    st.open_case(conn, case, now=NOW)
    assert tight.diagnose(conn, case, now=NOW).state == st.EXHAUSTED


# -- min_interval -------------------------------------------------------------


def test_min_interval_is_refused(conn, runner) -> None:
    # Built directly rather than by running an attempt, so the test does not depend
    # on which way the world resolved the first one.
    case = _seed(conn, runner)
    at = case.next_attempt_at or NOW
    db.insert_attempt(
        conn,
        db.Attempt(
            id=db.stable_id("att_", case.id, 1),
            case_id=case.id,
            attempt_number=1,
            idempotency_key=f"{case.id}:1",
            action="RETRY_NOW",
            target_rail=None,
            scheduled_at=at,
            executed_at=at,
            outcome="failed",
            error_code="request_timed_out",
            latency_ms=900,
        ),
    )
    with pytest.raises(PolicyViolation) as excinfo:
        runner.execute_attempt(conn, case, Exploding(), now=at + 1)
    assert excinfo.value.reason == "min_interval_not_elapsed"


def test_min_interval_permits_the_next_attempt_once_elapsed(conn, runner, world) -> None:
    case = _seed(conn, runner)
    at = case.next_attempt_at or NOW
    runner.execute_attempt(conn, case, world, now=at)
    current = db.get_case(conn, case.id)
    assert current is not None
    if current.state != st.SCHEDULED:
        return  # recovered or terminated on the first attempt; nothing to space out
    runner.execute_attempt(
        conn,
        current,
        world,
        now=max(current.next_attempt_at or at, at + MIN_INTERVAL_SECONDS),
    )
    assert db.attempt_count(conn, case.id) == 2


# -- nothing is written on a refusal ------------------------------------------


def test_a_refused_attempt_writes_nothing(conn, runner) -> None:
    case = _seed(conn, runner)
    audit_before = len(db.list_audit(conn, case.id))
    with pytest.raises(PolicyViolation):
        runner.execute_attempt(conn, case, Exploding(), now=case.drop_dead_at + 1)
    assert db.attempt_count(conn, case.id) == 0
    assert len(db.list_audit(conn, case.id)) == audit_before
    refreshed = db.get_case(conn, case.id)
    assert refreshed is not None and refreshed.state == st.SCHEDULED


def test_a_terminal_case_accepts_no_attempts(conn, runner) -> None:
    case = _seed(conn, runner, "payment_risk_check_failed")
    assert case.state == st.STOPPED
    with pytest.raises(PolicyViolation) as excinfo:
        runner.execute_attempt(conn, case, Exploding(), now=NOW + 60)
    assert excinfo.value.reason == "case_terminal"


def test_an_escalated_case_accepts_no_attempts(conn, runner) -> None:
    # I-4 again, from the executor side rather than the graph side.
    case = _seed(conn, runner, "incorrect_pin")
    assert case.state == st.ESCALATED
    with pytest.raises(PolicyViolation) as excinfo:
        runner.execute_attempt(conn, case, Exploding(), now=NOW + 60)
    assert excinfo.value.reason == "case_not_scheduled"

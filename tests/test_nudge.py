"""Nudges — the recovery path for the 23 NUDGE_CUSTOMER codes.

Without this path those codes cannot recover in any arm, and the policy arm scores a
tie on exactly the class where it should win most: control burns three doomed retries
on a human-blocked failure, baseline sends a message that sometimes lands.

The I-4 question, settled here rather than re-argued later: a nudge that lands means
the customer went and paid **themselves**. No attempt is scheduled, no idempotency key
is consumed, no attempt row is written, and this system charges nothing. I-4 forbids
scheduling a retry on a non-retrying class, and none is scheduled.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.arms.base import ArmDecision
from src.arms.control import ControlArm
from src.executor import state as st
from src.executor.runner import NUDGE_RESPONSE_WINDOW_HOURS, Runner
from src.simulator.world import NUDGE_ATTEMPT, AttemptOutcome
from src.store import db
from tests.conftest import NOW

HOUR = 3600
NUDGE = ArmDecision(action="NUDGE_CUSTOMER", reason_code="human_action_required")


class Responsive:
    """A customer who acts the first time they are asked."""

    def attempt(self, case, action, target_rail, at_ts):
        assert action == NUDGE_ATTEMPT
        return AttemptOutcome(True, None, None, 1200)


class Unresponsive:
    """A customer who never acts."""

    def attempt(self, case, action, target_rail, at_ts):
        return AttemptOutcome(False, case.error_code, "customer", 0)


def seed_case(conn: sqlite3.Connection, runner: Runner, code: str = "incorrect_pin") -> db.Case:
    payment = db.Payment(
        id=f"pay_{code}",
        customer_id="cust_NUDGE",
        merchant_id="mch_NUDGE",
        method="upi",
        rail="@oksbi",
        amount_paise=499000,
        created_at=NOW,
        first_outcome="failed",
        first_error_code=code,
    )
    db.insert_payments(conn, [payment])
    case = runner.build_case(
        payment=payment, error_code=code, error_source="customer", failed_at=NOW
    )
    st.open_case(conn, case, now=NOW)
    return case


# -- the graph ----------------------------------------------------------------


def test_escalated_can_reach_recovered_and_exhausted() -> None:
    assert st.RECOVERED in st.LEGAL_TRANSITIONS[st.ESCALATED]
    assert st.EXHAUSTED in st.LEGAL_TRANSITIONS[st.ESCALATED]


def test_escalated_still_cannot_reach_an_attempt() -> None:
    # I-4 unchanged: adding a recovery edge does not open a retry path.
    assert st.SCHEDULED not in st.LEGAL_TRANSITIONS[st.ESCALATED]
    assert st.ATTEMPTING not in st.LEGAL_TRANSITIONS[st.ESCALATED]


# -- sending -------------------------------------------------------------------


def test_a_nudge_escalates_the_case_and_stamps_the_time(conn, runner) -> None:
    case = seed_case(conn, runner)
    case = runner.apply(conn, case, NUDGE, Unresponsive(), now=NOW, actor="baseline")
    assert case.state == st.ESCALATED
    assert case.nudge_sent_at == NOW
    assert case.next_attempt_at is None


def test_a_nudge_writes_an_audit_row_but_no_attempt_row(conn, runner) -> None:
    case = seed_case(conn, runner)
    runner.apply(conn, case, NUDGE, Unresponsive(), now=NOW, actor="baseline")
    assert db.attempt_count(conn, case.id) == 0
    reasons = [row.reason for row in db.list_audit(conn, case.id)]
    assert "nudge_sent" in reasons


def test_a_nudge_consumes_no_idempotency_key(conn, runner) -> None:
    # I-5's keys are for money movement. A message is not a charge.
    case = seed_case(conn, runner)
    runner.apply(conn, case, NUDGE, Unresponsive(), now=NOW, actor="baseline")
    assert not db.idempotency_key_exists(conn, f"{case.id}:1")


# -- landing -------------------------------------------------------------------


def test_a_nudge_that_lands_recovers_the_case(conn, runner) -> None:
    case = seed_case(conn, runner)
    case = runner.apply(conn, case, NUDGE, Unresponsive(), now=NOW, actor="baseline")
    case = runner.apply(
        conn, case, NUDGE, Responsive(), now=NOW + HOUR, actor="baseline"
    )
    assert case.state == st.RECOVERED
    assert case.recovered_at == NOW + HOUR
    assert case.recovered_amount_paise == case.amount_paise


def test_a_recovery_by_nudge_still_writes_no_attempt(conn, runner) -> None:
    case = seed_case(conn, runner)
    case = runner.apply(conn, case, NUDGE, Unresponsive(), now=NOW, actor="baseline")
    runner.apply(conn, case, NUDGE, Responsive(), now=NOW + HOUR, actor="baseline")
    assert db.attempt_count(conn, case.id) == 0


def test_the_audit_trail_says_the_customer_paid(conn, runner) -> None:
    case = seed_case(conn, runner)
    case = runner.apply(conn, case, NUDGE, Unresponsive(), now=NOW, actor="baseline")
    runner.apply(conn, case, NUDGE, Responsive(), now=NOW + 5 * HOUR, actor="baseline")
    final = db.list_audit(conn, case.id)[-1]
    assert final.to_state == st.RECOVERED
    assert "nudge" in final.reason
    assert final.detail is not None and final.detail["hours_after_nudge"] == 5


# -- expiring ------------------------------------------------------------------


def test_an_unanswered_nudge_exhausts_the_case(conn, runner) -> None:
    case = seed_case(conn, runner)
    case = runner.apply(conn, case, NUDGE, Unresponsive(), now=NOW, actor="baseline")
    past = NOW + (NUDGE_RESPONSE_WINDOW_HOURS + 1) * HOUR
    case = runner.apply(conn, case, NUDGE, Unresponsive(), now=past, actor="baseline")
    assert case.state == st.EXHAUSTED
    assert db.list_audit(conn, case.id)[-1].reason == "nudge_window_expired"


def test_a_nudge_inside_the_window_keeps_waiting(conn, runner) -> None:
    case = seed_case(conn, runner)
    case = runner.apply(conn, case, NUDGE, Unresponsive(), now=NOW, actor="baseline")
    inside = NOW + (NUDGE_RESPONSE_WINDOW_HOURS - 1) * HOUR
    case = runner.apply(conn, case, NUDGE, Unresponsive(), now=inside, actor="baseline")
    assert case.state == st.ESCALATED


# -- the world's own nudge resolution ------------------------------------------


def test_the_world_resolves_nudges_per_hour(world) -> None:
    """Keyed on the hour, so the answer does not move with the evaluation's tick size.

    A half-hourly loop asks twice per hour and gets the same answer both times, so it
    recovers the same customers at the same hour as an hourly loop.
    """
    view = db.CaseView(
        id="case_TICK",
        customer_id="cust_TICK",
        merchant_id="mch_TICK",
        method="upi",
        rail="@oksbi",
        amount_paise=499000,
        error_code="incorrect_pin",
        failed_at=NOW,
        attempt_number=1,
    )
    hourly = [world.attempt(view, NUDGE_ATTEMPT, None, NOW + h * HOUR).success
              for h in range(24)]
    half_hourly = [world.attempt(view, NUDGE_ATTEMPT, None, NOW + h * HOUR + 1800).success
                   for h in range(24)]
    assert hourly == half_hourly


def test_nudge_conversion_is_a_plausible_minority(world) -> None:
    """Population conversion over the response window.

    Calibrated to ~25% before any arm was run, and deliberately not retuned since:
    tuning this number after seeing the arms would be fitting the simulator to the
    result.
    """
    landed = 0
    population = 300
    for index in range(population):
        view = db.CaseView(
            id=f"case_C{index}",
            customer_id=f"cust_C{index}",
            merchant_id="mch_C",
            method="upi",
            rail="@oksbi",
            amount_paise=499000,
            error_code="incorrect_pin",
            failed_at=NOW,
            attempt_number=1,
        )
        if any(
            world.attempt(view, NUDGE_ATTEMPT, None, NOW + h * HOUR).success
            for h in range(NUDGE_RESPONSE_WINDOW_HOURS)
        ):
            landed += 1
    assert 0.10 < landed / population < 0.45, landed / population


# -- control never nudges -------------------------------------------------------


def test_control_never_produces_a_nudge_decision(engine) -> None:
    from tests.test_arms import CODE_FOR_ACTION, NoHealth, snapshot

    arm = ControlArm()
    for code in CODE_FOR_ACTION.values():
        decision = arm.next_action(snapshot(code), engine, NoHealth(), NOW)
        assert decision is not None and decision.action != "NUDGE_CUSTOMER"


def test_a_control_run_sends_no_nudges(tmp_path) -> None:
    from eval.run_arms import run
    from tests.conftest import SEED

    result = run(
        seed=SEED,
        n_payments=250,
        days=20,
        scenario="normal",
        arms=["control"],
        trailing_days=3,
        db_path=tmp_path / "control_only.db",
    )
    conn = db.connect(result.db_path)
    try:
        nudged = conn.execute(
            "SELECT COUNT(*) FROM cases WHERE nudge_sent_at IS NOT NULL"
        ).fetchone()[0]
        escalated = conn.execute(
            "SELECT COUNT(*) FROM cases WHERE state = 'ESCALATED'"
        ).fetchone()[0]
        assert nudged == 0
        assert escalated == 0
    finally:
        conn.close()


def test_a_baseline_run_recovers_some_cases_by_nudge(tmp_path) -> None:
    from eval.run_arms import run
    from tests.conftest import SEED

    result = run(
        seed=SEED,
        n_payments=800,
        days=25,
        scenario="normal",
        arms=["baseline"],
        trailing_days=5,
        db_path=tmp_path / "baseline_only.db",
    )
    conn = db.connect(result.db_path)
    try:
        by_nudge = conn.execute(
            "SELECT COUNT(*) FROM cases WHERE state = 'RECOVERED' "
            "AND nudge_sent_at IS NOT NULL"
        ).fetchone()[0]
        assert by_nudge > 0, "no case recovered through a nudge"
        # Every one of them must have gone through ESCALATED without an attempt.
        rows = db.fetch_all(
            conn,
            "SELECT c.id FROM cases c WHERE c.state = 'RECOVERED' "
            "AND c.nudge_sent_at IS NOT NULL",
        )
        for row in rows:
            assert db.attempt_count(conn, row["id"]) == 0
    finally:
        conn.close()

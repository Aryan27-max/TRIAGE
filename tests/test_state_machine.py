"""The state graph, and I-8 — audit before action.

Two things are asserted here that nothing else covers:

* the declared graph is internally consistent and illegal edges raise, and
* the audit row for entering ATTEMPTING is on disk **before** the resolver is called,
  not written afterwards from what came back. An action with no preceding audit row
  is a bug; the test proves the ordering rather than trusting the code comment.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.executor import state as st
from src.executor.runner import NON_RETRYING_DESTINATION, Runner
from src.store import db
from tests.conftest import NOW


def _seed(conn: sqlite3.Connection, runner: Runner, error_code: str) -> db.Case:
    payment = db.Payment(
        id=f"pay_{error_code}",
        customer_id="cust_SM",
        merchant_id="mch_SM",
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
    st.open_case(conn, case, now=NOW)
    return case


# -- the graph as data --------------------------------------------------------


def test_every_state_has_a_transition_entry() -> None:
    assert set(st.LEGAL_TRANSITIONS) == set(st.STATES)


def test_every_destination_is_a_declared_state() -> None:
    for source, destinations in st.LEGAL_TRANSITIONS.items():
        assert destinations <= set(st.STATES), source


def test_terminal_states_have_no_outgoing_edges() -> None:
    for state in st.TERMINAL_STATES:
        assert st.LEGAL_TRANSITIONS[state] == frozenset()


def test_no_state_transitions_to_itself() -> None:
    for source, destinations in st.LEGAL_TRANSITIONS.items():
        assert source not in destinations


def test_every_state_is_reachable_from_received() -> None:
    seen, frontier = {st.RECEIVED}, [st.RECEIVED]
    while frontier:
        current = frontier.pop()
        for destination in st.LEGAL_TRANSITIONS[current]:
            if destination not in seen:
                seen.add(destination)
                frontier.append(destination)
    assert seen == set(st.STATES)


def test_escalated_cannot_reach_an_attempt() -> None:
    # I-4, structurally. The four non-retrying classes that land in ESCALATED have no
    # path onward to SCHEDULED or ATTEMPTING, whatever a later caller asks for.
    assert st.SCHEDULED not in st.LEGAL_TRANSITIONS[st.ESCALATED]
    assert st.ATTEMPTING not in st.LEGAL_TRANSITIONS[st.ESCALATED]


@pytest.mark.parametrize("destination", sorted(set(NON_RETRYING_DESTINATION.values())))
@pytest.mark.parametrize("source", [st.DIAGNOSED, st.ATTEMPTING])
def test_every_diagnosis_destination_is_reachable(source: str, destination: str) -> None:
    """A failed attempt is re-diagnosed, so ATTEMPTING must reach everything
    DIAGNOSED can. Missing one of these edges leaves a case wedged in SCHEDULED with
    an illegal-transition error on every retry."""
    assert st.is_legal(source, destination)


def test_attempting_reaches_every_diagnosis_destination_plus_recovery() -> None:
    expected = set(NON_RETRYING_DESTINATION.values()) | {
        st.RECOVERED,
        st.SCHEDULED,
        st.EXHAUSTED,
    }
    assert expected <= st.LEGAL_TRANSITIONS[st.ATTEMPTING]


def test_only_scheduled_can_produce_an_attempt() -> None:
    attemptable = {s for s, d in st.LEGAL_TRANSITIONS.items() if st.ATTEMPTING in d}
    assert attemptable == {st.SCHEDULED}
    assert st.ATTEMPTABLE_FROM == frozenset({st.SCHEDULED})


@pytest.mark.parametrize(
    "source,destination",
    [(s, d) for s, ds in st.LEGAL_TRANSITIONS.items() for d in ds],
    ids=lambda v: v,
)
def test_declared_edges_are_legal(source: str, destination: str) -> None:
    assert st.is_legal(source, destination)


@pytest.mark.parametrize(
    "source,destination",
    [
        (st.RECEIVED, st.ATTEMPTING),
        (st.RECEIVED, st.RECOVERED),
        (st.DIAGNOSED, st.ATTEMPTING),
        (st.ESCALATED, st.SCHEDULED),
        (st.AWAITING_STATUS, st.ATTEMPTING),
        (st.RECOVERED, st.SCHEDULED),
        (st.STOPPED, st.DIAGNOSED),
        (st.EXHAUSTED, st.SCHEDULED),
    ],
)
def test_undeclared_edges_are_illegal(source: str, destination: str) -> None:
    assert not st.is_legal(source, destination)


def test_an_unknown_state_is_never_legal() -> None:
    assert not st.is_legal(st.RECEIVED, "TELEPORTED")
    assert not st.is_legal("TELEPORTED", st.DIAGNOSED)


# -- transitions against a real case ------------------------------------------


def test_an_illegal_transition_raises(conn, runner) -> None:
    case = _seed(conn, runner, "insufficient_funds")
    with pytest.raises(st.IllegalTransitionError):
        st.transition(
            conn,
            case_id=case.id,
            to_state=st.ATTEMPTING,
            actor="test",
            reason="skipping diagnosis",
            now=NOW,
        )


def test_an_illegal_transition_writes_no_audit_row(conn, runner) -> None:
    case = _seed(conn, runner, "insufficient_funds")
    before = len(db.list_audit(conn, case.id))
    with pytest.raises(st.IllegalTransitionError):
        st.transition(
            conn, case_id=case.id, to_state=st.RECOVERED, actor="test",
            reason="wishful", now=NOW,
        )
    assert len(db.list_audit(conn, case.id)) == before


def test_a_wrong_from_state_raises(conn, runner) -> None:
    case = _seed(conn, runner, "insufficient_funds")
    with pytest.raises(st.IllegalTransitionError):
        st.transition(
            conn,
            case_id=case.id,
            from_state=st.SCHEDULED,  # it is actually RECEIVED
            to_state=st.DIAGNOSED,
            actor="test",
            reason="stale read",
            now=NOW,
        )


def test_opening_a_case_writes_the_first_audit_row(conn, runner) -> None:
    case = _seed(conn, runner, "insufficient_funds")
    audit = db.list_audit(conn, case.id)
    assert len(audit) == 1
    assert audit[0].from_state is None
    assert audit[0].to_state == st.RECEIVED
    assert audit[0].actor == "simulator"


def test_diagnosis_writes_two_rows(conn, runner) -> None:
    case = _seed(conn, runner, "insufficient_funds")
    runner.diagnose(conn, case, now=NOW)
    audit = db.list_audit(conn, case.id)
    assert [row.to_state for row in audit] == [
        st.RECEIVED,
        st.DIAGNOSED,
        st.SCHEDULED,
    ]
    assert audit[1].actor == "policy_engine"
    assert audit[1].reason == "error_policy lookup"


@pytest.mark.parametrize(
    "code,destination",
    [
        ("card_expired", NON_RETRYING_DESTINATION["SWITCH_INSTRUMENT"]),
        ("incorrect_pin", NON_RETRYING_DESTINATION["NUDGE_CUSTOMER"]),
        ("payment_pending", NON_RETRYING_DESTINATION["AWAIT_STATUS"]),
        ("payment_risk_check_failed", NON_RETRYING_DESTINATION["STOP"]),
        ("invalid_amount", NON_RETRYING_DESTINATION["MERCHANT_ALERT"]),
        ("insufficient_funds", st.SCHEDULED),
        ("bank_technical_error", st.SCHEDULED),
        ("request_timed_out", st.SCHEDULED),
    ],
)
def test_diagnosis_routes_by_action_class(conn, runner, code, destination) -> None:
    case = _seed(conn, runner, code)
    assert runner.diagnose(conn, case, now=NOW).state == destination


# -- I-8: the audit row lands before the action -------------------------------


def test_audit_is_written_before_the_resolver_runs(conn, runner) -> None:
    """The proof, not the promise.

    The stub resolver queries the audit table at the moment it is called. If the
    executor wrote the row afterwards — from what the resolver returned — this would
    find nothing and fail.
    """
    case = _seed(conn, runner, "insufficient_funds")
    case = runner.diagnose(conn, case, now=NOW)
    observed: dict[str, object] = {}

    class Observer:
        def attempt(self, case_view, action, target_rail, at_ts):
            rows = db.list_audit(conn, case_view.id)
            observed["states"] = [row.to_state for row in rows]
            observed["key"] = rows[-1].idempotency_key
            observed["attempts_on_disk"] = db.attempt_count(conn, case_view.id)
            from src.simulator.world import AttemptOutcome

            return AttemptOutcome(False, "payment_failed", "gateway", 900)

    runner.execute_attempt(conn, case, Observer(), now=case.next_attempt_at)

    assert observed["states"][-1] == st.ATTEMPTING
    assert observed["key"] == f"{case.id}:1"
    # The attempt row itself is written after the outcome is known; the audit row is
    # what has to precede the action.
    assert observed["attempts_on_disk"] == 0


def test_every_attempt_has_a_preceding_audit_row(conn, runner, world) -> None:
    case = _seed(conn, runner, "insufficient_funds")
    case = runner.diagnose(conn, case, now=NOW)
    runner.execute_attempt(conn, case, world, now=case.next_attempt_at)

    audit = db.list_audit(conn, case.id)
    for attempt in db.list_attempts(conn, case.id):
        preceding = [
            row
            for row in audit
            if row.to_state == st.ATTEMPTING
            and row.idempotency_key == attempt.idempotency_key
            and row.at <= attempt.executed_at
        ]
        assert preceding, f"attempt {attempt.id} has no preceding audit row"


def test_audit_is_append_only_across_a_full_case(conn, runner, world) -> None:
    case = _seed(conn, runner, "bank_technical_error")
    case = runner.diagnose(conn, case, now=NOW)
    ids = [row.id for row in db.list_audit(conn, case.id)]

    at = case.next_attempt_at
    for _ in range(4):
        current = db.get_case(conn, case.id)
        assert current is not None
        if current.state != st.SCHEDULED:
            break
        runner.execute_attempt(conn, current, world, now=max(at, current.next_attempt_at))
        at = (db.get_case(conn, case.id) or current).next_attempt_at or at

    later = [row.id for row in db.list_audit(conn, case.id)]
    assert later[: len(ids)] == ids  # nothing rewritten
    assert later == sorted(later)  # strictly append-order


def test_stop_terminates_from_any_live_state(conn, runner) -> None:
    case = _seed(conn, runner, "insufficient_funds")
    case = runner.diagnose(conn, case, now=NOW)
    stopped = runner.stop(conn, case, now=NOW + 60, reason="customer_cancelled")
    assert stopped.state == st.STOPPED
    assert db.list_audit(conn, case.id)[-1].reason == "customer_cancelled"


def test_stopping_a_terminal_case_is_refused(conn, runner) -> None:
    from src.executor.runner import PolicyViolation

    case = _seed(conn, runner, "payment_risk_check_failed")
    case = runner.diagnose(conn, case, now=NOW)
    assert case.state == st.STOPPED
    with pytest.raises(PolicyViolation):
        runner.stop(conn, case, now=NOW + 60, reason="again")

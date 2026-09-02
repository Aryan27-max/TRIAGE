"""The case state machine.

Legal transitions are declared as data, not scattered through `if` branches, so the
graph can be read in one place and asserted in one test. An illegal transition raises
rather than silently correcting itself.

Two structural properties matter more than the rest:

* **Every transition writes an audit row before the state changes**, and the caller
  writes it before the action executes. An action with no preceding audit row is a
  bug. (I-8)
* **ESCALATED and STOPPED have no path back to SCHEDULED or ATTEMPTING.** That is
  what makes I-4 structural: the five non-retrying classes land in one of those two
  states at diagnosis and therefore *cannot* reach an attempt, whatever a later
  caller asks for. AWAITING_STATUS keeps its path to SCHEDULED, gated on a resolved
  poll, which is what I-6 requires.
* **ESCALATED can still reach RECOVERED.** A nudge that lands is the customer paying
  again of their own accord — no attempt, no idempotency key, no charge from here.
  I-4 forbids scheduling a retry, not recovering without one.

Nothing here reads a clock; ``now`` is always passed in.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from src.store import db

RECEIVED = "RECEIVED"
DIAGNOSED = "DIAGNOSED"
SCHEDULED = "SCHEDULED"
ATTEMPTING = "ATTEMPTING"
AWAITING_STATUS = "AWAITING_STATUS"
ESCALATED = "ESCALATED"
RECOVERED = "RECOVERED"
EXHAUSTED = "EXHAUSTED"
STOPPED = "STOPPED"

STATES: tuple[str, ...] = (
    RECEIVED,
    DIAGNOSED,
    SCHEDULED,
    ATTEMPTING,
    AWAITING_STATUS,
    ESCALATED,
    RECOVERED,
    EXHAUSTED,
    STOPPED,
)

TERMINAL_STATES: frozenset[str] = frozenset({RECOVERED, EXHAUSTED, STOPPED})

LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    RECEIVED: frozenset({DIAGNOSED}),
    # The branch point. Which arm is taken is decided by the policy table alone.
    # EXHAUSTED is reachable directly: a retrying class whose next wait would land
    # past drop_dead_at has nowhere to be scheduled, so it ends here without ever
    # occupying SCHEDULED. (I-7)
    DIAGNOSED: frozenset({SCHEDULED, AWAITING_STATUS, ESCALATED, STOPPED, EXHAUSTED}),
    # AWAITING_STATUS is reachable from here because the I-6 guard must be able to
    # pull a case out of a scheduled retry the moment its code turns out to carry an
    # unresolved outcome — which happens when an attempt comes back `payment_pending`
    # and the case is re-diagnosed. Blocking an attempt is always a legal move.
    SCHEDULED: frozenset({ATTEMPTING, AWAITING_STATUS, EXHAUSTED, STOPPED}),
    # A failed attempt is re-diagnosed against whatever code came back, so ATTEMPTING
    # must be able to reach every destination diagnosis can: an attempt that fails
    # with `incorrect_pin` escalates, one that fails with `payment_pending` waits.
    ATTEMPTING: frozenset(
        {RECOVERED, SCHEDULED, AWAITING_STATUS, ESCALATED, EXHAUSTED, STOPPED}
    ),
    # Gated on status_resolved_at by the runner. Without a resolved poll this edge
    # is refused and the caller gets 423. (I-6)
    AWAITING_STATUS: frozenset({SCHEDULED, RECOVERED, EXHAUSTED, STOPPED}),
    # Deliberately no edge to SCHEDULED or ATTEMPTING. (I-4)
    #
    # RECOVERED is reachable and does not weaken that. I-4 forbids *scheduling a
    # retry* on a non-retrying class. A nudge that lands means the customer started a
    # fresh payment themselves: no attempt is scheduled, no idempotency key is
    # consumed, no attempt row is written. The money arrives without this system
    # having charged anything.
    ESCALATED: frozenset({RECOVERED, EXHAUSTED, STOPPED}),
    RECOVERED: frozenset(),
    EXHAUSTED: frozenset(),
    STOPPED: frozenset(),
}

# States from which an attempt can be constructed at all.
ATTEMPTABLE_FROM: frozenset[str] = frozenset({SCHEDULED})


class IllegalTransitionError(Exception):
    """The requested edge is not in the graph."""

    def __init__(self, from_state: str | None, to_state: str) -> None:
        self.from_state = from_state
        self.to_state = to_state
        allowed = sorted(LEGAL_TRANSITIONS.get(from_state or "", frozenset()))
        super().__init__(
            f"{from_state} -> {to_state} is not a legal transition; "
            f"{from_state} may go to {allowed or '(nothing — terminal)'}"
        )


def is_legal(from_state: str | None, to_state: str) -> bool:
    if to_state not in STATES:
        return False
    if from_state is None:
        return to_state == RECEIVED
    return to_state in LEGAL_TRANSITIONS.get(from_state, frozenset())


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


def open_case(
    conn: sqlite3.Connection,
    case: db.Case,
    *,
    now: int,
    actor: str = "simulator",
    reason: str = "payment.failed",
) -> db.Case:
    """Create a case in RECEIVED and write its first audit row.

    The row lands immediately after the insert and before anything acts on the case,
    so the audit trail opens with the reason the case exists at all.
    """
    if case.state != RECEIVED:
        raise IllegalTransitionError(None, case.state)
    db.insert_case(conn, case)
    db.append_audit(
        conn,
        case_id=case.id,
        at=now,
        from_state=None,
        to_state=RECEIVED,
        actor=actor,
        reason=reason,
        detail={"error_code": case.error_code, "amount_paise": case.amount_paise},
    )
    return case


def transition(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    to_state: str,
    actor: str,
    reason: str,
    now: int,
    from_state: str | None = None,
    idempotency_key: str | None = None,
    detail: dict[str, Any] | None = None,
    **case_fields: Any,
) -> db.Case:
    """Move a case, auditing first.

    ``case_fields`` are written to the case row in the same statement as the state,
    so a case is never briefly in the new state with stale fields.
    """
    current = db.get_case(conn, case_id)
    if current is None:
        raise IllegalTransitionError(from_state, to_state)
    if from_state is not None and current.state != from_state:
        raise IllegalTransitionError(current.state, to_state)
    if not is_legal(current.state, to_state):
        raise IllegalTransitionError(current.state, to_state)

    # I-8. The audit row is written before the state changes, and the caller writes
    # it before the action executes.
    db.append_audit(
        conn,
        case_id=case_id,
        at=now,
        from_state=current.state,
        to_state=to_state,
        actor=actor,
        reason=reason,
        idempotency_key=idempotency_key,
        detail=detail,
    )
    db.set_case_state(conn, case_id, to_state, **case_fields)

    updated = db.get_case(conn, case_id)
    assert updated is not None  # the row was just read and written
    return updated

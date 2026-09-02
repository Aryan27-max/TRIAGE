"""The bounded executor.

Reads the case's error code through the Stage 1 policy engine, applies the hard
bounds, and — for the three classes that may retry — asks an injected resolver what
happened. It never reaches for the simulator: the world arrives as an argument, typed
structurally, so ``src.executor`` has no import edge into ``src.simulator``. (I-12)

Stage 2 has no model. Timing for RETRY_SCHEDULED is ``min_wait_hours`` from the policy
table, flat. Stage 4 replaces that and nothing else.

Nothing here reads a clock; ``now`` is always passed in.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol

from src.executor import state as st
from src.policy.engine import RETRYING_ACTIONS, PolicyEngine, PolicyEntry
from src.store import db

HOUR = 3600
DAY = 24 * HOUR

# Policy owns the minimum wait in hours; the executor owns sub-hour resolution.
# `min_wait_hours` is an integer and is 0 for RETRY_NOW ("re-attempt in seconds"),
# which the table cannot express. error_policy.json is ground truth and is not edited
# to accommodate this — the floor lives here instead.
RETRY_NOW_FLOOR_SECONDS = 30

# research/02 §2.3 lists min_interval among the hard bounds. It is an absolute floor
# between two attempts on one case; the real spacing comes from policy's min_wait_hours.
MIN_INTERVAL_SECONDS = RETRY_NOW_FLOOR_SECONDS

DEFAULT_MAX_ATTEMPTS = 4  # research/02 §2.3
DEFAULT_DROP_DEAD_DAYS = 7  # assumption (ours): no published cutoff convention

# How long a nudged customer has to act before the case is written off. Assumption
# (ours): 48 hours is the window recovery emails and abandoned-cart flows typically
# allow. Fixed before any arm was run.
NUDGE_RESPONSE_WINDOW_HOURS = 48

# The action string the world understands for "has this customer acted yet?". Held as
# a literal rather than imported from src.simulator.world, because the executor has no
# import edge into the simulator. (I-12)
NUDGE_ACTION = "NUDGE"

# Where SWITCH_RAIL sends a payment. Fixed, not sampled: the executor is
# deterministic, and a rail choice that varied per run would break arm parity.
ALTERNATIVE_RAIL: dict[str, str] = {
    "upi": "card",
    "card": "upi",
    "netbanking": "upi",
    "wallet": "upi",
}

ACTION_REASON_CODE: dict[str, str] = {
    "RETRY_NOW": "transient_glitch",
    "RETRY_SCHEDULED": "recoverable_later",
    "SWITCH_RAIL": "rail_degraded",
    "SWITCH_INSTRUMENT": "instrument_permanently_unusable",
    "NUDGE_CUSTOMER": "human_action_required",
    "AWAIT_STATUS": "outcome_unresolved",
    "STOP": "retry_unsafe_or_penalised",
    "MERCHANT_ALERT": "merchant_misconfiguration",
}

# This project's analogue of Stripe's advice_code. Razorpay publishes no equivalent.
ACTION_ADVICE: dict[str, str] = {
    "RETRY_NOW": "try_again_now",
    "RETRY_SCHEDULED": "try_again_later",
    "SWITCH_RAIL": "try_another_rail",
    "SWITCH_INSTRUMENT": "do_not_try_again",
    "NUDGE_CUSTOMER": "contact_customer",
    "AWAIT_STATUS": "poll_status_first",
    "STOP": "do_not_try_again",
    "MERCHANT_ALERT": "contact_merchant",
}

# Where a case lands at diagnosis, per action class. The three retrying classes are
# the only ones that reach SCHEDULED, so only they can ever produce an attempt. (I-4)
NON_RETRYING_DESTINATION: dict[str, str] = {
    "SWITCH_INSTRUMENT": st.ESCALATED,
    "NUDGE_CUSTOMER": st.ESCALATED,
    "MERCHANT_ALERT": st.ESCALATED,
    "AWAIT_STATUS": st.AWAITING_STATUS,
    "STOP": st.STOPPED,
}


class ExecutorError(Exception):
    """Base for executor refusals."""


class PolicyViolation(ExecutorError):
    """The action would breach a hard bound. (I-7)"""

    def __init__(self, reason: str, description: str) -> None:
        self.reason = reason
        self.description = description
        super().__init__(description)


class AwaitingStatus(ExecutorError):
    """The prior outcome is unresolved; attempting now risks a double charge. (I-6)"""

    def __init__(self, case_id: str) -> None:
        self.case_id = case_id
        super().__init__(
            f"case {case_id} is awaiting a status poll; attempting now risks a "
            f"double charge"
        )


class OutcomeLike(Protocol):
    """What the executor reads back from a resolver. Four fields, nothing latent."""

    success: bool
    error_code: str | None
    error_source: str | None
    latency_ms: int


class AttemptResolver(Protocol):
    """Structural type for the world. Declared, never imported. (I-12)"""

    def attempt(
        self, case: db.CaseView, action: str, target_rail: str | None, at_ts: int
    ) -> OutcomeLike: ...


class ArmDecisionLike(Protocol):
    """What the runner reads off an arm's decision.

    Declared structurally so the executor does not import ``src.arms``, and the arms
    do not import the executor's concrete types. The dependency runs one way: the
    evaluation harness wires the two together, neither reaches for the other.
    """

    action: str
    target_rail: str | None
    scheduled_at: int | None
    reason_code: str
    policy_routed: bool


@dataclass(frozen=True, slots=True)
class Decision:
    """A stateless decision about one error code. No payment is touched."""

    decision_id: str
    error_code: str
    action: str
    family: str
    recoverable: bool
    scheduled_at: int | None
    target_rail: str | None
    min_wait_hours: int
    reason_code: str
    advice: str
    explanation: str
    next_steps: str
    model_eligible: bool
    constraints: dict[str, Any]


class Runner:
    """Owns the bounds. Takes the policy engine; receives the world per call."""

    def __init__(
        self,
        engine: PolicyEngine,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        drop_dead_days: int = DEFAULT_DROP_DEAD_DAYS,
        min_interval_seconds: int = MIN_INTERVAL_SECONDS,
    ) -> None:
        self.engine = engine
        self.max_attempts = max_attempts
        self.drop_dead_days = drop_dead_days
        self.min_interval_seconds = min_interval_seconds

    # -- stateless decision ----------------------------------------------------

    def decide(
        self,
        error_code: str,
        *,
        now: int,
        method: str | None = None,
        attempt_number: int = 1,
    ) -> Decision:
        """The policy table's answer for one code. Raises on an unknown code. (I-2)"""
        entry = self.engine.resolve(error_code)
        scheduled_at = self.next_attempt_at(entry, now) if entry.is_retrying else None
        target_rail = (
            ALTERNATIVE_RAIL.get(method or "", None)
            if entry.action == "SWITCH_RAIL"
            else None
        )
        return Decision(
            decision_id=db.stable_id("dec_", error_code, now, attempt_number, method),
            error_code=entry.code,
            action=entry.action,
            family=entry.family,
            recoverable=entry.recoverable,
            scheduled_at=scheduled_at,
            target_rail=target_rail,
            min_wait_hours=entry.min_wait_hours,
            reason_code=ACTION_REASON_CODE[entry.action],
            advice=ACTION_ADVICE[entry.action],
            explanation=entry.razorpay_explanation,
            next_steps=entry.razorpay_next_steps,
            model_eligible=entry.is_model_eligible,
            constraints={
                "attempts_remaining": max(0, self.max_attempts - attempt_number + 1)
                if entry.is_retrying
                else 0,
                "max_attempts": self.max_attempts,
                "drop_dead_at": now + self.drop_dead_days * DAY,
                "min_interval_seconds": self.min_interval_seconds,
                "min_wait_hours": entry.min_wait_hours,
            },
        )

    def next_attempt_at(self, entry: PolicyEntry, now: int) -> int:
        """When a retrying class may next fire.

        RETRY_NOW and SWITCH_RAIL both carry min_wait_hours = 0 and both mean "go
        again promptly", so they take the executor's sub-hour floor. RETRY_SCHEDULED
        takes its hours from the table, flat — Stage 4 is what makes it adaptive.
        """
        if entry.min_wait_hours > 0:
            return now + entry.min_wait_hours * HOUR
        return now + RETRY_NOW_FLOOR_SECONDS

    # -- case lifecycle --------------------------------------------------------

    def build_case(
        self,
        *,
        payment: db.Payment,
        error_code: str,
        error_source: str | None,
        failed_at: int,
        arm: str | None = None,
        city_tier: int | None = None,
        vpa_handle: str | None = None,
        payer_bank: str | None = None,
    ) -> db.Case:
        """A case row in RECEIVED. Not yet inserted — ``state.open_case`` does that."""
        return db.Case(
            id=db.stable_id("case_", payment.id),
            payment_id=payment.id,
            customer_id=payment.customer_id,
            merchant_id=payment.merchant_id,
            method=payment.method,
            rail=payment.rail,
            amount_paise=payment.amount_paise,
            error_code=error_code,
            error_source=error_source,
            failed_at=failed_at,
            city_tier=city_tier,
            vpa_handle=vpa_handle,
            payer_bank=payer_bank,
            state=st.RECEIVED,
            arm=arm,
            max_attempts=self.max_attempts,
            drop_dead_at=failed_at + self.drop_dead_days * DAY,
            next_attempt_at=None,
            status_resolved_at=None,
            nudge_sent_at=None,
            recovered_at=None,
            recovered_amount_paise=None,
            created_at=failed_at,
        )

    def diagnose(self, conn: sqlite3.Connection, case: db.Case, *, now: int) -> db.Case:
        """RECEIVED -> DIAGNOSED -> the class's destination.

        The policy table alone decides the destination. No model, no LLM. (I-1)
        """
        entry = self.engine.resolve(case.error_code)
        case = st.transition(
            conn,
            case_id=case.id,
            from_state=st.RECEIVED,
            to_state=st.DIAGNOSED,
            actor="policy_engine",
            reason="error_policy lookup",
            now=now,
            detail={
                "error_code": entry.code,
                "action": entry.action,
                "family": entry.family,
                "min_wait_hours": entry.min_wait_hours,
                "recoverable": entry.recoverable,
            },
        )
        return self._route(conn, case, entry, now=now, attempts_used=0)

    def _route(
        self,
        conn: sqlite3.Connection,
        case: db.Case,
        entry: PolicyEntry,
        *,
        now: int,
        attempts_used: int,
    ) -> db.Case:
        """Send a diagnosed case to the state its action class permits."""
        if not entry.is_retrying:
            # I-4: scheduled_at stays None for all five non-retrying classes, and the
            # destination state has no edge back to SCHEDULED.
            return st.transition(
                conn,
                case_id=case.id,
                to_state=NON_RETRYING_DESTINATION[entry.action],
                actor="executor",
                reason=ACTION_REASON_CODE[entry.action],
                now=now,
                detail={"action": entry.action, "advice": ACTION_ADVICE[entry.action]},
                next_attempt_at=None,
            )

        if attempts_used >= case.max_attempts:
            return self._exhaust(conn, case, now=now, reason="max_attempts_reached")

        scheduled_at = self.next_attempt_at(entry, now)
        if scheduled_at > case.drop_dead_at:
            return self._exhaust(conn, case, now=now, reason="drop_dead_at_passed")

        return st.transition(
            conn,
            case_id=case.id,
            to_state=st.SCHEDULED,
            actor="executor",
            reason=ACTION_REASON_CODE[entry.action],
            now=now,
            detail={
                "action": entry.action,
                "scheduled_at": scheduled_at,
                "min_wait_hours": entry.min_wait_hours,
            },
            next_attempt_at=scheduled_at,
        )

    def _exhaust(
        self, conn: sqlite3.Connection, case: db.Case, *, now: int, reason: str
    ) -> db.Case:
        return st.transition(
            conn,
            case_id=case.id,
            to_state=st.EXHAUSTED,
            actor="executor",
            reason=reason,
            now=now,
            next_attempt_at=None,
        )

    # -- attempts --------------------------------------------------------------

    def check_bounds(
        self, conn: sqlite3.Connection, case: db.Case, *, now: int
    ) -> int:
        """Every bound, checked before an attempt is constructed. Returns the number.

        Raises AwaitingStatus (423) or PolicyViolation (422). Nothing is written and
        no resolver is called until this has passed. (I-6, I-7)
        """
        if case.state == st.AWAITING_STATUS and case.status_resolved_at is None:
            raise AwaitingStatus(case.id)
        if st.is_terminal(case.state):
            raise PolicyViolation(
                "case_terminal", f"case {case.id} is {case.state} and accepts no attempts"
            )
        if case.state not in st.ATTEMPTABLE_FROM:
            raise PolicyViolation(
                "case_not_scheduled",
                f"case {case.id} is {case.state}; an attempt requires SCHEDULED",
            )

        attempt_number = db.attempt_count(conn, case.id) + 1
        if attempt_number > case.max_attempts:
            raise PolicyViolation(
                "max_attempts_exceeded",
                f"case {case.id} has used all {case.max_attempts} attempts",
            )
        if now > case.drop_dead_at:
            raise PolicyViolation(
                "drop_dead_at_passed",
                f"case {case.id} passed its drop-dead time at {case.drop_dead_at}",
            )
        previous = db.last_attempt(conn, case.id)
        if previous is not None and now < previous.executed_at + self.min_interval_seconds:
            raise PolicyViolation(
                "min_interval_not_elapsed",
                f"case {case.id} last attempted at {previous.executed_at}; "
                f"{self.min_interval_seconds}s must elapse between attempts",
            )
        return attempt_number

    def execute_attempt(
        self,
        conn: sqlite3.Connection,
        case: db.Case,
        world: AttemptResolver,
        *,
        now: int,
        idempotency_key: str | None = None,
        action: str | None = None,
        target_rail: str | None = None,
        actor: str = "executor",
        policy_routed: bool = True,
    ) -> tuple[db.Attempt, db.Case]:
        """Run one attempt end to end: bounds, audit, resolve, record, transition.

        ``action`` defaults to whatever the policy table says about the case's current
        code — that is TRIAGE's own behaviour, and what the API serves. An arm may
        override it: the control arm retries every code on a fixed schedule and must
        not have policy quietly correct it, or there would be nothing to measure
        against. ``policy_routed`` says whether the *outcome* is re-diagnosed the same
        way. The bounds, the idempotency guard and the I-6 block are not overridable.
        """
        # The idempotency check comes first, before the bounds. A client replaying a
        # request must be told it is a duplicate — not told about whatever state the
        # original attempt left the case in. Reporting a replay as a bound breach
        # would hide a double-charge attempt behind a 422.
        if idempotency_key is not None and db.idempotency_key_exists(
            conn, idempotency_key
        ):
            raise db.IdempotencyConflict(idempotency_key)

        attempt_number = self.check_bounds(conn, case, now=now)
        entry = self.engine.resolve(case.error_code)

        if action is None:
            action = entry.action
            # Defence in depth behind the structural guarantee: on the policy-routed
            # path a non-retrying class can only be here via a resolved status poll.
            if not entry.is_retrying and case.status_resolved_at is None:
                raise PolicyViolation(
                    "action_class_does_not_retry",
                    f"{entry.action} never schedules a retry",
                )
            target_rail = (
                ALTERNATIVE_RAIL.get(case.method)
                if entry.action == "SWITCH_RAIL"
                else None
            )

        key = idempotency_key or f"{case.id}:{attempt_number}"
        if idempotency_key is None and db.idempotency_key_exists(conn, key):
            raise db.IdempotencyConflict(key)

        scheduled_at = case.next_attempt_at

        # I-8. The audit row lands before the attempt executes, not after it returns.
        case = st.transition(
            conn,
            case_id=case.id,
            from_state=st.SCHEDULED,
            to_state=st.ATTEMPTING,
            actor=actor,
            reason=f"executing {action}",
            now=now,
            idempotency_key=key,
            detail={
                "action": action,
                "target_rail": target_rail,
                "attempt_number": attempt_number,
            },
        )

        outcome = world.attempt(
            db.CaseView(
                id=case.id,
                customer_id=case.customer_id,
                merchant_id=case.merchant_id,
                method=case.method,
                rail=case.rail,
                amount_paise=case.amount_paise,
                error_code=case.error_code,
                failed_at=case.failed_at,
                attempt_number=attempt_number,
            ),
            action,
            target_rail,
            now,
        )

        attempt = db.insert_attempt(
            conn,
            db.Attempt(
                id=db.stable_id("att_", case.id, attempt_number),
                case_id=case.id,
                attempt_number=attempt_number,
                idempotency_key=key,
                action=action,
                target_rail=target_rail,
                scheduled_at=scheduled_at,
                executed_at=now,
                outcome="success" if outcome.success else "failed",
                error_code=outcome.error_code,
                latency_ms=outcome.latency_ms,
            ),
        )

        case = self._settle(
            conn,
            case,
            outcome,
            now=now,
            attempt_number=attempt_number,
            key=key,
            actor=actor,
            policy_routed=policy_routed,
        )
        return attempt, case

    def _settle(
        self,
        conn: sqlite3.Connection,
        case: db.Case,
        outcome: OutcomeLike,
        *,
        now: int,
        attempt_number: int,
        key: str,
        actor: str = "executor",
        policy_routed: bool = True,
    ) -> db.Case:
        """ATTEMPTING -> wherever the outcome puts the case."""
        if outcome.success:
            return st.transition(
                conn,
                case_id=case.id,
                from_state=st.ATTEMPTING,
                to_state=st.RECOVERED,
                actor=actor,
                reason="attempt succeeded",
                now=now,
                idempotency_key=key,
                recovered_at=now,
                recovered_amount_paise=case.amount_paise,
                next_attempt_at=None,
            )

        # The failure code may differ from the one that opened the case. Re-diagnose
        # against the new code — that is how a case moves between action classes
        # across attempts. The payment keeps its original code for Stage 3 segmenting.
        new_code = outcome.error_code
        if new_code is None:
            return self._exhaust(conn, case, now=now, reason="failed_without_a_code")

        db.update_case(conn, case.id, error_code=new_code, error_source=outcome.error_source)
        refreshed = db.get_case(conn, case.id)
        assert refreshed is not None
        entry = self.engine.resolve(new_code)

        if attempt_number >= refreshed.max_attempts:
            return self._exhaust(conn, refreshed, now=now, reason="max_attempts_reached")

        if not policy_routed:
            # The arm owns routing on this path. Hand the case back ready for its next
            # decision rather than letting the policy table redirect an arm that never
            # consulted it — that would quietly turn the control arm into the baseline.
            return st.transition(
                conn,
                case_id=refreshed.id,
                from_state=st.ATTEMPTING,
                to_state=st.SCHEDULED,
                actor=actor,
                reason="attempt failed, awaiting the arm's next decision",
                now=now,
                detail={"error_code": new_code, "attempt_number": attempt_number},
                next_attempt_at=now,
            )

        if not entry.is_retrying:
            destination = NON_RETRYING_DESTINATION[entry.action]
            return st.transition(
                conn,
                case_id=refreshed.id,
                from_state=st.ATTEMPTING,
                to_state=destination,
                actor="executor",
                reason=ACTION_REASON_CODE[entry.action],
                now=now,
                detail={"error_code": new_code, "action": entry.action},
                next_attempt_at=None,
                # A fresh AWAIT_STATUS needs a fresh poll before it can attempt again.
                status_resolved_at=None,
            )

        scheduled_at = self.next_attempt_at(entry, now)
        if scheduled_at > refreshed.drop_dead_at:
            return self._exhaust(conn, refreshed, now=now, reason="drop_dead_at_passed")

        return st.transition(
            conn,
            case_id=refreshed.id,
            from_state=st.ATTEMPTING,
            to_state=st.SCHEDULED,
            actor="executor",
            reason=ACTION_REASON_CODE[entry.action],
            now=now,
            detail={
                "error_code": new_code,
                "action": entry.action,
                "scheduled_at": scheduled_at,
            },
            next_attempt_at=scheduled_at,
        )

    # -- arm-driven execution --------------------------------------------------
    #
    # An arm decides; the runner executes and enforces. Everything below takes the
    # action from the arm rather than from the policy table, so the control arm can be
    # genuinely naive. The bounds (I-7), the idempotency guard (I-5) and the
    # AWAIT_STATUS block (I-6) are not overridable by any arm.

    def apply(
        self,
        conn: sqlite3.Connection,
        case: db.Case,
        decision: ArmDecisionLike,
        world: AttemptResolver,
        *,
        now: int,
        actor: str = "arm",
    ) -> db.Case:
        """Execute one arm decision. Returns the case as it now stands."""
        if st.is_terminal(case.state):
            return case
        if now > case.drop_dead_at:
            return self._exhaust(conn, case, now=now, reason="drop_dead_at_passed")

        entry = self.engine.resolve(case.error_code)

        # I-6, enforced above the arm. An unresolved AWAIT_STATUS code can never be
        # attempted, whatever the arm asks for — including an arm that never reads the
        # policy table. Retrying one of these double-charges the customer.
        if entry.action == "AWAIT_STATUS" and case.status_resolved_at is None:
            if case.state != st.AWAITING_STATUS:
                case = self._ensure_diagnosed(
                    conn, case, now=now, actor="executor", reason="await_status_guard"
                )
                return st.transition(
                    conn,
                    case_id=case.id,
                    to_state=st.AWAITING_STATUS,
                    actor="executor",
                    reason="outcome unresolved; attempting would risk a double charge",
                    now=now,
                    detail={"error_code": case.error_code, "guard": "I-6"},
                    next_attempt_at=None,
                )
            if decision.action != "AWAIT_STATUS":
                # This arm has no status poll. The case waits and expires at its
                # drop-dead time. That is the safe outcome, not a bug.
                return case
            return self.poll_status(conn, case, world, now=now, actor=actor)

        action = decision.action
        if action in RETRYING_ACTIONS:
            return self._apply_retry(conn, case, decision, world, now=now, actor=actor)
        if action == "NUDGE_CUSTOMER":
            return self._apply_nudge(conn, case, decision, world, now=now, actor=actor)
        if action == "AWAIT_STATUS":
            return case  # nothing to poll: the code is not an AWAIT_STATUS code
        return self._apply_terminal(conn, case, decision, now=now, actor=actor)

    def _ensure_diagnosed(
        self,
        conn: sqlite3.Connection,
        case: db.Case,
        *,
        now: int,
        actor: str,
        reason: str,
    ) -> db.Case:
        """RECEIVED -> DIAGNOSED, attributed to whoever made the call.

        The actor matters in the trail: a case routed by the control arm must not read
        as though the policy engine decided it.
        """
        if case.state != st.RECEIVED:
            return case
        return st.transition(
            conn,
            case_id=case.id,
            from_state=st.RECEIVED,
            to_state=st.DIAGNOSED,
            actor=actor,
            reason=reason,
            now=now,
            detail={"error_code": case.error_code},
        )

    def _apply_retry(
        self,
        conn: sqlite3.Connection,
        case: db.Case,
        decision: ArmDecisionLike,
        world: AttemptResolver,
        *,
        now: int,
        actor: str,
    ) -> db.Case:
        case = self._ensure_diagnosed(
            conn, case, now=now, actor=actor, reason=decision.reason_code
        )
        scheduled_at = decision.scheduled_at if decision.scheduled_at is not None else now

        if case.state == st.DIAGNOSED:
            if scheduled_at > case.drop_dead_at:
                return self._exhaust(conn, case, now=now, reason="drop_dead_at_passed")
            case = st.transition(
                conn,
                case_id=case.id,
                to_state=st.SCHEDULED,
                actor=actor,
                reason=decision.reason_code,
                now=now,
                detail={"action": decision.action, "scheduled_at": scheduled_at},
                next_attempt_at=scheduled_at,
            )
        if case.state != st.SCHEDULED:
            return case

        if scheduled_at > now:
            if case.next_attempt_at != scheduled_at:
                db.update_case(conn, case.id, next_attempt_at=scheduled_at)
                refreshed = db.get_case(conn, case.id)
                assert refreshed is not None
                return refreshed
            return case

        try:
            _, case = self.execute_attempt(
                conn,
                case,
                world,
                now=now,
                action=decision.action,
                target_rail=decision.target_rail,
                actor=actor,
                policy_routed=decision.policy_routed,
            )
        except PolicyViolation as exc:
            return self._exhaust(conn, case, now=now, reason=exc.reason)
        except AwaitingStatus:
            return case
        return case

    def _apply_nudge(
        self,
        conn: sqlite3.Connection,
        case: db.Case,
        decision: ArmDecisionLike,
        world: AttemptResolver,
        *,
        now: int,
        actor: str,
    ) -> db.Case:
        """Send a nudge, or ask whether an outstanding one has landed.

        A nudge is not an attempt. No idempotency key is consumed, no attempt row is
        written, and nothing is scheduled — I-4 forbids scheduling a retry on this
        class, and none is scheduled. If the customer acts, they have paid of their own
        accord and the case recovers without this system charging anything.
        """
        if case.state in (st.RECEIVED, st.DIAGNOSED):
            case = self._ensure_diagnosed(
                conn, case, now=now, actor=actor, reason=decision.reason_code
            )
            return st.transition(
                conn,
                case_id=case.id,
                to_state=st.ESCALATED,
                actor=actor,
                reason="nudge_sent",
                now=now,
                detail={
                    "channel": "sms",
                    "error_code": case.error_code,
                    "response_window_hours": NUDGE_RESPONSE_WINDOW_HOURS,
                },
                nudge_sent_at=now,
                next_attempt_at=None,
            )

        if case.state != st.ESCALATED or case.nudge_sent_at is None:
            return case

        deadline = case.nudge_sent_at + NUDGE_RESPONSE_WINDOW_HOURS * HOUR
        if now > deadline:
            return st.transition(
                conn,
                case_id=case.id,
                to_state=st.EXHAUSTED,
                actor=actor,
                reason="nudge_window_expired",
                now=now,
                detail={"nudge_sent_at": case.nudge_sent_at, "deadline": deadline},
                next_attempt_at=None,
            )

        outcome = world.attempt(
            self._view(case, attempt_number=1), NUDGE_ACTION, None, now
        )
        if not outcome.success:
            return case

        return st.transition(
            conn,
            case_id=case.id,
            from_state=st.ESCALATED,
            to_state=st.RECOVERED,
            actor=actor,
            reason="customer acted on the nudge and paid",
            now=now,
            detail={"hours_after_nudge": (now - case.nudge_sent_at) // HOUR},
            recovered_at=now,
            recovered_amount_paise=case.amount_paise,
            next_attempt_at=None,
        )

    def _apply_terminal(
        self,
        conn: sqlite3.Connection,
        case: db.Case,
        decision: ArmDecisionLike,
        *,
        now: int,
        actor: str,
    ) -> db.Case:
        """SWITCH_INSTRUMENT, STOP and MERCHANT_ALERT all end the automated path."""
        case = self._ensure_diagnosed(
            conn, case, now=now, actor=actor, reason=decision.reason_code
        )
        if st.is_terminal(case.state):
            return case
        return st.transition(
            conn,
            case_id=case.id,
            to_state=st.STOPPED,
            actor=actor,
            reason=decision.reason_code,
            now=now,
            detail={"action": decision.action},
            next_attempt_at=None,
        )

    def poll_status(
        self,
        conn: sqlite3.Connection,
        case: db.Case,
        world: AttemptResolver,
        *,
        now: int,
        actor: str = "status_poll",
    ) -> db.Case:
        """Ask the acquirer whether a pending payment settled, then act on the answer.

        A late authorisation is modelled as the payment resolving the way an attempt at
        this moment would — the same latent balance, outage and instrument state decide
        it. A simplification, stated rather than hidden.
        """
        outcome = world.attempt(
            self._view(case, attempt_number=db.attempt_count(conn, case.id) + 1),
            "AWAIT_STATUS",
            None,
            now,
        )
        return self.resolve_status(
            conn,
            case,
            now=now,
            resolution="succeeded" if outcome.success else "failed",
        )

    def expire_if_past_deadline(
        self, conn: sqlite3.Connection, case: db.Case, *, now: int
    ) -> db.Case:
        """Close a case whose drop-dead time has passed. (I-7)"""
        if st.is_terminal(case.state) or now <= case.drop_dead_at:
            return case
        return self._exhaust(conn, case, now=now, reason="drop_dead_at_passed")

    def _view(self, case: db.Case, *, attempt_number: int) -> db.CaseView:
        return db.CaseView(
            id=case.id,
            customer_id=case.customer_id,
            merchant_id=case.merchant_id,
            method=case.method,
            rail=case.rail,
            amount_paise=case.amount_paise,
            error_code=case.error_code,
            failed_at=case.failed_at,
            attempt_number=attempt_number,
        )

    # -- status polls ----------------------------------------------------------

    def resolve_status(
        self,
        conn: sqlite3.Connection,
        case: db.Case,
        *,
        now: int,
        resolution: str,
    ) -> db.Case:
        """Settle an AWAITING_STATUS case. This is what unblocks I-6's guard.

        ``resolution`` is supplied by the caller — in a real deployment it is the
        answer from a status API. ``succeeded`` means the payment authorised late
        and there is nothing to recover; ``failed`` means the outcome is now known
        and a retry is finally safe.
        """
        if resolution not in ("succeeded", "failed"):
            raise ValueError("resolution must be 'succeeded' or 'failed'")
        if case.state != st.AWAITING_STATUS:
            raise PolicyViolation(
                "case_not_awaiting_status",
                f"case {case.id} is {case.state}, not {st.AWAITING_STATUS}",
            )

        if resolution == "succeeded":
            return st.transition(
                conn,
                case_id=case.id,
                from_state=st.AWAITING_STATUS,
                to_state=st.RECOVERED,
                actor="status_poll",
                reason="late authorisation confirmed",
                now=now,
                status_resolved_at=now,
                recovered_at=now,
                recovered_amount_paise=case.amount_paise,
                next_attempt_at=None,
            )

        attempts_used = db.attempt_count(conn, case.id)
        if attempts_used >= case.max_attempts or now > case.drop_dead_at:
            return st.transition(
                conn,
                case_id=case.id,
                from_state=st.AWAITING_STATUS,
                to_state=st.EXHAUSTED,
                actor="status_poll",
                reason="resolved as failed, no attempts left",
                now=now,
                status_resolved_at=now,
                next_attempt_at=None,
            )

        # The outcome is known, so a retry no longer risks a double charge. Policy has
        # nothing to say about post-resolution timing for AWAIT_STATUS codes, so the
        # executor's sub-hour floor applies.
        return st.transition(
            conn,
            case_id=case.id,
            from_state=st.AWAITING_STATUS,
            to_state=st.SCHEDULED,
            actor="status_poll",
            reason="resolved as failed, retry now safe",
            now=now,
            status_resolved_at=now,
            next_attempt_at=now + RETRY_NOW_FLOOR_SECONDS,
        )

    def stop(
        self, conn: sqlite3.Connection, case: db.Case, *, now: int, reason: str
    ) -> db.Case:
        """Force-terminate a case."""
        if st.is_terminal(case.state):
            raise PolicyViolation(
                "case_terminal", f"case {case.id} is already {case.state}"
            )
        return st.transition(
            conn,
            case_id=case.id,
            to_state=st.STOPPED,
            actor="operator",
            reason=reason,
            now=now,
            next_attempt_at=None,
        )

    # -- batch -----------------------------------------------------------------

    def run_due(
        self,
        conn: sqlite3.Connection,
        world: AttemptResolver,
        *,
        now: int,
        limit: int = 5000,
    ) -> dict[str, int]:
        """Execute every case whose scheduled time has arrived.

        A bound breach exhausts the case rather than propagating: in a batch, hitting
        max_attempts is an expected end state, not an error. The API path still
        raises, because there a breach is a caller mistake worth a 422.
        """
        counts = {"attempted": 0, "recovered": 0, "exhausted": 0, "refused": 0}
        for case in db.due_cases(conn, now, limit=limit):
            try:
                _, updated = self.execute_attempt(conn, case, world, now=now)
            except (PolicyViolation, AwaitingStatus) as exc:
                counts["refused"] += 1
                reason = getattr(exc, "reason", "awaiting_status")
                self._exhaust(conn, case, now=now, reason=reason)
                counts["exhausted"] += 1
                continue
            counts["attempted"] += 1
            if updated.state == st.RECOVERED:
                counts["recovered"] += 1
            elif updated.state == st.EXHAUSTED:
                counts["exhausted"] += 1
        return counts

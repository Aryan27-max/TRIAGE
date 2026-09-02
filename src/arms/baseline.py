"""Baseline — the policy table, and nothing learned.

Reads the action class from the Stage 1 policy engine and acts on it. No model, no
timing prediction, no ranking: `RETRY_SCHEDULED` waits exactly `min_wait_hours`, flat.
Stage 4's treatment arm replaces that one number and nothing else, which is what makes
the *baseline − control* and *treatment − baseline* gaps separable.

    RETRY_NOW          schedule at the executor's sub-hour floor
    RETRY_SCHEDULED    schedule at min_wait_hours, flat
    SWITCH_RAIL        switch per the fixed target map; if the alternate rail is also
                       degraded, wait instead of switching into a second outage
    NUDGE_CUSTOMER     nudge once, then wait out the response window
    AWAIT_STATUS       poll, then re-diagnose
    SWITCH_INSTRUMENT  stop — instrument_permanently_unusable
    STOP               stop
    MERCHANT_ALERT     stop — merchant_configuration_defect

The three non-retrying stops are where the taxonomy earns its keep: control burns its
whole retry budget on those codes, and no retry could ever have worked.
"""

from __future__ import annotations

from src.arms.base import ArmDecision, CaseSnapshot, RailHealthLike
from src.executor.runner import (
    ACTION_REASON_CODE,
    ALTERNATIVE_RAIL,
    NUDGE_RESPONSE_WINDOW_HOURS,
    RETRY_NOW_FLOOR_SECONDS,
)
from src.executor.state import AWAITING_STATUS, ESCALATED, RECEIVED, SCHEDULED
from src.policy.engine import PolicyEngine

HOUR = 3600

# research/02 §2.4 maps severity to policy: `high` means suppress retries on this rail.
# Switching onto a rail that is itself down is worse than waiting for the first to clear.
BLOCKING_SEVERITY = "high"

# How long to wait when both the current rail and its alternate are degraded.
# Assumption (ours): long enough for a typical outage window to clear, short enough to
# fit several waits inside the drop-dead budget.
RAIL_WAIT_HOURS = 4

STOP_REASONS: dict[str, str] = {
    "SWITCH_INSTRUMENT": "instrument_permanently_unusable",
    "STOP": "retry_unsafe_or_penalised",
    "MERCHANT_ALERT": "merchant_configuration_defect",
}


class BaselineArm:
    """The policy table, acted on directly."""

    name = "baseline"

    def __init__(
        self,
        *,
        rail_wait_hours: int = RAIL_WAIT_HOURS,
        nudge_window_hours: int = NUDGE_RESPONSE_WINDOW_HOURS,
    ) -> None:
        self.rail_wait_hours = rail_wait_hours
        self.nudge_window_hours = nudge_window_hours

    def next_action(
        self,
        case: CaseSnapshot,
        policy_engine: PolicyEngine,
        rail_health: RailHealthLike,
        now: int,
    ) -> ArmDecision | None:
        # I-1: the table decides the class. Nothing here overrides it, and there is no
        # default branch — an unknown code raises rather than becoming a retry. (I-2)
        entry = policy_engine.resolve(case.error_code)
        action = entry.action

        if action == "NUDGE_CUSTOMER":
            return self._nudge(case, now)
        if action == "AWAIT_STATUS":
            return self._poll(case)
        if action in STOP_REASONS:
            return ArmDecision(action=action, reason_code=STOP_REASONS[action])

        if action == "SWITCH_RAIL":
            return self._switch_rail(case, rail_health, now)
        if action == "RETRY_NOW":
            return ArmDecision(
                action="RETRY_NOW",
                scheduled_at=self._anchor(case, now) + RETRY_NOW_FLOOR_SECONDS,
                reason_code=ACTION_REASON_CODE["RETRY_NOW"],
            )
        # RETRY_SCHEDULED. Flat min_wait_hours from the table — Stage 4 is what makes
        # this adaptive, and the gap between the two is the value of the model.
        return ArmDecision(
            action="RETRY_SCHEDULED",
            scheduled_at=self._anchor(case, now) + entry.min_wait_hours * HOUR,
            reason_code=ACTION_REASON_CODE["RETRY_SCHEDULED"],
        )

    # -- per-class handling ----------------------------------------------------

    def _nudge(self, case: CaseSnapshot, now: int) -> ArmDecision | None:
        """Nudge once, then wait out the response window.

        Nothing is scheduled here: I-4 forbids scheduling a retry on this class, and
        the decision carries no ``scheduled_at``. The runner sends the message and
        polls for a response; if the customer acts, they paid of their own accord.
        """
        if case.state in (RECEIVED, ESCALATED):
            return ArmDecision(
                action="NUDGE_CUSTOMER",
                reason_code=ACTION_REASON_CODE["NUDGE_CUSTOMER"],
            )
        return None

    def _poll(self, case: CaseSnapshot) -> ArmDecision | None:
        """Resolve the unknown outcome before anything else can happen. (I-6)"""
        if case.status_resolved_at is not None and case.state == SCHEDULED:
            # Resolved as failed and rescheduled by the runner; the re-diagnosed code
            # will pick up the right class on the next tick.
            return None
        return ArmDecision(
            action="AWAIT_STATUS", reason_code=ACTION_REASON_CODE["AWAIT_STATUS"]
        )

    def _switch_rail(
        self, case: CaseSnapshot, rail_health: RailHealthLike, now: int
    ) -> ArmDecision:
        """Same instrument, different route — the India-specific lever.

        research/01 §1.4: an issuer outage does not affect UPI, so switching beats
        waiting. But only if the destination is actually up. Switching into a second
        high-severity outage spends an attempt to land in the same place.
        """
        target = ALTERNATIVE_RAIL.get(case.method)
        anchor = self._anchor(case, now)

        if target is not None:
            severity = rail_health.severity_at(target, now, case.instruments)
            if severity != BLOCKING_SEVERITY:
                return ArmDecision(
                    action="SWITCH_RAIL",
                    target_rail=target,
                    scheduled_at=anchor + RETRY_NOW_FLOOR_SECONDS,
                    reason_code=ACTION_REASON_CODE["SWITCH_RAIL"],
                )

        # Both rails degraded. Wait for one of them rather than burning an attempt.
        return ArmDecision(
            action="SWITCH_RAIL",
            target_rail=target,
            scheduled_at=anchor + self.rail_wait_hours * HOUR,
            reason_code="both_rails_degraded_waiting",
        )

    def _anchor(self, case: CaseSnapshot, now: int) -> int:
        """Wait from the most recent attempt, or from the failure if there is none."""
        if case.attempt_count and case.last_attempt_at is not None:
            return case.last_attempt_at
        return case.failed_at

"""Control — the naive baseline everyone actually runs.

Fixed retry: +24h, three attempts, then stop. The same action for every one of the 110
error codes. It does not read the policy table, does not look at rail health, does not
nudge, and does not switch rails.

**This arm is deliberately dumb. Resist improving it.** Every improvement made here is
value subtracted from the measured gap, and the gap is the entire result. If control
looks poor next to baseline, that is the finding, not a bug to fix.

Two things constrain it anyway, and neither is negotiable by any arm:

* the executor's AWAIT_STATUS block — control has no status poll, so its
  unknown-outcome cases sit in AWAITING_STATUS until they expire. That is the correct
  behaviour: retrying them risks a double charge. (I-6)
* max_attempts and drop_dead_at. (I-7)
"""

from __future__ import annotations

from src.arms.base import ArmDecision, CaseSnapshot, RailHealthLike
from src.policy.engine import PolicyEngine

HOUR = 3600
RETRY_INTERVAL_HOURS = 24  # research/02 §2.5: "Fixed retry: +24h, ×3, stop"
MAX_RETRIES = 3


class ControlArm:
    """Fixed +24h retry, three times, for every code alike."""

    name = "control"

    def __init__(
        self,
        *,
        retry_interval_hours: int = RETRY_INTERVAL_HOURS,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self.retry_interval_hours = retry_interval_hours
        self.max_retries = max_retries

    def next_action(
        self,
        case: CaseSnapshot,
        policy_engine: PolicyEngine,
        rail_health: RailHealthLike,
        now: int,
    ) -> ArmDecision | None:
        """``policy_engine`` and ``rail_health`` are accepted and ignored.

        They are in the signature because every arm shares one. They are unused here
        because that is the entire definition of the control arm.
        """
        if case.attempt_count >= self.max_retries:
            return ArmDecision(
                action="STOP",
                reason_code="fixed_retry_budget_spent",
                policy_routed=False,
            )

        # Spacing runs from the last thing that happened to this payment: +24h from
        # the failure, then +24h from each attempt.
        anchor = case.last_attempt_at if case.attempt_count else case.failed_at
        return ArmDecision(
            action="RETRY_SCHEDULED",
            target_rail=None,
            scheduled_at=(anchor or case.failed_at) + self.retry_interval_hours * HOUR,
            reason_code="fixed_retry_schedule",
            # The policy table must not re-route this arm's outcomes. Without this, a
            # control retry that failed with `card_expired` would be routed to
            # ESCALATED by policy, and control would silently become the baseline.
            policy_routed=False,
        )

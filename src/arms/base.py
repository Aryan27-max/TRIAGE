"""What an arm is.

An arm is **pure policy**. Given a case, the policy table, the published rail-health
feed and the current time, it says what should happen next. It never touches the
world, never writes to the store, and never resolves an outcome — the runner does all
three, and enforces the bounds the arm has no say over.

That separation is what makes the comparison meaningful. Every arm faces the same
executor, the same idempotency guard, the same AWAIT_STATUS block and the same
drop-dead cutoff. The only thing that differs between arms is the decision.

Arms never import ``src.simulator``. (I-12)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from src.policy.engine import PolicyEngine
from src.store.db import Case


@dataclass(frozen=True, slots=True)
class CaseSnapshot:
    """What an arm sees. Observable fields only, plus the case's own history.

    A curated view rather than the raw row: an arm gets exactly what a real recovery
    service would have on hand at decision time, and nothing else. ``attempt_count``
    and ``last_attempt_at`` are folded in so an arm never has to query the store.
    """

    id: str
    error_code: str
    error_source: str | None
    method: str
    rail: str
    amount_paise: int
    failed_at: int
    state: str
    max_attempts: int
    drop_dead_at: int
    next_attempt_at: int | None
    status_resolved_at: int | None
    nudge_sent_at: int | None
    attempt_count: int
    last_attempt_at: int | None
    city_tier: int | None
    vpa_handle: str | None
    payer_bank: str | None

    @classmethod
    def of(
        cls, case: Case, *, attempt_count: int, last_attempt_at: int | None
    ) -> "CaseSnapshot":
        return cls(
            id=case.id,
            error_code=case.error_code,
            error_source=case.error_source,
            method=case.method,
            rail=case.rail,
            amount_paise=case.amount_paise,
            failed_at=case.failed_at,
            state=case.state,
            max_attempts=case.max_attempts,
            drop_dead_at=case.drop_dead_at,
            next_attempt_at=case.next_attempt_at,
            status_resolved_at=case.status_resolved_at,
            nudge_sent_at=case.nudge_sent_at,
            attempt_count=attempt_count,
            last_attempt_at=last_attempt_at,
            city_tier=case.city_tier,
            vpa_handle=case.vpa_handle,
            payer_bank=case.payer_bank,
        )

    @property
    def instruments(self) -> tuple[str | None, ...]:
        """What identifies this payment on its rail, for a rail-health lookup."""
        return (self.rail, self.vpa_handle, self.payer_bank)


@dataclass(frozen=True, slots=True)
class ArmDecision:
    """One arm's answer for one case at one tick.

    ``action`` is one of the eight policy classes. It is what the arm wants done, not
    necessarily what the policy table says — the control arm names RETRY_SCHEDULED for
    every code precisely because it does not consult the table.

    ``scheduled_at`` is when the action should fire; ``None`` means now.

    ``policy_routed`` says whether the outcome of an attempt is re-diagnosed against
    the policy table. False for control: letting policy redirect an arm that never
    consulted it would quietly turn the control into the baseline, and there would be
    nothing left to measure.
    """

    action: str
    target_rail: str | None = None
    scheduled_at: int | None = None
    reason_code: str = "arm_decision"
    policy_routed: bool = True


class RailHealthLike(Protocol):
    """The published downtime feed, structurally.

    Rail health is *observable* — a real recovery service reads Razorpay's Downtime
    API — so consulting it is not a hidden-state leak. It is typed structurally anyway,
    so ``src/arms`` carries no import edge into ``src.simulator``.
    """

    def severity_at(
        self, method: str, at_ts: int, instruments: tuple[str | None, ...] = ()
    ) -> str | None: ...


@runtime_checkable
class Arm(Protocol):
    """The whole interface. One method, and no state that survives a tick."""

    name: str

    def next_action(
        self,
        case: CaseSnapshot,
        policy_engine: PolicyEngine,
        rail_health: RailHealthLike,
        now: int,
    ) -> ArmDecision | None:
        """What to do with this case now. ``None`` means nothing at this tick."""
        ...

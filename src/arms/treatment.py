"""Treatment — the policy table, plus a model that ranks executions within it.

**The model never chooses the action class.** (I-1) The class comes from the policy
table exactly as it does for the baseline arm, and for six of the eight classes this
arm *is* the baseline arm — it delegates, rather than reimplementing the logic, so the
two cannot drift apart.

The model is consulted for `RETRY_SCHEDULED` and `SWITCH_RAIL` only, which between them
cover 24 of the 110 codes. That bounds the achievable uplift before a single row is
scored, and it is the honest frame for reading the result: on 86 of 110 codes this arm
is baseline by construction.

Within a permitted class the model ranks candidate executions by expected value:

    EV = P(success) * amount_paise - ATTEMPT_COST_PAISE

If no candidate has positive EV the arm returns STOP with reason
`negative_expected_value` — research/06 §6.6's local equivalent of Stripe declining to
attempt a payment it does not expect to be authorised. Refusing to spend an attempt is
a decision, and the audit log records it as one.

Constraints the model cannot override: `max_attempts`, `drop_dead_at`, `min_interval`,
the AWAIT_STATUS block, and the action class itself. It ranks within a permission set;
it never widens one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from src.arms.base import ArmDecision, CaseSnapshot, RailHealthLike
from src.arms.baseline import BLOCKING_SEVERITY, BaselineArm
from src.executor.runner import (
    ACTION_REASON_CODE,
    ALTERNATIVE_RAIL,
    RETRY_NOW_FLOOR_SECONDS,
)
from src.features.build import Candidate, build_features
from src.model.score import Scorer
from src.policy.engine import MODEL_ELIGIBLE_ACTIONS, PolicyEngine

HOUR = 3600

# Cost of one executed attempt, in paise. Same figure eval/score.py reports with, so
# the arm optimises against the cost the report charges it.
ATTEMPT_COST_PAISE = 200

# How finely to enumerate candidate retry times between the policy floor and the
# drop-dead cutoff. Two hours keeps the candidate set small enough to score in a tick
# while still being finer than anything the flat baseline can express.
CANDIDATE_GRANULARITY_HOURS = 2

# Hard ceiling on candidates per decision, so a long drop-dead window cannot turn one
# tick into hundreds of model calls.
MAX_CANDIDATES = 48


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    candidate: Candidate
    probability: float
    expected_value: float


class TreatmentArm:
    """Policy for the class, model for the execution."""

    name = "treatment"

    def __init__(
        self,
        scorer: Scorer,
        conn_provider=None,
        *,
        granularity_hours: int = CANDIDATE_GRANULARITY_HOURS,
        attempt_cost_paise: int = ATTEMPT_COST_PAISE,
        max_candidates: int = MAX_CANDIDATES,
    ) -> None:
        self.scorer = scorer
        self._conn_provider = conn_provider
        self.baseline = BaselineArm()
        self.granularity_hours = granularity_hours
        self.attempt_cost_paise = attempt_cost_paise
        self.max_candidates = max_candidates
        self.negative_ev_stops = 0
        self.model_decisions = 0

    # The arm is handed its store connection by the tick loop rather than opening one:
    # arms do not own resources, and the feature builder needs the same connection the
    # runner is writing through.
    def bind(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn_provider is not None:
            return self._conn_provider()
        return self._conn

    def next_action(
        self,
        case: CaseSnapshot,
        policy_engine: PolicyEngine,
        rail_health: RailHealthLike,
        now: int,
    ) -> ArmDecision | None:
        # I-1. The class comes from the table, before the model is reachable at all.
        entry = policy_engine.resolve(case.error_code)
        if entry.action not in MODEL_ELIGIBLE_ACTIONS:
            # 86 of 110 codes land here and are handled by the baseline arm verbatim.
            return self.baseline.next_action(case, policy_engine, rail_health, now)

        if entry.action == "RETRY_SCHEDULED":
            return self._retry_scheduled(case, entry, now)
        return self._switch_rail(case, entry, rail_health, now)

    # -- RETRY_SCHEDULED -------------------------------------------------------

    def _retry_scheduled(self, case: CaseSnapshot, entry, now: int) -> ArmDecision:
        anchor = self._anchor(case)
        earliest = max(now, anchor + entry.min_wait_hours * HOUR)
        candidates = [
            Candidate(action="RETRY_SCHEDULED", target_rail=None, scheduled_at=ts)
            for ts in self._slots(earliest, case.drop_dead_at)
        ]
        if not candidates:
            return ArmDecision(
                action="STOP", reason_code="no_slot_before_drop_dead", policy_routed=True
            )
        return self._argmax(case, candidates, now, fallback_action="RETRY_SCHEDULED")

    # -- SWITCH_RAIL -----------------------------------------------------------

    def _switch_rail(
        self, case: CaseSnapshot, entry, rail_health: RailHealthLike, now: int
    ) -> ArmDecision:
        anchor = self._anchor(case)
        earliest = max(now, anchor + RETRY_NOW_FLOOR_SECONDS)
        target = ALTERNATIVE_RAIL.get(case.method)

        candidates: list[Candidate] = []
        # Switching now, but only if the destination is actually up — the same guard
        # baseline applies. The model ranks; it does not get to ignore rail health.
        if target is not None and rail_health.severity_at(
            target, now, case.instruments
        ) != BLOCKING_SEVERITY:
            candidates.append(
                Candidate(action="SWITCH_RAIL", target_rail=target, scheduled_at=earliest)
            )
        # Waiting on the current rail is always a candidate: research/01 §1.4 calls the
        # switch the primary lever, but Stage 3 found that waiting out a short outage
        # can beat switching, so the model gets to weigh both.
        for ts in self._slots(earliest, case.drop_dead_at, limit=8):
            candidates.append(
                Candidate(action="SWITCH_RAIL", target_rail=None, scheduled_at=ts)
            )
        if not candidates:
            return ArmDecision(
                action="STOP", reason_code="no_slot_before_drop_dead", policy_routed=True
            )
        return self._argmax(case, candidates, now, fallback_action="SWITCH_RAIL")

    # -- ranking ---------------------------------------------------------------

    def _argmax(
        self,
        case: CaseSnapshot,
        candidates: list[Candidate],
        now: int,
        *,
        fallback_action: str,
    ) -> ArmDecision:
        rows = [
            build_features(self.conn, case.id, candidate, now)
            for candidate in candidates
        ]
        probabilities = self.scorer.score_batch(rows)
        self.model_decisions += 1

        scored = [
            ScoredCandidate(
                candidate=candidate,
                probability=p,
                expected_value=p * case.amount_paise - self.attempt_cost_paise,
            )
            for candidate, p in zip(candidates, probabilities)
        ]
        best = max(scored, key=lambda s: s.expected_value)

        if best.expected_value <= 0:
            # research/06 §6.6. Declining to act is a decision, and it is recorded as
            # one. This is the local equivalent of Stripe's do_not_try_again.
            self.negative_ev_stops += 1
            return ArmDecision(
                action="STOP",
                reason_code="negative_expected_value",
                policy_routed=True,
            )

        return ArmDecision(
            action=best.candidate.action,
            target_rail=best.candidate.target_rail,
            scheduled_at=best.candidate.scheduled_at,
            reason_code=ACTION_REASON_CODE[fallback_action],
            policy_routed=True,
        )

    # -- helpers ---------------------------------------------------------------

    def _slots(self, earliest: int, drop_dead_at: int, limit: int | None = None) -> list[int]:
        """Candidate execution times, from the policy floor to the drop-dead cutoff."""
        step = self.granularity_hours * HOUR
        cap = limit or self.max_candidates
        slots: list[int] = []
        ts = earliest
        while ts <= drop_dead_at and len(slots) < cap:
            slots.append(ts)
            ts += step
        return slots

    def _anchor(self, case: CaseSnapshot) -> int:
        if case.attempt_count and case.last_attempt_at is not None:
            return case.last_attempt_at
        return case.failed_at

"""Scoring. Every rule in here is an invariant, not a preference.

    I-14  Deduplicate to the payment, scored on final outcome. One payment attempted
          four times is ONE payment, recovered or not. Attempts are never the
          denominator.
    I-15  Apply the trailing window before scoring. A retry scheduled on day 28 may
          land on day 33; cutting at day 30 systematically undercounts whichever arm
          schedules further out.
    I-16  Per-error-code breakdown, always including the rows where an arm loses.
          Negative uplift is never filtered, sorted away or hidden behind a flag.
    I-17  Attempt counts and cost reported alongside recovery rates. An arm that
          recovers 3% more using twice the attempts has not necessarily won.

Confidence intervals are Wilson score, and arm gaps carry a two-proportion z-test.
With a few hundred cases split across arms the samples are small; the point of
reporting the interval is to say so numerically rather than implying precision that
is not there. Everything is stdlib — no scipy.
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from src.policy.engine import ACTIONS, PolicyEngine
from src.store import db

DAY = 86400

# Cost per executed payment attempt, in paise. Assumption (ours): Razorpay publishes
# no per-attempt cost. The absolute number does not matter to the comparison — both
# arms are charged the same rate — but reporting cost at all is I-17.
ATTEMPT_COST_PAISE = 200

# Cost of one nudge (an SMS with a payment link). Assumption (ours). Deliberately far
# below an attempt: the asymmetry is part of why nudging beats retrying on codes that
# a retry can never fix.
NUDGE_COST_PAISE = 20

RECOVERED = "RECOVERED"

TTR_BUCKETS: tuple[tuple[str, int], ...] = (
    ("< 1h", 3600),
    ("1-6h", 6 * 3600),
    ("6-24h", DAY),
    ("1-3d", 3 * DAY),
    ("3-7d", 7 * DAY),
    ("> 7d", 1 << 62),
)


# -- statistics ---------------------------------------------------------------


def wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval. Behaves at small n and at rates near 0 or 1, where the
    normal approximation gives intervals that run past the ends of the scale."""
    if trials == 0:
        return (0.0, 0.0)
    p = successes / trials
    denominator = 1.0 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denominator
    margin = (
        z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))
    ) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def two_proportion_z(
    successes_a: int, trials_a: int, successes_b: int, trials_b: int
) -> tuple[float, float]:
    """Two-sided z-test on the difference of two proportions. Returns (z, p)."""
    if trials_a == 0 or trials_b == 0:
        return (0.0, 1.0)
    pooled = (successes_a + successes_b) / (trials_a + trials_b)
    standard_error = math.sqrt(
        pooled * (1.0 - pooled) * (1.0 / trials_a + 1.0 / trials_b)
    )
    if standard_error == 0.0:
        return (0.0, 1.0)
    z = (successes_a / trials_a - successes_b / trials_b) / standard_error
    return (z, 2.0 * (1.0 - normal_cdf(abs(z))))


def percentile(values: Sequence[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


# -- result shapes ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimeToRecovery:
    n: int
    median_s: int
    p25_s: int
    p75_s: int
    mean_s: int
    buckets: dict[str, int]


@dataclass(frozen=True, slots=True)
class ArmScore:
    arm: str
    payments: int
    recovered: int
    rate: float
    ci_low: float
    ci_high: float
    recovered_without_trailing_window: int
    attempts: int
    attempt_cost_paise: int
    nudges: int
    nudge_cost_paise: int
    total_cost_paise: int
    amount_at_risk_paise: int
    amount_recovered_paise: int
    attempts_per_payment: float
    cost_per_recovery_paise: int
    time_to_recovery: TimeToRecovery
    states: dict[str, int]


@dataclass(frozen=True, slots=True)
class Gap:
    focus: str
    reference: str
    focus_rate: float
    reference_rate: float
    pp: float
    relative: float
    z: float
    p_value: float


@dataclass(frozen=True, slots=True)
class SegmentRow:
    key: str
    label: str
    n: int
    counts: dict[str, tuple[int, int]] = field(default_factory=dict)
    pp: float | None = None

    def rate(self, arm: str) -> float | None:
        entry = self.counts.get(arm)
        if not entry or entry[0] == 0:
            return None
        return entry[1] / entry[0]


@dataclass(frozen=True, slots=True)
class Scorecard:
    run: db.Run
    arms: list[str]
    reference: str
    window_days: int
    trailing_days: int
    cutoff_ts: int
    scores: dict[str, ArmScore]
    gaps: list[Gap]
    by_error_code: list[SegmentRow]
    by_action: list[SegmentRow]
    by_rail: list[SegmentRow]

    @property
    def losses(self) -> list[SegmentRow]:
        """Segments where the focus arm underperforms the reference. (I-16)

        Not a filter applied for tidiness — these rows are also in `by_error_code`.
        This is the same data pulled out so the report cannot bury it.
        """
        return [row for row in self.by_error_code if row.pp is not None and row.pp < 0]


# -- scoring ------------------------------------------------------------------


def _rows(conn: sqlite3.Connection, cutoff_ts: int) -> list[dict[str, Any]]:
    """One row per payment that failed, joined to its case. (I-14)

    The join is on the case, and `cases.payment_id` is UNIQUE, so this cannot produce
    two rows for one payment however many attempts it took. The *original* failure
    code comes from the payment, not from the case: a case's code mutates as it moves
    between action classes across attempts, and segmenting by the cause that opened
    the case is what makes the per-code table interpretable.
    """
    return db.fetch_all(
        conn,
        """
        SELECT c.id            AS case_id,
               c.payment_id    AS payment_id,
               c.arm           AS arm,
               c.state         AS state,
               c.method        AS method,
               c.rail          AS rail,
               c.amount_paise  AS amount_paise,
               c.failed_at     AS failed_at,
               c.recovered_at  AS recovered_at,
               c.recovered_amount_paise AS recovered_amount_paise,
               c.nudge_sent_at AS nudge_sent_at,
               p.first_error_code AS error_code
        FROM cases c
        JOIN payments p ON p.id = c.payment_id
        ORDER BY c.id
        """,
    )


def _recovered(row: dict[str, Any], cutoff_ts: int) -> bool:
    """I-14 + I-15: final outcome, counted only if it landed inside the window."""
    return (
        row["state"] == RECOVERED
        and row["recovered_at"] is not None
        and row["recovered_at"] <= cutoff_ts
    )


def _time_to_recovery(deltas: list[int]) -> TimeToRecovery:
    buckets = {label: 0 for label, _ in TTR_BUCKETS}
    for delta in deltas:
        for label, upper in TTR_BUCKETS:
            if delta < upper:
                buckets[label] += 1
                break
    return TimeToRecovery(
        n=len(deltas),
        median_s=percentile(deltas, 0.5),
        p25_s=percentile(deltas, 0.25),
        p75_s=percentile(deltas, 0.75),
        mean_s=int(sum(deltas) / len(deltas)) if deltas else 0,
        buckets=buckets,
    )


def _segment(
    rows: Iterable[dict[str, Any]],
    arms: Sequence[str],
    cutoff_ts: int,
    key_of,
    label_of,
    reference: str,
    focus: str | None,
) -> list[SegmentRow]:
    totals: dict[str, dict[str, list[int]]] = defaultdict(
        lambda: {arm: [0, 0] for arm in arms}
    )
    labels: dict[str, str] = {}
    for row in rows:
        key = key_of(row)
        labels.setdefault(key, label_of(row))
        bucket = totals[key][row["arm"]]
        bucket[0] += 1
        if _recovered(row, cutoff_ts):
            bucket[1] += 1

    out: list[SegmentRow] = []
    for key, per_arm in totals.items():
        counts = {arm: (per_arm[arm][0], per_arm[arm][1]) for arm in arms}
        n = sum(c[0] for c in counts.values())
        pp: float | None = None
        if focus is not None and focus in counts and reference in counts:
            f_n, f_k = counts[focus]
            r_n, r_k = counts[reference]
            if f_n and r_n:
                pp = round((f_k / f_n - r_k / r_n) * 100.0, 1)
        out.append(SegmentRow(key=key, label=labels[key], n=n, counts=counts, pp=pp))
    out.sort(key=lambda r: (-r.n, r.key))
    return out


def score(
    conn: sqlite3.Connection,
    run: db.Run,
    engine: PolicyEngine,
    *,
    reference: str = "control",
) -> Scorecard:
    """Score one run. Pure read — nothing in here writes to the store."""
    arms = run.arm_names
    if not arms:
        raise ValueError(f"run {run.run_id} records no arms")

    # I-15. Scoring runs to the end of the trailing window, not the end of the main
    # window, and the harness executes that far too — an unexecuted trailing window
    # would undercount exactly the arm that schedules furthest out.
    cutoff_ts = run.start_ts + (run.days + run.trailing_days) * DAY
    main_cutoff_ts = run.start_ts + run.days * DAY

    rows = _rows(conn, cutoff_ts)
    attempts_by_arm: dict[str, int] = defaultdict(int)
    for record in db.fetch_all(
        conn,
        "SELECT c.arm AS arm, COUNT(*) AS n FROM attempts a "
        "JOIN cases c ON c.id = a.case_id GROUP BY c.arm",
    ):
        attempts_by_arm[record["arm"]] = record["n"]

    scores: dict[str, ArmScore] = {}
    for arm in arms:
        arm_rows = [r for r in rows if r["arm"] == arm]
        payments = len(arm_rows)
        recovered_rows = [r for r in arm_rows if _recovered(r, cutoff_ts)]
        recovered = len(recovered_rows)
        early = sum(
            1
            for r in arm_rows
            if r["state"] == RECOVERED
            and r["recovered_at"] is not None
            and r["recovered_at"] <= main_cutoff_ts
        )
        attempts = attempts_by_arm.get(arm, 0)
        nudges = sum(1 for r in arm_rows if r["nudge_sent_at"] is not None)
        low, high = wilson_interval(recovered, payments)
        attempt_cost = attempts * ATTEMPT_COST_PAISE
        nudge_cost = nudges * NUDGE_COST_PAISE
        states: dict[str, int] = defaultdict(int)
        for r in arm_rows:
            states[r["state"]] += 1

        scores[arm] = ArmScore(
            arm=arm,
            payments=payments,
            recovered=recovered,
            rate=recovered / payments if payments else 0.0,
            ci_low=low,
            ci_high=high,
            recovered_without_trailing_window=early,
            attempts=attempts,
            attempt_cost_paise=attempt_cost,
            nudges=nudges,
            nudge_cost_paise=nudge_cost,
            total_cost_paise=attempt_cost + nudge_cost,
            amount_at_risk_paise=sum(r["amount_paise"] for r in arm_rows),
            amount_recovered_paise=sum(
                r["recovered_amount_paise"] or 0 for r in recovered_rows
            ),
            attempts_per_payment=attempts / payments if payments else 0.0,
            cost_per_recovery_paise=(
                (attempt_cost + nudge_cost) // recovered if recovered else 0
            ),
            time_to_recovery=_time_to_recovery(
                [r["recovered_at"] - r["failed_at"] for r in recovered_rows]
            ),
            states=dict(sorted(states.items())),
        )

    focus = next((a for a in arms if a != reference), None)
    gaps: list[Gap] = []
    if reference in scores:
        for arm in arms:
            if arm == reference:
                continue
            a, b = scores[arm], scores[reference]
            z, p = two_proportion_z(a.recovered, a.payments, b.recovered, b.payments)
            gaps.append(
                Gap(
                    focus=arm,
                    reference=reference,
                    focus_rate=a.rate,
                    reference_rate=b.rate,
                    pp=round((a.rate - b.rate) * 100.0, 1),
                    relative=(a.rate - b.rate) / b.rate if b.rate else 0.0,
                    z=z,
                    p_value=p,
                )
            )

    def action_of(row: dict[str, Any]) -> str:
        return engine.resolve(row["error_code"]).action

    by_error_code = _segment(
        rows, arms, cutoff_ts, lambda r: r["error_code"], action_of, reference, focus
    )
    by_action = _segment(
        rows, arms, cutoff_ts, action_of, lambda r: "", reference, focus
    )
    by_action.sort(key=lambda r: ACTIONS.index(r.key) if r.key in ACTIONS else 99)
    by_rail = _segment(
        rows, arms, cutoff_ts, lambda r: r["method"], lambda r: "", reference, focus
    )

    return Scorecard(
        run=run,
        arms=arms,
        reference=reference,
        window_days=run.days,
        trailing_days=run.trailing_days,
        cutoff_ts=cutoff_ts,
        scores=scores,
        gaps=gaps,
        by_error_code=by_error_code,
        by_action=by_action,
        by_rail=by_rail,
    )


def to_api_shape(card: Scorecard) -> dict[str, Any]:
    """The response shape in research/05 §5.4."""
    return {
        "run_id": card.run.run_id,
        "scenario": card.run.scenario,
        "seed": card.run.seed,
        "measurement": {
            "window_days": card.window_days,
            "trailing_window_days": card.trailing_days,
            "dedup": "by_payment_final_outcome",
            "tick_seconds": card.run.tick_seconds,
        },
        "arms": {
            arm: {
                "payments": s.payments,
                "recovered": s.recovered,
                "rate": round(s.rate, 4),
                "ci_low": round(s.ci_low, 4),
                "ci_high": round(s.ci_high, 4),
                "attempts": s.attempts,
                "attempt_cost": s.attempt_cost_paise,
                "nudges": s.nudges,
                "total_cost_paise": s.total_cost_paise,
                "amount_recovered_paise": s.amount_recovered_paise,
            }
            for arm, s in card.scores.items()
        },
        "uplift": {
            f"{g.focus}_vs_{g.reference}": {
                "pp": g.pp,
                "relative": round(g.relative, 4),
                "z": round(g.z, 3),
                "p_value": round(g.p_value, 4),
            }
            for g in card.gaps
        },
        "by_error_code": [
            {
                "code": row.key,
                "action": row.label,
                "n": row.n,
                **{
                    arm: (round(rate, 4) if (rate := row.rate(arm)) is not None else None)
                    for arm in card.arms
                },
                "pp": row.pp,
            }
            for row in card.by_error_code
        ],
        "by_action_class": [
            {
                "action": row.key,
                "n": row.n,
                **{
                    arm: (round(rate, 4) if (rate := row.rate(arm)) is not None else None)
                    for arm in card.arms
                },
                "pp": row.pp,
            }
            for row in card.by_action
        ],
        # I-16. Published in its own field so a consumer cannot render the report
        # without it.
        "losing_segments": [
            {"code": row.key, "n": row.n, "pp": row.pp} for row in card.losses
        ],
    }

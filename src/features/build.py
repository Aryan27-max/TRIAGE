"""Point-in-time feature construction.

**The single most important implementation constraint in the project.** Every rolling
aggregate here filters on ``event_time < as_of``, strictly less than. Computing a
customer's historical success rate over the whole table leaks the outcome and produces
an AUC that collapses the moment the model meets data it has not already seen. It is
the first thing a payments ML reviewer checks. (I-9)

``as_of`` is required and has no default. Omitting it is a TypeError, not a silently
wrong call.

Two things this module may never do:

* import ``src.simulator`` — the world's latent state is not an input (I-12). In
  particular ``days_to_salary_date`` is **derived from the day of the month**, not read
  from the customer's true salary day. Inferring it is exactly the job the model is
  being given.
* read a clock. Everything temporal comes from ``as_of`` or from stored rows.

If a value cannot be computed from the store plus the candidate, it is not a feature.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from src.policy.engine import PolicyEngine
from src.store import db

HOUR = 3600
DAY = 86400

# IST is UTC+5:30. Civil-time questions are arithmetic on a passed-in timestamp.
IST_OFFSET_SECONDS = 5 * HOUR + 30 * 60

# research/04 §4.6: UPI success sags during evening peaks as banks contend.
PEAK_WINDOW_IST = (19, 22)

# research/03 §3.3: Indian salary cycles concentrate on the 1st and the 7th. The model
# sees these candidate dates and the day of the month; it never sees which one applies
# to this customer, because that is latent.
SALARY_DAYS = (1, 7)

PRIOR_FAILURE_WINDOW_DAYS = 30
RAIL_WINDOW_SECONDS = HOUR

# Ticket bands in paise, matching the simulator's merchant bands.
TICKET_BANDS = ((150_000, 0), (900_000, 1), (1 << 62, 2))

SEVERITY_RANK: dict[str | None, int] = {None: 0, "low": 1, "medium": 2, "high": 3}

# Where SWITCH_RAIL sends a payment. Duplicated from the executor rather than imported
# so the feature builder stays a leaf of the dependency graph.
ALTERNATIVE_RAIL: dict[str, str] = {
    "upi": "card",
    "card": "upi",
    "netbanking": "upi",
    "wallet": "upi",
}

CATEGORICAL_FEATURES: tuple[str, ...] = (
    "payer_bank",
    "vpa_handle",
    "mcc",
    "error_code",
    "error_source",
    "action_class",
    "method",
    "candidate_action",
    "candidate_target_rail",
)

FEATURE_NAMES: tuple[str, ...] = (
    # Customer
    "payer_bank",
    "vpa_handle",
    "city_tier",
    "cust_hist_success_rate",
    "cust_prior_failures_30d",
    "cust_prior_recovery_lag_h",
    # Business
    "mcc",
    "ticket_size_band",
    "is_recurring",
    # Payment
    "error_code",
    "error_source",
    "action_class",
    "method",
    "attempt_number",
    "hours_since_first_failure",
    "log_amount",
    # Seasonality
    "hour_sin",
    "hour_cos",
    "dow_sin",
    "dow_cos",
    "day_of_month",
    "days_to_salary_date",
    "is_peak_window",
    "is_month_end",
    # Rail health
    "rail_downtime_active",
    "rail_downtime_severity",
    "rail_success_rate_1h",
    "alt_rail_success_rate_1h",
    # Candidate
    "candidate_action",
    "candidate_target_rail",
    "candidate_delay_hours",
)


class FeatureError(Exception):
    """The store cannot produce features for this request."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """One execution the policy table already permits. Never a choice of class."""

    action: str
    target_rail: str | None = None
    scheduled_at: int | None = None


def ist_civil(at_ts: int) -> datetime:
    """The Indian civil date for a timestamp. Conversion, not a clock read."""
    return datetime.fromtimestamp(at_ts + IST_OFFSET_SECONDS, tz=timezone.utc)


def days_to_salary_date(at_ts: int) -> int:
    """Days until the next conventional Indian salary date, from the day of month.

    Derived, never read from the world. The customer's true salary day is latent; the
    model gets the calendar and has to infer the rest, which is the whole point of the
    feature. (I-12)
    """
    civil = ist_civil(at_ts)
    day = civil.day
    ahead = [d - day for d in SALARY_DAYS if d >= day]
    if ahead:
        return min(ahead)
    first_next = (civil.replace(day=1) + timedelta(days=32)).replace(day=1)
    days_in_month = (first_next - timedelta(days=1)).day
    return days_in_month - day + SALARY_DAYS[0]


def ticket_band(amount_paise: int) -> int:
    for ceiling, band in TICKET_BANDS:
        if amount_paise < ceiling:
            return band
    return TICKET_BANDS[-1][1]


def _rolling_rail_success(
    conn: sqlite3.Connection, method: str, as_of: int, window: int = RAIL_WINDOW_SECONDS
) -> float:
    """Success rate on this rail over the preceding window, from recorded attempts.

    Read out of the attempts table, not from the world and not from the downtime
    generator's ground truth: this is what a real system could compute for itself.
    Returns -1.0 when the window holds nothing, so "no data" stays distinguishable from
    "no successes" rather than both being encoded as zero.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n, "
        "       SUM(CASE WHEN a.outcome = 'success' THEN 1 ELSE 0 END) AS wins "
        "FROM attempts a JOIN cases c ON c.id = a.case_id "
        "WHERE a.executed_at < ? AND a.executed_at >= ? "
        "  AND COALESCE(a.target_rail, c.method) = ?",
        (as_of, as_of - window, method),
    ).fetchone()
    total = row["n"] or 0
    if total == 0:
        return -1.0
    return (row["wins"] or 0) / total


def _state_as_of(
    conn: sqlite3.Connection, case: db.Case, as_of: int
) -> tuple[str, str | None, int]:
    """The case's error code, source and attempt count as they stood at ``as_of``.

    A case's ``error_code`` mutates as it moves between action classes across attempts,
    so reading the stored value while replaying attempt 1 of a four-attempt case hands
    the row a code that was not known until three attempts later. The code in force is
    the one the most recent *prior* attempt reported, or the payment's original failure
    if no attempt has run yet.
    """
    rows = conn.execute(
        "SELECT attempt_number, error_code FROM attempts "
        "WHERE case_id = ? AND executed_at < ? ORDER BY attempt_number DESC",
        (case.id, as_of),
    ).fetchall()
    attempts_before = len(rows)
    for row in rows:
        if row["error_code"]:
            return row["error_code"], _source_for(case, row["error_code"]), attempts_before

    payment = db.get_payment(conn, case.payment_id)
    original = (payment.first_error_code if payment else None) or case.error_code
    return original, case.error_source, attempts_before


def _source_for(case: db.Case, code: str) -> str | None:
    """Error source for a reconstructed code.

    The attempts table records the code but not the party that reported it, so the
    stored source is only trustworthy while the code still matches. Otherwise the row
    gets `unknown`, which is honest and is a level the model can learn.
    """
    return case.error_source if code == case.error_code else None


def build_features(
    conn: sqlite3.Connection,
    case_id: str,
    candidate: Candidate,
    as_of: int,
    *,
    engine: PolicyEngine | None = None,
) -> dict[str, Any]:
    """Everything knowable about this case at ``as_of``, and nothing else.

    ``as_of`` has no default on purpose: a feature builder whose cutoff is optional
    will eventually be called without one.
    """
    if not isinstance(as_of, int) or isinstance(as_of, bool):
        raise TypeError(
            f"as_of must be an int unix timestamp, got {type(as_of).__name__}"
        )

    case = db.get_case(conn, case_id)
    if case is None:
        raise FeatureError(f"no case {case_id!r}")
    engine = engine or PolicyEngine().load()

    # The case row carries the code from its *latest* failure, which for a replayed
    # attempt is the future. Both the code and the attempt number have to be
    # reconstructed as of the cutoff, or the row learns from its own outcome.
    error_code, error_source, attempts_before = _state_as_of(conn, case, as_of)
    entry = engine.resolve(error_code)

    # Two clocks, and the distinction is load-bearing.
    #
    # `as_of` is the decision moment: every historical aggregate is cut here, strictly
    # before, and that is what I-9 governs.
    #
    # `candidate_ts` is when the candidate execution would actually run. The question
    # the model answers is "will an attempt at time T succeed?", so the calendar
    # features have to describe T — otherwise two candidates a day apart are identical
    # rows and the model cannot express a timing preference at all. Reading the
    # calendar at a future instant leaks nothing: it is arithmetic on a proposed
    # timestamp, not a peek at what happened there.
    #
    # At training time the two coincide (`as_of = attempt.scheduled_at`, and the
    # candidate is that same attempt), so this changes no training row.
    candidate_ts = candidate.scheduled_at if candidate.scheduled_at is not None else as_of
    civil = ist_civil(candidate_ts)
    hour, weekday = civil.hour, civil.weekday()
    method = case.method

    # -- customer history, strictly before as_of ------------------------------
    history = conn.execute(
        "SELECT COUNT(*) AS n, "
        "       SUM(CASE WHEN a.outcome = 'success' THEN 1 ELSE 0 END) AS wins "
        "FROM attempts a JOIN cases c ON c.id = a.case_id "
        "WHERE c.customer_id = ? AND a.executed_at < ?",
        (case.customer_id, as_of),
    ).fetchone()
    prior_attempts = history["n"] or 0
    prior_wins = history["wins"] or 0

    prior_failures = (
        conn.execute(
            "SELECT COUNT(*) AS n FROM payments "
            "WHERE customer_id = ? AND first_outcome = 'failed' "
            "  AND created_at < ? AND created_at >= ?",
            (case.customer_id, as_of, as_of - PRIOR_FAILURE_WINDOW_DAYS * DAY),
        ).fetchone()["n"]
        or 0
    )

    lag = conn.execute(
        "SELECT AVG(recovered_at - failed_at) AS lag FROM cases "
        "WHERE customer_id = ? AND recovered_at IS NOT NULL AND recovered_at < ?",
        (case.customer_id, as_of),
    ).fetchone()["lag"]

    # -- rail health, as published --------------------------------------------
    downtime = conn.execute(
        "SELECT severity FROM downtimes "
        "WHERE method = ? AND begin < ? AND (end IS NULL OR end > ?) "
        "  AND (scope = 'all' OR instrument IN (?, ?, ?)) "
        "ORDER BY CASE severity WHEN 'high' THEN 3 WHEN 'medium' THEN 2 ELSE 1 END DESC "
        "LIMIT 1",
        (method, as_of, as_of, case.rail, case.vpa_handle, case.payer_bank),
    ).fetchone()
    severity = downtime["severity"] if downtime else None
    alt_rail = ALTERNATIVE_RAIL.get(method)

    delay_hours = 0.0
    if candidate.scheduled_at is not None:
        delay_hours = max(0.0, (candidate.scheduled_at - as_of) / HOUR)

    return {
        # Customer
        "payer_bank": case.payer_bank or "unknown",
        "vpa_handle": case.vpa_handle or "none",
        "city_tier": case.city_tier if case.city_tier is not None else -1,
        "cust_hist_success_rate": (
            (prior_wins / prior_attempts) if prior_attempts else -1.0
        ),
        "cust_prior_failures_30d": prior_failures,
        "cust_prior_recovery_lag_h": (lag / HOUR) if lag is not None else -1.0,
        # Business
        "mcc": case.mcc or "unknown",
        "ticket_size_band": ticket_band(case.amount_paise),
        # Declared in research/06 §6.3. The simulator models no mandates, so this is
        # constant across every row and LightGBM will give it zero gain. Kept and
        # reported as zero-variance rather than quietly dropped.
        "is_recurring": 0,
        # Payment
        "error_code": error_code,
        "error_source": error_source or "unknown",
        "action_class": entry.action,
        "method": method,
        "attempt_number": attempts_before + 1,
        "hours_since_first_failure": max(0.0, (candidate_ts - case.failed_at) / HOUR),
        "log_amount": math.log1p(case.amount_paise),
        # Seasonality
        "hour_sin": math.sin(2 * math.pi * hour / 24),
        "hour_cos": math.cos(2 * math.pi * hour / 24),
        "dow_sin": math.sin(2 * math.pi * weekday / 7),
        "dow_cos": math.cos(2 * math.pi * weekday / 7),
        "day_of_month": civil.day,
        "days_to_salary_date": days_to_salary_date(candidate_ts),
        "is_peak_window": int(PEAK_WINDOW_IST[0] <= hour < PEAK_WINDOW_IST[1]),
        "is_month_end": int(civil.day >= 26),
        # Rail health
        "rail_downtime_active": int(severity is not None),
        "rail_downtime_severity": SEVERITY_RANK[severity],
        "rail_success_rate_1h": _rolling_rail_success(conn, method, as_of),
        "alt_rail_success_rate_1h": (
            _rolling_rail_success(conn, alt_rail, as_of) if alt_rail else -1.0
        ),
        # Candidate
        "candidate_action": candidate.action,
        "candidate_target_rail": candidate.target_rail or "same",
        "candidate_delay_hours": delay_hours,
    }

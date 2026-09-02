"""The generated population, and the shape of the failure stream.

The simulator has to be defensible rather than convenient. Two things are checked:
that it produces the run the build plan asks for (2000 payments over 30 days), and
that the failures it emits are a real spread across the taxonomy rather than a
distribution chosen to flatter the policy table.

Every code it can emit must be one of the 110 — a simulator that invents codes would
make coverage claims meaningless.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from src.policy.engine import ACTIONS, PolicyEngine
from src.simulator import declines
from src.simulator.generate import DEFAULT_START_TS, generate
from src.simulator.rails import (
    DAY,
    METHODS,
    OUTAGE_HANDLE,
    SCENARIOS,
    RailHealth,
    generate_downtimes,
    is_peak_window,
)
from src.store import db
from tests.conftest import SEED


@pytest.fixture(scope="module")
def run(tmp_path_factory) -> dict:
    """One 2000-payment, 30-day run, shared across this module."""
    path = tmp_path_factory.mktemp("sim") / "run.db"
    conn = db.open_db(path)
    result = generate(
        conn, n_payments=2000, days=30, seed=SEED, scenario="normal",
        start_ts=DEFAULT_START_TS,
    )
    cases = db.list_cases(conn, limit=5000)
    payments = db.fetch_all(conn, "SELECT * FROM payments")
    downtimes = db.list_downtimes(conn)
    conn.close()
    return {
        "result": result,
        "cases": cases,
        "payments": payments,
        "downtimes": downtimes,
    }


# -- the run ------------------------------------------------------------------


def test_two_thousand_payments_over_thirty_days(run) -> None:
    assert run["result"].payments == 2000
    assert len(run["payments"]) == 2000
    assert run["result"].days == 30


def test_payments_land_inside_the_window(run) -> None:
    end = DEFAULT_START_TS + 30 * DAY
    assert all(DEFAULT_START_TS <= p["created_at"] < end for p in run["payments"])


def test_a_plausible_number_of_cases(run) -> None:
    # Every failed payment gets exactly one case, and the failure rate has to be a
    # minority of the stream or the simulator is not modelling payments.
    failures = [p for p in run["payments"] if p["first_outcome"] == "failed"]
    assert len(run["cases"]) == len(failures)
    assert 0.05 < len(failures) / 2000 < 0.35


def test_cases_open_undiagnosed_and_unassigned(run) -> None:
    # I-13: the population is generated once and split by assignment in Stage 3.
    assert {case.state for case in run["cases"]} == {"RECEIVED"}
    assert {case.arm for case in run["cases"]} == {None}


def test_every_case_has_an_opening_audit_row(tmp_path: Path) -> None:
    conn = db.open_db(tmp_path / "audit.db")
    try:
        generate(conn, n_payments=200, days=30, seed=SEED, start_ts=DEFAULT_START_TS)
        for case in db.list_cases(conn, limit=500):
            audit = db.list_audit(conn, case.id)
            assert len(audit) == 1
            assert audit[0].to_state == "RECEIVED"
            assert audit[0].reason == "payment.failed"
    finally:
        conn.close()


def test_money_is_integer_paise(run) -> None:
    assert all(isinstance(p["amount_paise"], int) for p in run["payments"])
    assert all(p["amount_paise"] > 0 for p in run["payments"])


def test_every_method_appears(run) -> None:
    assert set(run["result"].by_method) == set(METHODS)


def test_customers_repeat_so_history_exists(run) -> None:
    # Stage 4's rolling features need more than one payment per customer to compute.
    counts = Counter(p["customer_id"] for p in run["payments"])
    assert max(counts.values()) > 1
    assert len(counts) < 2000


# -- the failure stream -------------------------------------------------------


def test_no_code_outside_the_hundred_and_ten(run, engine: PolicyEngine) -> None:
    known = set(engine.codes)
    emitted = {case.error_code for case in run["cases"]}
    assert emitted <= known, sorted(emitted - known)


def test_the_whole_emittable_inventory_is_in_the_policy_table(
    engine: PolicyEngine,
) -> None:
    # Stronger than the run: every code the cause model *could* produce, whether or
    # not this particular seed happened to produce it.
    unknown = declines.all_emittable_codes() - set(engine.codes)
    assert unknown == set(), sorted(unknown)


def test_failures_span_multiple_action_classes(run, engine: PolicyEngine) -> None:
    actions = {engine.resolve(case.error_code).action for case in run["cases"]}
    assert len(actions) >= 6, sorted(actions)


def test_all_eight_action_classes_are_represented(run, engine: PolicyEngine) -> None:
    actions = Counter(engine.resolve(case.error_code).action for case in run["cases"])
    missing = [action for action in ACTIONS if action not in actions]
    assert missing == [], f"no case produced {missing}"


def test_no_single_code_dominates(run) -> None:
    # A stream that is 80% one code grades the policy table on one row.
    counts = Counter(case.error_code for case in run["cases"])
    assert counts.most_common(1)[0][1] / len(run["cases"]) < 0.35


def test_a_wide_spread_of_codes(run) -> None:
    assert len({case.error_code for case in run["cases"]}) >= 20


def test_recoverable_share_is_a_minority(run, engine: PolicyEngine) -> None:
    # The project's whole claim is that most failures are not silently recoverable.
    recoverable = sum(
        1 for case in run["cases"] if engine.resolve(case.error_code).recoverable
    )
    assert 0.10 < recoverable / len(run["cases"]) < 0.50


def test_error_source_is_recorded(run) -> None:
    assert all(case.error_source for case in run["cases"])


# -- latent causes actually drive the codes -----------------------------------


def test_insufficient_funds_concentrates_late_in_the_salary_cycle(run) -> None:
    """The balance model, observed from outside.

    Balance is credited on the salary day and drawn down across the month, so a
    short balance is a late-cycle event. This is the signal Stage 4's model has to
    infer from day_of_month without ever seeing the balance itself.
    """
    from src.simulator.world import ist_civil

    days = [
        ist_civil(case.failed_at).day
        for case in run["cases"]
        if case.error_code == "insufficient_funds"
    ]
    assert len(days) >= 10
    # Salary days cluster on the 1st and 7th, so shortfalls should skew later.
    assert sum(days) / len(days) > 12


def test_card_expired_only_appears_on_card_payments(run) -> None:
    assert all(
        case.method == "card"
        for case in run["cases"]
        if case.error_code == "card_expired"
    )


def test_outage_codes_appear(run, engine: PolicyEngine) -> None:
    switch_rail = [
        case
        for case in run["cases"]
        if engine.resolve(case.error_code).action == "SWITCH_RAIL"
    ]
    # research/01 §1.4 calls rail switching the primary India-specific lever. If the
    # simulator never produces a degraded rail, the lever is untested.
    assert len(switch_rail) >= 5


# -- rails --------------------------------------------------------------------


def test_downtimes_match_the_published_schema(run) -> None:
    for event in run["downtimes"]:
        row = event.as_dict()
        assert row["entity"] == "payment.downtime"
        assert set(row) == {
            "id", "entity", "method", "scope", "instrument",
            "severity", "status", "begin", "end",
        }
        assert row["method"] in METHODS
        assert row["severity"] in ("low", "medium", "high")
        assert row["status"] in ("started", "resolved")
        assert row["end"] is None or row["end"] > row["begin"]


def test_normal_scenario_has_no_high_severity_window(run) -> None:
    assert all(event.severity != "high" for event in run["downtimes"])


def test_bank_outage_adds_one_high_severity_psp_window() -> None:
    events = generate_downtimes(
        days=30, seed=SEED, scenario="bank_outage", start_ts=DEFAULT_START_TS
    )
    high = [e for e in events if e.severity == "high"]
    assert len(high) == 1
    assert high[0].method == "upi"
    assert high[0].scope == "psp"
    assert high[0].instrument == OUTAGE_HANDLE


def test_rail_health_reports_the_worst_active_severity() -> None:
    events = generate_downtimes(
        days=30, seed=SEED, scenario="bank_outage", start_ts=DEFAULT_START_TS
    )
    health = RailHealth.from_events(events)
    window = next(e for e in events if e.severity == "high")
    inside = (window.begin + window.end) // 2
    assert health.severity_at("upi", inside, instruments=(OUTAGE_HANDLE,)) == "high"
    assert health.severity_at("upi", window.end + DAY, instruments=(OUTAGE_HANDLE,)) != "high"


def test_peak_window_is_evening_ist() -> None:
    # 19:00-22:00 IST. 13:30 UTC is 19:00 IST.
    day_start = DEFAULT_START_TS
    assert is_peak_window(day_start + 13 * 3600 + 30 * 60 + 60)
    assert not is_peak_window(day_start + 6 * 3600)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_both_scenarios_generate(tmp_path: Path, scenario: str) -> None:
    conn = db.open_db(tmp_path / f"{scenario}.db")
    try:
        result = generate(
            conn, n_payments=300, days=30, seed=SEED, scenario=scenario,
            start_ts=DEFAULT_START_TS,
        )
        assert result.payments == 300
        assert result.cases > 0
    finally:
        conn.close()


def test_an_unknown_scenario_is_refused(tmp_path: Path) -> None:
    conn = db.open_db(tmp_path / "bad.db")
    try:
        with pytest.raises(ValueError):
            generate(conn, n_payments=10, days=1, seed=SEED, scenario="festival_peak")
    finally:
        conn.close()

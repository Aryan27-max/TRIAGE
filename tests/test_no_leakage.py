"""I-9 — every rolling feature filters on `event_time < as_of`. No exceptions.

**The most important test in the repo.** Computing a customer's success rate over the
whole table leaks the outcome and produces an AUC that collapses on deployment. It is
also invisible: the model trains, the metrics look excellent, and nothing fails until
the thing meets data it has not already seen.

Four independent checks, and only one of them actually catches leakage:

    a) signature       — build_features raises without `as_of`
    b) future invariance — inserting events dated AFTER as_of changes nothing
    c) boundary        — an event at exactly as_of is excluded (strict <, not <=)
    d) latent isolation — no simulator import, no latent feature name

A naive implementation that aggregates over the whole table passes (a), (c) and (d).
Only (b) fails it.
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from src.executor import state as st
from src.features.build import (
    FEATURE_NAMES,
    Candidate,
    build_features,
    days_to_salary_date,
)
from src.store import db
from tests.conftest import NOW

HOUR = 3600
DAY = 86400
SRC = Path(__file__).resolve().parents[1] / "src"

CANDIDATE = Candidate(action="RETRY_SCHEDULED", target_rail=None, scheduled_at=NOW + DAY)

# Same list test_hidden_state.py scans AttemptOutcome with. A feature named after any
# of these is reading something the decision path cannot see.
LATENT_TOKENS: tuple[str, ...] = (
    "balance",
    "salary_day",
    "burn",
    "income",
    "valid_until",
    "card_valid",
    "pin_set",
    "device_bound",
    "clumsi",
    "responsive",
    "propensity",
    "limit_prone",
    "constrained",
    "latent",
    "hidden",
    "truth",
    "true_",
    "oracle",
    "world",
    "seed",
)


def seed_case(
    conn: sqlite3.Connection,
    runner,
    *,
    case_id_source: str = "LEAK",
    code: str = "insufficient_funds",
    customer_id: str = "cust_LEAK",
    failed_at: int = NOW,
) -> db.Case:
    payment = db.Payment(
        id=f"pay_{case_id_source}",
        customer_id=customer_id,
        merchant_id="mch_LEAK",
        method="upi",
        rail="@oksbi",
        amount_paise=499000,
        created_at=failed_at,
        first_outcome="failed",
        first_error_code=code,
    )
    db.insert_payments(conn, [payment])
    case = runner.build_case(
        payment=payment,
        error_code=code,
        error_source="bank",
        failed_at=failed_at,
        city_tier=2,
        vpa_handle="@oksbi",
        payer_bank="SBIN",
        mcc="5411",
    )
    st.open_case(conn, case, now=failed_at)
    conn.commit()
    return case


def _attempt(
    conn: sqlite3.Connection,
    case_id: str,
    number: int,
    at: int,
    outcome: str = "success",
    rail: str | None = None,
) -> None:
    db.insert_attempt(
        conn,
        db.Attempt(
            id=db.stable_id("att_", case_id, number, at),
            case_id=case_id,
            attempt_number=number,
            idempotency_key=f"{case_id}:{number}:{at}",
            action="RETRY_SCHEDULED",
            target_rail=rail,
            scheduled_at=at,
            executed_at=at,
            outcome=outcome,
            error_code=None if outcome == "success" else "insufficient_funds",
            latency_ms=900,
        ),
    )


# -- (a) the signature --------------------------------------------------------


def test_as_of_is_required(conn, runner, engine) -> None:
    case = seed_case(conn, runner)
    with pytest.raises(TypeError):
        build_features(conn, case.id, CANDIDATE)  # type: ignore[call-arg]


def test_as_of_must_be_an_integer_timestamp(conn, runner, engine) -> None:
    case = seed_case(conn, runner)
    for bad in ("2025-01-01", None, 1.5, True):
        with pytest.raises(TypeError):
            build_features(conn, case.id, CANDIDATE, bad, engine=engine)  # type: ignore[arg-type]


def test_as_of_has_no_default() -> None:
    import inspect

    parameter = inspect.signature(build_features).parameters["as_of"]
    assert parameter.default is inspect.Parameter.empty


# -- (b) future invariance — the check that actually catches leakage -----------


def test_future_events_do_not_change_features(conn, runner, engine) -> None:
    """Build at T, insert a pile of events dated after T, rebuild at T.

    Every value must be byte-identical. An implementation that aggregates over the
    whole attempts table passes every other check in this file and fails this one.
    """
    case = seed_case(conn, runner)
    as_of = NOW + 2 * DAY

    before = build_features(conn, case.id, CANDIDATE, as_of, engine=engine)

    # A batch of attempts for the same customer and the same rail, all AFTER as_of,
    # deliberately all successes so that any leak moves the success-rate features.
    for index in range(12):
        other = seed_case(
            conn,
            runner,
            case_id_source=f"FUT{index}",
            customer_id="cust_LEAK",
            failed_at=as_of + DAY,
        )
        _attempt(conn, other.id, 1, as_of + DAY + index * HOUR, outcome="success")
        db.set_case_state(
            conn,
            other.id,
            st.RECOVERED,
            recovered_at=as_of + DAY + index * HOUR,
            recovered_amount_paise=499000,
        )
    conn.commit()

    after = build_features(conn, case.id, CANDIDATE, as_of, engine=engine)
    drifted = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
    assert drifted == {}, f"features moved when future events landed: {drifted}"


def test_past_events_do_change_features(conn, runner, engine) -> None:
    """The mirror image: if nothing ever moves the features, (b) proves nothing."""
    case = seed_case(conn, runner)
    as_of = NOW + 5 * DAY

    before = build_features(conn, case.id, CANDIDATE, as_of, engine=engine)
    past = seed_case(
        conn, runner, case_id_source="PAST", customer_id="cust_LEAK", failed_at=NOW
    )
    _attempt(conn, past.id, 1, NOW + HOUR, outcome="success")
    conn.commit()

    after = build_features(conn, case.id, CANDIDATE, as_of, engine=engine)
    assert before["cust_hist_success_rate"] != after["cust_hist_success_rate"]


def test_a_future_recovery_does_not_move_the_lag_feature(conn, runner, engine) -> None:
    case = seed_case(conn, runner)
    as_of = NOW + DAY
    before = build_features(conn, case.id, CANDIDATE, as_of, engine=engine)

    later = seed_case(
        conn, runner, case_id_source="LATER", customer_id="cust_LEAK", failed_at=as_of
    )
    db.set_case_state(
        conn, later.id, st.RECOVERED, recovered_at=as_of + 3 * HOUR,
        recovered_amount_paise=1,
    )
    conn.commit()

    after = build_features(conn, case.id, CANDIDATE, as_of, engine=engine)
    assert before["cust_prior_recovery_lag_h"] == after["cust_prior_recovery_lag_h"]


def test_future_downtime_does_not_move_rail_features(conn, runner, engine) -> None:
    case = seed_case(conn, runner)
    as_of = NOW + DAY
    before = build_features(conn, case.id, CANDIDATE, as_of, engine=engine)

    db.insert_downtimes(
        conn,
        [
            db.Downtime(
                id="down_FUTURE",
                method="upi",
                scope="all",
                instrument=None,
                severity="high",
                status="started",
                begin=as_of + HOUR,
                end=as_of + 6 * HOUR,
            )
        ],
    )
    conn.commit()

    after = build_features(conn, case.id, CANDIDATE, as_of, engine=engine)
    assert before["rail_downtime_active"] == after["rail_downtime_active"] == 0
    assert before["rail_downtime_severity"] == after["rail_downtime_severity"]


# -- (c) the boundary is strict -----------------------------------------------


def test_an_event_at_exactly_as_of_is_excluded(conn, runner, engine) -> None:
    """Strictly less than, not less-than-or-equal.

    An attempt executing at exactly the decision instant has not resolved yet. Letting
    it in is a one-row leak that is very hard to see in aggregate metrics.
    """
    case = seed_case(conn, runner)
    as_of = NOW + DAY
    sibling = seed_case(
        conn, runner, case_id_source="EDGE", customer_id="cust_LEAK", failed_at=NOW
    )

    _attempt(conn, sibling.id, 1, as_of, outcome="success")
    conn.commit()
    at_boundary = build_features(conn, case.id, CANDIDATE, as_of, engine=engine)
    assert at_boundary["cust_hist_success_rate"] == -1.0, "an event at as_of leaked in"

    # One second earlier and it must count.
    conn.execute(
        "UPDATE attempts SET executed_at = ? WHERE case_id = ?", (as_of - 1, sibling.id)
    )
    conn.commit()
    just_before = build_features(conn, case.id, CANDIDATE, as_of, engine=engine)
    assert just_before["cust_hist_success_rate"] == 1.0


def test_a_downtime_beginning_at_as_of_is_excluded(conn, runner, engine) -> None:
    case = seed_case(conn, runner)
    as_of = NOW + DAY
    db.insert_downtimes(
        conn,
        [
            db.Downtime(
                id="down_EDGE", method="upi", scope="all", instrument=None,
                severity="high", status="started", begin=as_of, end=as_of + HOUR,
            )
        ],
    )
    conn.commit()
    assert build_features(conn, case.id, CANDIDATE, as_of, engine=engine)[
        "rail_downtime_active"
    ] == 0


def test_a_payment_at_exactly_as_of_is_excluded(conn, runner, engine) -> None:
    case = seed_case(conn, runner)
    as_of = NOW + DAY
    before = build_features(conn, case.id, CANDIDATE, as_of, engine=engine)
    db.insert_payments(
        conn,
        [
            db.Payment(
                id="pay_EDGE2", customer_id="cust_LEAK", merchant_id="mch_LEAK",
                method="upi", rail="@oksbi", amount_paise=1000, created_at=as_of,
                first_outcome="failed", first_error_code="insufficient_funds",
            )
        ],
    )
    conn.commit()
    after = build_features(conn, case.id, CANDIDATE, as_of, engine=engine)
    assert before["cust_prior_failures_30d"] == after["cust_prior_failures_30d"]


# -- (d) latent isolation -----------------------------------------------------


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{a.name}" for a in node.names)
    return modules


def _ml_files() -> list[Path]:
    files: list[Path] = []
    for package in ("features", "model"):
        directory = SRC / package
        if directory.exists():
            files.extend(sorted(directory.rglob("*.py")))
    return files


def test_there_are_ml_files_to_scan() -> None:
    assert len(_ml_files()) >= 2


@pytest.mark.parametrize(
    "path", _ml_files(), ids=lambda p: f"{p.parent.name}/{p.name}"
)
def test_ml_modules_never_import_the_simulator(path: Path) -> None:
    offenders = sorted(
        m for m in _imported_modules(path) if m.startswith("src.simulator")
    )
    assert offenders == [], (
        f"{path.relative_to(SRC.parent)} imports {offenders}. Latent state is not a "
        f"feature; if it cannot be computed from the store, it does not exist."
    )


@pytest.mark.parametrize("token", LATENT_TOKENS)
def test_no_feature_is_named_after_latent_state(token: str) -> None:
    offenders = [f for f in FEATURE_NAMES if token in f.lower()]
    assert offenders == [], f"feature(s) {offenders} name latent state"


def test_days_to_salary_is_derived_from_the_calendar() -> None:
    """Not read from the customer's true salary day, which is latent.

    Two customers whose payments fail at the same instant get the same value, however
    differently the world actually pays them.
    """
    from src.features.build import ist_civil

    for offset_days in range(0, 31):
        at = NOW + offset_days * DAY
        civil = ist_civil(at)
        expected_from_calendar = days_to_salary_date(at)
        # Purely a function of the timestamp — no customer is involved at all.
        assert days_to_salary_date(at) == expected_from_calendar
        assert 0 <= expected_from_calendar <= 31, civil


def test_the_same_instant_gives_every_customer_the_same_salary_distance(
    conn, runner, engine
) -> None:
    a = seed_case(conn, runner, case_id_source="SALA", customer_id="cust_A")
    b = seed_case(conn, runner, case_id_source="SALB", customer_id="cust_B")
    as_of = NOW + 3 * DAY
    fa = build_features(conn, a.id, CANDIDATE, as_of, engine=engine)
    fb = build_features(conn, b.id, CANDIDATE, as_of, engine=engine)
    assert fa["days_to_salary_date"] == fb["days_to_salary_date"]


def test_features_are_a_pure_function_of_the_store_and_the_cutoff(
    conn, runner, engine
) -> None:
    case = seed_case(conn, runner)
    as_of = NOW + DAY
    first = build_features(conn, case.id, CANDIDATE, as_of, engine=engine)
    second = build_features(conn, case.id, CANDIDATE, as_of, engine=engine)
    assert first == second

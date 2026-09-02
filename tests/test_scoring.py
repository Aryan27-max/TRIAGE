"""The measurement rules, against hand-built fixtures with known answers.

Scoring a real run tells you the code ran. It does not tell you the rules are right,
because you have nothing to compare the answer to. Everything here is a store built by
hand where the correct number is known in advance:

    I-14  a payment with four attempts counts once
    I-15  an attempt landing in the trailing window counts
    I-16  a code where the focus arm loses appears in the output
    I-17  attempt cost is reported
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from eval.score import (
    ATTEMPT_COST_PAISE,
    NUDGE_COST_PAISE,
    score,
    to_api_shape,
    two_proportion_z,
    wilson_interval,
)
from src.executor import state as st
from src.policy.engine import PolicyEngine
from src.store import db

START = 1735689600
DAY = 86400
ARMS = "control,baseline"


def _store(tmp_path: Path, *, days: int = 30, trailing_days: int = 7) -> sqlite3.Connection:
    conn = db.open_db(tmp_path / "score.db")
    db.insert_run(
        conn,
        db.Run(
            run_id="run_TEST",
            seed=1,
            n_payments=0,
            days=days,
            scenario="normal",
            trailing_days=trailing_days,
            tick_seconds=3600,
            arms=ARMS,
            start_ts=START,
            created_at=START,
            git_sha=None,
        ),
    )
    return conn


def _case(
    conn: sqlite3.Connection,
    case_id: str,
    *,
    arm: str,
    code: str,
    state: str,
    recovered_at: int | None = None,
    failed_at: int = START,
    method: str = "upi",
    amount: int = 100_000,
    nudge_sent_at: int | None = None,
) -> None:
    payment_id = f"pay_{case_id}"
    db.insert_payments(
        conn,
        [
            db.Payment(
                id=payment_id,
                customer_id="cust_1",
                merchant_id="mch_1",
                method=method,
                rail="@oksbi",
                amount_paise=amount,
                created_at=failed_at,
                first_outcome="failed",
                first_error_code=code,
            )
        ],
    )
    conn.execute(
        f"INSERT INTO cases ({db._CASE_COLUMNS}) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            case_id, payment_id, "cust_1", "mch_1", method, "@oksbi", amount,
            code, "bank", failed_at, 2, "@oksbi", "SBIN", state, arm, 4,
            failed_at + 7 * DAY, None, None, nudge_sent_at, recovered_at,
            amount if recovered_at else None, failed_at,
        ),
    )


def _attempts(conn: sqlite3.Connection, case_id: str, n: int, at: int = START) -> None:
    for i in range(1, n + 1):
        db.insert_attempt(
            conn,
            db.Attempt(
                id=f"att_{case_id}_{i}",
                case_id=case_id,
                attempt_number=i,
                idempotency_key=f"{case_id}:{i}",
                action="RETRY_SCHEDULED",
                target_rail=None,
                scheduled_at=at,
                executed_at=at + i,
                outcome="failed",
                error_code="insufficient_funds",
                latency_ms=900,
            ),
        )


def _score(conn: sqlite3.Connection, engine: PolicyEngine):
    run = db.get_run(conn, "run_TEST")
    assert run is not None
    return score(conn, run, engine)


# -- I-14: dedup to the payment -----------------------------------------------


def test_a_payment_with_four_attempts_counts_once(tmp_path: Path, engine) -> None:
    conn = _store(tmp_path)
    _case(conn, "case_A", arm="control", code="insufficient_funds", state=st.RECOVERED,
          recovered_at=START + 3 * DAY)
    _attempts(conn, "case_A", 4)
    conn.commit()

    card = _score(conn, engine)
    control = card.scores["control"]
    assert control.payments == 1          # not 4
    assert control.recovered == 1
    assert control.rate == 1.0            # not 1/4
    assert control.attempts == 4          # attempts are reported, never the denominator
    conn.close()


def test_attempts_are_never_the_denominator(tmp_path: Path, engine) -> None:
    conn = _store(tmp_path)
    # Three payments, wildly different attempt counts, all recovered.
    for index, attempts in enumerate((1, 4, 4)):
        case_id = f"case_D{index}"
        _case(conn, case_id, arm="control", code="insufficient_funds",
              state=st.RECOVERED, recovered_at=START + DAY)
        _attempts(conn, case_id, attempts)
    conn.commit()

    control = _score(conn, engine).scores["control"]
    assert control.payments == 3
    assert control.rate == 1.0
    assert control.attempts == 9
    conn.close()


def test_an_unrecovered_payment_counts_in_the_denominator(tmp_path: Path, engine) -> None:
    conn = _store(tmp_path)
    _case(conn, "case_R", arm="control", code="insufficient_funds", state=st.RECOVERED,
          recovered_at=START + DAY)
    _case(conn, "case_X", arm="control", code="card_expired", state=st.EXHAUSTED)
    conn.commit()
    control = _score(conn, engine).scores["control"]
    assert (control.payments, control.recovered) == (2, 1)
    assert control.rate == 0.5
    conn.close()


# -- I-15: the trailing window ------------------------------------------------


def test_a_recovery_in_the_trailing_window_counts(tmp_path: Path, engine) -> None:
    conn = _store(tmp_path, days=30, trailing_days=7)
    # Failed on day 28, recovered on day 33 — inside the trailing window, outside
    # the main one. Cutting at day 30 would discard it.
    _case(conn, "case_LATE", arm="baseline", code="insufficient_funds",
          state=st.RECOVERED, failed_at=START + 28 * DAY,
          recovered_at=START + 33 * DAY)
    conn.commit()

    baseline = _score(conn, engine).scores["baseline"]
    assert baseline.recovered == 1
    assert baseline.recovered_without_trailing_window == 0  # the undercount, shown
    conn.close()


def test_a_recovery_past_the_trailing_window_does_not_count(
    tmp_path: Path, engine
) -> None:
    conn = _store(tmp_path, days=30, trailing_days=7)
    _case(conn, "case_TOOLATE", arm="baseline", code="insufficient_funds",
          state=st.RECOVERED, failed_at=START + 28 * DAY,
          recovered_at=START + 40 * DAY)
    conn.commit()
    baseline = _score(conn, engine).scores["baseline"]
    assert baseline.payments == 1
    assert baseline.recovered == 0
    conn.close()


def test_the_cutoff_is_the_end_of_the_trailing_window(tmp_path: Path, engine) -> None:
    conn = _store(tmp_path, days=30, trailing_days=7)
    _case(conn, "case_C", arm="control", code="insufficient_funds", state=st.EXHAUSTED)
    conn.commit()
    card = _score(conn, engine)
    assert card.cutoff_ts == START + 37 * DAY
    conn.close()


# -- I-16: losing segments are published --------------------------------------


def test_a_code_where_the_focus_arm_loses_appears(tmp_path: Path, engine) -> None:
    conn = _store(tmp_path)
    # control recovers both, baseline recovers neither: a clear loss for baseline.
    for i in range(2):
        _case(conn, f"case_CL{i}", arm="control", code="payment_cancelled",
              state=st.RECOVERED, recovered_at=START + DAY)
        _case(conn, f"case_BL{i}", arm="baseline", code="payment_cancelled",
              state=st.EXHAUSTED)
    conn.commit()

    card = _score(conn, engine)
    row = next(r for r in card.by_error_code if r.key == "payment_cancelled")
    assert row.pp == -100.0
    assert row in card.losses
    assert card.losses, "a losing segment must survive into the report"
    conn.close()


def test_losing_segments_are_also_in_the_full_table(tmp_path: Path, engine) -> None:
    # The losses list is the same rows pulled out, not a separate filtered view. A
    # reader of the full table must see them too.
    conn = _store(tmp_path)
    _case(conn, "case_C1", arm="control", code="payment_cancelled",
          state=st.RECOVERED, recovered_at=START + DAY)
    _case(conn, "case_B1", arm="baseline", code="payment_cancelled", state=st.EXHAUSTED)
    conn.commit()
    card = _score(conn, engine)
    assert all(row in card.by_error_code for row in card.losses)
    conn.close()


def test_losing_segments_reach_the_api_shape(tmp_path: Path, engine) -> None:
    conn = _store(tmp_path)
    _case(conn, "case_C2", arm="control", code="payment_cancelled",
          state=st.RECOVERED, recovered_at=START + DAY)
    _case(conn, "case_B2", arm="baseline", code="payment_cancelled", state=st.EXHAUSTED)
    conn.commit()
    payload = to_api_shape(_score(conn, engine))
    assert payload["losing_segments"], payload
    assert payload["losing_segments"][0]["code"] == "payment_cancelled"
    assert any(row["pp"] is not None and row["pp"] < 0 for row in payload["by_error_code"])
    conn.close()


def test_negative_rows_are_not_sorted_away(tmp_path: Path, engine) -> None:
    # by_error_code sorts by sample size, never by outcome — a big losing segment
    # must sit at the top of the table where it cannot be missed.
    conn = _store(tmp_path)
    for i in range(6):
        _case(conn, f"case_L{i}", arm="baseline", code="payment_cancelled",
              state=st.EXHAUSTED)
        _case(conn, f"case_M{i}", arm="control", code="payment_cancelled",
              state=st.RECOVERED, recovered_at=START + DAY)
    _case(conn, "case_S", arm="control", code="card_expired", state=st.EXHAUSTED)
    conn.commit()
    card = _score(conn, engine)
    assert card.by_error_code[0].key == "payment_cancelled"
    assert card.by_error_code[0].pp is not None and card.by_error_code[0].pp < 0
    conn.close()


# -- I-17: cost is reported ---------------------------------------------------


def test_attempt_cost_is_reported(tmp_path: Path, engine) -> None:
    conn = _store(tmp_path)
    _case(conn, "case_K", arm="control", code="insufficient_funds", state=st.EXHAUSTED)
    _attempts(conn, "case_K", 3)
    conn.commit()
    control = _score(conn, engine).scores["control"]
    assert control.attempts == 3
    assert control.attempt_cost_paise == 3 * ATTEMPT_COST_PAISE
    assert control.total_cost_paise == 3 * ATTEMPT_COST_PAISE
    conn.close()


def test_nudge_cost_is_reported_separately(tmp_path: Path, engine) -> None:
    conn = _store(tmp_path)
    _case(conn, "case_N", arm="baseline", code="incorrect_pin", state=st.RECOVERED,
          recovered_at=START + DAY, nudge_sent_at=START + 60)
    conn.commit()
    baseline = _score(conn, engine).scores["baseline"]
    assert baseline.nudges == 1
    assert baseline.attempts == 0  # a nudge is not an attempt
    assert baseline.nudge_cost_paise == NUDGE_COST_PAISE
    assert baseline.total_cost_paise == NUDGE_COST_PAISE
    conn.close()


def test_attempts_per_payment_and_cost_per_recovery(tmp_path: Path, engine) -> None:
    conn = _store(tmp_path)
    _case(conn, "case_P", arm="control", code="insufficient_funds",
          state=st.RECOVERED, recovered_at=START + DAY)
    _attempts(conn, "case_P", 4)
    conn.commit()
    control = _score(conn, engine).scores["control"]
    assert control.attempts_per_payment == 4.0
    assert control.cost_per_recovery_paise == 4 * ATTEMPT_COST_PAISE
    conn.close()


def test_cost_reaches_the_api_shape(tmp_path: Path, engine) -> None:
    conn = _store(tmp_path)
    _case(conn, "case_Q", arm="control", code="insufficient_funds", state=st.EXHAUSTED)
    _attempts(conn, "case_Q", 2)
    conn.commit()
    payload = to_api_shape(_score(conn, engine))
    assert payload["arms"]["control"]["attempts"] == 2
    assert payload["arms"]["control"]["attempt_cost"] == 2 * ATTEMPT_COST_PAISE
    conn.close()


# -- statistics ---------------------------------------------------------------


def test_wilson_interval_brackets_the_point_estimate() -> None:
    low, high = wilson_interval(30, 100)
    assert low < 0.30 < high
    assert 0.0 <= low and high <= 1.0


def test_wilson_interval_stays_inside_the_scale_at_the_ends() -> None:
    assert wilson_interval(0, 20)[0] == 0.0
    assert wilson_interval(20, 20)[1] == 1.0


def test_wilson_interval_is_wider_at_small_n() -> None:
    small = wilson_interval(3, 10)
    large = wilson_interval(300, 1000)
    assert (small[1] - small[0]) > (large[1] - large[0])


def test_wilson_interval_of_nothing_is_empty() -> None:
    assert wilson_interval(0, 0) == (0.0, 0.0)


def test_z_test_finds_no_difference_between_identical_proportions() -> None:
    z, p = two_proportion_z(50, 100, 50, 100)
    assert z == 0.0
    assert p == pytest.approx(1.0)


def test_z_test_finds_a_large_difference() -> None:
    z, p = two_proportion_z(80, 100, 20, 100)
    assert z > 5
    assert p < 0.001


def test_z_test_on_a_small_sample_is_not_significant() -> None:
    # 3/10 vs 2/10 is nothing at all, and the test must say so rather than flatter it.
    _, p = two_proportion_z(3, 10, 2, 10)
    assert p > 0.05


# -- api shape ----------------------------------------------------------------


def test_api_shape_matches_the_reference(tmp_path: Path, engine) -> None:
    conn = _store(tmp_path)
    _case(conn, "case_Z", arm="control", code="insufficient_funds",
          state=st.RECOVERED, recovered_at=START + DAY)
    _case(conn, "case_Y", arm="baseline", code="insufficient_funds", state=st.EXHAUSTED)
    conn.commit()
    payload = to_api_shape(_score(conn, engine))
    # research/05 §5.4
    assert set(payload) >= {"run_id", "measurement", "arms", "uplift", "by_error_code"}
    assert payload["measurement"]["dedup"] == "by_payment_final_outcome"
    assert payload["measurement"]["window_days"] == 30
    assert payload["measurement"]["trailing_window_days"] == 7
    assert "baseline_vs_control" in payload["uplift"]
    conn.close()

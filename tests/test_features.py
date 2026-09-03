"""The feature contract: every declared feature is produced, and nothing is NaN.

Separate from test_no_leakage.py, which governs *when* values may be read. This file
governs *what* comes out: the full set, the right types, sane values for a complete
case, and graceful behaviour on a categorical level the model has never seen.
"""

from __future__ import annotations

import math

import pytest

from src.features.build import (
    CATEGORICAL_FEATURES,
    FEATURE_NAMES,
    Candidate,
    FeatureError,
    build_features,
    days_to_salary_date,
    ist_civil,
    ticket_band,
)
from tests.conftest import NOW
from tests.test_no_leakage import seed_case

DAY = 86400
HOUR = 3600
CANDIDATE = Candidate(action="RETRY_SCHEDULED", target_rail=None, scheduled_at=NOW + DAY)


@pytest.fixture()
def features(conn, runner, engine):
    case = seed_case(conn, runner)
    return build_features(conn, case.id, CANDIDATE, NOW + DAY, engine=engine)


# -- completeness -------------------------------------------------------------


def test_every_declared_feature_is_produced(features) -> None:
    assert set(features) == set(FEATURE_NAMES)


def test_the_feature_count_is_stable(features) -> None:
    # If this changes, feature_names.json and every trained model are stale.
    assert len(FEATURE_NAMES) == 31
    assert len(features) == 31


def test_no_feature_is_none_or_nan(features) -> None:
    missing = [k for k, v in features.items() if v is None]
    assert missing == []
    nan = [
        k
        for k, v in features.items()
        if isinstance(v, float) and math.isnan(v)
    ]
    assert nan == []


def test_categoricals_are_strings(features) -> None:
    for name in CATEGORICAL_FEATURES:
        assert isinstance(features[name], str), name


def test_numerics_are_numbers(features) -> None:
    for name in set(FEATURE_NAMES) - set(CATEGORICAL_FEATURES):
        assert isinstance(features[name], (int, float)), name


def test_every_categorical_is_a_declared_feature() -> None:
    assert set(CATEGORICAL_FEATURES) <= set(FEATURE_NAMES)


# -- values make sense --------------------------------------------------------


def test_cyclical_encodings_are_on_the_unit_circle(features) -> None:
    assert math.isclose(
        features["hour_sin"] ** 2 + features["hour_cos"] ** 2, 1.0, abs_tol=1e-9
    )
    assert math.isclose(
        features["dow_sin"] ** 2 + features["dow_cos"] ** 2, 1.0, abs_tol=1e-9
    )


def test_absent_history_is_encoded_as_minus_one_not_zero(features) -> None:
    """A customer with no history is not a customer with a 0% success rate.

    Collapsing both to zero teaches the model that new customers always fail.
    """
    assert features["cust_hist_success_rate"] == -1.0
    assert features["cust_prior_recovery_lag_h"] == -1.0


def test_the_action_class_comes_from_the_policy_table(features, engine) -> None:
    assert features["action_class"] == engine.resolve("insufficient_funds").action
    assert features["action_class"] == "RETRY_SCHEDULED"


def test_candidate_fields_describe_the_candidate(features) -> None:
    assert features["candidate_action"] == "RETRY_SCHEDULED"
    assert features["candidate_target_rail"] == "same"
    assert features["candidate_delay_hours"] == 0.0  # candidate == as_of here


def test_candidate_delay_measures_from_the_decision(conn, runner, engine) -> None:
    case = seed_case(conn, runner)
    later = Candidate("RETRY_SCHEDULED", None, NOW + 3 * DAY)
    built = build_features(conn, case.id, later, NOW, engine=engine)
    assert built["candidate_delay_hours"] == pytest.approx(72.0)


def test_seasonality_describes_the_candidate_not_the_decision(
    conn, runner, engine
) -> None:
    """Two candidates a day apart must not look identical.

    The question is "will an attempt at time T succeed?", so the calendar features
    have to describe T. Without this the model cannot express a timing preference at
    all — every candidate is the same row but for one number.
    """
    case = seed_case(conn, runner)
    now = NOW
    soon = build_features(
        conn, case.id, Candidate("RETRY_SCHEDULED", None, now + HOUR), now, engine=engine
    )
    later = build_features(
        conn, case.id, Candidate("RETRY_SCHEDULED", None, now + 5 * DAY), now,
        engine=engine,
    )
    assert soon["day_of_month"] != later["day_of_month"]
    assert soon["hours_since_first_failure"] != later["hours_since_first_failure"]


def test_ticket_bands_are_ordered() -> None:
    assert ticket_band(9_900) == 0
    assert ticket_band(500_000) == 1
    assert ticket_band(2_000_000) == 2


def test_days_to_salary_is_never_negative() -> None:
    for offset in range(0, 62):
        value = days_to_salary_date(NOW + offset * DAY)
        assert 0 <= value <= 31, offset


def test_days_to_salary_is_zero_on_a_salary_day() -> None:
    # Walk a couple of months and check the 1st and the 7th both read zero.
    hits = 0
    for offset in range(0, 62):
        at = NOW + offset * DAY
        if ist_civil(at).day in (1, 7):
            assert days_to_salary_date(at) == 0
            hits += 1
    assert hits >= 3


def test_is_peak_window_matches_evening_ist(conn, runner, engine) -> None:
    case = seed_case(conn, runner)
    # 13:30 UTC is 19:00 IST.
    day_start = (NOW // DAY) * DAY
    peak = day_start + 13 * HOUR + 45 * 60
    off_peak = day_start + 6 * HOUR
    assert build_features(
        conn, case.id, Candidate("RETRY_SCHEDULED", None, peak), NOW, engine=engine
    )["is_peak_window"] == 1
    assert build_features(
        conn, case.id, Candidate("RETRY_SCHEDULED", None, off_peak), NOW, engine=engine
    )["is_peak_window"] == 0


# -- robustness ---------------------------------------------------------------


def test_an_unseen_categorical_level_does_not_crash(conn, runner, engine) -> None:
    """A bank or handle the training data never contained must still produce a row.

    LightGBM handles unseen categorical levels; the feature builder must not be the
    thing that falls over first.
    """
    from src.store import db

    case = seed_case(conn, runner, case_id_source="UNSEEN")
    conn.execute(
        "UPDATE cases SET payer_bank = ?, vpa_handle = ?, mcc = ? WHERE id = ?",
        ("NEWBANK", "@brandnew", "9999", case.id),
    )
    conn.commit()
    built = build_features(conn, case.id, CANDIDATE, NOW + DAY, engine=engine)
    assert built["payer_bank"] == "NEWBANK"
    assert built["vpa_handle"] == "@brandnew"
    assert set(built) == set(FEATURE_NAMES)


def test_missing_observable_columns_fall_back_to_a_level(conn, runner, engine) -> None:
    case = seed_case(conn, runner, case_id_source="NULLS")
    conn.execute(
        "UPDATE cases SET payer_bank = NULL, vpa_handle = NULL, mcc = NULL, "
        "city_tier = NULL WHERE id = ?",
        (case.id,),
    )
    conn.commit()
    built = build_features(conn, case.id, CANDIDATE, NOW + DAY, engine=engine)
    assert built["payer_bank"] == "unknown"
    assert built["vpa_handle"] == "none"
    assert built["mcc"] == "unknown"
    assert built["city_tier"] == -1


def test_an_unknown_case_raises(conn, engine) -> None:
    with pytest.raises(FeatureError):
        build_features(conn, "case_NOPE", CANDIDATE, NOW, engine=engine)


def test_a_switch_rail_candidate_records_its_target(conn, runner, engine) -> None:
    case = seed_case(conn, runner, case_id_source="SR", code="bank_technical_error")
    built = build_features(
        conn, case.id, Candidate("SWITCH_RAIL", "card", NOW + 60), NOW, engine=engine
    )
    assert built["candidate_action"] == "SWITCH_RAIL"
    assert built["candidate_target_rail"] == "card"
    assert built["action_class"] == "SWITCH_RAIL"

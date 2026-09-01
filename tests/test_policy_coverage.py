"""The Stage 1 gate.

Everything downstream reads this table. If these fail, nothing built on top of it is
trustworthy, so this file asserts the taxonomy exactly rather than approximately: the
counts are hard-coded, not derived from the file under test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.policy.engine import (
    ACTIONS,
    FAMILIES,
    MODEL_ELIGIBLE_ACTIONS,
    RETRYING_ACTIONS,
    PolicyEngine,
    PolicyEntry,
    PolicyLoadError,
    UnknownErrorCodeError,
)

TOTAL_CODES = 110
RECOVERABLE_CODES = 27

# Hard-coded on purpose. Deriving these from the file would make the test agree with
# whatever the file happens to say, which is not a test.
EXPECTED_ACTION_COUNTS: dict[str, int] = {
    "RETRY_NOW": 3,
    "RETRY_SCHEDULED": 9,
    "SWITCH_RAIL": 15,
    "SWITCH_INSTRUMENT": 28,
    "NUDGE_CUSTOMER": 23,
    "AWAIT_STATUS": 5,
    "STOP": 4,
    "MERCHANT_ALERT": 23,
}


# -- shape of the table -------------------------------------------------------


def test_exactly_110_codes(engine: PolicyEngine) -> None:
    assert len(engine) == TOTAL_CODES


def test_codes_are_unique_in_the_file(raw_policy: dict[str, Any]) -> None:
    # Checked against the raw JSON: the engine keys by code, so a duplicate would be
    # invisible after loading if _validate ever stopped catching it.
    codes = [row["code"] for row in raw_policy["codes"]]
    assert len(codes) == TOTAL_CODES
    assert len(set(codes)) == len(codes)


def test_every_code_resolves(engine: PolicyEngine) -> None:
    for code in engine.codes:
        assert engine.resolve(code).code == code


def test_nothing_is_still_unmapped(raw_policy: dict[str, Any]) -> None:
    assert "UNMAPPED" not in json.dumps(raw_policy)


# -- the eight action classes -------------------------------------------------


def test_every_action_is_one_of_the_eight(entries: list[PolicyEntry]) -> None:
    assert {e.action for e in entries} <= set(ACTIONS)


def test_all_eight_actions_are_represented(entries: list[PolicyEntry]) -> None:
    assert {e.action for e in entries} == set(ACTIONS)


@pytest.mark.parametrize("action,expected", sorted(EXPECTED_ACTION_COUNTS.items()))
def test_action_counts_are_exact(
    engine: PolicyEngine, action: str, expected: int
) -> None:
    assert engine.counts_by_action()[action] == expected


def test_action_counts_sum_to_the_total() -> None:
    assert sum(EXPECTED_ACTION_COUNTS.values()) == TOTAL_CODES


def test_every_family_is_one_of_absx(entries: list[PolicyEntry]) -> None:
    assert {e.family for e in entries} == set(FAMILIES)


# -- the 27-of-110 finding ----------------------------------------------------


def test_exactly_27_codes_are_recoverable(entries: list[PolicyEntry]) -> None:
    assert sum(1 for e in entries if e.recoverable) == RECOVERABLE_CODES


def test_recoverable_is_exactly_the_three_retrying_actions(
    entries: list[PolicyEntry],
) -> None:
    recoverable = {e.code for e in entries if e.recoverable}
    retrying = {e.code for e in entries if e.action in RETRYING_ACTIONS}
    assert recoverable == retrying
    assert len(recoverable) == RECOVERABLE_CODES


def test_is_retrying_agrees_with_recoverable(entries: list[PolicyEntry]) -> None:
    for entry in entries:
        assert entry.is_retrying is entry.recoverable, entry.code


def test_coverage_summary_reports_27_of_110(engine: PolicyEngine) -> None:
    summary = engine.coverage_summary()
    assert summary["total_codes"] == TOTAL_CODES
    assert summary["recoverable_codes"] == RECOVERABLE_CODES
    assert summary["unrecoverable_codes"] == TOTAL_CODES - RECOVERABLE_CODES


# -- I-2: unknown codes raise -------------------------------------------------


def test_unknown_code_raises(engine: PolicyEngine) -> None:
    with pytest.raises(UnknownErrorCodeError):
        engine.resolve("no_such_error_code")


def test_unknown_code_error_carries_the_code(engine: PolicyEngine) -> None:
    with pytest.raises(UnknownErrorCodeError) as excinfo:
        engine.resolve("no_such_error_code")
    assert excinfo.value.code == "no_such_error_code"


@pytest.mark.parametrize(
    "near_miss",
    ["", " ", "INSUFFICIENT_FUNDS", "insufficient-funds", "insufficient_fund"],
)
def test_near_miss_codes_still_raise(engine: PolicyEngine, near_miss: str) -> None:
    # Guards against case folding, separator normalisation or fuzzy matching creeping
    # in later. `insufficient_funds` resolves; nothing adjacent to it may.
    assert "insufficient_funds" in engine
    with pytest.raises(UnknownErrorCodeError):
        engine.resolve(near_miss)


def test_resolve_has_no_fallback_branch(engine: PolicyEngine) -> None:
    # If a default were ever added, it would almost certainly be a retrying class.
    for code in ("", "unknown", "xxx"):
        with pytest.raises(UnknownErrorCodeError):
            engine.resolve(code)


# -- I-4: only retrying classes wait ------------------------------------------


def test_positive_min_wait_only_on_retrying_actions(
    entries: list[PolicyEntry],
) -> None:
    offenders = [
        (e.code, e.action, e.min_wait_hours)
        for e in entries
        if e.min_wait_hours > 0 and e.action not in RETRYING_ACTIONS
    ]
    assert offenders == []


def test_min_wait_hours_is_never_negative(entries: list[PolicyEntry]) -> None:
    assert all(e.min_wait_hours >= 0 for e in entries)


# -- I-1: the model's scope is structural -------------------------------------


def test_model_eligible_actions_are_exactly_two() -> None:
    assert MODEL_ELIGIBLE_ACTIONS == {"RETRY_SCHEDULED", "SWITCH_RAIL"}


def test_model_eligible_is_a_subset_of_retrying() -> None:
    assert MODEL_ELIGIBLE_ACTIONS < RETRYING_ACTIONS


def test_is_model_eligible_matches_the_set(entries: list[PolicyEntry]) -> None:
    for entry in entries:
        assert entry.is_model_eligible is (entry.action in MODEL_ELIGIBLE_ACTIONS)


def test_no_unrecoverable_code_is_model_eligible(entries: list[PolicyEntry]) -> None:
    assert not [e.code for e in entries if e.is_model_eligible and not e.recoverable]


# -- every row is presentable -------------------------------------------------


@pytest.mark.parametrize(
    "field", ["policy_note", "razorpay_explanation", "razorpay_next_steps"]
)
def test_text_fields_are_non_empty(entries: list[PolicyEntry], field: str) -> None:
    empty = [e.code for e in entries if not getattr(e, field).strip()]
    assert empty == []


def test_entries_are_immutable(engine: PolicyEngine) -> None:
    entry = engine.resolve("insufficient_funds")
    with pytest.raises(Exception):
        entry.action = "RETRY_NOW"  # type: ignore[misc]


def test_insufficient_funds_is_the_documented_row(engine: PolicyEngine) -> None:
    entry = engine.resolve("insufficient_funds")
    assert entry.action == "RETRY_SCHEDULED"
    assert entry.min_wait_hours == 72
    assert entry.recoverable is True
    assert entry.family == "S"


# -- filters ------------------------------------------------------------------


def test_list_entries_filters_compose(engine: PolicyEngine) -> None:
    switch_rail_b = engine.list_entries(family="B", action="SWITCH_RAIL")
    assert switch_rail_b
    assert all(e.family == "B" and e.action == "SWITCH_RAIL" for e in switch_rail_b)


def test_list_entries_rejects_an_unknown_filter_value(engine: PolicyEngine) -> None:
    with pytest.raises(ValueError):
        engine.list_entries(action="RETRY_MAYBE")
    with pytest.raises(ValueError):
        engine.list_entries(family="Z")


def test_list_entries_is_sorted_by_code(entries: list[PolicyEntry]) -> None:
    codes = [e.code for e in entries]
    assert codes == sorted(codes)


# -- the validation pass actually bites ---------------------------------------
#
# A broken table must fail at load, not produce a wrong decision three stages later.
# These write tampered copies to tmp_path; error_policy.json is never touched.


def _tampered(raw: dict[str, Any], tmp_path: Path, mutate: Any) -> Path:
    copy = json.loads(json.dumps(raw))
    mutate(copy)
    path = tmp_path / "tampered_policy.json"
    path.write_text(json.dumps(copy), encoding="utf-8")
    return path


def test_load_rejects_an_unknown_action(
    raw_policy: dict[str, Any], tmp_path: Path
) -> None:
    def mutate(doc: dict[str, Any]) -> None:
        doc["codes"][0]["action"] = "RETRY_MAYBE"

    with pytest.raises(PolicyLoadError):
        PolicyEngine(_tampered(raw_policy, tmp_path, mutate)).load()


def test_load_rejects_a_still_unmapped_entry(
    raw_policy: dict[str, Any], tmp_path: Path
) -> None:
    def mutate(doc: dict[str, Any]) -> None:
        doc["codes"][0]["action"] = "UNMAPPED"

    with pytest.raises(PolicyLoadError):
        PolicyEngine(_tampered(raw_policy, tmp_path, mutate)).load()


def test_load_rejects_recoverable_disagreeing_with_the_action(
    raw_policy: dict[str, Any], tmp_path: Path
) -> None:
    def mutate(doc: dict[str, Any]) -> None:
        row = next(r for r in doc["codes"] if r["action"] == "STOP")
        row["recoverable"] = True

    with pytest.raises(PolicyLoadError):
        PolicyEngine(_tampered(raw_policy, tmp_path, mutate)).load()


def test_load_rejects_a_wait_on_a_non_retrying_action(
    raw_policy: dict[str, Any], tmp_path: Path
) -> None:
    def mutate(doc: dict[str, Any]) -> None:
        row = next(r for r in doc["codes"] if r["action"] == "AWAIT_STATUS")
        row["min_wait_hours"] = 24

    with pytest.raises(PolicyLoadError):
        PolicyEngine(_tampered(raw_policy, tmp_path, mutate)).load()


def test_load_rejects_a_bad_family(raw_policy: dict[str, Any], tmp_path: Path) -> None:
    def mutate(doc: dict[str, Any]) -> None:
        doc["codes"][0]["family"] = "Z"

    with pytest.raises(PolicyLoadError):
        PolicyEngine(_tampered(raw_policy, tmp_path, mutate)).load()


def test_load_rejects_a_duplicate_code(
    raw_policy: dict[str, Any], tmp_path: Path
) -> None:
    def mutate(doc: dict[str, Any]) -> None:
        doc["codes"].append(json.loads(json.dumps(doc["codes"][0])))

    with pytest.raises(PolicyLoadError):
        PolicyEngine(_tampered(raw_policy, tmp_path, mutate)).load()


def test_load_error_lists_every_problem(
    raw_policy: dict[str, Any], tmp_path: Path
) -> None:
    def mutate(doc: dict[str, Any]) -> None:
        doc["codes"][0]["family"] = "Z"
        doc["codes"][1]["action"] = "RETRY_MAYBE"
        doc["codes"][2]["policy_note"] = "   "

    with pytest.raises(PolicyLoadError) as excinfo:
        PolicyEngine(_tampered(raw_policy, tmp_path, mutate)).load()
    assert len(excinfo.value.problems) >= 3


def test_load_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PolicyLoadError):
        PolicyEngine(tmp_path / "absent.json").load()


def test_reading_before_load_raises(policy_path: Path) -> None:
    unloaded = PolicyEngine(policy_path)
    with pytest.raises(PolicyLoadError):
        unloaded.resolve("insufficient_funds")

"""I-10 — train/test splits are temporal, never random.

A random split on time-series payment data is leakage by another name: a case's later
attempts land in train while its earlier ones land in test, and the model is scored on
rows whose siblings it has already memorised.

Two properties, both required:

* every training timestamp precedes every validation timestamp, which precedes every
  test timestamp
* no case_id appears in more than one split — a case straddling a boundary goes
  entirely to the earlier one
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.model.dataset import (
    SPLIT_DAYS,
    build_rows,
    day_of_run,
    provenance,
    split_for_day,
)
from src.store import db
from tests.conftest import SEED

DAY = 86400


@pytest.fixture(scope="module")
def rows_and_run(tmp_path_factory):
    from eval.run_arms import run

    path = tmp_path_factory.mktemp("split") / "run.db"
    result = run(
        seed=SEED,
        n_payments=2500,
        days=30,
        scenario="normal",
        arms=["baseline"],
        trailing_days=5,
        db_path=path,
    )
    conn = db.connect(result.db_path)
    try:
        run_row = db.get_run(conn, result.run_id)
        assert run_row is not None
        return build_rows(conn, run_row), run_row
    finally:
        conn.close()


# -- the split boundaries as declared -----------------------------------------


def test_split_days_match_the_spec() -> None:
    # research/06 §6.5: train 1-21, validate 22-26, test 27-30.
    assert SPLIT_DAYS == {"train": (1, 21), "valid": (22, 26), "test": (27, 30)}


def test_split_ranges_do_not_overlap() -> None:
    spans = sorted(SPLIT_DAYS.values())
    for (_, end), (start, _) in zip(spans, spans[1:]):
        assert end < start


def test_days_map_to_the_right_split() -> None:
    assert split_for_day(1) == "train"
    assert split_for_day(21) == "train"
    assert split_for_day(22) == "valid"
    assert split_for_day(26) == "valid"
    assert split_for_day(27) == "test"
    assert split_for_day(30) == "test"


def test_the_trailing_window_is_in_no_split() -> None:
    # Executed so late-landing retries resolve (I-15), but never trained or scored on:
    # the trailing window is a partial window and would bias whichever split it joined.
    assert split_for_day(31) is None
    assert split_for_day(37) is None


def test_day_of_run_is_one_indexed() -> None:
    assert day_of_run(1000, 1000) == 1
    assert day_of_run(1000 + DAY - 1, 1000) == 1
    assert day_of_run(1000 + DAY, 1000) == 2


# -- the split as actually produced -------------------------------------------


def test_the_run_produced_all_three_splits(rows_and_run) -> None:
    rows, _ = rows_and_run
    assert {r.split for r in rows} == {"train", "valid", "test"}


def test_train_precedes_valid_precedes_test(rows_and_run) -> None:
    """The ordering property, asserted on timestamps rather than on day labels."""
    rows, _ = rows_and_run
    train = [r.as_of for r in rows if r.split == "train"]
    valid = [r.as_of for r in rows if r.split == "valid"]
    test = [r.as_of for r in rows if r.split == "test"]
    assert train and valid and test
    assert max(train) < min(valid)
    assert max(valid) < min(test)


def test_no_case_appears_in_two_splits(rows_and_run) -> None:
    """A case straddling a boundary goes entirely to the earlier split.

    Without this, attempt 1 of a case trains the model and attempt 3 of the same case
    scores it — the same customer, the same rail, the same failure.
    """
    rows, _ = rows_and_run
    seen: dict[str, str] = {}
    offenders: list[tuple[str, str, str]] = []
    for row in rows:
        if row.case_id in seen and seen[row.case_id] != row.split:
            offenders.append((row.case_id, seen[row.case_id], row.split))
        seen.setdefault(row.case_id, row.split)
    assert offenders == []


def test_no_row_sits_outside_its_own_splits_window(rows_and_run) -> None:
    """The resolution of the tension between the two properties.

    A case is pinned to the split of its first attempt so it cannot appear twice; a
    later attempt that drifts past that split's window is dropped rather than dragged
    backwards, because a day-25 row in the training set would break the ordering
    property — the stronger of the two guarantees.
    """
    rows, _ = rows_and_run
    for row in rows:
        low, high = SPLIT_DAYS[row.split]
        assert low <= row.day <= high, (row.case_id, row.day, row.split)
        assert split_for_day(row.day) == row.split


def test_the_dropped_count_is_recorded(rows_and_run) -> None:
    rows, run_row = rows_and_run
    prov = provenance(rows, run_row)
    # Visible in the provenance sidecar rather than silently absorbed.
    assert prov.dropped_straddling_attempts >= 0


def test_provenance_records_the_split(rows_and_run) -> None:
    rows, run_row = rows_and_run
    prov = provenance(rows, run_row)
    assert prov.n_rows == len(rows)
    assert sum(prov.split_rows.values()) == len(rows)
    assert prov.split_days["train"] == [1, 21]
    assert prov.first_ts <= prov.last_ts


def test_every_row_carries_a_declared_split(rows_and_run) -> None:
    rows, _ = rows_and_run
    assert {r.split for r in rows} <= set(SPLIT_DAYS)


def test_the_split_is_not_random(rows_and_run) -> None:
    """A random split would interleave days; a temporal one cannot.

    Each split's day range must sit inside its declared window with no stragglers.
    """
    rows, _ = rows_and_run
    for name, (low, high) in SPLIT_DAYS.items():
        days = [r.day for r in rows if r.split == name]
        if not days:
            continue
        assert min(days) >= low, name
        assert max(days) <= high, name

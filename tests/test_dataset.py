"""I-11 — one row per attempt, not per payment; label is the attempt outcome.

A payment attempted four times made four decisions in four different contexts.
Collapsing them into one row conflates them and discards exactly what the model is
supposed to learn.

Also asserted here: only the baseline arm's attempts are used, and provenance is
emitted alongside the data so a dataset on disk can be traced to the run that made it.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from src.model.dataset import (
    TRAINING_ARM,
    build_rows,
    provenance,
    to_frame,
    write_dataset,
)
from src.store import db
from tests.conftest import SEED


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    from eval.run_arms import run

    path = tmp_path_factory.mktemp("dataset") / "run.db"
    result = run(
        seed=SEED,
        n_payments=2500,
        days=30,
        scenario="normal",
        arms=["control", "baseline"],
        trailing_days=5,
        db_path=path,
    )
    conn = db.connect(result.db_path)
    try:
        run_row = db.get_run(conn, result.run_id)
        assert run_row is not None
        rows = build_rows(conn, run_row)
        attempts = db.fetch_all(
            conn,
            "SELECT a.*, c.arm AS arm FROM attempts a JOIN cases c ON c.id = a.case_id",
        )
        return {"rows": rows, "run": run_row, "attempts": attempts, "path": result.db_path}
    finally:
        conn.close()


# -- one row per attempt ------------------------------------------------------


def test_rows_exist(built) -> None:
    assert len(built["rows"]) > 50, "too few rows to assert anything about"


def test_one_row_per_attempt_not_per_payment(built) -> None:
    """The row count matches the attempt count, and cases with several attempts
    contribute several rows."""
    rows = built["rows"]
    assert len({r.attempt_id for r in rows}) == len(rows)
    per_case: dict[str, int] = {}
    for row in rows:
        per_case[row.case_id] = per_case.get(row.case_id, 0) + 1
    assert max(per_case.values()) > 1, "no case contributed more than one row"
    assert len(rows) > len(per_case)


def test_every_row_maps_to_a_real_attempt(built) -> None:
    ids = {a["id"] for a in built["attempts"]}
    assert {r.attempt_id for r in built["rows"]} <= ids


def test_the_label_matches_the_attempt_outcome(built) -> None:
    outcomes = {a["id"]: a["outcome"] for a in built["attempts"]}
    for row in built["rows"]:
        assert row.label == (1 if outcomes[row.attempt_id] == "success" else 0)


def test_labels_are_binary(built) -> None:
    assert {r.label for r in built["rows"]} <= {0, 1}


def test_both_classes_are_present(built) -> None:
    assert {r.label for r in built["rows"]} == {0, 1}


# -- baseline only ------------------------------------------------------------


def test_only_baseline_attempts_are_used(built) -> None:
    """Control's action choice is uncorrelated with the error code.

    Its rows would teach the unconditional base rate and nothing action-conditional,
    so including them makes the dataset larger and less informative at the same time.
    """
    arms = {a["id"]: a["arm"] for a in built["attempts"]}
    assert {arms[r.attempt_id] for r in built["rows"]} == {TRAINING_ARM}


def test_the_run_actually_contained_control_attempts(built) -> None:
    # Otherwise the test above passes for the wrong reason.
    assert any(a["arm"] == "control" for a in built["attempts"])


# -- point-in-time replay -----------------------------------------------------


def test_as_of_is_the_scheduled_time(built) -> None:
    scheduled = {
        a["id"]: (a["scheduled_at"] or a["executed_at"]) for a in built["attempts"]
    }
    for row in built["rows"]:
        assert row.as_of == scheduled[row.attempt_id]


def test_attempt_number_is_reconstructed_not_read(built) -> None:
    """Attempt 1 of a four-attempt case must carry attempt_number 1, not 4."""
    firsts = [r for r in built["rows"] if r.attempt_number == 1]
    assert firsts
    for row in firsts:
        assert row.features["attempt_number"] == 1


def test_later_attempts_carry_higher_attempt_numbers(built) -> None:
    by_case: dict[str, list] = {}
    for row in built["rows"]:
        by_case.setdefault(row.case_id, []).append(row)
    multi = [rs for rs in by_case.values() if len(rs) > 1]
    assert multi
    for group in multi:
        ordered = sorted(group, key=lambda r: r.attempt_number)
        assert [r.features["attempt_number"] for r in ordered] == [
            r.attempt_number for r in ordered
        ]


def test_rows_are_ordered_in_time(built) -> None:
    """Sorted by decision time, so a reader of the CSV sees the run unfold in order."""
    stamps = [r.as_of for r in built["rows"]]
    assert stamps == sorted(stamps)


def test_straddling_attempts_are_dropped_not_dragged_backwards(built) -> None:
    """Keeping them would put a late row in an early split and break I-10's ordering.

    The count is recorded rather than silently absorbed.
    """
    from src.model.dataset import SPLIT_DAYS, provenance, split_for_day

    for row in built["rows"]:
        low, high = SPLIT_DAYS[row.split]
        assert low <= row.day <= high
        assert split_for_day(row.day) == row.split
    prov = provenance(built["rows"], built["run"])
    assert prov.dropped_straddling_attempts >= 0


# -- provenance ---------------------------------------------------------------


def test_provenance_is_emitted(built, tmp_path: Path) -> None:
    prov = provenance(built["rows"], built["run"])
    data_path, prov_path = write_dataset(built["rows"], prov, tmp_path)
    assert data_path.exists() and prov_path.exists()

    payload = json.loads(prov_path.read_text(encoding="utf-8"))
    for key in (
        "run_id", "seed", "scenario", "n_rows", "positive_rate", "first_ts", "last_ts",
        "split_days", "feature_names", "arm",
    ):
        assert key in payload, key
    assert payload["run_id"] == built["run"].run_id
    assert payload["seed"] == built["run"].seed
    assert payload["n_rows"] == len(built["rows"])
    assert payload["arm"] == TRAINING_ARM


def test_provenance_counts_positives_correctly(built) -> None:
    prov = provenance(built["rows"], built["run"])
    assert prov.n_positive == sum(r.label for r in built["rows"])
    assert 0.0 < prov.positive_rate < 1.0


def test_the_csv_has_one_line_per_row(built, tmp_path: Path) -> None:
    prov = provenance(built["rows"], built["run"])
    data_path, _ = write_dataset(built["rows"], prov, tmp_path)
    with data_path.open(encoding="utf-8") as handle:
        lines = list(csv.reader(handle))
    assert len(lines) == len(built["rows"]) + 1  # + header
    from src.features.build import FEATURE_NAMES

    assert lines[0][-len(FEATURE_NAMES):] == list(FEATURE_NAMES)


def test_the_frame_carries_every_feature(built) -> None:
    frame = to_frame(built["rows"])
    from src.features.build import FEATURE_NAMES

    assert set(FEATURE_NAMES) <= set(frame.columns)
    assert len(frame) == len(built["rows"])
    assert not frame[list(FEATURE_NAMES)].isna().any().any()

"""Two full runs at the same seed produce the same report.

Stage 2 proved the *population* is byte-identical. This proves the whole pipeline is:
generation, assignment, 800-odd ticks of arm decisions, every attempt the world
resolved, every nudge, and the scored output at the end.

If any of that picked up an ordering dependency — a shared RNG, a dict iterated in
insertion order that changed, a wall-clock read — the numbers would drift between runs
and every comparison in the report would be unreproducible.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.report import build, render
from eval.run_arms import run, run_id_for
from eval.score import to_api_shape
from tests.conftest import SEED

N = 400
DAYS = 20
TRAILING = 4


def _run(path: Path, *, seed: int = SEED, scenario: str = "normal"):
    return run(
        seed=seed,
        n_payments=N,
        days=DAYS,
        scenario=scenario,
        arms=["control", "baseline"],
        trailing_days=TRAILING,
        db_path=path,
    )


@pytest.fixture(scope="module")
def twice(tmp_path_factory) -> tuple[dict, dict]:
    directory = tmp_path_factory.mktemp("e2e")
    first = _run(directory / "first.db")
    second = _run(directory / "second.db")
    return (
        to_api_shape(build(first.db_path, first.run_id)),
        to_api_shape(build(second.db_path, second.run_id)),
    )


def test_the_run_id_is_the_same(twice) -> None:
    a, b = twice
    assert a["run_id"] == b["run_id"]


def test_the_run_id_is_derived_from_the_parameters() -> None:
    args = dict(
        seed=SEED,
        n_payments=N,
        days=DAYS,
        scenario="normal",
        arms=["control", "baseline"],
        trailing_days=TRAILING,
        tick_seconds=3600,
    )
    assert run_id_for(**args) == run_id_for(**args)
    assert run_id_for(**{**args, "seed": SEED + 1}) != run_id_for(**args)
    assert run_id_for(**{**args, "scenario": "bank_outage"}) != run_id_for(**args)


def test_the_whole_report_payload_is_identical(twice) -> None:
    a, b = twice
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_the_headline_numbers_are_identical(twice) -> None:
    a, b = twice
    for arm in a["arms"]:
        assert a["arms"][arm] == b["arms"][arm], arm


def test_the_uplift_is_identical(twice) -> None:
    a, b = twice
    assert a["uplift"] == b["uplift"]


def test_the_per_code_table_is_identical(twice) -> None:
    a, b = twice
    assert a["by_error_code"] == b["by_error_code"]


def test_the_losing_segments_are_identical(twice) -> None:
    a, b = twice
    assert a["losing_segments"] == b["losing_segments"]


def test_the_rendered_markdown_is_identical(tmp_path: Path) -> None:
    first = _run(tmp_path / "md_a.db")
    second = _run(tmp_path / "md_b.db")
    assert render(build(first.db_path, first.run_id)) == render(
        build(second.db_path, second.run_id)
    )


def test_a_different_seed_gives_different_numbers(tmp_path: Path) -> None:
    # The determinism must come from the seed, not from the harness being degenerate.
    a = _run(tmp_path / "s1.db", seed=SEED)
    b = _run(tmp_path / "s2.db", seed=SEED + 7)
    assert to_api_shape(build(a.db_path, a.run_id)) != to_api_shape(
        build(b.db_path, b.run_id)
    )


def test_a_rerun_overwrites_rather_than_accumulating(tmp_path: Path) -> None:
    path = tmp_path / "rerun.db"
    first = _run(path)
    second = _run(path)
    assert first.run_id == second.run_id
    assert first.assignment == second.assignment

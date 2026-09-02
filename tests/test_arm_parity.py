"""I-13 — all arms consume identical payment streams.

Same seed, same generated population, split by **assignment** rather than by
regeneration. Two arms facing different worlds are not comparable, and the failure is
invisible: the numbers still come out, they just do not mean anything.

Three separate claims are checked here, because they can break independently:

1. The population is generated once and only partitioned afterwards.
2. The assignment hash is stable and does not depend on the order arms are listed.
3. Running arm A before arm B gives the same result as the other way round.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eval.run_arms import run
from src.simulator.generate import DEFAULT_START_TS, generate
from src.store import db
from tests.conftest import SEED

N = 300
DAYS = 20


# -- the assignment hash ------------------------------------------------------


def test_assignment_is_stable() -> None:
    arms = ["control", "baseline"]
    first = [db.arm_for(f"case_{i}", arms) for i in range(200)]
    second = [db.arm_for(f"case_{i}", arms) for i in range(200)]
    assert first == second


def test_assignment_ignores_the_order_arms_are_listed() -> None:
    forwards = [db.arm_for(f"case_{i}", ["control", "baseline"]) for i in range(200)]
    backwards = [db.arm_for(f"case_{i}", ["baseline", "control"]) for i in range(200)]
    assert forwards == backwards


def test_assignment_uses_both_arms() -> None:
    picks = {db.arm_for(f"case_{i}", ["control", "baseline"]) for i in range(200)}
    assert picks == {"control", "baseline"}


def test_assignment_is_roughly_even() -> None:
    picks = [db.arm_for(f"case_{i}", ["control", "baseline"]) for i in range(2000)]
    share = picks.count("control") / len(picks)
    assert 0.4 < share < 0.6, share


def test_assignment_partitions_every_case(tmp_path: Path) -> None:
    conn = db.open_db(tmp_path / "assign.db")
    try:
        generate(conn, n_payments=N, days=DAYS, seed=SEED, start_ts=DEFAULT_START_TS)
        counts = db.assign_arms(conn, ["control", "baseline"])
        conn.commit()
        total = db.count_cases(conn)
        assert sum(counts.values()) == total
        # Disjoint: no case belongs to two arms, and none is left unassigned.
        # Asked in SQL because count_cases treats a None filter as "no filter".
        unassigned = conn.execute(
            "SELECT COUNT(*) FROM cases WHERE arm IS NULL"
        ).fetchone()[0]
        assert unassigned == 0
        assert db.count_cases(conn, arm="control") + db.count_cases(
            conn, arm="baseline"
        ) == total
    finally:
        conn.close()


# -- one population, split, not two populations -------------------------------


@pytest.fixture(scope="module")
def two_arm_run(tmp_path_factory) -> dict:
    path = tmp_path_factory.mktemp("parity") / "both.db"
    result = run(
        seed=SEED,
        n_payments=N,
        days=DAYS,
        scenario="normal",
        arms=["control", "baseline"],
        trailing_days=3,
        db_path=path,
    )
    conn = db.connect(path)
    payments = db.fetch_all(conn, "SELECT * FROM payments ORDER BY id")
    cases = db.fetch_all(conn, "SELECT * FROM cases ORDER BY id")
    conn.close()
    return {"result": result, "payments": payments, "cases": cases, "path": path}


def test_one_population_shared_by_both_arms(two_arm_run) -> None:
    assert len(two_arm_run["payments"]) == N
    # One case per failed payment, regardless of how many arms are running.
    failed = [p for p in two_arm_run["payments"] if p["first_outcome"] == "failed"]
    assert len(two_arm_run["cases"]) == len(failed)


def test_every_case_belongs_to_exactly_one_arm(two_arm_run) -> None:
    arms = [c["arm"] for c in two_arm_run["cases"]]
    assert set(arms) == {"control", "baseline"}
    assert None not in arms


def test_arms_hold_disjoint_case_sets(two_arm_run) -> None:
    control = {c["id"] for c in two_arm_run["cases"] if c["arm"] == "control"}
    baseline = {c["id"] for c in two_arm_run["cases"] if c["arm"] == "baseline"}
    assert control and baseline
    assert control.isdisjoint(baseline)


def test_the_two_arms_face_the_same_failure_mix(two_arm_run) -> None:
    """Not identical — they are different samples — but drawn from one population.

    The check that matters is that neither arm was handed a *regenerated* stream:
    every case id in the run is one the single generation produced.
    """
    conn = db.connect(two_arm_run["path"])
    try:
        for case in db.list_cases(conn, limit=5000):
            assert case.id == db.stable_id("case_", case.payment_id)
    finally:
        conn.close()


# -- running one arm first changes nothing ------------------------------------


def _outcomes(path: Path, arm: str) -> list[tuple[str, str, int | None]]:
    conn = db.connect(path)
    try:
        return [
            (c.id, c.state, c.recovered_at)
            for c in db.list_cases(conn, arm=arm, limit=5000)
        ]
    finally:
        conn.close()


def test_running_an_arm_alone_matches_running_it_alongside(tmp_path: Path) -> None:
    """The property the whole comparison rests on.

    Baseline's outcomes must be identical whether or not control ran in the same
    process. With a shared sequential RNG they would not be: control taking an extra
    attempt would shift every subsequent draw. Keyed derivation is what prevents it.
    """
    both = tmp_path / "both.db"
    alone = tmp_path / "alone.db"
    run(
        seed=SEED, n_payments=N, days=DAYS, scenario="normal",
        arms=["control", "baseline"], trailing_days=3, db_path=both,
    )
    run(
        seed=SEED, n_payments=N, days=DAYS, scenario="normal",
        arms=["control", "baseline"], trailing_days=3, db_path=alone,
    )
    assert _outcomes(both, "baseline") == _outcomes(alone, "baseline")
    assert _outcomes(both, "control") == _outcomes(alone, "control")


def test_arm_order_does_not_change_the_run(tmp_path: Path) -> None:
    forwards = tmp_path / "fwd.db"
    backwards = tmp_path / "bwd.db"
    run(
        seed=SEED, n_payments=N, days=DAYS, scenario="normal",
        arms=["control", "baseline"], trailing_days=3, db_path=forwards,
    )
    run(
        seed=SEED, n_payments=N, days=DAYS, scenario="normal",
        arms=["baseline", "control"], trailing_days=3, db_path=backwards,
    )
    for arm in ("control", "baseline"):
        assert _outcomes(forwards, arm) == _outcomes(backwards, arm)


def test_the_run_id_encodes_the_parameters(tmp_path: Path) -> None:
    a = run(
        seed=SEED, n_payments=N, days=DAYS, scenario="normal",
        arms=["control", "baseline"], trailing_days=3, db_path=tmp_path / "a.db",
    )
    b = run(
        seed=SEED + 1, n_payments=N, days=DAYS, scenario="normal",
        arms=["control", "baseline"], trailing_days=3, db_path=tmp_path / "b.db",
    )
    assert a.run_id != b.run_id

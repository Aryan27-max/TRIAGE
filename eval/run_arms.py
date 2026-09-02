"""The tick loop.

Generates the population **once**, splits it across arms by a stable hash of the case
id, then walks the simulated window in fixed ticks. At each tick every case that needs
a decision is handed to its arm; whatever the arm answers goes to the runner, which
enforces the bounds the arm has no say over.

    I-13  All arms consume identical payment streams. One generation, one world, one
          downtime timeline, split by assignment. Arms hold disjoint sets of cases, so
          running one before the other changes nothing.
    I-15  The trailing window is executed, not merely tolerated. A retry scheduled on
          day 28 has to be given the chance to actually run on day 33, or the arm that
          schedules furthest out is undercounted.

    python -m eval.run_arms --seed 42 --scenario normal

Every timestamp is simulated. The one wall-clock read in this file is the optional git
sha; ``runs.created_at`` holds the run's simulated start instead, because the run_id is
derived from the parameters and a real timestamp would be the only thing that differed
between two otherwise identical runs.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from src.arms.base import Arm, ArmDecision, CaseSnapshot
from src.arms.baseline import BaselineArm
from src.arms.control import ControlArm
from src.executor.runner import Runner
from src.executor.state import TERMINAL_STATES
from src.policy.engine import PolicyEngine
from src.simulator.generate import DEFAULT_START_TS, generate
from src.simulator.rails import RailHealth, generate_downtimes
from src.simulator.world import World
from src.store import db

DAY = 86400
HOUR = 3600

DEFAULT_TICK_SECONDS = HOUR
DEFAULT_TRAILING_DAYS = 7  # research/02 §2.5 and I-15
RUNS_DIR = Path(__file__).resolve().parent / "runs"

ARM_FACTORIES = {
    "control": ControlArm,
    "baseline": BaselineArm,
}


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    db_path: Path
    assignment: dict[str, int]
    ticks: int
    decisions: int


def build_arms(names: list[str]) -> dict[str, Arm]:
    unknown = [n for n in names if n not in ARM_FACTORIES]
    if unknown:
        raise ValueError(
            f"unknown arm(s) {unknown}; available: {sorted(ARM_FACTORIES)}"
        )
    return {name: ARM_FACTORIES[name]() for name in names}


def run_id_for(
    *,
    seed: int,
    n_payments: int,
    days: int,
    scenario: str,
    arms: list[str],
    trailing_days: int,
    tick_seconds: int,
) -> str:
    """Derived from the parameters, so an identical run reproduces to the same id.

    That makes reruns idempotent and makes "same seed, same numbers" checkable by
    looking at the id before the report is even opened.
    """
    return db.stable_id(
        "run_",
        seed,
        n_payments,
        days,
        scenario,
        ",".join(sorted(arms)),
        trailing_days,
        tick_seconds,
    )


def git_sha() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path(__file__).resolve().parents[1],
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def run(
    *,
    seed: int = 42,
    n_payments: int = 2000,
    days: int = 30,
    scenario: str = "normal",
    arms: list[str] | None = None,
    trailing_days: int = DEFAULT_TRAILING_DAYS,
    tick_seconds: int = DEFAULT_TICK_SECONDS,
    start_ts: int = DEFAULT_START_TS,
    db_path: Path | str | None = None,
    engine: PolicyEngine | None = None,
) -> RunResult:
    """Generate, assign, and walk the window. Returns the run's id and database."""
    arm_names = sorted(arms or ["control", "baseline"])
    arm_impls = build_arms(arm_names)
    engine = engine or PolicyEngine().load()

    run_id = run_id_for(
        seed=seed,
        n_payments=n_payments,
        days=days,
        scenario=scenario,
        arms=arm_names,
        trailing_days=trailing_days,
        tick_seconds=tick_seconds,
    )
    path = Path(db_path) if db_path is not None else RUNS_DIR / f"{run_id}.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()  # a rerun of the same parameters rebuilds from scratch

    conn = db.open_db(path)
    try:
        # I-13. One population, one world, one downtime timeline. Everything after
        # this point only decides what to do with cases that already exist.
        generate(
            conn,
            n_payments=n_payments,
            days=days,
            seed=seed,
            scenario=scenario,
            start_ts=start_ts,
        )
        assignment = db.assign_arms(conn, arm_names)
        conn.commit()

        downtimes = generate_downtimes(
            days=days, seed=seed, scenario=scenario, start_ts=start_ts
        )
        health = RailHealth.from_events(downtimes)
        world = World(
            seed=seed, scenario=scenario, start_ts=start_ts, health=health
        )
        runner = Runner(engine)

        db.insert_run(
            conn,
            db.Run(
                run_id=run_id,
                seed=seed,
                n_payments=n_payments,
                days=days,
                scenario=scenario,
                trailing_days=trailing_days,
                tick_seconds=tick_seconds,
                arms=",".join(arm_names),
                start_ts=start_ts,
                created_at=start_ts,
                git_sha=git_sha(),
            ),
        )

        # I-15. The loop runs to the end of the *trailing* window, so a retry
        # scheduled near the end of the main window actually executes.
        end_ts = start_ts + (days + trailing_days) * DAY
        ticks = decisions = 0
        for now in range(start_ts, end_ts + tick_seconds, tick_seconds):
            ticks += 1
            decisions += _tick(conn, runner, arm_impls, world, health, engine, now)
            conn.commit()

        # Anything still open at the end of the window never resolved.
        for case in db.open_cases(conn):
            runner.expire_if_past_deadline(conn, case, now=end_ts + 1)
        conn.commit()
    finally:
        conn.close()

    return RunResult(
        run_id=run_id,
        db_path=path,
        assignment=assignment,
        ticks=ticks,
        decisions=decisions,
    )


def _tick(conn, runner, arm_impls, world, health, engine, now: int) -> int:
    """One pass over everything that needs a decision at this instant."""
    decisions = 0
    for case in db.due_for_tick(conn, now):
        arm = arm_impls.get(case.arm or "")
        if arm is None:
            continue
        case = runner.expire_if_past_deadline(conn, case, now=now)
        if case.state in TERMINAL_STATES:
            continue

        last = db.last_attempt(conn, case.id)
        snapshot = CaseSnapshot.of(
            case,
            attempt_count=db.attempt_count(conn, case.id),
            last_attempt_at=last.executed_at if last else None,
        )
        decision: ArmDecision | None = arm.next_action(snapshot, engine, health, now)
        if decision is None:
            continue
        runner.apply(conn, case, decision, world, now=now, actor=arm.name)
        decisions += 1
    return decisions


# -- CLI ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    from eval.report import write_report  # local import: report imports score

    parser = argparse.ArgumentParser(
        prog="python -m eval.run_arms",
        description="Run the arms over one simulated window and write the report.",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--n", type=int, default=2000, dest="n_payments")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--scenario", choices=("normal", "bank_outage"), default="normal")
    parser.add_argument("--arms", default="control,baseline")
    parser.add_argument("--trailing-days", type=int, default=DEFAULT_TRAILING_DAYS)
    parser.add_argument("--tick-seconds", type=int, default=DEFAULT_TICK_SECONDS)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="also print the API shape")
    args = parser.parse_args(argv)

    arm_names = [a.strip() for a in args.arms.split(",") if a.strip()]
    result = run(
        seed=args.seed,
        n_payments=args.n_payments,
        days=args.days,
        scenario=args.scenario,
        arms=arm_names,
        trailing_days=args.trailing_days,
        tick_seconds=args.tick_seconds,
        db_path=args.db,
    )

    report_path = args.report or (
        Path(__file__).resolve().parent
        / f"report-{args.scenario.replace('_', '-')}.md"
    )
    card = write_report(result.db_path, result.run_id, report_path)

    print(f"run          {result.run_id}")
    print(f"db           {result.db_path}")
    print(f"assignment   {result.assignment}")
    print(f"ticks        {result.ticks}  decisions {result.decisions}")
    print(f"report       {report_path}")
    print()
    for arm in card.arms:
        s = card.scores[arm]
        print(
            f"  {arm:<10} {s.recovered:>4}/{s.payments:<4} "
            f"= {s.rate:6.1%}  [{s.ci_low:.1%}, {s.ci_high:.1%}]  "
            f"attempts {s.attempts:>4}  nudges {s.nudges:>4}"
        )
    for gap in card.gaps:
        print(
            f"  {gap.focus} - {gap.reference}: {gap.pp:+.1f}pp "
            f"({gap.relative:+.1%}), p = {gap.p_value:.3f}"
        )
    print(f"  losing segments: {len(card.losses)}")

    if args.json:
        from eval.score import to_api_shape

        print(json.dumps(to_api_shape(card), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

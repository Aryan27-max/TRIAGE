"""Simulation and evaluation endpoints.

The run executes synchronously and returns 202 with status ``completed``. That is a
deliberate simplification: a 30-day window at hourly ticks takes a few seconds, and a
task queue would add a dependency, a failure mode and a piece of infrastructure to
explain, for no benefit a reviewer can see.

Each run owns its own SQLite file under ``eval/runs/``, named by a run id derived from
the run's parameters. Two requests with the same parameters therefore address the same
run rather than accumulating near-duplicates.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends

from eval.report import build as build_scorecard
from eval.run_arms import ARM_FACTORIES, RUNS_DIR, run as execute_run, run_id_for
from eval.score import to_api_shape
from src.api import schemas
from src.api.deps import get_engine
from src.api.errors import InvalidQueryParamError, NotFoundError
from src.api.schemas import ErrorEnvelope
from src.policy.engine import PolicyEngine
from src.simulator.rails import SCENARIOS
from src.store import db

router = APIRouter(prefix="/v1", tags=["evaluation"])

MAX_PAYMENTS = 20_000
MAX_DAYS = 120


def _run_db(run_id: str) -> Path:
    path = RUNS_DIR / f"{run_id}.db"
    if not path.exists():
        raise NotFoundError(
            f"No evaluation run exists with id {run_id!r}",
            reason="run_not_found",
            step="eval_report",
            field="run_id",
        )
    return path


@router.post(
    "/simulator/run",
    response_model=schemas.RunAccepted,
    status_code=202,
    summary="Run the arms over one simulated window",
    responses={400: {"model": ErrorEnvelope}},
)
def simulator_run(
    body: schemas.SimulatorRunRequest,
    engine: PolicyEngine = Depends(get_engine),
) -> schemas.RunAccepted:
    if body.scenario not in SCENARIOS:
        raise InvalidQueryParamError(
            "scenario", body.scenario, SCENARIOS, step="simulator_run"
        )
    unknown = [a for a in body.arms if a not in ARM_FACTORIES]
    if unknown:
        raise InvalidQueryParamError(
            "arms", body.arms, sorted(ARM_FACTORIES), step="simulator_run"
        )
    if not 1 <= body.n_payments <= MAX_PAYMENTS:
        raise InvalidQueryParamError(
            "n_payments", body.n_payments, (f"1..{MAX_PAYMENTS}",), step="simulator_run"
        )
    if not 1 <= body.days <= MAX_DAYS:
        raise InvalidQueryParamError(
            "days", body.days, (f"1..{MAX_DAYS}",), step="simulator_run"
        )

    result = execute_run(
        seed=body.seed,
        n_payments=body.n_payments,
        days=body.days,
        scenario=body.scenario,
        arms=list(body.arms),
        trailing_days=body.trailing_days,
        tick_seconds=body.tick_seconds,
        engine=engine,
    )
    return schemas.RunAccepted(
        run_id=result.run_id,
        status="completed",
        assignment=result.assignment,
        ticks=result.ticks,
    )


@router.get(
    "/eval/runs",
    response_model=schemas.RunCollection,
    summary="Evaluation runs on disk",
)
def list_runs() -> schemas.RunCollection:
    items: list[schemas.RunSummary] = []
    if RUNS_DIR.exists():
        for path in sorted(RUNS_DIR.glob("*.db")):
            conn = db.connect(path)
            try:
                for run in db.list_runs(conn):
                    items.append(
                        schemas.RunSummary(
                            run_id=run.run_id,
                            seed=run.seed,
                            n_payments=run.n_payments,
                            days=run.days,
                            scenario=run.scenario,
                            trailing_days=run.trailing_days,
                            tick_seconds=run.tick_seconds,
                            arms=run.arm_names,
                            start_ts=run.start_ts,
                            git_sha=run.git_sha,
                        )
                    )
            except Exception:  # noqa: BLE001 - a half-written run must not 500 the list
                continue
            finally:
                conn.close()
    return schemas.RunCollection(count=len(items), items=items)


@router.get(
    "/eval/report/{run_id}",
    summary="Scored report for one run",
    responses={404: {"model": ErrorEnvelope}},
)
def eval_report(run_id: str) -> dict[str, Any]:
    """The shape in research/05 §5.4, plus the losing segments it demands. (I-16)"""
    card = build_scorecard(_run_db(run_id), run_id)
    return to_api_shape(card)

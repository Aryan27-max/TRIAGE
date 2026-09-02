"""Case lifecycle and the stateless decision endpoint.

Handlers translate executor and store exceptions into the Razorpay envelope and do
nothing else: the bounds live in ``src.executor.runner``, the graph lives in
``src.executor.state``, and the action class comes from the policy table. A route
never decides anything.

    409 IDEMPOTENCY_CONFLICT  idempotency key reused                        (I-5)
    423 AWAITING_STATUS       attempt on an unresolved AWAIT_STATUS case    (I-6)
    422 POLICY_VIOLATION      would breach max_attempts or drop_dead_at     (I-7)
    400 BAD_REQUEST_ERROR     unknown error_code submitted to /decide       (I-2)
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict

from fastapi import APIRouter, Body, Depends, Header, Response

from src.api import schemas
from src.api.schemas import ErrorEnvelope
from src.api.deps import get_conn, get_runner, get_world
from src.api.errors import (
    AwaitingStatusError,
    BadErrorCodeError,
    ConflictError,
    IdempotencyConflictError,
    InvalidQueryParamError,
    NotFoundError,
    PolicyViolationError,
)
from src.executor import state as st
from src.executor.runner import AwaitingStatus, PolicyViolation, Runner
from src.policy.engine import UnknownErrorCodeError
from src.simulator.world import World
from src.store import db

router = APIRouter(prefix="/v1/recovery", tags=["recovery"])

_ERROR_RESPONSES = {
    400: {"model": ErrorEnvelope},
    404: {"model": ErrorEnvelope},
    409: {"model": ErrorEnvelope},
    422: {"model": ErrorEnvelope},
    423: {"model": ErrorEnvelope},
}


# -- serialisation ------------------------------------------------------------


def _summary(conn: sqlite3.Connection, case: db.Case) -> schemas.CaseSummary:
    return schemas.CaseSummary(
        id=case.id,
        payment_id=case.payment_id,
        status=case.state,
        arm=case.arm,
        method=case.method,
        rail=case.rail,
        amount_paise=case.amount_paise,
        error_code=case.error_code,
        error_source=case.error_source,
        failed_at=case.failed_at,
        max_attempts=case.max_attempts,
        drop_dead_at=case.drop_dead_at,
        next_attempt_at=case.next_attempt_at,
        status_resolved_at=case.status_resolved_at,
        recovered_at=case.recovered_at,
        recovered_amount_paise=case.recovered_amount_paise,
        created_at=case.created_at,
        attempt_count=db.attempt_count(conn, case.id),
    )


def _decision(
    conn: sqlite3.Connection, runner: Runner, case: db.Case
) -> schemas.Decision:
    """What policy currently says about this case, for the next attempt.

    The case's error_code is whatever the most recent failure reported, so this
    tracks the case as it moves between action classes across attempts.
    """
    decision = runner.decide(
        case.error_code,
        now=case.next_attempt_at or case.failed_at,
        method=case.method,
        attempt_number=db.attempt_count(conn, case.id) + 1,
    )
    return schemas.Decision(**asdict(decision))


def _detail(
    conn: sqlite3.Connection, case: db.Case, runner: Runner
) -> schemas.CaseDetail:
    summary = _summary(conn, case)
    return schemas.CaseDetail(
        **summary.model_dump(),
        decision=_decision(conn, runner, case),
        attempts=[
            schemas.AttemptRecord(
                id=a.id,
                attempt_number=a.attempt_number,
                idempotency_key=a.idempotency_key,
                action=a.action,
                target_rail=a.target_rail,
                scheduled_at=a.scheduled_at,
                executed_at=a.executed_at,
                outcome=a.outcome,
                error_code=a.error_code,
                latency_ms=a.latency_ms,
            )
            for a in db.list_attempts(conn, case.id)
        ],
        audit=[
            schemas.AuditRecord(
                at=row.at,
                from_state=row.from_state,
                to_state=row.to_state,
                actor=row.actor,
                reason=row.reason,
                idempotency_key=row.idempotency_key,
                detail=row.detail,
            )
            for row in db.list_audit(conn, case.id)
        ],
    )


def _require_case(conn: sqlite3.Connection, case_id: str) -> db.Case:
    case = db.get_case(conn, case_id)
    if case is None:
        raise NotFoundError(
            f"No recovery case exists with id {case_id!r}",
            reason="case_not_found",
            step="recovery_case_lookup",
            field="id",
        )
    return case


# -- stateless decision -------------------------------------------------------


@router.post(
    "/decide",
    response_model=schemas.Decision,
    summary="Decide an action for one error code",
    responses={400: {"model": ErrorEnvelope, "description": "Unknown error_code"}},
)
def decide(
    body: schemas.DecideRequest,
    runner: Runner = Depends(get_runner),
) -> schemas.Decision:
    """The policy table's answer. No payment is touched and nothing is stored.

    An unrecognised code raises rather than defaulting to a retry — defaulting here
    is the single most dangerous shortcut available in this system. (I-2)
    """
    try:
        decision = runner.decide(
            body.error_code,
            now=body.now,
            method=body.method,
            attempt_number=body.attempt_number,
        )
    except UnknownErrorCodeError as exc:
        raise BadErrorCodeError(str(exc.code), step="recovery_decide") from exc
    return schemas.Decision(**asdict(decision))


# -- case lifecycle -----------------------------------------------------------


@router.post(
    "/cases",
    response_model=schemas.CaseDetail,
    status_code=201,
    summary="Open a recovery case from a failed payment",
    responses=_ERROR_RESPONSES,
)
def create_case(
    body: schemas.CaseCreate,
    response: Response,
    conn: sqlite3.Connection = Depends(get_conn),
    runner: Runner = Depends(get_runner),
) -> schemas.CaseDetail:
    """Idempotent on ``payment_id``. Reopening returns the existing case with 200.

    The case is opened and immediately diagnosed against the policy table, so the
    response already carries the action class and where the case landed.
    """
    existing = db.get_case_by_payment(conn, body.payment_id)
    if existing is not None:
        response.status_code = 200
        return _detail(conn, existing, runner)

    try:
        runner.engine.resolve(body.error_code)
    except UnknownErrorCodeError as exc:
        raise BadErrorCodeError(str(exc.code), step="recovery_case_create") from exc

    customer_id = body.customer.id if body.customer else f"cust_of_{body.payment_id}"
    merchant_id = body.merchant.id if body.merchant else f"mch_of_{body.payment_id}"

    if db.get_payment(conn, body.payment_id) is None:
        db.insert_payments(
            conn,
            [
                db.Payment(
                    id=body.payment_id,
                    customer_id=customer_id,
                    merchant_id=merchant_id,
                    method=body.method,
                    rail=body.rail or body.method,
                    amount_paise=body.amount,
                    created_at=body.failed_at,
                    first_outcome="failed",
                    first_error_code=body.error_code,
                )
            ],
        )
    payment = db.get_payment(conn, body.payment_id)
    assert payment is not None

    case = runner.build_case(
        payment=payment,
        error_code=body.error_code,
        error_source=body.source,
        failed_at=body.failed_at,
        arm=body.arm,
    )
    st.open_case(conn, case, now=body.failed_at, actor="api", reason="payment.failed")
    # `failed_at` is the current time for a case being opened; there is no separate
    # server clock to read.
    diagnosed = runner.diagnose(conn, case, now=body.failed_at)
    return _detail(conn, diagnosed, runner)


@router.get(
    "/cases",
    response_model=schemas.CaseCollection,
    summary="List recovery cases",
    responses={400: {"model": ErrorEnvelope}},
)
def list_cases(
    state: str | None = None,
    arm: str | None = None,
    error_code: str | None = None,
    method: str | None = None,
    limit: int = 50,
    offset: int = 0,
    conn: sqlite3.Connection = Depends(get_conn),
) -> schemas.CaseCollection:
    if state is not None and state not in st.STATES:
        raise InvalidQueryParamError("state", state, st.STATES, step="recovery_case_list")
    if not 1 <= limit <= 500:
        raise InvalidQueryParamError("limit", limit, ("1..500",), step="recovery_case_list")

    cases = db.list_cases(
        conn,
        state=state,
        arm=arm,
        error_code=error_code,
        method=method,
        limit=limit,
        offset=offset,
    )
    return schemas.CaseCollection(
        count=db.count_cases(
            conn, state=state, arm=arm, error_code=error_code, method=method
        ),
        items=[_summary(conn, case) for case in cases],
    )


@router.get(
    "/cases/{case_id}",
    response_model=schemas.CaseDetail,
    summary="One case with its attempts and audit trail",
    responses={404: {"model": ErrorEnvelope}},
)
def get_case(
    case_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
    runner: Runner = Depends(get_runner),
) -> schemas.CaseDetail:
    return _detail(conn, _require_case(conn, case_id), runner)


@router.post(
    "/cases/{case_id}/attempts",
    response_model=schemas.AttemptResult,
    status_code=201,
    summary="Execute and record one attempt",
    responses=_ERROR_RESPONSES,
)
def create_attempt(
    case_id: str,
    body: schemas.AttemptCreate,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    conn: sqlite3.Connection = Depends(get_conn),
    runner: Runner = Depends(get_runner),
    world: World = Depends(get_world),
) -> schemas.AttemptResult:
    """Run one attempt against the bounds.

    The Idempotency-Key header is stored verbatim in the column carrying the UNIQUE
    index, so a reused key is rejected by the schema rather than by an ``if``. (I-5)
    """
    if not idempotency_key or not idempotency_key.strip():
        # Taken by hand rather than as a required Header so the refusal leaves through
        # the Razorpay envelope like every other failure, not FastAPI's `detail` blob.
        raise InvalidQueryParamError(
            "Idempotency-Key",
            idempotency_key,
            ("a non-empty key, conventionally {case_id}:{attempt_number}",),
            step="recovery_attempt",
        )

    case = _require_case(conn, case_id)
    try:
        attempt, updated = runner.execute_attempt(
            conn, case, world, now=body.now, idempotency_key=idempotency_key
        )
    except AwaitingStatus as exc:
        raise AwaitingStatusError(exc.case_id) from exc
    except db.IdempotencyConflict as exc:
        raise IdempotencyConflictError(exc.key) from exc
    except PolicyViolation as exc:
        raise PolicyViolationError(exc.description, reason=exc.reason) from exc
    except st.IllegalTransitionError as exc:
        raise ConflictError(
            str(exc), reason="illegal_state_transition", step="recovery_attempt"
        ) from exc

    return schemas.AttemptResult(
        attempt=schemas.AttemptRecord(
            id=attempt.id,
            attempt_number=attempt.attempt_number,
            idempotency_key=attempt.idempotency_key,
            action=attempt.action,
            target_rail=attempt.target_rail,
            scheduled_at=attempt.scheduled_at,
            executed_at=attempt.executed_at,
            outcome=attempt.outcome,
            error_code=attempt.error_code,
            latency_ms=attempt.latency_ms,
        ),
        case=_summary(conn, updated),
    )


@router.post(
    "/cases/{case_id}/status-poll",
    response_model=schemas.CaseDetail,
    summary="Resolve an AWAITING_STATUS case",
    responses=_ERROR_RESPONSES,
)
def status_poll(
    case_id: str,
    body: schemas.StatusPollRequest,
    conn: sqlite3.Connection = Depends(get_conn),
    runner: Runner = Depends(get_runner),
) -> schemas.CaseDetail:
    """Settle the unknown outcome. This is what lifts I-6's block on attempting."""
    case = _require_case(conn, case_id)
    if body.resolution not in ("succeeded", "failed"):
        raise InvalidQueryParamError(
            "resolution",
            body.resolution,
            ("succeeded", "failed"),
            step="recovery_status_poll",
        )
    try:
        updated = runner.resolve_status(
            conn, case, now=body.now, resolution=body.resolution
        )
    except PolicyViolation as exc:
        raise PolicyViolationError(
            exc.description, reason=exc.reason, step="recovery_status_poll"
        ) from exc
    return _detail(conn, updated, runner)


@router.post(
    "/cases/{case_id}/stop",
    response_model=schemas.CaseDetail,
    summary="Force-terminate a case",
    responses=_ERROR_RESPONSES,
)
def stop_case(
    case_id: str,
    body: schemas.StopRequest = Body(default=schemas.StopRequest(now=0)),
    conn: sqlite3.Connection = Depends(get_conn),
    runner: Runner = Depends(get_runner),
) -> schemas.CaseDetail:
    case = _require_case(conn, case_id)
    try:
        updated = runner.stop(conn, case, now=body.now, reason=body.reason)
    except PolicyViolation as exc:
        raise PolicyViolationError(
            exc.description, reason=exc.reason, step="recovery_case_stop"
        ) from exc
    return _detail(conn, updated, runner)

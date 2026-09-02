"""Rail health.

Mirrors the Razorpay Downtime API schema (research/05 §5.3). The live API needs
account enablement; where it is unavailable the prototype serves a simulated feed
conforming to the same shape, which research/02 §2.4 says to state explicitly rather
than obscure.

``POST`` is prototype-only. It exists so the demo can inject an outage on stage and
watch the next attempt switch rails — the world is rebuilt per request from whatever
is in this table, so an injected event takes effect immediately.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query

from src.api import schemas
from src.api.deps import get_conn
from src.api.errors import InvalidQueryParamError
from src.api.schemas import ErrorEnvelope
from src.simulator.rails import METHODS
from src.store import db

router = APIRouter(prefix="/v1/rails", tags=["rails"])

SEVERITIES = ("low", "medium", "high")
STATUSES = ("started", "resolved")
SCOPES = ("all", "psp", "issuer", "network", "wallet")


def _record(event: db.Downtime) -> schemas.DowntimeRecord:
    return schemas.DowntimeRecord(**event.as_dict())


@router.get(
    "/health",
    response_model=schemas.DowntimeCollection,
    summary="Downtime events",
    responses={400: {"model": ErrorEnvelope}},
)
def rail_health(
    at: int | None = Query(
        None, description="Unix seconds. Only events active at this instant."
    ),
    method: str | None = Query(None, description="upi | card | netbanking | wallet"),
    conn: sqlite3.Connection = Depends(get_conn),
) -> schemas.DowntimeCollection:
    if method is not None and method not in METHODS:
        raise InvalidQueryParamError("method", method, METHODS, step="rail_health")
    events = db.list_downtimes(conn, at=at, method=method)
    return schemas.DowntimeCollection(
        count=len(events), items=[_record(e) for e in events]
    )


@router.post(
    "/health",
    response_model=schemas.DowntimeRecord,
    status_code=201,
    summary="Inject a downtime event (prototype only)",
    responses={400: {"model": ErrorEnvelope}},
)
def inject_downtime(
    body: schemas.DowntimeCreate,
    conn: sqlite3.Connection = Depends(get_conn),
) -> schemas.DowntimeRecord:
    for name, value, allowed in (
        ("method", body.method, METHODS),
        ("severity", body.severity, SEVERITIES),
        ("status", body.status, STATUSES),
        ("scope", body.scope, SCOPES),
    ):
        if value not in allowed:
            raise InvalidQueryParamError(name, value, allowed, step="rail_downtime_inject")
    if body.end is not None and body.end <= body.begin:
        raise InvalidQueryParamError(
            "end", body.end, ("> begin",), step="rail_downtime_inject"
        )

    event = db.Downtime(
        id=db.stable_id("down_", body.method, body.scope, body.instrument, body.begin),
        method=body.method,
        scope=body.scope,
        instrument=body.instrument,
        severity=body.severity,
        status=body.status,
        begin=body.begin,
        end=body.end,
    )
    db.insert_downtimes(conn, [event])
    return _record(event)

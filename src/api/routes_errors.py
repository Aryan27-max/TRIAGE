"""Read-only policy surface: what does the decision table say about a code?

Nothing here decides anything about a specific payment — that is Stage 2. Every
handler reads the one engine the app loaded at startup from ``request.app.state``;
no route ever touches ``error_policy.json`` itself.
"""

from __future__ import annotations

from fastapi import APIRouter, Path, Query, Request

from src.api.deps import get_engine
from src.api.errors import ErrorCodeNotFoundError, InvalidQueryParamError
from src.api.schemas import (
    ActionClass,
    ActionClassCollection,
    CoverageSummary,
    ErrorEnvelope,
    ErrorPolicy,
    ErrorPolicyCollection,
)
from src.policy.engine import ACTIONS, FAMILIES, UnknownErrorCodeError

router = APIRouter(prefix="/v1/errors", tags=["errors"])

_BOOL_TRUE = frozenset({"true", "1"})
_BOOL_FALSE = frozenset({"false", "0"})
_BOOL_ALLOWED = ("true", "false", "1", "0")


def _parse_recoverable(value: str | None) -> bool | None:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in _BOOL_TRUE:
        return True
    if lowered in _BOOL_FALSE:
        return False
    raise InvalidQueryParamError("recoverable", value, _BOOL_ALLOWED)


@router.get(
    "",
    response_model=ErrorPolicyCollection,
    summary="List policy entries",
    responses={400: {"model": ErrorEnvelope, "description": "Invalid query parameter"}},
)
def list_errors(
    request: Request,
    family: str | None = Query(
        None, description="A cards/netbanking · B UPI/wallets · S shared · X merchant"
    ),
    action: str | None = Query(None, description="One of the eight action classes"),
    recoverable: str | None = Query(
        None, description="true | false | 1 | 0 — recoverable without human intervention"
    ),
) -> ErrorPolicyCollection:
    # Validated here rather than by FastAPI's own coercion so that a bad value leaves
    # through the Razorpay envelope like every other failure, not a 422 `detail` blob.
    if family is not None and family not in FAMILIES:
        raise InvalidQueryParamError("family", family, FAMILIES)
    if action is not None and action not in ACTIONS:
        raise InvalidQueryParamError("action", action, ACTIONS)

    entries = get_engine(request).list_entries(
        family=family,
        action=action,
        recoverable=_parse_recoverable(recoverable),
    )
    items = [ErrorPolicy.from_entry(e) for e in entries]
    return ErrorPolicyCollection(count=len(items), items=items)


@router.get(
    "/meta/actions",
    response_model=ActionClassCollection,
    summary="The eight action classes",
)
def list_action_classes(request: Request) -> ActionClassCollection:
    catalogue = get_engine(request).actions_catalogue()
    items = [ActionClass(**row) for row in catalogue]
    return ActionClassCollection(count=len(items), items=items)


@router.get(
    "/meta/coverage",
    response_model=CoverageSummary,
    summary="Recoverable-code coverage — the 27-of-110 finding",
)
def coverage(request: Request) -> CoverageSummary:
    return CoverageSummary(**get_engine(request).coverage_summary())


@router.get(
    "/{code}",
    response_model=ErrorPolicy,
    summary="Policy lookup for a single code",
    responses={404: {"model": ErrorEnvelope, "description": "No such error code"}},
)
def get_error(
    request: Request,
    code: str = Path(..., description="Exact Razorpay error code. Not case-insensitive."),
) -> ErrorPolicy:
    try:
        entry = get_engine(request).resolve(code)
    except UnknownErrorCodeError as exc:
        # I-2: unknown codes raise. They are never resolved to a default action.
        raise ErrorCodeNotFoundError(exc.code) from exc
    return ErrorPolicy.from_entry(entry)

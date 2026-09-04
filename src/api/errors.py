"""Razorpay-style error envelope.

Every failure the API reports leaves through one of these. Route handlers raise them
and never hand-build a response body; a single handler registered in ``main.py``
turns them into JSON. That is what keeps the envelope identical across endpoints.

    {"error": {"code": "...", "description": "...", "field": "...",
               "source": "...", "step": "...", "reason": "..."}}

The HTTP-status/code pairs are fixed by CLAUDE.md and research/05 section 5.5.
"""

from __future__ import annotations

from typing import Any


class TriageAPIError(Exception):
    """Base for anything the API reports to a caller.

    Subclasses fix ``status_code`` and ``code``; the rest is per-instance detail.
    """

    status_code: int = 500
    code: str = "SERVER_ERROR"
    source: str = "business"

    def __init__(
        self,
        description: str,
        *,
        reason: str,
        step: str,
        field: str | None = None,
        source: str | None = None,
    ) -> None:
        self.description = description
        self.reason = reason
        self.step = step
        self.field = field
        if source is not None:
            self.source = source
        super().__init__(description)

    def to_envelope(self) -> dict[str, Any]:
        """The response body. Keys are always present; ``field`` may be null."""
        return {
            "error": {
                "code": self.code,
                "description": self.description,
                "field": self.field,
                "source": self.source,
                "step": self.step,
                "reason": self.reason,
            }
        }


class BadErrorCodeError(TriageAPIError):
    """An unrecognised payment failure reason was submitted as input. (I-2)

    Declared here because I-2 fixes its shape. Stage 1 has no endpoint that accepts
    an error code as *input* — the read-only lookup returns 404 instead — so this
    first fires from ``POST /v1/recovery/decide`` in Stage 2.
    """

    status_code = 400
    code = "BAD_REQUEST_ERROR"

    def __init__(self, error_code: str, *, step: str = "recovery_decide") -> None:
        super().__init__(
            f"{error_code!r} is not a recognised payment failure reason",
            reason="unknown_error_code",
            step=step,
            field="error_code",
        )
        self.error_code = error_code


class InvalidQueryParamError(TriageAPIError):
    """A query parameter carried a value outside its permitted set."""

    status_code = 400
    code = "BAD_REQUEST_ERROR"

    def __init__(
        self,
        param: str,
        value: Any,
        allowed: list[str] | tuple[str, ...],
        *,
        step: str = "error_lookup",
    ) -> None:
        super().__init__(
            f"{param}={value!r} is not valid; expected one of {list(allowed)}",
            reason="invalid_query_param",
            step=step,
            field=param,
        )
        self.param = param
        self.value = value
        self.allowed = list(allowed)


class NotFoundError(TriageAPIError):
    """The addressed entity does not exist.

    Distinct from BadErrorCodeError on purpose: a caller browsing the taxonomy asked
    for a row that is not there, which is not the same failure as submitting an
    unrecognised code to a decision endpoint. Different codes, different shapes.
    """

    status_code = 404
    code = "NOT_FOUND_ERROR"

    def __init__(
        self,
        description: str,
        *,
        reason: str,
        step: str,
        field: str | None = None,
    ) -> None:
        super().__init__(description, reason=reason, step=step, field=field)


class ErrorCodeNotFoundError(NotFoundError):
    """No policy row exists for the requested code."""

    def __init__(self, error_code: str) -> None:
        super().__init__(
            f"No policy entry exists for error code {error_code!r}",
            reason="error_code_not_found",
            step="error_lookup",
            field="code",
        )
        self.error_code = error_code


class IdempotencyConflictError(TriageAPIError):
    """The idempotency key has already been used. The double-charge guard. (I-5)

    Raised off the UNIQUE index on ``attempts.idempotency_key``, not off an
    application-level check — a race that gets past the pre-check still lands here.
    """

    status_code = 409
    code = "IDEMPOTENCY_CONFLICT"

    def __init__(self, key: str, *, step: str = "record_attempt") -> None:
        super().__init__(
            f"idempotency key {key!r} has already been used on this case",
            reason="idempotency_key_reused",
            step=step,
            field="Idempotency-Key",
        )
        self.key = key


class AwaitingStatusError(TriageAPIError):
    """The prior outcome is unresolved; attempting now risks a double charge. (I-6)

    Razorpay's own documentation notes that pending transactions may authorise late
    and that a deemed transaction's outcome is unknown until the following day. This
    is that guard surfacing at the API boundary.
    """

    status_code = 423
    code = "AWAITING_STATUS"

    def __init__(self, case_id: str, *, step: str = "recovery_attempt") -> None:
        super().__init__(
            f"case {case_id} has an unresolved prior outcome; poll status before "
            f"attempting again",
            reason="prior_outcome_unresolved",
            step=step,
            field="case_id",
        )
        self.case_id = case_id


class PolicyViolationError(TriageAPIError):
    """The action would breach max_attempts or drop_dead_at. (I-7)"""

    status_code = 422
    code = "POLICY_VIOLATION"

    def __init__(
        self, description: str, *, reason: str, step: str = "recovery_attempt"
    ) -> None:
        super().__init__(description, reason=reason, step=step, field="case_id")


class ConflictError(TriageAPIError):
    """The request conflicts with the current state of the entity."""

    status_code = 409
    code = "CONFLICT_ERROR"

    def __init__(self, description: str, *, reason: str, step: str) -> None:
        super().__init__(description, reason=reason, step=step)


class ReadOnlyError(TriageAPIError):
    """This instance is a read-only exhibit and will not mutate the store.

    The deployed demo serves pre-computed runs baked into the image. Refusing writes
    is what keeps the numbers in the report the numbers a visitor sees.
    """

    status_code = 503
    code = "SERVICE_UNAVAILABLE"

    def __init__(self, what: str, *, step: str) -> None:
        super().__init__(
            f"{what} is disabled: this TRIAGE instance is read-only and serves "
            f"pre-computed evaluation runs. Clone the repo and run it locally to "
            f"generate your own.",
            reason="read_only_instance",
            step=step,
        )

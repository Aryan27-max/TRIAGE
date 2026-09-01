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

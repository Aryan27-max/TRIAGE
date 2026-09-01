"""Pydantic response models for the read-only policy surface.

Deliberately separate from ``src/policy/engine.py``. The engine's ``PolicyEntry`` is a
domain object with derived behaviour; these are wire shapes that the dashboard and the
OpenAPI page depend on. Keeping them apart means a field can be added to one without
silently changing the other.

Collections use Razorpay's envelope: ``{entity, count, items}``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.policy.engine import PolicyEntry


class ErrorPolicy(BaseModel):
    """One row of the decision table, as served."""

    code: str = Field(..., examples=["insufficient_funds"])
    family: str = Field(..., description="A cards/netbanking · B UPI/wallets · S shared · X merchant")
    action: str = Field(..., description="One of the eight action classes")
    min_wait_hours: int = Field(
        ..., description="Minimum hours before a retry may be attempted. 0 unless the class schedules."
    )
    recoverable: bool = Field(
        ..., description="True when the action class recovers without human intervention"
    )
    is_retrying: bool = Field(..., description="The class may schedule another attempt")
    is_model_eligible: bool = Field(
        ..., description="The Stage 4 model may rank executions within this class (I-1)"
    )
    policy_note: str
    razorpay_explanation: str
    razorpay_next_steps: str

    @classmethod
    def from_entry(cls, entry: PolicyEntry) -> "ErrorPolicy":
        return cls(
            code=entry.code,
            family=entry.family,
            action=entry.action,
            min_wait_hours=entry.min_wait_hours,
            recoverable=entry.recoverable,
            is_retrying=entry.is_retrying,
            is_model_eligible=entry.is_model_eligible,
            policy_note=entry.policy_note,
            razorpay_explanation=entry.razorpay_explanation,
            razorpay_next_steps=entry.razorpay_next_steps,
        )


class ErrorPolicyCollection(BaseModel):
    entity: str = "collection"
    count: int
    items: list[ErrorPolicy]


class ActionClass(BaseModel):
    action: str
    description: str
    code_count: int
    recoverable: bool
    schedules_retry: bool
    model_eligible: bool


class ActionClassCollection(BaseModel):
    entity: str = "collection"
    count: int
    items: list[ActionClass]


class ActionCoverage(BaseModel):
    action: str
    count: int
    recoverable: bool
    description: str


class FamilyCoverage(BaseModel):
    family: str
    label: str
    count: int
    recoverable_count: int


class CoverageSummary(BaseModel):
    """The 27-of-110 finding. Read by the Stage 5 taxonomy board."""

    policy_version: str
    source: str
    total_codes: int
    recoverable_codes: int
    unrecoverable_codes: int
    recoverable_share: float
    recoverable_actions: list[str]
    model_eligible_actions: list[str]
    by_action: list[ActionCoverage]
    by_family: list[FamilyCoverage]
    headline: str


class Health(BaseModel):
    status: str
    policy_codes_loaded: int
    policy_version: str


class ErrorDetail(BaseModel):
    code: str
    description: str
    field: str | None = None
    source: str
    step: str
    reason: str


class ErrorEnvelope(BaseModel):
    """Documents the failure shape in OpenAPI. Built by ``TriageAPIError.to_envelope``."""

    error: ErrorDetail

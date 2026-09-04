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
    read_only: bool = False


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


# -- Stage 2: cases, attempts, audit ------------------------------------------
#
# Every write endpoint takes the current time explicitly. There is no server clock
# anywhere in src/, because a 30-day simulation has to run in seconds.


class CustomerRef(BaseModel):
    id: str
    vpa_handle: str | None = None
    payer_bank: str | None = None
    city_tier: int | None = None


class MerchantRef(BaseModel):
    id: str
    mcc: str | None = None


class CaseCreate(BaseModel):
    """Open a recovery case from a failed payment. Idempotent on payment_id."""

    payment_id: str
    error_code: str
    method: str
    amount: int = Field(..., gt=0, description="Integer paise. Never a float.")
    failed_at: int = Field(..., description="Unix seconds. Also taken as 'now'.")
    source: str | None = None
    rail: str | None = Field(None, description="Instrument-level route; defaults to method")
    order_id: str | None = None
    customer: CustomerRef | None = None
    merchant: MerchantRef | None = None
    arm: str | None = None


class DecideRequest(BaseModel):
    """Stateless decision. Nothing is stored and no payment is touched."""

    error_code: str
    now: int = Field(..., description="Unix seconds. Required — there is no server clock.")
    method: str | None = None
    attempt_number: int = Field(1, ge=1)
    vpa_handle: str | None = Field(None, description="Narrows the rail-health lookup")
    payer_bank: str | None = None


class AttemptCreate(BaseModel):
    now: int = Field(..., description="Unix seconds. Required — there is no server clock.")


class StatusPollRequest(BaseModel):
    now: int
    resolution: str = Field(
        ..., description="'succeeded' (authorised late) or 'failed' (outcome now known)"
    )


class StopRequest(BaseModel):
    now: int
    reason: str = "operator_stopped"


class DowntimeCreate(BaseModel):
    method: str
    severity: str
    begin: int
    scope: str = "all"
    instrument: str | None = None
    status: str = "started"
    end: int | None = None


class RailHealthSnapshot(BaseModel):
    """What the published Downtime feed says at the decision instant.

    Observable — a real recovery service reads Razorpay's Downtime API — so consulting
    it is not a hidden-state leak. Returned on every decision so the Inspector can show
    the same signal the baseline arm acts on.
    """

    method: str | None = None
    severity: str | None = None
    target_rail: str | None = None
    target_severity: str | None = None
    active_events: int = 0
    switch_blocked: bool = Field(
        False, description="True when the alternate rail is also high-severity"
    )


class ModelDisposition(BaseModel):
    """Whether the model was consulted, and why not when it was not. (I-1)

    The absence of a model call on 86 of the 110 codes is the structural safety claim,
    so it is reported explicitly rather than inferred from a missing field.
    """

    eligible: bool
    consulted: bool
    reason: str
    eligible_actions: list[str] = []


class Decision(BaseModel):
    decision_id: str
    error_code: str
    action: str
    family: str
    recoverable: bool
    scheduled_at: int | None = Field(
        None, description="Always null for the five non-retrying classes (I-4)"
    )
    target_rail: str | None = None
    min_wait_hours: int
    reason_code: str
    advice: str = Field(..., description="Analogue of Stripe's advice_code")
    explanation: str
    next_steps: str
    model_eligible: bool
    constraints: dict
    rail_health: RailHealthSnapshot | None = None
    model: ModelDisposition | None = None


class AttemptRecord(BaseModel):
    id: str
    attempt_number: int
    idempotency_key: str
    action: str
    target_rail: str | None
    scheduled_at: int | None
    executed_at: int
    outcome: str
    error_code: str | None
    latency_ms: int


class AuditRecord(BaseModel):
    at: int
    from_state: str | None
    to_state: str
    actor: str
    reason: str
    idempotency_key: str | None = None
    detail: dict | None = None


class CaseSummary(BaseModel):
    id: str
    entity: str = "recovery.case"
    payment_id: str
    status: str = Field(..., description="Case state machine position")
    arm: str | None
    method: str
    rail: str
    amount_paise: int
    error_code: str
    error_source: str | None
    failed_at: int
    max_attempts: int
    drop_dead_at: int
    next_attempt_at: int | None
    status_resolved_at: int | None
    recovered_at: int | None
    recovered_amount_paise: int | None
    created_at: int
    attempt_count: int = 0


class CaseDetail(CaseSummary):
    decision: Decision | None = None
    attempts: list[AttemptRecord] = []
    audit: list[AuditRecord] = []


class CaseCollection(BaseModel):
    entity: str = "collection"
    count: int
    items: list[CaseSummary]


class AttemptResult(BaseModel):
    entity: str = "recovery.attempt"
    attempt: AttemptRecord
    case: CaseSummary


class DowntimeRecord(BaseModel):
    id: str
    entity: str = "payment.downtime"
    method: str
    scope: str
    instrument: str | None
    severity: str
    status: str
    begin: int
    end: int | None


class DowntimeCollection(BaseModel):
    entity: str = "collection"
    count: int
    items: list[DowntimeRecord]


# -- Stage 3: simulation and evaluation ---------------------------------------


class SimulatorRunRequest(BaseModel):
    """Run the arms over one simulated window. Executes synchronously."""

    n_payments: int = Field(8000, ge=1, description="Payments to generate")
    days: int = Field(30, ge=1, description="Length of the main window")
    seed: int = Field(42, description="Every draw in the run derives from this")
    scenario: str = Field("normal", description="normal | bank_outage")
    arms: list[str] = Field(default_factory=lambda: ["control", "baseline"])
    trailing_days: int = Field(
        7, ge=0, description="Executed as well as scored — a day-28 retry may land on day 33 (I-15)"
    )
    tick_seconds: int = Field(3600, ge=1, description="Simulated clock step")


class RunAccepted(BaseModel):
    run_id: str
    status: str = "completed"
    assignment: dict[str, int] = Field(
        default_factory=dict, description="Cases per arm, split by a stable hash (I-13)"
    )
    ticks: int = 0


class RunSummary(BaseModel):
    run_id: str
    entity: str = "eval.run"
    seed: int
    n_payments: int
    days: int
    scenario: str
    trailing_days: int
    tick_seconds: int
    arms: list[str]
    start_ts: int
    git_sha: str | None = None


class RunCollection(BaseModel):
    entity: str = "collection"
    count: int
    items: list[RunSummary]

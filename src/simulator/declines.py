"""The cause model: what went wrong, and how often.

Error codes are **not** sampled from a hand-picked distribution. Latent state decides
the cause — a customer whose balance is short gets `insufficient_funds`, a card past
its validity window gets `card_expired`, a payment routed through a degraded PSP gets
an outage code — and the rates in CONFIG decide how often each cause fires at all.

That ordering is what keeps the evaluation from being circular. If the code
distribution were imposed directly, the policy table would be graded against a
distribution chosen to make it look good.

Every rate lives in CONFIG with a comment saying where it came from. Numbers that are
our own assumption say so; the simulator is defensible because the assumptions are
visible, not because they are hidden.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Sequence

# (code, source, relative weight). Sources use Razorpay's vocabulary.
CodeWeights = Sequence[tuple[str, str, float]]

CONFIG: dict[str, object] = {
    # -- geography ------------------------------------------------------------
    # research/01 §1.4: "Metro vs tier-3 success gap exceeds 25 points". Published
    # as a gap on overall success; applied here as a multiplier on customer-side
    # failure rates. The values below put roughly 25pp between tier 1 and tier 3.
    "city_tier_failure_multiplier": {1: 0.50, 2: 1.00, 3: 2.30},
    # Assumption (ours): population mix across tiers. No published split.
    "city_tier_mix": ((1, 0.40), (2, 0.35), (3, 0.25)),
    # -- time of day ----------------------------------------------------------
    # research/01 §1.4 and research/04 §4.6: bank load sags measurably during
    # evening peaks; is_peak_window is called out as a strong India-specific signal.
    # Magnitude is our assumption — the docs assert the direction, not the size.
    "peak_window_failure_multiplier": 1.6,
    # -- base rates -----------------------------------------------------------
    # Assumption (ours): Razorpay publishes no aggregate first-attempt decline rate.
    # This is the residual customer-side rate before tier and peak multipliers. With
    # the rest of CONFIG it lands the overall first-attempt failure rate at ~17%, and
    # spreads the failures across all eight action classes rather than concentrating
    # them in one. Tuned against the printed distribution, not fitted to any dataset.
    "customer_side_failure_rate": 0.055,
    # P(the attempt fails) given an active downtime of each severity on that rail.
    # research/02 §2.4 fixes the ordering (high = down, medium = elevated declines,
    # low = minimal impact); the magnitudes are our assumption.
    "outage_failure_probability": {"low": 0.08, "medium": 0.38, "high": 0.85},
    # Share of merchants carrying a live misconfiguration, and how often it bites.
    # Assumption (ours). Family X is 23 of the 110 codes but these are setup faults,
    # so they should be concentrated in a few merchants rather than spread thin.
    "misconfigured_merchant_share": 0.04,
    "merchant_misconfig_failure_rate": 0.55,
    # Velocity limits. Assumption (ours): NPCI caps UPI at 20 transactions per day
    # per account, so this fires rarely and only for high-frequency customers.
    "limit_breach_rate": 0.012,
    # AWAIT_STATUS. research/03 §3.5 notes pending transactions may authorise late
    # and a deemed transaction's outcome is unknown until the next day. Rate is our
    # assumption; it must be non-trivial because these five codes are the whole
    # double-charge safety story.
    "unknown_outcome_rate": 0.02,
    # STOP. Assumption (ours): risk declines are rare but must appear.
    "risk_block_rate": 0.004,
    # -- retries --------------------------------------------------------------
    # A repeat attempt on a rail that has not changed is somewhat more likely to hit
    # the same wall than a first attempt was. Assumption (ours).
    "retry_penalty_multiplier": 1.15,
    # Attempt latency in ms, (min, max) by outcome. Assumption (ours); cosmetic.
    "latency_ms_success": (600, 2600),
    "latency_ms_failed": (900, 9000),
}


# -- code inventories ---------------------------------------------------------
#
# Every code below is one of the 110 in error_policy.json. The simulator can only
# emit codes the policy table knows about; tests/test_simulator.py enforces that.

OUTAGE_CODES: dict[str, CodeWeights] = {
    # research/04 §4.2 maps break points to codes: payer PSP, remitter bank, NPCI
    # switch, beneficiary bank, gateway.
    "upi": (
        ("psp_not_available", "bank", 0.30),
        ("upi_app_technical_error", "gateway", 0.20),
        ("authorisation_declined_by_psp", "bank", 0.20),
        ("vpa_resolution_failed", "npci", 0.15),
        ("payment_failed", "gateway", 0.15),
    ),
    "card": (
        ("issuer_technical_error", "issuer", 0.40),
        ("bank_not_available", "bank", 0.20),
        ("gateway_technical_error", "gateway", 0.25),
        ("payment_failed", "gateway", 0.15),
    ),
    "netbanking": (
        ("bank_technical_error", "bank", 0.50),
        ("bank_not_available", "bank", 0.30),
        ("gateway_technical_error", "gateway", 0.20),
    ),
    "wallet": (
        ("server_error", "gateway", 0.50),
        ("payment_failed", "gateway", 0.50),
    ),
}

# Fires during the evening peak: contention, not a hard fault.
PEAK_CODES: CodeWeights = (
    ("payment_declined_due_to_high_traffic", "bank", 0.40),
    ("payment_timed_out", "gateway", 0.35),
    ("request_timed_out", "gateway", 0.25),
)

# Customer-side residual: the human and instrument faults that dominate the tail.
CUSTOMER_CODES: dict[str, CodeWeights] = {
    "upi": (
        ("incorrect_pin", "customer", 0.22),
        ("payment_cancelled", "customer", 0.20),
        ("collect_request_pending", "customer", 0.14),
        ("invalid_vpa", "customer", 0.12),
        ("payment_timed_out", "gateway", 0.12),
        ("invalid_device", "customer", 0.08),
        ("pin_not_set", "customer", 0.07),
        ("transaction_on_vpa_restricted", "bank", 0.05),
    ),
    "card": (
        ("incorrect_otp", "customer", 0.18),
        ("card_declined", "issuer", 0.17),
        ("authentication_failed", "customer", 0.15),
        ("incorrect_cvv", "customer", 0.12),
        ("otp_expired", "customer", 0.11),
        ("payment_cancelled", "customer", 0.10),
        ("incorrect_card_details", "customer", 0.07),
        ("transaction_limit_exceeded", "issuer", 0.06),
        ("debit_instrument_blocked", "issuer", 0.04),
    ),
    "netbanking": (
        ("payment_session_expired", "gateway", 0.28),
        ("payment_cancelled", "customer", 0.24),
        ("user_not_registered_for_netbanking", "bank", 0.20),
        ("authentication_failed", "customer", 0.16),
        ("bank_account_invalid", "bank", 0.12),
    ),
    "wallet": (
        ("payment_cancelled", "customer", 0.40),
        ("payment_timed_out", "gateway", 0.35),
        ("invalid_user_details", "customer", 0.25),
    ),
}

# Velocity caps. All RETRY_SCHEDULED at 24h — the daily counter resets.
LIMIT_CODES: dict[str, CodeWeights] = {
    "upi": (
        ("transaction_frequency_limit_exceeded", "bank", 0.55),
        ("pin_attempts_exceeded", "bank", 0.45),
    ),
    "card": (
        ("transaction_daily_limit_exceeded", "issuer", 0.40),
        ("transaction_daily_count_exceeded", "issuer", 0.30),
        ("otp_attempts_exceeded", "issuer", 0.30),
    ),
    "netbanking": (
        ("transaction_daily_limit_exceeded", "bank", 0.60),
        ("bank_cutoff_in_progress", "bank", 0.40),
    ),
    "wallet": (("transaction_daily_limit_exceeded", "gateway", 1.0),),
}

# AWAIT_STATUS. Outcome genuinely unknown — retrying risks a double charge. (I-6)
UNKNOWN_OUTCOME_CODES: CodeWeights = (
    ("payment_pending", "bank", 0.55),
    ("deemed_transaction", "bank", 0.25),
    ("capture_failed", "gateway", 0.20),
)

# MERCHANT_ALERT. Setup faults, not customer failures.
MERCHANT_CODES: CodeWeights = (
    ("payment_method_not_enabled", "business", 0.25),
    ("international_transaction_not_allowed", "business", 0.20),
    ("invalid_amount", "business", 0.15),
    ("order_amount_mismatch", "business", 0.15),
    ("input_validation_failed", "business", 0.15),
    ("upi_collect_not_enabled", "business", 0.10),
)

# STOP. Retrying is unsafe or penalised.
RISK_CODES: CodeWeights = (
    ("payment_risk_check_failed", "bank", 0.70),
    ("compliance_violation", "business", 0.30),
)

# Deterministic instrument faults, keyed off latent validity windows.
CARD_EXPIRED = ("card_expired", "issuer")


@dataclass(frozen=True, slots=True)
class CauseContext:
    """Everything the cause model needs, assembled by the world from latent state.

    Internal to ``src/simulator``. It never crosses into the decision path — the only
    thing that leaves the simulator is an AttemptOutcome. (I-12)
    """

    method: str
    amount_paise: int
    at_ts: int
    city_tier: int
    is_peak: bool
    outage_severity: str | None
    card_is_expired: bool
    funds_are_short: bool
    merchant_misconfigured: bool
    limit_prone: bool
    clumsiness: float
    is_retry: bool


@dataclass(frozen=True, slots=True)
class Cause:
    code: str
    source: str


def weighted_pick(rng: random.Random, options: CodeWeights) -> Cause:
    total = sum(weight for _, _, weight in options)
    roll = rng.random() * total
    cumulative = 0.0
    for code, source, weight in options:
        cumulative += weight
        if roll < cumulative:
            return Cause(code, source)
    code, source, _ = options[-1]
    return Cause(code, source)


def customer_side_probability(ctx: CauseContext) -> float:
    """Residual customer-side failure rate after geography, load and habit."""
    tiers: dict[int, float] = CONFIG["city_tier_failure_multiplier"]  # type: ignore[assignment]
    rate: float = CONFIG["customer_side_failure_rate"]  # type: ignore[assignment]
    probability = rate * tiers.get(ctx.city_tier, 1.0) * ctx.clumsiness
    if ctx.is_peak:
        probability *= CONFIG["peak_window_failure_multiplier"]  # type: ignore[operator]
    if ctx.is_retry:
        probability *= CONFIG["retry_penalty_multiplier"]  # type: ignore[operator]
    return min(probability, 0.95)


def peak_probability(ctx: CauseContext) -> float:
    """Contention failures on top of the residual rate, only inside the peak window."""
    if not ctx.is_peak:
        return 0.0
    tiers: dict[int, float] = CONFIG["city_tier_failure_multiplier"]  # type: ignore[assignment]
    multiplier: float = CONFIG["peak_window_failure_multiplier"]  # type: ignore[assignment]
    base: float = CONFIG["customer_side_failure_rate"]  # type: ignore[assignment]
    return min(base * (multiplier - 1.0) * tiers.get(ctx.city_tier, 1.0), 0.5)


def sample_cause(rng: random.Random, ctx: CauseContext) -> Cause | None:
    """Resolve one attempt. ``None`` means the payment went through.

    The order is the causal order, not a ranking: a merchant that cannot accept the
    method never reaches the bank, an outage swallows the request before the customer
    sees a PIN pad, and a short balance is decided before habit gets a say.
    """
    # 1. Merchant misconfiguration. Nothing about the customer matters here.
    if ctx.merchant_misconfigured:
        if rng.random() < CONFIG["merchant_misconfig_failure_rate"]:  # type: ignore[operator]
            return weighted_pick(rng, MERCHANT_CODES)

    # 2. Rail degradation. Observable to the decision path through the Downtime feed.
    if ctx.outage_severity is not None:
        probabilities: dict[str, float] = CONFIG["outage_failure_probability"]  # type: ignore[assignment]
        if rng.random() < probabilities[ctx.outage_severity]:
            return weighted_pick(rng, OUTAGE_CODES[ctx.method])

    # 3. Instrument validity. Deterministic: an expired card cannot authorise.
    if ctx.card_is_expired:
        return Cause(*CARD_EXPIRED)

    # 4. Balance. Deterministic given the latent balance the world tracks.
    if ctx.funds_are_short:
        return Cause("insufficient_funds", "bank")

    # 5. Velocity caps.
    if ctx.limit_prone and rng.random() < CONFIG["limit_breach_rate"]:  # type: ignore[operator]
        return weighted_pick(rng, LIMIT_CODES[ctx.method])

    # 6. Risk and compliance blocks.
    if rng.random() < CONFIG["risk_block_rate"]:  # type: ignore[operator]
        return weighted_pick(rng, RISK_CODES)

    # 7. Outcome genuinely unknown. Must survive as its own class. (I-6)
    if rng.random() < CONFIG["unknown_outcome_rate"]:  # type: ignore[operator]
        return weighted_pick(rng, UNKNOWN_OUTCOME_CODES)

    # 8. Evening-peak contention.
    if rng.random() < peak_probability(ctx):
        return weighted_pick(rng, PEAK_CODES)

    # 9. Residual customer-side.
    if rng.random() < customer_side_probability(ctx):
        return weighted_pick(rng, CUSTOMER_CODES[ctx.method])

    return None


def latency_ms(rng: random.Random, success: bool) -> int:
    low, high = CONFIG["latency_ms_success" if success else "latency_ms_failed"]  # type: ignore[misc]
    return rng.randint(low, high)


def all_emittable_codes() -> set[str]:
    """Every code the simulator can produce. Checked against the 110 in tests."""
    codes = {CARD_EXPIRED[0], "insufficient_funds"}
    for table in (*OUTAGE_CODES.values(), *CUSTOMER_CODES.values(), *LIMIT_CODES.values()):
        codes.update(code for code, _, _ in table)
    for table in (PEAK_CODES, UNKNOWN_OUTCOME_CODES, MERCHANT_CODES, RISK_CODES):
        codes.update(code for code, _, _ in table)
    return codes

"""HIDDEN STATE. The decision path must never read anything in this module. (I-12)

The world knows what a real recovery system cannot: the customer's true balance and
salary day, the card's validity window, whether a PIN is set and a device bound, how
responsive the customer is to a nudge, and the full outage timeline.

The decision path sees only the error code, the published rail-health feed, and
observable history. If policy could read latent state the evaluation would be
circular and worth nothing — a lookup table graded against its own answer key.

The only thing that crosses the boundary is ``AttemptOutcome``, and it carries four
fields: whether the attempt worked, the code and source if it did not, and latency.
No hints, no debug payload, no latent fields. ``tests/test_hidden_state.py``
introspects it and fails if that ever changes.

Nothing here reads a clock. Every question about time is answered from ``at_ts``.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.simulator import declines
from src.simulator.rails import (
    BANKS,
    DAY,
    IST_OFFSET_SECONDS,
    VPA_HANDLES,
    WALLETS,
    RailHealth,
    derive_rng,
    is_peak_window,
)
from src.store.db import CaseView

# The action passed to `attempt` for the original payment, before any case exists.
INITIAL_ATTEMPT = "INITIAL"

LATENT_CONFIG: dict[str, object] = {
    # Monthly income in paise, by city tier. Assumption (ours): sets the scale at
    # which `insufficient_funds` starts to bite for a given ticket size.
    "monthly_income_paise": {1: (3_500_000, 15_000_000), 2: (2_000_000, 8_000_000), 3: (1_200_000, 4_500_000)},
    # Fraction of the month's income spent per month. >1.0 means the account is dry
    # before the next credit. Assumption (ours).
    "burn_rate": (0.55, 1.30),
    # Floor on remaining balance, so a high-burn customer is not locked out for a
    # third of every month. Assumption (ours).
    "balance_floor_fraction": 0.05,
    # Share of customers who run a tight balance at all. Everyone else carries enough
    # buffer that a ticket in this size range always clears. Assumption (ours): the
    # published material says nothing about balance distributions, and this is the
    # single knob that sets how much of the failure stream is `insufficient_funds` —
    # the flagship RETRY_SCHEDULED code (research/03 §3.3).
    "balance_constrained_share": 0.20,
    # Indian salary cycles concentrate on the 1st and the 7th (research/03 §3.3).
    "salary_days": ((1, 0.55), (7, 0.25), (2, 0.10), (10, 0.10)),
    # Card validity relative to the run start, in days. The negative tail is what
    # produces `card_expired`. Assumption (ours).
    "card_valid_days": (-45, 1095),
    # Per-customer multiplier on the residual customer-side failure rate.
    "clumsiness": (0.35, 1.85),
    # Share of customers with no UPI PIN set / no bound device. Assumption (ours).
    "pin_not_set_share": 0.03,
    "device_unbound_share": 0.02,
    # Share prone to breaching velocity caps. Assumption (ours).
    "limit_prone_share": 0.08,
    # How reliably a customer acts on a nudge. Held for Stage 3's arms; no Stage 2
    # code path consumes it, because the executor never nudges. (I-4)
    "nudge_responsiveness": (0.05, 0.65),
    # A rail switch lands on infrastructure the original failure did not touch, so
    # the switched attempt starts from a higher base. Assumption (ours).
    "switch_rail_bonus": 0.12,
}


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    """The entire contract between the world and everything downstream.

    Four fields. Adding a fifth that names anything latent breaks
    tests/test_hidden_state.py, which is the point.
    """

    success: bool
    error_code: str | None
    error_source: str | None
    latency_ms: int


@dataclass(frozen=True, slots=True)
class _CustomerLatent:
    """Never leaves this module. Not returned, not logged, not serialised."""

    city_tier: int
    payer_bank: str
    vpa_handle: str
    salary_day: int
    monthly_income_paise: int
    burn_rate: float
    card_valid_until: int
    balance_constrained: bool
    pin_set: bool
    device_bound: bool
    clumsiness: float
    nudge_responsiveness: float
    limit_prone: bool


@dataclass(frozen=True, slots=True)
class _MerchantLatent:
    """Never leaves this module."""

    mcc: str
    ticket_band: str
    misconfigured: bool


def ist_civil(at_ts: int) -> datetime:
    """The Indian civil date for a timestamp. Conversion, not a clock read."""
    return datetime.fromtimestamp(at_ts + IST_OFFSET_SECONDS, tz=timezone.utc)


def days_since_salary(at_ts: int, salary_day: int) -> int:
    """Days elapsed since the customer's most recent salary credit."""
    civil = ist_civil(at_ts)
    if civil.day >= salary_day:
        return civil.day - salary_day
    previous_month_end = civil.replace(day=1) - timedelta(days=1)
    return previous_month_end.day - salary_day + civil.day


def _pick_salary_day(rng: random.Random) -> int:
    roll = rng.random()
    cumulative = 0.0
    for day, weight in LATENT_CONFIG["salary_days"]:  # type: ignore[misc]
        cumulative += weight
        if roll < cumulative:
            return day
    return 1


class World:
    """The attempt oracle. Latent state is derived, never registered.

    Each customer's latent state comes from a generator seeded by
    ``(seed, "customer", customer_id)``, so adding a customer to the population does
    not shift the state of any existing one. Each attempt is resolved from a
    generator seeded by ``(seed, "attempt", case_id, attempt_number)``, so an arm
    taking one extra attempt cannot shift the draws for any other arm. Without that,
    the Stage 3 comparison is between different worlds. (I-13)
    """

    def __init__(
        self,
        *,
        seed: int,
        scenario: str,
        start_ts: int,
        health: RailHealth,
    ) -> None:
        self._seed = seed
        self._scenario = scenario
        self._start_ts = start_ts
        self._health = health
        self._customers: dict[str, _CustomerLatent] = {}
        self._merchants: dict[str, _MerchantLatent] = {}

    @property
    def seed(self) -> int:
        return self._seed

    @property
    def scenario(self) -> str:
        return self._scenario

    @property
    def start_ts(self) -> int:
        return self._start_ts

    @property
    def health(self) -> RailHealth:
        """The published downtime feed. Observable — a real system reads this."""
        return self._health

    # -- observable projections ------------------------------------------------

    def customer_profile(self, customer_id: str) -> dict[str, object]:
        """The subset a real gateway would already hold about a customer.

        research/05 §5.1 puts exactly these on the decide request. Balance, salary
        day, card validity, responsiveness and habit are **not** here and never will
        be.
        """
        latent = self._customer(customer_id)
        return {
            "city_tier": latent.city_tier,
            "payer_bank": latent.payer_bank,
            "vpa_handle": latent.vpa_handle,
        }

    def merchant_profile(self, merchant_id: str) -> dict[str, object]:
        latent = self._merchant(merchant_id)
        return {"mcc": latent.mcc, "ticket_band": latent.ticket_band}

    # -- the one resolution method --------------------------------------------

    def attempt(
        self,
        case: CaseView,
        action: str,
        target_rail: str | None,
        at_ts: int,
    ) -> AttemptOutcome:
        """Resolve one attempt against latent state. The only way out of the world.

        ``action`` is ``INITIAL_ATTEMPT`` for the original payment, otherwise one of
        the eight policy classes. ``target_rail`` overrides the case's rail on a
        SWITCH_RAIL; ``None`` means stay where you are.
        """
        rng = derive_rng(self._seed, "attempt", case.id, case.attempt_number)
        method = target_rail or case.method
        if method not in declines.OUTAGE_CODES:
            raise ValueError(f"unknown rail {method!r}")

        ctx = self._context(case, method, at_ts, is_retry=case.attempt_number > 1)
        cause = declines.sample_cause(rng, ctx)

        # A rail switch reaches infrastructure the original failure did not touch.
        # Modelled as a second chance, not as immunity: a switch onto a rail that is
        # itself degraded still fails.
        if cause is not None and action == "SWITCH_RAIL" and target_rail:
            if rng.random() < LATENT_CONFIG["switch_rail_bonus"]:  # type: ignore[operator]
                cause = declines.sample_cause(rng, ctx)

        if cause is None:
            return AttemptOutcome(
                success=True,
                error_code=None,
                error_source=None,
                latency_ms=declines.latency_ms(rng, True),
            )
        return AttemptOutcome(
            success=False,
            error_code=cause.code,
            error_source=cause.source,
            latency_ms=declines.latency_ms(rng, False),
        )

    # -- latent state ----------------------------------------------------------

    def _context(
        self, case: CaseView, method: str, at_ts: int, *, is_retry: bool
    ) -> declines.CauseContext:
        latent = self._customer(case.customer_id)
        merchant = self._merchant(case.merchant_id)
        return declines.CauseContext(
            method=method,
            amount_paise=case.amount_paise,
            at_ts=at_ts,
            city_tier=latent.city_tier,
            is_peak=is_peak_window(at_ts),
            outage_severity=self._health.severity_at(
                method,
                at_ts,
                # The instrument the payment actually rides. On a rail switch the
                # case's own instrument no longer applies, so only the customer's
                # bank and handle can match.
                instruments=(
                    case.rail if method == case.method else None,
                    latent.vpa_handle,
                    latent.payer_bank,
                ),
            ),
            card_is_expired=method == "card" and at_ts > latent.card_valid_until,
            funds_are_short=self._funds_are_short(latent, case.amount_paise, at_ts),
            merchant_misconfigured=merchant.misconfigured,
            limit_prone=latent.limit_prone,
            clumsiness=self._effective_clumsiness(latent, method),
            is_retry=is_retry,
        )

    def _effective_clumsiness(self, latent: _CustomerLatent, method: str) -> float:
        """An unset PIN or an unbound device makes UPI fail far more often."""
        clumsiness = latent.clumsiness
        if method == "upi" and (not latent.pin_set or not latent.device_bound):
            clumsiness *= 3.0
        return clumsiness

    def _funds_are_short(
        self, latent: _CustomerLatent, amount_paise: int, at_ts: int
    ) -> bool:
        """Whether the latent balance covers this amount at this moment.

        Only balance-constrained customers can be short. Everyone else carries enough
        buffer across the cycle that ticket sizes in this range always clear — which
        is why `insufficient_funds` is a meaningful minority of failures rather than
        the whole tail.
        """
        if not latent.balance_constrained:
            return False
        return amount_paise > self._balance_paise(latent, at_ts)

    def _balance_paise(self, latent: _CustomerLatent, at_ts: int) -> int:
        """Latent balance: credited on salary day, drawn down across the month.

        This is the mechanism behind `insufficient_funds`, and behind why a
        RETRY_SCHEDULED that lands after the next credit works. The decision path
        never sees this number — it only sees the code and the day of the month,
        which is precisely the inference Stage 4's model has to make.
        """
        elapsed = days_since_salary(at_ts, latent.salary_day)
        floor: float = LATENT_CONFIG["balance_floor_fraction"]  # type: ignore[assignment]
        remaining = max(floor, 1.0 - latent.burn_rate * elapsed / 30.0)
        return int(latent.monthly_income_paise * remaining)

    def _customer(self, customer_id: str) -> _CustomerLatent:
        cached = self._customers.get(customer_id)
        if cached is not None:
            return cached

        rng = derive_rng(self._seed, "customer", customer_id)
        tiers: tuple[tuple[int, float], ...] = declines.CONFIG["city_tier_mix"]  # type: ignore[assignment]
        roll, cumulative, tier = rng.random(), 0.0, 1
        for candidate, weight in tiers:
            cumulative += weight
            if roll < cumulative:
                tier = candidate
                break

        income_lo, income_hi = LATENT_CONFIG["monthly_income_paise"][tier]  # type: ignore[index]
        burn_lo, burn_hi = LATENT_CONFIG["burn_rate"]  # type: ignore[misc]
        card_lo, card_hi = LATENT_CONFIG["card_valid_days"]  # type: ignore[misc]
        clumsy_lo, clumsy_hi = LATENT_CONFIG["clumsiness"]  # type: ignore[misc]
        nudge_lo, nudge_hi = LATENT_CONFIG["nudge_responsiveness"]  # type: ignore[misc]

        latent = _CustomerLatent(
            city_tier=tier,
            payer_bank=BANKS[rng.randrange(len(BANKS))],
            vpa_handle=VPA_HANDLES[rng.randrange(len(VPA_HANDLES))],
            salary_day=_pick_salary_day(rng),
            monthly_income_paise=rng.randrange(income_lo, income_hi, 1000),
            burn_rate=rng.uniform(burn_lo, burn_hi),
            card_valid_until=self._start_ts + rng.randint(card_lo, card_hi) * DAY,
            balance_constrained=rng.random() < LATENT_CONFIG["balance_constrained_share"],  # type: ignore[operator]
            pin_set=rng.random() >= LATENT_CONFIG["pin_not_set_share"],  # type: ignore[operator]
            device_bound=rng.random() >= LATENT_CONFIG["device_unbound_share"],  # type: ignore[operator]
            clumsiness=rng.uniform(clumsy_lo, clumsy_hi),
            nudge_responsiveness=rng.uniform(nudge_lo, nudge_hi),
            limit_prone=rng.random() < LATENT_CONFIG["limit_prone_share"],  # type: ignore[operator]
        )
        self._customers[customer_id] = latent
        return latent

    def _merchant(self, merchant_id: str) -> _MerchantLatent:
        cached = self._merchants.get(merchant_id)
        if cached is not None:
            return cached

        rng = derive_rng(self._seed, "merchant", merchant_id)
        mcc = ("5411", "5812", "4900", "5732", "7372", "4131")[rng.randrange(6)]
        band = ("small", "medium", "large")[rng.randrange(3)]
        latent = _MerchantLatent(
            mcc=mcc,
            ticket_band=band,
            misconfigured=rng.random() < declines.CONFIG["misconfigured_merchant_share"],  # type: ignore[operator]
        )
        self._merchants[merchant_id] = latent
        return latent


def wallet_names() -> tuple[str, ...]:
    """Re-exported so generate.py has one import site for rail inventory."""
    return WALLETS

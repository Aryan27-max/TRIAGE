"""Rail inventory and the simulated downtime feed.

Razorpay publishes rail degradation through a Downtime API, graded by severity and
scoped to a VPA handle, a whole PSP, a netbanking bank, a card network, or all of a
method (research/02 §2.4). The live API needs account enablement, so the prototype
emits a feed conforming to the same schema and says so rather than obscuring it.

Severity maps to policy directly:

    high    issuer/bank/network down   suppress retries on this rail, prefer SWITCH_RAIL
    medium  elevated declines          longer scheduled wait, penalise the rail
    low     minimal impact             feature input only

Every timestamp here is an integer unix second, derived from an explicit ``start_ts``.
Nothing in this module reads a clock.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Iterable, Sequence

from src.store.db import Downtime, stable_id

HOUR = 3600
DAY = 24 * HOUR

# IST is UTC+5:30. Held as a constant so civil-time questions ("which hour of the
# Indian day is this?") are pure arithmetic on a passed-in timestamp.
IST_OFFSET_SECONDS = 5 * HOUR + 30 * 60

SCENARIOS: tuple[str, ...] = ("normal", "bank_outage")

METHODS: tuple[str, ...] = ("upi", "card", "netbanking", "wallet")

# UPI handles are separate entities from the underlying bank and independently affect
# the outcome — research/04 §4.6 lists psp_handle as a signal with no card analogue.
VPA_HANDLES: tuple[str, ...] = ("@oksbi", "@ybl", "@paytm", "@okhdfcbank", "@okaxis", "@apl")

BANKS: tuple[str, ...] = ("SBIN", "HDFC", "ICIC", "UTIB", "PUNB", "KKBK")

CARD_NETWORKS: tuple[str, ...] = ("VISA", "MASTERCARD", "RUPAY")

WALLETS: tuple[str, ...] = ("payzapp", "freecharge", "mobikwik")

# The PSP the `bank_outage` scenario takes down. Named so the demo can point at it.
OUTAGE_HANDLE = "@oksbi"

CONFIG: dict[str, object] = {
    # Expected downtime events per rail across a 30-day window under `normal`.
    # Assumption (ours): Razorpay publishes no downtime frequency statistics. Roughly
    # one event per rail per day, which is the order NPCI's bank-wise UPI decline
    # publication implies — several banks are degraded on any given day, and banks
    # run nightly maintenance windows. Set low enough that outage-caused failures
    # stay a minority, so SWITCH_RAIL discriminates rather than dominates.
    "normal_events_per_rail_per_30d": {
        "upi": 30.0,
        "card": 18.0,
        "netbanking": 30.0,
        "wallet": 14.0,
    },
    # Severity mix under `normal`. No high-severity events: a high-severity window is
    # what the `bank_outage` scenario exists to introduce.
    "normal_severity_mix": (("low", 0.6), ("medium", 0.4)),
    # Share of events scoped to a whole method rather than one instrument.
    # Assumption (ours): NPCI switch and gateway-level degradation affect everyone on
    # the rail, issuer and PSP events do not.
    "scope_all_share": 0.35,
    # Event duration in minutes, (min, max) per severity. Assumption (ours).
    "duration_minutes": {"low": (45, 180), "medium": (90, 360), "high": (120, 420)},
    # research/04 §4.6: UPI success sags during evening peaks as banks contend under
    # simultaneous load. Modelled as 19:00-22:00 IST.
    "peak_window_ist": (19, 22),
    # `bank_outage`: one high-severity window on OUTAGE_HANDLE, starting on this day
    # of the run at this IST hour, for this many hours. Placed inside the evening peak
    # so the scenario stacks the two effects the demo is about.
    "bank_outage_day": 12,
    "bank_outage_start_hour_ist": 19,
    "bank_outage_hours": 6,
}


def derive_rng(seed: int, *parts: object) -> random.Random:
    """A generator seeded by a hash of stable keys, never by a shared sequence.

    Every draw in the simulator comes from one of these. Two runs with the same seed
    agree, and — critically for Stage 3 — adding a draw in one place does not shift
    the draws anywhere else. (I-13)
    """
    key = "|".join([str(seed), *(str(p) for p in parts)])
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    return random.Random(int.from_bytes(digest, "big"))


def ist_hour(at_ts: int) -> int:
    """Hour of the Indian civil day, 0-23. Pure arithmetic on a passed-in timestamp."""
    return ((at_ts + IST_OFFSET_SECONDS) // HOUR) % 24


def is_peak_window(at_ts: int) -> bool:
    low, high = CONFIG["peak_window_ist"]  # type: ignore[misc]
    return low <= ist_hour(at_ts) < high


@dataclass(frozen=True, slots=True)
class RailHealth:
    """Point-in-time view over a downtime feed.

    This is *observable* state — a real recovery system reads the Downtime API — so
    unlike the world's latent state it is fine for the decision path to consult.
    """

    events: tuple[Downtime, ...]

    @classmethod
    def from_events(cls, events: Iterable[Downtime]) -> "RailHealth":
        return cls(tuple(events))

    def active_at(self, at_ts: int) -> list[Downtime]:
        return [
            e for e in self.events if e.begin <= at_ts and (e.end is None or e.end > at_ts)
        ]

    def severity_at(
        self, method: str, at_ts: int, instruments: Sequence[str | None] = ()
    ) -> str | None:
        """Worst severity affecting this method and these instruments, if any.

        ``instruments`` is whatever identifies the payment on that rail — a VPA
        handle, a bank code, a card network. A ``scope='all'`` event matches the
        whole method regardless.
        """
        wanted = {i for i in instruments if i}
        worst: str | None = None
        for event in self.active_at(at_ts):
            if event.method != method:
                continue
            if event.scope != "all" and event.instrument not in wanted:
                continue
            worst = _worse(worst, event.severity)
        return worst


_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}


def _worse(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if _SEVERITY_RANK[a] >= _SEVERITY_RANK[b] else b


def _instruments_for(method: str) -> tuple[tuple[str, str], ...]:
    """(scope, instrument) pairs a downtime event may be scoped to, per method."""
    if method == "upi":
        return tuple(("psp", h) for h in VPA_HANDLES)
    if method == "netbanking":
        return tuple(("issuer", b) for b in BANKS)
    if method == "card":
        return tuple(("network", n) for n in CARD_NETWORKS) + tuple(
            ("issuer", b) for b in BANKS
        )
    return tuple(("wallet", w) for w in WALLETS)


def _pick_severity(rng: random.Random) -> str:
    roll = rng.random()
    cumulative = 0.0
    mix: tuple[tuple[str, float], ...] = CONFIG["normal_severity_mix"]  # type: ignore[assignment]
    for severity, weight in mix:
        cumulative += weight
        if roll < cumulative:
            return severity
    return mix[-1][0]


def generate_downtimes(
    *, days: int, seed: int, scenario: str, start_ts: int
) -> list[Downtime]:
    """The outage timeline for one run. Deterministic in ``seed``."""
    if scenario not in SCENARIOS:
        raise ValueError(f"scenario must be one of {list(SCENARIOS)}, got {scenario!r}")

    events: list[Downtime] = []
    rates: dict[str, float] = CONFIG["normal_events_per_rail_per_30d"]  # type: ignore[assignment]
    durations: dict[str, tuple[int, int]] = CONFIG["duration_minutes"]  # type: ignore[assignment]

    for method in METHODS:
        rng = derive_rng(seed, "rails", scenario, method)
        expected = rates[method] * days / 30.0
        count = _poisson(rng, expected)
        candidates = _instruments_for(method)
        share: float = CONFIG["scope_all_share"]  # type: ignore[assignment]
        for index in range(count):
            if rng.random() < share:
                scope, instrument = "all", None
            else:
                scope, instrument = candidates[rng.randrange(len(candidates))]
            severity = _pick_severity(rng)
            low, high = durations[severity]
            begin = start_ts + rng.randrange(days * DAY)
            end = begin + rng.randint(low, high) * 60
            events.append(
                Downtime(
                    id=stable_id("down_", seed, scenario, method, instrument, begin, index),
                    method=method,
                    scope=scope,
                    instrument=instrument,
                    severity=severity,
                    status="resolved" if end <= start_ts + days * DAY else "started",
                    begin=begin,
                    end=end,
                )
            )

    if scenario == "bank_outage":
        events.append(_bank_outage_window(seed=seed, start_ts=start_ts))

    events.sort(key=lambda e: (e.begin, e.id))
    return events


def _bank_outage_window(*, seed: int, start_ts: int) -> Downtime:
    """One high-severity PSP window — the scenario the rail-switch demo turns on."""
    day: int = CONFIG["bank_outage_day"]  # type: ignore[assignment]
    hour: int = CONFIG["bank_outage_start_hour_ist"]  # type: ignore[assignment]
    hours: int = CONFIG["bank_outage_hours"]  # type: ignore[assignment]
    begin = start_ts + day * DAY + hour * HOUR - IST_OFFSET_SECONDS
    return Downtime(
        id=stable_id("down_", seed, "bank_outage", OUTAGE_HANDLE, begin),
        method="upi",
        scope="psp",
        instrument=OUTAGE_HANDLE,
        severity="high",
        status="started",
        begin=begin,
        end=begin + hours * HOUR,
    )


def _poisson(rng: random.Random, lam: float) -> int:
    """Knuth's method. Small lambdas only, which is all this module produces."""
    if lam <= 0:
        return 0
    target = 2.718281828459045 ** -lam
    count, product = 0, 1.0
    while True:
        product *= rng.random()
        if product <= target:
            return count
        count += 1
        if count > 200:  # guard; unreachable for the lambdas in CONFIG
            return count

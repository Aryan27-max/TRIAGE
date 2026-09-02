"""Deterministic population generator.

Produces the payment stream **once**. Stage 3 splits this population across arms by
assignment; it never regenerates per arm, because two arms facing different worlds
are not comparable. (I-13) Cases are therefore written with ``arm`` unset and
``state = RECEIVED`` — undiagnosed. Diagnosis is the executor's job, and running it is
Stage 3's.

Every draw comes from a generator keyed on stable identifiers, so:

* two runs with the same seed produce byte-identical rows, and
* adding payments or customers does not shift the ones already there.

Nothing here reads a clock. The run window starts at an explicit ``start_ts``.

    python -m src.simulator.generate --n 2000 --days 30 --seed 42 --scenario normal
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from src.executor.runner import DEFAULT_DROP_DEAD_DAYS, DEFAULT_MAX_ATTEMPTS
from src.executor.state import RECEIVED, open_case
from src.simulator.rails import (
    CARD_NETWORKS,
    DAY,
    SCENARIOS,
    RailHealth,
    derive_rng,
    generate_downtimes,
)
from src.simulator.world import INITIAL_ATTEMPT, World
from src.store import db

# 2025-01-01 00:00:00 UTC. Fixed, not read from a clock: a run must be reproducible
# next month as well as today.
DEFAULT_START_TS = 1735689600

CONFIG: dict[str, object] = {
    # Assumption (ours): UPI-dominant mix, in line with UPI's share of Indian online
    # payment volume. Razorpay publishes no per-merchant method split.
    "method_mix": (("upi", 0.55), ("card", 0.25), ("netbanking", 0.12), ("wallet", 0.08)),
    # Customers must repeat across payments or there is no history for Stage 4's
    # rolling features to compute. Assumption (ours): ~4 payments per customer.
    "payments_per_customer": 4,
    "payments_per_merchant": 40,
    # Ticket size in paise by merchant band. Money is integer paise throughout.
    "amount_paise_by_band": {
        "small": (9_900, 150_000),
        "medium": (150_000, 900_000),
        "large": (900_000, 2_500_000),
    },
}


@dataclass(frozen=True, slots=True)
class GenerateResult:
    seed: int
    scenario: str
    days: int
    start_ts: int
    end_ts: int
    payments: int
    failures: int
    cases: int
    downtimes: int
    by_error_code: Counter[str] = field(default_factory=Counter)
    by_method: Counter[str] = field(default_factory=Counter)

    @property
    def failure_rate(self) -> float:
        return self.failures / self.payments if self.payments else 0.0


def _weighted_pick(rng, options: tuple[tuple[str, float], ...]) -> str:
    roll, cumulative = rng.random(), 0.0
    for value, weight in options:
        cumulative += weight
        if roll < cumulative:
            return value
    return options[-1][0]


def _rail_for(method: str, profile: dict[str, object], rng) -> str:
    """The instrument-level route, as distinct from the method.

    A UPI payment goes down a specific handle and a card down a specific network;
    research/04 §4.6 makes the point that the handle affects the outcome independently
    of the bank behind it.
    """
    if method == "upi":
        return str(profile["vpa_handle"])
    if method == "netbanking":
        return str(profile["payer_bank"])
    if method == "card":
        return CARD_NETWORKS[rng.randrange(len(CARD_NETWORKS))]
    return ("payzapp", "freecharge", "mobikwik")[rng.randrange(3)]


def generate(
    conn: sqlite3.Connection,
    *,
    n_payments: int,
    days: int,
    seed: int,
    scenario: str = "normal",
    start_ts: int = DEFAULT_START_TS,
) -> GenerateResult:
    """Write the population. Returns counts; prints nothing."""
    if scenario not in SCENARIOS:
        raise ValueError(f"scenario must be one of {list(SCENARIOS)}, got {scenario!r}")
    if n_payments <= 0 or days <= 0:
        raise ValueError("n_payments and days must both be positive")

    end_ts = start_ts + days * DAY
    downtimes = generate_downtimes(days=days, seed=seed, scenario=scenario, start_ts=start_ts)
    world = World(
        seed=seed,
        scenario=scenario,
        start_ts=start_ts,
        health=RailHealth.from_events(downtimes),
    )

    per_customer: int = CONFIG["payments_per_customer"]  # type: ignore[assignment]
    per_merchant: int = CONFIG["payments_per_merchant"]  # type: ignore[assignment]
    n_customers = max(1, n_payments // per_customer)
    n_merchants = max(1, n_payments // per_merchant)
    bands: dict[str, tuple[int, int]] = CONFIG["amount_paise_by_band"]  # type: ignore[assignment]
    method_mix: tuple[tuple[str, float], ...] = CONFIG["method_mix"]  # type: ignore[assignment]

    payments: list[db.Payment] = []
    cases: list[tuple[db.Case, int]] = []
    by_error_code: Counter[str] = Counter()
    by_method: Counter[str] = Counter()

    for index in range(n_payments):
        rng = derive_rng(seed, "payment", index)
        customer_id = db.stable_id("cust_", rng.randrange(n_customers), length=6)
        merchant_id = db.stable_id("mch_", rng.randrange(n_merchants), length=6)
        profile = world.customer_profile(customer_id)
        band = str(world.merchant_profile(merchant_id)["ticket_band"])

        method = _weighted_pick(rng, method_mix)
        rail = _rail_for(method, profile, rng)
        low, high = bands[band]
        amount_paise = rng.randrange(low, high, 100)
        created_at = start_ts + rng.randrange(days * DAY)
        payment_id = db.stable_id("pay_", seed, index)

        outcome = world.attempt(
            db.CaseView(
                id=payment_id,
                customer_id=customer_id,
                merchant_id=merchant_id,
                method=method,
                rail=rail,
                amount_paise=amount_paise,
                error_code=None,
                failed_at=created_at,
                attempt_number=1,
            ),
            INITIAL_ATTEMPT,
            None,
            created_at,
        )

        payment = db.Payment(
            id=payment_id,
            customer_id=customer_id,
            merchant_id=merchant_id,
            method=method,
            rail=rail,
            amount_paise=amount_paise,
            created_at=created_at,
            first_outcome="success" if outcome.success else "failed",
            first_error_code=outcome.error_code,
        )
        payments.append(payment)
        by_method[method] += 1

        if outcome.success or outcome.error_code is None:
            continue

        by_error_code[outcome.error_code] += 1
        cases.append(
            (
                db.Case(
                    id=db.stable_id("case_", payment_id),
                    payment_id=payment_id,
                    customer_id=customer_id,
                    merchant_id=merchant_id,
                    method=method,
                    rail=rail,
                    amount_paise=amount_paise,
                    error_code=outcome.error_code,
                    error_source=outcome.error_source,
                    failed_at=created_at,
                    # Observable: a real gateway holds all three on the payment.
                    # Stored now so Stage 4's features need no schema migration.
                    city_tier=int(profile["city_tier"]),  # type: ignore[arg-type]
                    vpa_handle=str(profile["vpa_handle"]),
                    payer_bank=str(profile["payer_bank"]),
                    state=RECEIVED,
                    arm=None,  # Stage 3 assigns. Never regenerated per arm. (I-13)
                    max_attempts=DEFAULT_MAX_ATTEMPTS,
                    drop_dead_at=created_at + DEFAULT_DROP_DEAD_DAYS * DAY,
                    next_attempt_at=None,
                    status_resolved_at=None,
                    nudge_sent_at=None,
                    recovered_at=None,
                    recovered_amount_paise=None,
                    created_at=created_at,
                ),
                created_at,
            )
        )

    payments.sort(key=lambda p: (p.created_at, p.id))
    cases.sort(key=lambda pair: (pair[1], pair[0].id))

    db.insert_payments(conn, payments)
    for case, failed_at in cases:
        open_case(conn, case, now=failed_at, actor="simulator", reason="payment.failed")
    db.insert_downtimes(conn, downtimes)
    conn.commit()

    return GenerateResult(
        seed=seed,
        scenario=scenario,
        days=days,
        start_ts=start_ts,
        end_ts=end_ts,
        payments=len(payments),
        failures=len(cases),
        cases=len(cases),
        downtimes=len(downtimes),
        by_error_code=by_error_code,
        by_method=by_method,
    )


# -- CLI ----------------------------------------------------------------------


def _report(result: GenerateResult, conn: sqlite3.Connection, db_path: Path) -> str:
    """Human-readable run summary. Maps codes to action classes for the breakdown."""
    from src.policy.engine import ACTIONS, PolicyEngine  # display only

    engine = PolicyEngine().load()
    by_action: Counter[str] = Counter()
    for code, count in result.by_error_code.items():
        by_action[engine.resolve(code).action] += count

    recoverable = sum(
        count for code, count in result.by_error_code.items()
        if engine.resolve(code).recoverable
    )

    lines = [
        f"TRIAGE simulator — scenario={result.scenario} seed={result.seed} days={result.days}",
        f"db            {db_path}",
        f"window        {result.start_ts} .. {result.end_ts}",
        "",
        f"payments      {result.payments}",
        f"failed        {result.failures}  ({result.failure_rate:.1%})",
        f"cases opened  {result.cases}  (state=RECEIVED, arm unassigned)",
        f"downtimes     {result.downtimes}",
        "",
        "by method",
    ]
    for method, count in result.by_method.most_common():
        lines.append(f"  {method:<12} {count:>6}")

    lines += ["", "by action class"]
    for action in ACTIONS:
        count = by_action.get(action, 0)
        share = count / result.failures if result.failures else 0.0
        lines.append(f"  {action:<20} {count:>5}  {share:>6.1%}")
    lines.append(
        f"  {'recoverable':<20} {recoverable:>5}  "
        f"{recoverable / result.failures if result.failures else 0:>6.1%}"
    )

    lines += ["", f"by error code ({len(result.by_error_code)} distinct)"]
    for code, count in result.by_error_code.most_common(15):
        entry = engine.resolve(code)
        lines.append(f"  {code:<40} {count:>5}  {entry.action}")
    if len(result.by_error_code) > 15:
        lines.append(f"  ... {len(result.by_error_code) - 15} more")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.simulator.generate",
        description="Generate a deterministic payment population for TRIAGE.",
    )
    parser.add_argument("--n", type=int, default=2000, help="number of payments")
    parser.add_argument("--days", type=int, default=30, help="length of the run window")
    parser.add_argument("--seed", type=int, default=42, help="every draw derives from this")
    parser.add_argument("--scenario", choices=SCENARIOS, default="normal")
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument("--start-ts", type=int, default=DEFAULT_START_TS)
    parser.add_argument(
        "--keep", action="store_true", help="append instead of dropping existing tables"
    )
    args = parser.parse_args(argv)

    conn = db.connect(args.db)
    if args.keep:
        db.init_db(conn)
    else:
        db.reset_db(conn)

    result = generate(
        conn,
        n_payments=args.n,
        days=args.days,
        seed=args.seed,
        scenario=args.scenario,
        start_ts=args.start_ts,
    )
    print(_report(result, conn, args.db))
    conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

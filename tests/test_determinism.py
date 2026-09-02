"""Determinism, and the property Stage 3's arm comparison rests on.

Two separate claims:

1. **Same seed, byte-identical population.** Without this, two runs are two different
   worlds and nothing can be compared across them.

2. **Per-attempt randomness is keyed, not sequential.** Resolving attempt 3 of one
   case must not depend on how many attempts any other case took. This is the one
   that is easy to get wrong and invisible when you do: with a shared sequential RNG,
   an arm that takes one extra attempt shifts every subsequent draw for every other
   arm, and the arms are no longer facing the same world. That violates I-13, and it
   surfaces only as results that look strange with no explanation.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from src.simulator.generate import DEFAULT_START_TS, generate
from src.simulator.rails import RailHealth, derive_rng, generate_downtimes
from src.simulator.world import INITIAL_ATTEMPT, World
from src.store import db
from tests.conftest import SEED

N = 200
DAYS = 30


def _make_world(seed: int = SEED, scenario: str = "normal") -> World:
    events = generate_downtimes(
        days=DAYS, seed=seed, scenario=scenario, start_ts=DEFAULT_START_TS
    )
    return World(
        seed=seed,
        scenario=scenario,
        start_ts=DEFAULT_START_TS,
        health=RailHealth.from_events(events),
    )


def _population_digest(path: Path, seed: int = SEED, scenario: str = "normal") -> str:
    conn = db.open_db(path)
    try:
        generate(
            conn, n_payments=N, days=DAYS, seed=seed, scenario=scenario,
            start_ts=DEFAULT_START_TS,
        )
        digest = hashlib.blake2b(digest_size=16)
        for table, order in (
            ("payments", "id"),
            ("cases", "id"),
            ("downtimes", "id"),
        ):
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}"):
                digest.update(repr(tuple(row)).encode("utf-8"))
        return digest.hexdigest()
    finally:
        conn.close()


# -- the population -----------------------------------------------------------


def test_same_seed_gives_an_identical_population(tmp_path: Path) -> None:
    first = _population_digest(tmp_path / "a.db")
    second = _population_digest(tmp_path / "b.db")
    assert first == second


def test_a_different_seed_gives_a_different_population(tmp_path: Path) -> None:
    assert _population_digest(tmp_path / "c.db", seed=SEED) != _population_digest(
        tmp_path / "d.db", seed=SEED + 1
    )


def test_a_different_scenario_gives_a_different_population(tmp_path: Path) -> None:
    assert _population_digest(tmp_path / "e.db") != _population_digest(
        tmp_path / "f.db", scenario="bank_outage"
    )


def test_downtimes_are_deterministic() -> None:
    first = generate_downtimes(
        days=DAYS, seed=SEED, scenario="normal", start_ts=DEFAULT_START_TS
    )
    second = generate_downtimes(
        days=DAYS, seed=SEED, scenario="normal", start_ts=DEFAULT_START_TS
    )
    assert first == second


def test_ids_are_derived_not_random() -> None:
    assert db.stable_id("case_", "pay_X") == db.stable_id("case_", "pay_X")
    assert db.stable_id("case_", "pay_X") != db.stable_id("case_", "pay_Y")


# -- keyed randomness ---------------------------------------------------------


def test_derive_rng_is_reproducible() -> None:
    a = derive_rng(SEED, "attempt", "case_A", 3).random()
    b = derive_rng(SEED, "attempt", "case_A", 3).random()
    assert a == b


def test_derive_rng_separates_keys() -> None:
    draws = {
        derive_rng(SEED, "attempt", "case_A", n).random() for n in range(1, 6)
    }
    assert len(draws) == 5


def _resolve(world: World, case_id: str, attempt_number: int):
    return world.attempt(
        db.CaseView(
            id=case_id,
            customer_id="cust_DET",
            merchant_id="mch_DET",
            method="upi",
            rail="@oksbi",
            amount_paise=499000,
            error_code="insufficient_funds",
            failed_at=DEFAULT_START_TS,
            attempt_number=attempt_number,
        ),
        "RETRY_SCHEDULED",
        None,
        DEFAULT_START_TS + 10 * 86400,
    )


def test_attempt_three_is_unaffected_by_other_cases(tmp_path: Path) -> None:
    """The Stage 3 property, stated directly.

    Arm A resolves attempt 3 of case_X having touched nothing else. Arm B resolves
    the same attempt after running fifty attempts across other cases. With a shared
    sequential RNG the two would differ; with keyed derivation they cannot.
    """
    quiet = _make_world()
    busy = _make_world()

    for index in range(50):
        _resolve(busy, f"case_NOISE_{index}", (index % 4) + 1)

    assert _resolve(quiet, "case_X", 3) == _resolve(busy, "case_X", 3)


def test_attempt_order_within_a_case_does_not_matter() -> None:
    forwards = _make_world()
    backwards = _make_world()
    ascending = [_resolve(forwards, "case_Y", n) for n in (1, 2, 3, 4)]
    descending = [_resolve(backwards, "case_Y", n) for n in (4, 3, 2, 1)]
    assert ascending == list(reversed(descending))


def test_a_fresh_world_answers_identically() -> None:
    # The API rebuilds the World per request. That must change nothing: latent state
    # is derived from (seed, customer_id), never accumulated across calls.
    assert _resolve(_make_world(), "case_Z", 2) == _resolve(_make_world(), "case_Z", 2)


def test_latent_state_is_stable_across_instances() -> None:
    first, second = _make_world(), _make_world()
    for customer in ("cust_1", "cust_2", "cust_3"):
        assert first.customer_profile(customer) == second.customer_profile(customer)


def test_adding_customers_does_not_shift_existing_ones() -> None:
    """Latent state is keyed on customer_id, so the population can grow safely."""
    baseline = _make_world()
    profiles = {c: baseline.customer_profile(c) for c in ("cust_1", "cust_2")}

    grown = _make_world()
    for index in range(200):  # touch many other customers first
        grown.customer_profile(f"cust_EXTRA_{index}")

    for customer, profile in profiles.items():
        assert grown.customer_profile(customer) == profile


@pytest.mark.parametrize("attempt_number", [1, 2, 3, 4])
def test_the_initial_attempt_does_not_collide_with_case_attempts(
    attempt_number: int,
) -> None:
    # generate() resolves origination against the payment id; the case's own attempts
    # are keyed on the case id. Distinct keys, so no draw is ever reused.
    world = _make_world()
    payment_view = db.CaseView(
        id="pay_COLLIDE",
        customer_id="cust_DET",
        merchant_id="mch_DET",
        method="upi",
        rail="@oksbi",
        amount_paise=499000,
        error_code=None,
        failed_at=DEFAULT_START_TS,
        attempt_number=attempt_number,
    )
    origination = world.attempt(payment_view, INITIAL_ATTEMPT, None, DEFAULT_START_TS)
    case_attempt = _resolve(world, "case_COLLIDE", attempt_number)
    # Not asserting they differ — they might coincide by chance — only that the keys
    # are distinct, which is what the derivation guarantees.
    assert derive_rng(SEED, "attempt", "pay_COLLIDE", attempt_number).random() != (
        derive_rng(SEED, "attempt", "case_COLLIDE", attempt_number).random()
    )
    assert origination is not None and case_attempt is not None

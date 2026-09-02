"""Shared fixtures.

The policy engine is session-scoped: loading the table is pure I/O with no mutable
state, so 110 rows are parsed once for the whole run. Everything that *writes* —
the store, the app, the world — is per-test, so no test can see another's rows.

Tests that need a different policy table build their own engine against a tmp_path
copy rather than mutating the shared one.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from src.api.main import create_app
from src.executor.runner import Runner
from src.policy.engine import DEFAULT_POLICY_PATH, PolicyEngine, PolicyEntry
from src.simulator.generate import DEFAULT_START_TS
from src.simulator.rails import RailHealth, generate_downtimes
from src.simulator.world import World
from src.store import db

SEED = 42
DAY = 86400

# A moment inside the run window, away from the evening peak and away from a salary
# date, so fixtures are not accidentally sitting on a latent edge case.
NOW = DEFAULT_START_TS + 15 * DAY + 11 * 3600


@pytest.fixture(scope="session")
def policy_path() -> Path:
    return DEFAULT_POLICY_PATH


@pytest.fixture(scope="session")
def raw_policy(policy_path: Path) -> dict[str, Any]:
    """The table straight off disk, before the engine touches it."""
    return json.loads(policy_path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def engine(policy_path: Path) -> PolicyEngine:
    return PolicyEngine(policy_path).load()


@pytest.fixture(scope="session")
def entries(engine: PolicyEngine) -> list[PolicyEntry]:
    return engine.list_entries()


# -- store and executor -------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "triage_test.db"


@pytest.fixture()
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.open_db(db_path)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def runner(engine: PolicyEngine) -> Runner:
    return Runner(engine)


@pytest.fixture()
def world() -> World:
    events = generate_downtimes(
        days=30, seed=SEED, scenario="normal", start_ts=DEFAULT_START_TS
    )
    return World(
        seed=SEED,
        scenario="normal",
        start_ts=DEFAULT_START_TS,
        health=RailHealth.from_events(events),
    )


# -- API ----------------------------------------------------------------------


@pytest.fixture()
def client(policy_path: Path, db_path: Path) -> Iterator[TestClient]:
    """TestClient as a context manager, so the lifespan actually runs."""
    app = create_app(policy_path, db_path=db_path, sim_seed=SEED)
    with TestClient(app) as test_client:
        yield test_client


def open_case(
    client: TestClient,
    *,
    error_code: str,
    payment_id: str = "pay_TEST0001",
    method: str = "upi",
    amount: int = 499000,
    failed_at: int = NOW,
    customer_id: str = "cust_TEST01",
    merchant_id: str = "mch_TEST01",
) -> dict[str, Any]:
    """Open one case through the API and return the response body."""
    response = client.post(
        "/v1/recovery/cases",
        json={
            "payment_id": payment_id,
            "error_code": error_code,
            "method": method,
            "amount": amount,
            "failed_at": failed_at,
            "source": "bank",
            "customer": {"id": customer_id},
            "merchant": {"id": merchant_id},
        },
    )
    assert response.status_code in (200, 201), response.text
    return response.json()

"""Shared fixtures.

The engine is session-scoped: loading the table is pure I/O with no mutable state, so
110 rows are parsed once for the whole run. Tests that need a *different* table build
their own engine against a tmp_path copy rather than mutating this one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from src.api.main import create_app
from src.policy.engine import DEFAULT_POLICY_PATH, PolicyEngine, PolicyEntry


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


@pytest.fixture(scope="session")
def client(policy_path: Path) -> Iterator[TestClient]:
    """TestClient as a context manager, so the lifespan actually runs."""
    with TestClient(create_app(policy_path)) as test_client:
        yield test_client

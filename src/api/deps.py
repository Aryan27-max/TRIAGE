"""Request-scoped dependencies.

The policy engine is loaded once at startup and shared. Everything else is built per
request: a SQLite connection that commits on a clean exit, and a World rebuilt from
whatever downtimes are currently in the store — so injecting a downtime event through
``POST /v1/rails/health`` changes the next attempt's outcome immediately, which is
what the rail-switch demo needs.

Rebuilding the World per request costs nothing and changes nothing: its latent state
is derived from ``(seed, customer_id)``, not accumulated, so a fresh instance answers
identically to a long-lived one.
"""

from __future__ import annotations

import sqlite3
from typing import Iterator

from fastapi import Depends, Request

from src.executor.runner import Runner
from src.policy.engine import PolicyEngine
from src.simulator.rails import RailHealth
from src.simulator.world import World
from src.store import db


def get_engine(request: Request) -> PolicyEngine:
    """The single engine instance the lifespan loaded."""
    return request.app.state.policy_engine


def get_runner(request: Request) -> Runner:
    return request.app.state.runner


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = db.connect(request.app.state.db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_world(
    request: Request, conn: sqlite3.Connection = Depends(get_conn)
) -> World:
    return World(
        seed=request.app.state.sim_seed,
        scenario=request.app.state.sim_scenario,
        start_ts=request.app.state.sim_start_ts,
        health=RailHealth.from_events(db.list_downtimes(conn)),
    )

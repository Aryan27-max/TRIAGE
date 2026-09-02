"""FastAPI application.

The app owns exactly one PolicyEngine and one Runner, both built once in the lifespan
and hung off ``app.state``. If the policy table is broken the app refuses to boot: a
table that fails validation must be a startup failure, not a wrong decision discovered
later.

There is no server clock. Every write endpoint takes the current time as an explicit
field, because a 30-day simulation has to run in seconds and nothing in ``src/`` may
read ``datetime.now()``. ``tests/test_no_wall_clock.py`` enforces that.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api import routes_cases, routes_errors, routes_rails
from src.api.errors import TriageAPIError
from src.api.schemas import Health
from src.executor.runner import Runner
from src.policy.engine import DEFAULT_POLICY_PATH, PolicyEngine, PolicyLoadError
from src.simulator.generate import DEFAULT_START_TS
from src.store import db

DESCRIPTION = """
Razorpay publishes 110 payment failure reasons and explains what each one means. It
does not say what to do about them. TRIAGE is the decision layer.

**Stage 1** exposes the decision table read-only. **Stage 2** adds the case engine:
open a case from a failed payment, diagnose it against the table, and execute bounded
attempts with a full audit trail.

Only **27 of the 110** codes are recoverable without human intervention, so a naive
"retry three times" loop is wrong on roughly three quarters of failures.

**No server clock.** Write endpoints take `now` (unix seconds) explicitly so a
30-day simulation runs in seconds and every run is reproducible.

| HTTP | Code | When |
|---|---|---|
| 400 | `BAD_REQUEST_ERROR` | Unknown error code, or an unusable parameter |
| 404 | `NOT_FOUND_ERROR` | No such policy entry or case |
| 409 | `IDEMPOTENCY_CONFLICT` | Idempotency key reused — the double-charge guard |
| 422 | `POLICY_VIOLATION` | Would breach `max_attempts` or `drop_dead_at` |
| 423 | `AWAITING_STATUS` | Prior outcome unresolved; retrying risks a double charge |
"""


def create_app(
    policy_path: Path | str = DEFAULT_POLICY_PATH,
    *,
    db_path: Path | str = db.DEFAULT_DB_PATH,
    sim_seed: int = 42,
    sim_scenario: str = "normal",
    sim_start_ts: int = DEFAULT_START_TS,
) -> FastAPI:
    """Build an app bound to one policy table and one store. Tests point elsewhere."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = PolicyEngine(policy_path)
        try:
            engine.load()
        except PolicyLoadError as exc:
            # Refuse to serve. Every downstream stage reads this table.
            raise RuntimeError(
                f"TRIAGE cannot start: the policy table is invalid.\n{exc}"
            ) from exc
        app.state.policy_engine = engine
        app.state.runner = Runner(engine)
        app.state.db_path = Path(db_path)
        app.state.sim_seed = sim_seed
        app.state.sim_scenario = sim_scenario
        app.state.sim_start_ts = sim_start_ts

        # The schema is idempotent, so this is safe against an existing store and
        # gives a usable API on a fresh checkout before the simulator has ever run.
        conn = db.connect(db_path)
        try:
            db.init_db(conn)
        finally:
            conn.close()
        yield

    app = FastAPI(
        title="TRIAGE",
        description=DESCRIPTION,
        version="0.2.0",
        lifespan=lifespan,
    )

    # Wide open: the Stage 5 dashboard is a localhost Next.js app on another port.
    # This is a prototype with no auth and no customer data behind it.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(TriageAPIError)
    async def handle_triage_error(
        request: Request, exc: TriageAPIError
    ) -> JSONResponse:
        """The only place an error body is built. Routes raise; this serialises."""
        return JSONResponse(status_code=exc.status_code, content=exc.to_envelope())

    @app.get("/health", response_model=Health, tags=["meta"])
    def health(request: Request) -> Health:
        engine: PolicyEngine = request.app.state.policy_engine
        return Health(
            status="ok",
            policy_codes_loaded=len(engine),
            policy_version=engine.version,
        )

    app.include_router(routes_errors.router)
    app.include_router(routes_cases.router)
    app.include_router(routes_rails.router)
    return app


app = create_app()

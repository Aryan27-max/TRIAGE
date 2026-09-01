"""FastAPI application.

The app owns exactly one PolicyEngine, loaded once in the lifespan and hung off
``app.state``. If the policy table is broken the app refuses to boot: a table that
fails validation must be a startup failure, not a wrong decision discovered later.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api import routes_errors
from src.api.errors import TriageAPIError
from src.api.schemas import Health
from src.policy.engine import DEFAULT_POLICY_PATH, PolicyEngine, PolicyLoadError

DESCRIPTION = """
Razorpay publishes 110 payment failure reasons and explains what each one means. It
does not say what to do about them. TRIAGE is the decision layer.

**Stage 1** exposes the decision table read-only: what does policy say about error
code X? Deciding what to do about a specific payment arrives in Stage 2.

Only **27 of the 110** codes are recoverable without human intervention, so a naive
"retry three times" loop is wrong on roughly three quarters of failures.
"""


def create_app(policy_path: Path | str = DEFAULT_POLICY_PATH) -> FastAPI:
    """Build an app bound to one policy table. Tests can point it elsewhere."""

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
        yield

    app = FastAPI(
        title="TRIAGE",
        description=DESCRIPTION,
        version="0.1.0",
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
    return app


app = create_app()

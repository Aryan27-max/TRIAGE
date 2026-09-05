"""Deployment configuration, read from the environment.

A deployed instance is a **read-only** exhibit: the run databases are baked into the
image, the results are pre-computed, and nothing a visitor does may mutate them. That
is enforced here rather than by convention, because the alternative is a public
endpoint that can rewrite the numbers the submission reports.

Everything is optional and defaults to the local development behaviour, so nothing in
the test suite has to know this module exists.
"""

from __future__ import annotations

import os
from pathlib import Path

from src.store import db

REPO_ROOT = Path(__file__).resolve().parents[2]


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def read_only() -> bool:
    """True when this instance must never write to the store."""
    return _flag("TRIAGE_READ_ONLY", False)


def db_path() -> Path:
    """The main store. Overridable so verification can point at a scratch copy."""
    raw = os.environ.get("TRIAGE_DB_PATH")
    return Path(raw) if raw else db.DEFAULT_DB_PATH


def runs_dir() -> Path:
    """Where pre-computed evaluation runs live."""
    raw = os.environ.get("TRIAGE_DB_DIR")
    return Path(raw) if raw else REPO_ROOT / "eval" / "runs"


def cors_origins() -> list[str]:
    """Allowed origins, comma-separated, split at startup.

    Wide open locally, because the dashboard runs on another port and there is no
    auth and no data worth stealing. In production the deploy sets an explicit list —
    `*` on a public origin is a habit worth not forming even when it is harmless.

    ``ALLOWED_ORIGINS`` is the name to set. ``TRIAGE_CORS_ORIGINS`` is read as a
    fallback for deploy configs written before the rename; new configs should use
    ``ALLOWED_ORIGINS``.
    """
    raw = os.environ.get("ALLOWED_ORIGINS", "").strip()
    if not raw:
        raw = os.environ.get("TRIAGE_CORS_ORIGINS", "").strip()
    if raw:
        return [origin.strip() for origin in raw.split(",") if origin.strip()]
    return ["*"]


def sim_seed() -> int:
    return int(os.environ.get("TRIAGE_SIM_SEED", "42"))

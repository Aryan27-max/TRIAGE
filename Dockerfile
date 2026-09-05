# TRIAGE API — read-only exhibit.
#
# The evaluation runs are baked into the image and the container refuses to write to
# them: the numbers a visitor sees are the numbers in the report, and nothing reachable
# from the internet can change that.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first, so a source change does not re-resolve the environment.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra ml --no-install-project

# The decision table is the root of everything and is copied explicitly rather than
# swept up by a wildcard — if it were ever missing the app refuses to boot, and that
# should be a build-time surprise, not a runtime one.
COPY error_policy.json ./
COPY src/ ./src/
COPY eval/__init__.py eval/run_arms.py eval/score.py eval/report.py ./eval/
COPY eval/model/ ./eval/model/
COPY eval/runs/ ./eval/runs/

ENV PATH="/app/.venv/bin:${PATH}" \
    TRIAGE_READ_ONLY=true \
    TRIAGE_DB_DIR=/app/eval/runs \
    TRIAGE_DB_PATH=/app/eval/runs/exhibit.db \
    PORT=7860

# A writable scratch store for the read-only path to open. It is never written to;
# SQLite still wants a file to exist behind `mode=ro`.
RUN python -c "import sys; sys.path.insert(0,'.'); from src.store import db; db.open_db('/app/eval/runs/exhibit.db').close()"

# 7860 is Hugging Face Spaces' fixed convention (it does not inject $PORT itself).
# Render/Railway-style hosts still work: they inject their own $PORT at container
# start, which overrides this image-baked default — shell form so ${PORT} actually
# expands rather than being passed to uvicorn as a literal string.
EXPOSE 7860
CMD uvicorn src.api.main:app --host 0.0.0.0 --port ${PORT}

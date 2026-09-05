# STARTCMDS — how to run TRIAGE

Quick reference for starting the project locally. Full detail (retraining, deployment,
verification) is in the [README](README.md#running-it) — this is just the commands.

Run everything from the repo root unless a step says `cd`.

---

## 1. API (FastAPI + policy engine + simulator)

```bash
# One-time setup — installs deps (incl. ML extras for the model)
uv sync --extra dev --extra ml

# Generate a local payment population to decide against (skip if you already
# have a store, e.g. from eval/runs/)
uv run python -m src.simulator.generate --n 8000 --days 30 --seed 42

# Start the API
uv run uvicorn src.api.main:app --reload
```

- Docs: http://127.0.0.1:8000/docs
- Health: http://127.0.0.1:8000/health

## 2. Dashboard (Next.js)

```bash
cd dashboard
npm install              # skip if node_modules already exists
cp .env.local.example .env.local
npm run dev
```

- Dashboard: http://localhost:3000
- `.env.local` → `NEXT_PUBLIC_API_URL` must point at the running API (defaults to
  `http://127.0.0.1:8000`).

## 3. Run both together

Two terminals: one running the `uvicorn` command from step 1, one running `npm run dev`
from step 2. The dashboard talks to the API over HTTP; no shared process.

---

## Tests

```bash
uv run pytest -v --durations=20
```

## Reproduce the evaluation

```bash
uv run python -m eval.run_arms --seed 42 --scenario normal      --arms control,baseline,treatment
uv run python -m eval.run_arms --seed 42 --scenario bank_outage --arms control,baseline,treatment
```

## Retrain the model

```bash
uv run python -m eval.run_arms --seed 7 --scenario normal --n 40000 --arms baseline
uv run python -m src.model.train
```

## Verification scripts

```bash
bash scripts/verify_api.sh
uv run python scripts/verify_model.py
uv run python scripts/verify_determinism.py
```

---

## Notes

- `uv` and `npm` must both be on PATH (confirmed present in this environment: `uv 0.11.18`,
  `node v22`, `npm 10.9`).
- The API defaults to a local SQLite store (`TRIAGE_DB_PATH`) and is writable by default;
  set `TRIAGE_READ_ONLY=true` to mirror the read-only production deployment (see
  `.env.example`).
- No secrets are required for local dev. LLM explanation text (Stage 5, optional) reads
  its key from the environment — see `src/explain/llm.py` — nothing else needs one.

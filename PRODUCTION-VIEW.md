# Production view — where this deploys, and how

Two pieces to ship. No database service (Supabase or otherwise) is needed — see
**Why no Supabase** below.

| Piece | Platform | Cost | Config already in repo |
|---|---|---|---|
| API (FastAPI) | **Railway** | See **cost note** below — not fully free | [`Dockerfile`](Dockerfile), [`railway.json`](railway.json) |
| Dashboard (Next.js) | **Vercel** | Free (Hobby) | zero-config — standard Next.js app |
| Database | — none — | — | pre-computed SQLite baked into the image |

## Cost note — read this before you deploy

Railway does **not** have an ongoing free tier. New accounts get a one-time trial
credit (historically ~$5, non-renewing); once that's used, Railway requires a card on
file and bills the **Hobby plan** (a ~$5/month minimum, usage-based beyond that) to
keep a service running. This is a real difference from Render's free web-service tier,
which stays free indefinitely (with a cold-start trade-off) and was the platform this
project was built against. If keeping the API deployment genuinely free is the
priority, Render is still the better fit. This document switches to Railway as asked;
flagging the cost so it's an informed choice, not a surprise on a bill.

---

## Why no Supabase

TRIAGE's production instance is deliberately **read-only**: the two evaluation runs
(`eval/runs/run_DbCttM8e.db`, `eval/runs/run_gsoV0kLl.db`) are pre-computed, committed
to the repo, and baked into the Docker image at build time. `TRIAGE_READ_ONLY=true`
enforces two ways that nothing can write to them (a route dependency, and SQLite opened
with `mode=ro`) — see [`src/api/config.py`](src/api/config.py) and
[`src/api/deps.py`](src/api/deps.py). There is nothing for a managed Postgres instance
to do: no user accounts, no live case history that needs to survive a restart, no
multi-instance writes to coordinate.

The one endpoint that looks like it needs a database, `POST /v1/recovery/decide`, is
explicitly stateless — it resolves a policy + rail-health lookup and returns an answer
without touching storage, so it works fine in read-only mode.

**If you later want a live, writable instance** (real case history persisted across
restarts, not just the exhibit), that's the point where Supabase's free Postgres tier
would earn its place — a platform container's filesystem is ephemeral (Railway's
included), so a local SQLite write there vanishes on redeploy or restart. That would
mean swapping the SQLite calls in [`src/store/db.py`](src/store/db.py) for a Postgres
adapter. That's a real change to the storage layer, not a config toggle, and is out of
scope unless you decide you want a persistent live demo rather than the exhibit this
submission ships.

---

## 1. Push to GitHub

Already done — this repo is `Aryan27-max/TRIAGE` on GitHub. Both Railway and Vercel
deploy by connecting to that repo directly; no separate upload step.

## 2. Deploy the API — Railway

1. [railway.app](https://railway.app) → sign in with GitHub → **New Project → Deploy
   from GitHub repo** → select `Aryan27-max/TRIAGE`.
2. Railway detects the [`Dockerfile`](Dockerfile) at the repo root and the
   [`railway.json`](railway.json) alongside it (builder: `DOCKERFILE`, health check at
   `/health`, restart on failure). No root-directory setting needed — the Dockerfile
   already lives at the repo root, and it doesn't touch `dashboard/` at all.
3. **Unlike `render.yaml`, `railway.json` does not carry environment variable
   values** — Railway's config-as-code covers build/deploy settings only; secrets and
   env vars are set separately so they're never committed. Three of the four vars this
   API needs are already baked in as image defaults in the `Dockerfile` itself
   (`TRIAGE_READ_ONLY=true`, `TRIAGE_DB_DIR`, `TRIAGE_DB_PATH`), so nothing to do there.
   The one you must set by hand, in the Railway dashboard → your service →
   **Variables**:
   - `TRIAGE_CORS_ORIGINS` = `http://localhost:3000` for now — you'll add the real
     Vercel domain in step 4.
4. Railway injects `PORT` automatically; the Dockerfile's `CMD` already reads
   `${PORT:-8000}`, so no change needed there.
5. **Generate a public URL:** Railway does not expose a domain by default. Go to your
   service → **Settings → Networking → Generate Domain**. You'll get something like
   `https://triage-api-production.up.railway.app` — note it down.
6. Deploy. `GET /health` on that domain should return
   `{"status":"ok","read_only":true,...}`.

**Idle behaviour to expect:** Railway does not sleep a service the way Render's free
tier does — as long as your usage credit or Hobby plan is active, the API stays up
continuously (no cold-start wake state to design around). This also means it keeps
consuming usage/billing while it sits idle, which is the trade-off for not sleeping.

## 3. Deploy the dashboard — Vercel

Unchanged by the platform switch.

1. [vercel.com](https://vercel.com) → sign in with GitHub → **Add New → Project** →
   import `Aryan27-max/TRIAGE`.
2. **Root Directory: `dashboard`** — this is the one setting you must change; Vercel
   defaults to the repo root and this is a monorepo. Framework preset auto-detects
   Next.js once the root is set correctly.
3. Add an environment variable: `NEXT_PUBLIC_API_URL` = the Railway domain from step
   2.5 (e.g. `https://triage-api-production.up.railway.app`). This mirrors
   [`dashboard/.env.local.example`](dashboard/.env.local.example).
4. Deploy. Vercel assigns a domain like `triage-xyz.vercel.app`.

## 4. Close the loop — allow the dashboard's origin on the API

1. Railway → your service → **Variables**.
2. Set `TRIAGE_CORS_ORIGINS` to your actual Vercel domain(s), comma-separated, e.g.
   `https://triage-xyz.vercel.app` (add a custom domain here too if you attach one
   later).
3. Save — Railway redeploys automatically. Without this step the API's CORS
   middleware ([`src/api/main.py`](src/api/main.py)) will reject the dashboard's
   requests from the deployed origin (it works locally because `TRIAGE_CORS_ORIGINS`
   defaults to `*` when unset).

## 5. Verify

- `curl https://<railway-domain>/health` → `read_only: true`.
- Open the Vercel URL → **Live** screen should load rail health and let you submit a
  scenario without a CORS error in the browser console.
- **Results** and **Cases** screens should show the two baked-in runs.

---

## Production readiness — verified this session

The following was checked against the actual repo, not assumed:

| Check | Result |
|---|---|
| `uv run pytest` | **544 passed** |
| `next build` (dashboard) | Compiles clean, all 6 routes generate |
| API booted with production env vars (`TRIAGE_READ_ONLY=true`, pointed at `eval/runs/`) | `/health` reports `read_only: true`; `/v1/eval/runs` and `/v1/errors/{code}` serve correctly |
| Stateless decide endpoint in read-only mode | `POST /v1/recovery/decide` → `200` |
| Write guard in read-only mode | `POST /v1/recovery/cases` → `503 SERVICE_UNAVAILABLE`, correctly refused |
| Docker build | **Not run** — Docker Desktop's engine isn't available in this sandbox. The Dockerfile's `COPY` paths (`error_policy.json`, `src/`, `eval/__init__.py`, `eval/run_arms.py`, `eval/score.py`, `eval/report.py`, `eval/model/`, `eval/runs/`) were confirmed to exist; the equivalent runtime behaviour was verified by running the API directly with the same env vars the container sets. **Recommended:** run `docker build -t triage-api .` once locally, or trust Railway's own build log on first deploy, before relying on this for the demo. |

Note: `render.yaml` is still in the repo but is now unused by this deployment path —
left in place rather than deleted, since removing it wasn't asked for. `README.md`
still describes the Render deployment in its "Deployment" section; that wasn't in
scope for this change, but it will read inconsistently against this file until one of
them is updated — say the word and I'll bring it in line.

Nothing else changed. `error_policy.json`, the eval reports, and the two production
invariants (I-1 through I-17 in [`CLAUDE.md`](CLAUDE.md)) are untouched.

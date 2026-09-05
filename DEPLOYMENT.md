# Deployment — the HF Space / Vercel split

Two deploy targets, two repos. This file explains the split; it is not itself part of
the submission (the root [`README.md`](README.md) is — see `CLAUDE.md`).

| Piece | Host | Repo |
|---|---|---|
| API (FastAPI, Docker SDK) | Hugging Face Spaces | a **separate** git repo, staged at `hf-space/` |
| Dashboard (Next.js) | Vercel | this repo, `dashboard/` as the project root |

## Why a separate repo for the Space

A Hugging Face Space **is** a git repo — pushing to it means pushing to a remote HF
controls, with its own history. It is not a subfolder or a branch of this submission,
and its `README.md` needs YAML frontmatter (`sdk: docker`, `app_port: 7860`, …) that
must never land on this repo's root `README.md` — that file is the write-up a judge
reads, not a Spaces config block.

`hf-space/` is a **staging directory**: a plain, gitignored folder in this repo's
working tree (see `.gitignore`) that holds exactly the files the Space needs, copied
from this repo's actual source of truth. It is regenerated, not hand-maintained.

## Keeping it in sync

```bash
scripts/sync_hf_space.sh
```

Wipes and rebuilds `hf-space/` from the current working tree: `src/`,
`error_policy.json`, `pyproject.toml`, `uv.lock`, `Dockerfile`, `.dockerignore`,
the `eval/` modules the API imports, `eval/model/` (the trained model + its
sidecars), and the two committed exhibit runs (`eval/runs/run_DbCttM8e.db`,
`eval/runs/run_gsoV0kLl.db` — not the 12 MB training population, which was never
meant to ship; see `.gitignore`'s note on it). Writes `hf-space/README.md` with the
Spaces frontmatter and a short description, and a minimal `hf-space/.gitignore` for
when it becomes its own repo. Re-run it any time `src/`, the policy table, or the
model changes, before you push again.

This list is slightly broader than "src/, error_policy.json, model.txt, eval/runs/\*.db,
pyproject.toml, Dockerfile, .dockerignore" — that shorthand omits `uv.lock` (without
which `uv sync --frozen` in the Dockerfile fails outright), `feature_names.json` next
to `model.txt` (the scorer refuses to load one without the other — see
`src/model/score.py`), and the `eval/*.py` modules `src/api/routes_eval.py` imports.
The script copies what the Dockerfile's own `COPY` lines actually require, which is
the only definition of "enough to build" that matters.

## First-time setup — exact commands

Run these yourself; nothing here was pushed on your behalf (no HF or Vercel
credentials are available in this environment).

```bash
# 1. Generate the staging directory (if you haven't already)
scripts/sync_hf_space.sh

# 2. Create the Space on huggingface.co first (huggingface.co/new-space), or via CLI:
#    - SDK: Docker
#    - Visibility: your choice
#    Note the Space's git URL, e.g.:
#    https://huggingface.co/spaces/<your-username>/triage-api

# 3. Turn the staging directory into that Space's git repo
cd hf-space
git init
git add .
git commit -m "Deploy TRIAGE API"
git remote add origin https://huggingface.co/spaces/<your-username>/triage-api
git branch -M main
git push -u origin main
```

Pushing triggers HF's build of the same `Dockerfile` this repo uses. First build takes
a few minutes. The Space gets a public URL automatically —
`https://<your-username>-triage-api.hf.space` — no "generate domain" step required
(unlike some other Docker hosts).

## Environment variables

Three of the four the API reads are already baked into the `Dockerfile`'s own `ENV`
(`TRIAGE_READ_ONLY=true`, `TRIAGE_DB_DIR`, `TRIAGE_DB_PATH`, and now `PORT=7860` for
Spaces' fixed-port convention) — nothing to set for those unless you want to override
them. The one you must set **after your first deploy**, once you know the dashboard's
real domain:

**On the Space** → Settings → Variables and secrets:

| Variable | Value |
|---|---|
| `ALLOWED_ORIGINS` | your Vercel domain, e.g. `https://triage-xyz.vercel.app` (comma-separate more than one) |

**On Vercel** → Project → Settings → Environment Variables:

| Variable | Value |
|---|---|
| `NEXT_PUBLIC_API_URL` | your Space's URL, e.g. `https://<your-username>-triage-api.hf.space` |

Without the first, the API's CORS middleware won't allow the deployed dashboard's
requests (it works locally because `ALLOWED_ORIGINS` defaults to `*` when unset).
Without the second, the dashboard falls back to `http://127.0.0.1:8000` and will only
ever talk to a local API.

## Deploying the dashboard — Vercel

Unchanged from before: [vercel.com](https://vercel.com) → Add New → Project → import
this repo → **set Root Directory to `dashboard`** (this is a monorepo; Vercel defaults
to the repo root) → add `NEXT_PUBLIC_API_URL` as above → deploy.

## What's already correct and needed no change

Verified this session, not assumed — see the audit in the assistant's report for the
full detail:

- The dashboard already reads the API's base URL from `NEXT_PUBLIC_API_URL` through a
  single exported constant (`dashboard/lib/api.ts`), used everywhere, with a
  local-dev-only fallback. No hardcoded URL anywhere in `dashboard/`.
- `GET /health` already reports `read_only`. `POST /v1/simulator/run` (and every other
  write route) was already gated by the `require_writable` dependency, returning `503
  SERVICE_UNAVAILABLE` with a clear message on a read-only instance. There is no "run
  simulator" control in the dashboard's four screens, so there was nothing to hide.

## Superseded

`render.yaml` and `PRODUCTION-VIEW.md` describe an earlier Render/Railway-based plan
and are now superseded by this file for the API host. They're left in place rather
than deleted (removing them wasn't asked for), but Hugging Face Spaces + Vercel is the
deploy path this document and `scripts/sync_hf_space.sh` actually support.

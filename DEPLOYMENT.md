# Deployment

Two pieces. The backend is live; the dashboard is the remaining step.

| Piece | Host | Status |
|---|---|---|
| API (FastAPI, Docker) | **Railway** | **Deployed and active** — https://triage-api-production-00b4.up.railway.app |
| Dashboard (Next.js) | **Vercel** | To deploy — steps below |

Verify the backend at any time with `scripts/verify_live.sh`, which runs 25 assertions
against the live URL (not localhost): every action class through
`POST /v1/recovery/decide`, the 27-of-110 coverage figure, the 404 envelope, the
read-only refusal, and one committed evaluation report.

---

## Backend — Railway

### Redeploying

Railway watches the connected branch. **Push to it and Railway rebuilds and redeploys
automatically** — there is no separate deploy command:

```bash
git push origin main
```

The build uses this repo's `Dockerfile` directly. `railway.toml` sets the health check
so Railway waits for `GET /health` to answer before shifting traffic to the new deploy,
rather than treating "container started" as "ready".

### Environment variables

Set on the service's **Variables** tab, not in git.

| Variable | Value | Why |
|---|---|---|
| `TRIAGE_READ_ONLY` | `true` | Refuses every write. The evaluation runs are baked into the image and must stay exactly as reported — a public endpoint that can rewrite the submission's numbers is not a demo. Already set and verified live. |
| `ALLOWED_ORIGINS` | `<your-vercel-url>,http://localhost:3000` | Comma-separated, split at startup. Unset means `*`, which works but is a habit worth not forming on a public origin. |

**Do not set `PORT`.** Railway injects its own at container start and the Dockerfile's
shell-form `CMD` picks it up automatically. Setting it manually shadows the platform's
value, and the health check would never pass.

`TRIAGE_DB_DIR` and `TRIAGE_DB_PATH` are already baked into the image's `ENV` and need
no override.

---

## Frontend — Vercel

1. [vercel.com](https://vercel.com) → sign in with GitHub → **Add New → Project** →
   import this repository.
2. **Set Root Directory to `dashboard`.** This is a monorepo — the API is at the root
   and the Next.js app is one level down. Vercel defaults to the repo root and the
   build will fail without this. Once set, the Next.js preset is detected automatically.
3. Add an environment variable:

   | Name | Value |
   |---|---|
   | `NEXT_PUBLIC_API_URL` | `https://triage-api-production-00b4.up.railway.app` |

   Same value as `dashboard/.env.local.example`. Without it the dashboard falls back to
   `http://127.0.0.1:8000` and a deployed build would only ever talk to the visitor's
   own machine.
4. Deploy. Vercel assigns a URL like `triage-xyz.vercel.app`.

## After the Vercel URL exists — close the CORS loop

This is the one step that cannot be done in advance, because it needs a domain that
does not exist until step 4 above.

1. Railway → the `triage-api` service → **Variables**.
2. Set `ALLOWED_ORIGINS` to your real Vercel domain plus localhost for development:

   ```
   https://triage-xyz.vercel.app,http://localhost:3000
   ```

3. Save. Railway redeploys automatically.
4. Confirm: open the Vercel URL, load the **Live** screen, and check the browser
   console is free of CORS errors. `scripts/verify_live.sh` will still pass either way —
   CORS is enforced by the browser, not by the server refusing the request, so a
   curl-based check cannot catch this one.

---

## What the deployment guarantees

The instance is a **read-only exhibit**. The two 8000-payment evaluation runs are
committed to the repo, copied into the image at build time, and served as-is.
`TRIAGE_READ_ONLY=true` is enforced twice — by a route dependency (`require_writable`)
and by opening SQLite through `mode=ro` — so `POST /v1/simulator/run` and every other
write returns `503`. Nothing reachable from the internet can change the numbers the
submission reports.

`POST /v1/recovery/decide` stays available because it is stateless: it resolves the
policy table and rail health and returns an answer without touching storage. That is
what makes the dashboard's Live screen work against a read-only instance.

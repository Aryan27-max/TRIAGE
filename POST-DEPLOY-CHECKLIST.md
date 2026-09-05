# Post-deploy checklist

Two human actions remain: push the backend, then deploy the dashboard. In order.
Everything above this line has been verified locally and against the live API —
see `eval/PRE-PUSH-BASELINE.txt` for the pre-push state.

Railway API: `https://triage-api-production-00b4.up.railway.app`

---

### Backend

- [ ] **Check `eval/runs/` holds only the three expected files** before staging:

      ```bash
      git status --porcelain eval/runs/    # must be empty
      ```

      `scripts/verify_api.sh` exercises `POST /v1/simulator/run`, which writes a
      new run database into `eval/runs/`. Those strays are untracked, but a
      `git add -A` would commit them — and the Dockerfile does
      `COPY eval/runs/ ./eval/runs/`, so they would bloat the image and show up
      in `/v1/eval/runs` on the live API next to the real results. Delete any
      run id that isn't `run_DbCttM8e`, `run_gsoV0kLl` (committed results) or
      `run_1NXsztG1` (the gitignored training population).

- [ ] **`git push`**

- [ ] **Watch Railway's Deployments tab until the new build shows ACTIVE.**
      Don't move on while it says Building or Deploying — the next step would
      measure the old container.

- [ ] **Re-run `scripts/verify_live.sh` and diff against the baseline.**

      ```bash
      bash scripts/verify_live.sh > /tmp/after.txt 2>&1
      diff <(sed 's/\x1b\[[0-9;]*m//g' /tmp/after.txt) \
           <(tail -n +9 eval/PRE-PUSH-BASELINE.txt)
      ```

      Expected: no differences in the PASS/FAIL lines (the baseline's first 8
      lines are its header, hence `tail -n +9`). Baseline was **25 passed, 0
      failed, 0 config warnings**. Investigate any difference before continuing.

### Frontend

- [ ] **Import the repo into Vercel. Set Root Directory to `dashboard/`.**
      This is a monorepo — the API is at the root, the Next.js app one level
      down. Vercel defaults to the repo root and the build fails without this.

- [ ] **Set `NEXT_PUBLIC_API_URL` on Vercel** to:

      ```
      https://triage-api-production-00b4.up.railway.app
      ```

      This is baked in at build time, not read at runtime — so it must be set
      *before* the build, and changing it later requires a redeploy, not just a
      restart.

- [ ] **Deploy. Copy the assigned `*.vercel.app` production URL.**

### Close the CORS loop

- [ ] **On Railway, update `ALLOWED_ORIGINS`** to include that exact Vercel URL
      alongside the existing localhost entry:

      ```
      https://<your-app>.vercel.app,http://localhost:3000
      ```

      Railway restarts on a variable change automatically — no redeploy needed.
      Use the exact origin: scheme and host, no trailing slash, no path.

- [ ] **Open the Vercel URL and confirm the dashboard reaches the API.**
      Open DevTools → Network, load the **Live** screen, and look for the
      request to `/health` and `/v1/errors`: **200**, not a CORS error.

      `verify_live.sh` cannot catch this one — CORS is enforced by the browser,
      not by the server refusing the request, so a curl-based check passes
      whether or not the origin is allowed. The browser is the only real test.

---

### If the dashboard loads but every API call fails

In order of likelihood:

1. **CORS** — `ALLOWED_ORIGINS` doesn't contain the exact Vercel origin. The
   console says "No 'Access-Control-Allow-Origin' header". Fix on Railway's
   Variables tab.
2. **Wrong API URL baked in** — `NEXT_PUBLIC_API_URL` was missing or wrong at
   build time, so the bundle points at `127.0.0.1:8000`. The Network tab shows
   requests to localhost. Fix the variable and **redeploy** (a restart won't do
   it).
3. **Backend down** — check `https://triage-api-production-00b4.up.railway.app/health`
   directly. If that fails, it's Railway, not Vercel.

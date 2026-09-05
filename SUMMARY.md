# SUMMARY — build log

For a human returning after a break. One section per stage, compressed as they age but
never deleted. Decisions and gaps, not diffs. `CLAUDE.md` holds the invariants.

## Stage 1 — Skeleton and policy engine · complete

`error_policy.json` moved to the repo root, unmodified. `src/policy/engine.py` loads and
validates it; `src/api/` serves it read-only behind the Razorpay error envelope, with a
lifespan that refuses to boot on a broken table.

**Decisions.** The repo had no `.git` — git was walking up to a stray
TrustedInstaller-owned repo at `C:\`, so `git init` was run inside `TRIAGE/`. An unknown
code on `GET /v1/errors/{code}` returns 404, not I-2's 400: browsing the taxonomy for a
missing row is a different failure from submitting a bad code to a decision endpoint.
*(I-2 now says "in the decision path".)* Query parameters are validated by hand so every
failure leaves through the envelope.

**Tests — 75.** 110 unique codes, all eight actions at the exact counts, exactly 27
recoverable and those 27 precisely the three retrying classes — counts hard-coded, not
derived from the file under test. Near misses all raise, so fuzzy matching cannot creep in.

## Stage 2 — Simulator and case engine · complete

`src/store/` (five tables, UNIQUE index on `attempts.idempotency_key`, no REAL column),
`src/simulator/` (rails and downtime feed; `declines.py`'s cited CONFIG and causal
cascade; `world.py`'s latent state behind one resolution method), `src/executor/` (nine
states with edges as data, audit-before-state, bounds, polls), and the case API.

**Decisions.** No server clock: `now` is required on every write endpoint. `ESCALATED`
and `STOPPED` have no edge back to `SCHEDULED`, making I-4 structural rather than
conventional. The idempotency check runs **before** the bounds check — a replayed request
must be told it is a duplicate, or a double-charge attempt hides behind a 422. A failed
attempt re-diagnoses against the new code; the original stays on
`payments.first_error_code` so Stage 3 can segment by the cause that opened the case. IDs
derive from stable keys (`blake2b`), which is what makes a run byte-identical and
`POST /cases` idempotent without a lookup-then-insert race.

**Deviations.** `test_no_wall_clock.py` parses the AST rather than grepping: a text grep
fires on `main.py`'s own docstring, which names `datetime.now()` to say it is banned.
**Two bugs the tests caught:** `ATTEMPTING -> ESCALATED` and `DIAGNOSED -> EXHAUSTED` were
both missing from the graph.

**Tests — 303.** I-5: a raw SQL insert with a duplicate key raises `IntegrityError` with
no application code in the way. I-6: an attempt on an unresolved AWAIT_STATUS case returns
423 and writes nothing, and a resolver that raises on call proves the world is never
consulted. I-8: a stub resolver queries the audit table when called and finds the
`ATTEMPTING` row already there. I-13: byte-identical database, and attempt 3 of one case
resolves the same whether or not 50 others ran first.

**Still open:** `psp_app_ not_available` has a space in it in `error_policy.json`, left
as ground truth and not emitted by the simulator.

## Stage 3 — Two arms and the eval harness · complete · **SHIPPABLE**

`src/arms/` (the `Arm` protocol and `CaseSnapshot`; control's fixed +24h × 3, reading
neither the table nor rail health; baseline's eight-class policy), `eval/run_arms.py`
(tick loop, stable-hash assignment, main + trailing window), `eval/score.py` and
`report.py` (I-14/15/16/17, Wilson intervals, two-proportion z, segments), `routes_eval.py`.

**The result at 2000 payments** was baseline +19.2pp over control on a sixth of the
attempts, carried by `AWAIT_STATUS` (91% vs 0% — control has no poll, so I-6 blocks it)
and `NUDGE_CUSTOMER` (25% vs 6%). **Baseline lost on `SWITCH_RAIL`**, published and not
tuned away: every simulated outage is shorter than control's 24-hour wait, so "wait a day"
accidentally dominates the class the taxonomy exists to fix. The real case for switching
is that the *customer* will not wait a day, and abandonment is not modelled.

**Decisions.** `Runner.apply` takes the action from the arm so control can be genuinely
naive; the bounds, the idempotency guard and the AWAIT_STATUS block sit above every arm.
`ArmDecision.policy_routed` exists because without it a control retry failing with
`card_expired` would be re-diagnosed into `ESCALATED`, and control would silently become
the baseline while still producing numbers. Nudges are not attempts: no attempt row, no
key, no schedule. Run ids derive from the run's parameters, so reruns are idempotent.

**Three bugs the first run caught.** The simulator let a blind retry fix a wrong PIN —
control recovered 9/9 `NUDGE_CUSTOMER` and 12/13 `MERCHANT_ALERT`, contradicting
CLAUDE.md's own "a retry can never work". Fixed by splitting gates into **persistent**
(merchant misconfiguration, risk, customer error — keyed on the payment) and
**time-varying** (outage, balance, peak, limits — per attempt). Also: `bank_outage`
regenerated the whole downtime timeline instead of adding one window, and
`SCHEDULED -> AWAITING_STATUS` was missing.

**Tests — 409.** I-13: the assignment hash is stable and order-independent, and
baseline's outcomes are identical whether control ran first or not at all. I-14/15/16/17:
hand-built stores with known answers — four attempts count as one payment, a day-33
recovery counts and a day-40 one does not, and a losing code reaches the table, the
losses list and the API payload.

**Carried forward and still true:** the simulator and the taxonomy make the same causal
claim, so the evaluation tests whether *acting* on it beats ignoring it, not whether it is
true. In every report's caveats.

## Stage 4 — Features and LightGBM · complete · **null result**

`src/features/build.py` (31 point-in-time features, `as_of` required with no default),
`src/model/` (one row per attempt, temporal split, provenance sidecar, research/06 §6.5's
params verbatim, and a scorer that raises on a missing model or feature drift),
`src/arms/treatment.py` (policy for the class, model for the execution, delegating to
`BaselineArm` on the six ineligible classes), and the three-arm chain through `eval/`.

### The result — the model adds nothing

8000 payments, 30 days + 7 trailing, seed 42. Control 13.5% / baseline 34.7% /
treatment 34.9% under `normal`.

| gap | measures | `normal` | `bank_outage` |
|---|---|---|---|
| baseline − control | the **taxonomy** | **+21.2pp**, p < 0.001 | **+19.9pp**, p < 0.001 |
| treatment − baseline | the **model** | +0.2pp, p = 0.94 | −0.2pp, p = 0.95 |

On the model-eligible surface alone — the 24 of 110 codes where treatment can differ at
all — the model is **−4.5pp** against baseline (p = 0.52).

### Why

The model is not the problem: held-out temporal test PR-AUC 0.965 against a 0.247 base
rate, with the features research/03 §3.3 predicted — `hours_since_first_failure` 37%,
`days_to_salary_date` 21%, `day_of_month` 13%. It found the salary cycle from the
calendar alone.

**`candidate_delay_hours` has zero gain.** The training data comes from the baseline arm,
which schedules `RETRY_SCHEDULED` at exactly `min_wait_hours` every time, so the dataset
contains no variation in the timing decision. It can rank *which cases* recover, not
*when* to retry — off-policy evaluation without exploration, fixable only by an explorer
arm. The high AUC also flatters: the simulator's causal structure is largely exposed by
the observable features.

**Decisions.** Trained on a separate, larger population (seed 7, 40 000 payments,
baseline-only) and evaluated on seed 42 — at 8000 payments baseline makes ~250 attempts,
yielding 113 rows and a model that stopped at iteration 1, and seed separation is also
stronger than training on the evaluation population. **Seasonality features describe the
candidate's execution time, not the decision time**: the question is "will an attempt at T
succeed?", so the calendar must describe T, and reading a future calendar leaks nothing.
At training the two coincide so no row changed, but the first version cost 0.8pp because
every candidate looked identical but for one number. `error_code` and `attempt_number` are
reconstructed as of the cutoff rather than read off the case row. Straddling attempts are
dropped, not dragged backwards — a day-25 row in the training set would break I-10's
ordering. `is_recurring` is declared but constant and reported as zero-variance rather
than quietly dropped.

**The negative-EV STOP fired zero times and the arithmetic says it cannot:**
`EV = P(success) × amount − ₹2` against tickets of ₹99–₹25 000 needs a prediction below
~0.04%. Reported as zero rather than made to fire by raising the attempt cost.

**Tests — 543.** I-9's four checks, only one of which catches real leakage: inserting
events dated *after* `as_of` must leave every feature byte-identical. I-10: train precedes
valid precedes test, no case_id in two splits. I-1: treatment equals baseline on all 86
ineligible codes with a scorer that raises on contact, so the model is never called at
all; a scorer returning 1.0 still cannot make `card_expired` retry.

---
## Stage 5 — Interface and submission · complete

### What was built

`scripts/` — `verify_api.sh` (87 assertions against a real uvicorn process),
`verify_model.py` (re-derives PR-AUC, ROC-AUC, Brier and calibration from `model.txt` and
compares them to `metrics.json`), `verify_determinism.py` (A4, A5, read-only),
`make_diagram.py`. `src/api/config.py` for env-driven deployment. `dashboard/` — Next.js,
four screens, no component library. Plus `README.md`, `DEMO-SCRIPT.md` and
`eval/PRODUCTION-CHECK.md`.

### Two real bugs the verification pass caught

- **Categorical codes drifted between training and inference.** `Scorer.score_batch`
  called `astype("category")`, and pandas derives category *codes* from whichever values
  are in the batch — so a five-candidate batch assigned `error_code` a different integer
  than training had for the same string. LightGBM matches categoricals by code, so the
  model was silently scoring a different feature: a plausible probability, quietly wrong.
  Found because `verify_model.py` re-derives PR-AUC from disk and disagreed by 0.0015.
  Level lists are now pinned into `feature_names.json`; an unseen level lands as NaN
  rather than being reassigned. Re-running the evaluation gave identical arm numbers, so
  the Stage 4 null is unaffected.
- **The verification script's own paths.** `mktemp -d` yields a POSIX path Git Bash's curl
  writes to happily and a native Windows Python cannot open, so half the assertions were
  failing on a missing file rather than on the API. A relative work directory fixed it.

**Decisions.** `POST /v1/recovery/decide` gained `rail_health` and `model` blocks: the
Inspector's two most important rows need them, both are reads, so the whole Live screen
works against a read-only instance. `model.eligible` is reported explicitly rather than
inferred from a missing field — the absence of a model call on 86 of 110 codes is the
structural safety claim, and a field that says so is harder to miss. Read-only is enforced
twice, by a `require_writable` dependency and by opening SQLite `mode=ro`. The
40 000-payment training population is not committed (12 MB, reproduces from one command);
the two 8000-payment evaluation runs are, because they are the results. Next was bumped to
14.2.35 — the version `create-next-app` pins carries a published advisory.

### Verification — 175 checks, zero failures

544 tests · 87 live API assertions · 22 model · 22 determinism, cold-boot and read-only.
Method in `eval/PRODUCTION-CHECK.md`. Determinism holds at four times the population with
three arms and a model in the loop: two runs at one seed give identical report JSON *and*
identical rendered markdown.

### Open

**Record the 5-minute video** — `DEMO-SCRIPT.md` has the beats and the null lands at 2:30.
Beyond the submission: **an explorer arm** is the highest-value change to the system
itself, since without it the timing model is untrainable in principle rather than merely
underfit; and **abandonment is still unmodelled**, which is why `SWITCH_RAIL` loses.

---
## Deployment — Hugging Face Spaces + Vercel

Host pivot from Render/Railway to **HF Spaces (Docker SDK)** for the API, Vercel for the
dashboard. `DEPLOYMENT.md` carries the full split and the exact push commands.

**Changed.** `Dockerfile`: added `ENV PORT=7860` and moved `CMD` to shell form
(`--port ${PORT}`), `EXPOSE 7860`. The old `${PORT:-8000}` default was the real bug —
HF Spaces never sets `$PORT` and proxies to 7860, so the container would have listened
on 8000 and the Space would have come up unhealthy. A host that injects its own `$PORT`
still overrides the image default, so Render/Railway compatibility is unchanged. *(The
old `CMD ["sh","-c","…"]` array form did expand `${PORT}` correctly — exec-form arrays
only fail to expand when they don't invoke a shell — so that half of the suspicion was
unfounded; the wrong default was the actual defect.)* CORS now reads **`ALLOWED_ORIGINS`**,
falling back to `TRIAGE_CORS_ORIGINS` so pre-rename deploy configs keep working. New
`.dockerignore` (build context only — the Dockerfile never did `COPY . .`, so no image
content changes; `eval/runs/*.db` explicitly re-included). New
`scripts/sync_hf_space.sh` rebuilds the gitignored `hf-space/` staging directory.

**Already correct, no change.** The dashboard's API base URL was already env-driven
through one `API_URL` constant in `dashboard/lib/api.ts` (`NEXT_PUBLIC_API_URL`, local
fallback only), used by the single `fetch` wrapper — no hardcoded URL anywhere.
`GET /health` already returned `read_only`; `POST /v1/simulator/run` was already behind
`require_writable` → 503. There is no "run simulator" control in the four screens, so
nothing to hide.

**Verified.** 544 tests still pass. Docker Desktop's engine was unavailable in the
session, so the image was **not** built; verified instead by running uvicorn with
`PORT=7860 TRIAGE_READ_ONLY=true` — `/health` → `read_only: true`,
`/v1/errors/meta/coverage` → the 27-of-110 summary, `POST /v1/simulator/run` → 503 (not
500). CORS with a fake `ALLOWED_ORIGINS`: preflight from a matching origin 200 + echoed
header, mismatched preflight 400; simple GET from a mismatched origin returns 200 with
**no** `Access-Control-Allow-Origin` — the standard browser-enforced behaviour, not a
server-side block.

**Needs a human.** `docker build` once locally (or trust HF's first build log); create
the Space and push `hf-space/` to its remote; set `ALLOWED_ORIGINS` on the Space and
`NEXT_PUBLIC_API_URL` on Vercel after each side's domain exists. `render.yaml` and
`PRODUCTION-VIEW.md` are superseded but left in place.

### Azure App Service for Containers — second target, no code change

Added as an alternative to HF Spaces; `AZURE.md` has both the portal and CLI paths.
**Nothing in `src/` changed to support it** — the only `src/` diff remains the
`ALLOWED_ORIGINS` rename from the pass above. Azure builds this repo's `Dockerfile`
directly, so `hf-space/` is irrelevant to it and was left untouched.

The one Azure-specific fact worth carrying: Azure routes to whatever `WEBSITES_PORT`
says, so that app setting must equal the container's listening port. `ENV PORT=7860`
already makes that 7860 — `WEBSITES_PORT=7860` and it works; omit it and Azure probes
80/8080, every request times out, and the logs read like a crash that never happened.
A paid B1 Linux plan and a container registry (App Service deploys images, not source)
are the two prerequisites.

Re-verified this pass rather than assumed: `ENV PORT=7860` + shell-form
`CMD … --port ${PORT}` intact; `allow_origins=config.cors_origins()` with no hardcoded
URL anywhere in `src/api/`; `eval/runs/*.db` and `eval/model/` still `COPY`'d in at
build time. Every filesystem write in `src/` is either gated (`main.py`'s `init_db`
behind `if not resolved_read_only`, the store behind `mode=ro`) or unreachable from the
API — `src/api/` imports nothing from `src/model/`, and `eval.report.write_report` is
CLI-only; the API imports only `build`. Proved it empirically too: snapshotted 53 files,
exercised every read route plus a refused write, and the tree came back byte-identical.

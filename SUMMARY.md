# SUMMARY — build log

For a human returning after a break. One section per stage; earlier stages are compressed
as they age, never deleted. Decisions and gaps, not diffs. `CLAUDE.md` holds the
invariants.

## Stage 1 — Skeleton and policy engine · complete

`error_policy.json` moved to the repo root (runtime data, not research) and left
unmodified. `src/policy/engine.py` loads and validates it and answers `resolve` /
`list_entries` / `coverage_summary`; `src/api/` serves it read-only behind the Razorpay
error envelope, with `create_app`, a lifespan that refuses to boot on a broken table,
CORS, `/health` and one exception handler.

**Decisions.** The repo had no `.git` — git was walking up to a stray
TrustedInstaller-owned repo at `C:\`, so `git init` was run inside `TRIAGE/`. Unknown code
on `GET /v1/errors/{code}` returns 404 `NOT_FOUND_ERROR`, not I-2's 400: browsing the
taxonomy for a missing row is a different failure from submitting a bad code to a decision
endpoint. *(Stage 2 wired the 400 into `/decide`; I-2 now says "in the decision path".)*
Query parameters are validated by hand so every failure leaves through the envelope rather
than FastAPI's 422 blob, and validation is exhaustive. `error_policy.json` is untouched
and needs none: all 110 rows pass and the counts match the brief exactly.

**Tests — 75.** The taxonomy is exactly what CLAUDE.md claims — 110 unique codes, all
eight actions at the exact counts, exactly 27 recoverable and those 27 precisely the three
retrying classes, with counts hard-coded rather than derived from the file under test.
I-2: near misses (`INSUFFICIENT_FUNDS`, `insufficient-funds`, `insufficient_fund`, `""`,
`" "`) all raise, so fuzzy or case-insensitive matching cannot creep in. Six tampered-table
tests fail at load; the app refuses to boot broken.

**Still open:** the table is read once at startup so editing it needs a restart, and
there is no auth — research/05 specifies a Bearer key, the brief excluded it.

## Stage 2 — Simulator and case engine · complete

`src/store/` (five tables, UNIQUE index on `attempts.idempotency_key`, no REAL column),
`src/simulator/` (rails and the downtime feed; `declines.py`'s cited CONFIG and causal
cascade; `world.py`'s latent state behind one resolution method), `src/executor/` (nine
states with edges as data, audit-before-state, bounds, routing, polls), and the case API.

**Decisions.** No server clock: `now` is required on every write endpoint. `ESCALATED`
and `STOPPED` have no edge back to `SCHEDULED`, making I-4 structural rather than
conventional. The idempotency check runs **before** the bounds check — a replayed request
must be told it is a duplicate, or a double-charge attempt hides behind a 422. A failed
attempt re-diagnoses against the new code; the original stays on
`payments.first_error_code` so Stage 3 can segment by the cause that opened the case. IDs
derive from stable keys (`blake2b`), which is what makes a run byte-identical and
`POST /cases` idempotent without a lookup-then-insert race. Rates were tuned against the
printed distribution until all eight classes appeared with none above 35%.

**Deviations.** `test_no_wall_clock.py` parses the AST rather than grepping: a text grep
fires on `main.py`'s own docstring, which names `datetime.now()` to say it is banned.
`world.attempt` also resolves the original payment (`action="INITIAL"`) and status polls
take the resolution from the caller, both to keep the world at one resolution method.
**Two bugs the tests caught:** `ATTEMPTING -> ESCALATED` was missing, wedging any attempt
that failed with a nudge-class code, and `DIAGNOSED -> EXHAUSTED` was missing, so a case
whose first wait exceeded `drop_dead_at` could not be closed.

**Tests — 303.** I-5: a raw SQL insert with a duplicate key raises `IntegrityError` with
no application code in the way. I-6: an attempt on an unresolved AWAIT_STATUS case returns
423 and writes nothing, and a resolver that raises on call proves the world is never
consulted. I-7: all three bounds 422. I-8: a stub resolver queries the audit table when
called and finds the `ATTEMPTING` row already there. I-13: byte-identical database, and
attempt 3 of one case resolves the same whether or not 50 others ran first.

**Still open:** `psp_app_ not_available` has a space in it in `error_policy.json`, left as
ground truth and not emitted by the simulator — flag it if the table is regenerated.

## Stage 3 — Two arms and the eval harness · complete · **SHIPPABLE**

`src/arms/` (the `Arm` protocol and `CaseSnapshot`; control's fixed +24h × 3, reading
neither the table nor rail health; baseline's eight-class policy with a rail-health check
before any switch), `eval/run_arms.py` (tick loop, stable-hash assignment, main + trailing
window), `eval/score.py` and `report.py` (I-14/15/16/17, Wilson intervals, two-proportion z,
segments, time-to-recovery), and `routes_eval.py`.

**The result at 2000 payments** was baseline +19.2pp over control on a sixth of the
attempts, carried by `AWAIT_STATUS` (91% vs 0% — control has no poll, so I-6 blocks it)
and `NUDGE_CUSTOMER` (25% vs 6%). **Baseline lost on `SWITCH_RAIL`**, published in section
6 and not tuned away: every simulated outage is shorter than control's 24-hour wait, so
"wait a day" accidentally dominates the class the taxonomy exists to fix. The real case
for switching is that the *customer* will not wait a day, and abandonment is not modelled.

**Decisions.** `Runner.apply` takes the action from the arm so control can be genuinely
naive; the bounds, the idempotency guard and the AWAIT_STATUS block sit above every arm.
`ArmDecision.policy_routed` exists because without it a control retry failing with
`card_expired` would be re-diagnosed into `ESCALATED`, and control would silently become
the baseline while still producing numbers. Nudges are not attempts: no attempt row, no
key, no schedule; conversion is an hourly hazard keyed on the hour, so tick size does not
move it.

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
recovery counts and a day-40 one does not, a losing code reaches the table, the losses list
and the API payload. Two runs at one seed give identical report JSON *and* markdown.

**Carried into Stage 4 and still true:** the simulator and the taxonomy make the same
causal claim (a wrong PIN does not fix itself), so the evaluation tests whether *acting*
on it beats ignoring it, not whether it is true. In every report's caveats.

## Stage 4 — Features and LightGBM · complete · **null result**

| Module | Responsible for |
|---|---|
| `src/features/build.py` | 31 point-in-time features. `as_of` required, no default; every aggregate filters `event_time < as_of`. |
| `src/model/dataset.py` | One row per attempt (I-11), replayed at `as_of = scheduled_at`. Temporal split (I-10), provenance sidecar. |
| `src/model/train.py` · `score.py` | LightGBM with research/06 §6.5's params verbatim, emitting model.txt / feature_names.json / metrics.json / importances.json; scoring raises on a missing model and on feature drift rather than filling zeros. |
| `src/arms/treatment.py` | Policy for the class, model for the execution. Delegates to `BaselineArm` on the six ineligible classes. |
| `eval/*` | Three-arm chain, both gaps separately, model-eligible section, diagnostics, negative-EV counts. |

### The result — the model adds nothing

8000 payments, 30 days + 7 trailing, seed 42, three-way split:

| gap | measures | `normal` | `bank_outage` |
|---|---|---|---|
| baseline − control | the **taxonomy** | **+21.2pp**, p < 0.001 | **+19.9pp**, p < 0.001 |
| treatment − baseline | the **model** | +0.2pp, p = 0.94 | −0.2pp, p = 0.95 |

(control 13.5% / baseline 34.7% / treatment 34.9% under `normal`.) On the model-eligible
surface alone — the 24 of 110 codes where treatment can differ at all — the model is
**−4.5pp** against baseline (p = 0.52). The taxonomy delivered 21 points; the model added
nothing measurable, and across both scenarios it is indistinguishable from the flat policy
it replaces.

### Why — from the calibration table and the importances
The model is not the problem. Held-out temporal test PR-AUC is 0.97 against a 0.25 base
rate, and the top features are exactly the ones research/03 §3.3 predicted:
`hours_since_first_failure` (39% of gain), `day_of_month` (20%), `days_to_salary_date`
(16%). It found the salary cycle from the calendar alone, without being told.

**`candidate_delay_hours` has zero gain.** That is the finding. The training data comes
from the baseline arm, which schedules `RETRY_SCHEDULED` at exactly `min_wait_hours` every
time, so the dataset contains no variation in the timing decision and the model never had
a chance to learn what a different delay would do. It can rank *which cases* recover; it
cannot rank *when* to retry. This is off-policy evaluation without exploration, and no
amount of training fixes it — the fix is an explorer arm randomising delay within policy
bounds, a simulator change rather than a model change.

The high AUC also flatters: the simulator's causal structure is largely exposed by the
observable features, so a well-specified model should score highly. Read it as "the
features describe this world", not as a claim about production.

### Decisions not in CLAUDE.md or research/

- **Trained on a separate, larger population** (seed 7, 40 000 payments, baseline-only),
  evaluated on seed 42. At 8000 payments baseline makes ~250 attempts, yielding 113 rows
  and a model that stopped at iteration 1. Seed separation is also stronger than training
  on the evaluation population.
- **Seasonality features describe the candidate's execution time, not the decision time.**
  The question is "will an attempt at T succeed?", so the calendar must describe T; reading
  a future calendar leaks nothing, being arithmetic on a proposed timestamp. At training
  the two coincide so no row changed, but the first version cost 0.8pp because every
  candidate looked identical but for one number.
- **`error_code` and `attempt_number` are reconstructed as of the cutoff**, not read off
  the case row: a case's code mutates across attempts, so replaying attempt 1 of a
  four-attempt case would otherwise hand it a code known only three attempts later.
- **Straddling attempts are dropped, not dragged backwards** — a day-25 row in the
  training set would break the ordering property. The count is in the provenance sidecar.
- **`mcc` added to `cases`**; **`is_recurring` declared but constant** (research/06 §6.3
  lists it, the simulator models no mandates), reported as zero-variance not dropped.

**The negative-EV STOP fired zero times, and the arithmetic says it cannot.**
`EV = P(success) × amount − ₹2`; against tickets of ₹99–₹25 000 the model would need to
predict below ~0.04% for EV to go negative. Built, tested and wired to the audit trail,
but structurally unreachable at these ratios — reported as zero rather than made to fire
by raising the attempt cost, which would be manufacturing a demo beat.

### Tests — 543 passing (134 new)

**I-9** — four checks, only one of which catches real leakage: inserting attempts,
recoveries and downtimes dated *after* `as_of` must leave every feature byte-identical. A
naive whole-table aggregate passes the signature, boundary and isolation checks and fails
that one. An event at exactly `as_of` is excluded (strict `<`), and `days_to_salary_date`
is identical for two customers at the same instant, being derived from the calendar rather
than read from the world. **I-10** — train precedes valid precedes test on timestamps, no
case_id in two splits. **I-11** — row count equals attempt count, labels match outcomes,
only baseline rows used. **I-1** — treatment equals baseline on all 86 ineligible codes
with a scorer that raises on contact, so the model is never called at all; a scorer
returning 1.0 still cannot make `card_expired` retry.

### Open questions carried into Stage 5

- **The model needs exploration to be worth anything.** An explorer arm randomising delay
  within policy bounds is the highest-value change left; without it the timing model is
  untrainable in principle, not merely underfit.
- **The eligible surface is small** — 24 of 110 codes, ~324 of 1373 payments — so even a
  perfect model moves the blended number by a point or two. Stage 3's `SWITCH_RAIL` loss
  also persists, consistent with abandonment still being unmodelled.
- **Lead the video with the null.** "The taxonomy delivered 21 points; the model added
  nothing, and here is exactly why" is the strongest thing in this submission.

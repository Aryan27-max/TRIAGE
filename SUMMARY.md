# SUMMARY — build log

For a human returning after a break. One section per stage; earlier stages are
compressed as they age, never deleted. Decisions and gaps, not diffs. Read `CLAUDE.md`
first for the invariants.

## Stage 1 — Skeleton and policy engine · complete

`error_policy.json` moved to the repo root (runtime data, not research) and left
unmodified. `src/policy/engine.py` loads and validates it and answers `resolve` /
`list_entries` / `coverage_summary`. `src/api/` serves it read-only behind the Razorpay
error envelope: `errors.py` (envelope + subclasses), `schemas.py` (wire shapes, separate
from the domain objects), `routes_errors.py` (list, lookup, `/meta/actions`,
`/meta/coverage`), `main.py` (`create_app`, lifespan, CORS, `/health`, one exception
handler).

**Decisions.** The repo had no `.git` — git was walking up to a stray TrustedInstaller-
owned repo at `C:\`, so `git init` was run inside `TRIAGE/`. Unknown code on
`GET /v1/errors/{code}` returns 404 `NOT_FOUND_ERROR`, not I-2's 400: browsing the
taxonomy for a missing row is a different failure from submitting a bad code to a decision
endpoint. *(Stage 2 wired the 400 into `/decide`; I-2 now says "in the decision path".)*
Query parameters are validated by hand so every failure leaves through the envelope rather
than FastAPI's 422 `detail` blob. Collections use `{entity, count, items}` per research/05
§5.3. Validation is exhaustive — `PolicyLoadError.problems` lists every problem at once.

**On `error_policy.json`.** Untouched and needs no editing: all 110 rows pass the full
validation pass and the counts match the brief exactly.

**Tests — 75.** The taxonomy is exactly what CLAUDE.md claims — 110 unique codes, all
eight actions at the exact counts, exactly 27 recoverable and those 27 precisely the three
retrying classes, with counts hard-coded rather than derived from the file under test.
I-2: near misses (`INSUFFICIENT_FUNDS`, `insufficient-funds`, `insufficient_fund`, `""`,
`" "`) all raise, so fuzzy or case-insensitive matching cannot creep in. I-4 and I-1 hold
over all 110 rows. Six tampered-table tests fail at load; the app refuses to boot broken.

**Carried into Stage 2 and resolved there:** `RETRY_NOW` has no sub-hour representation
in the table (`RETRY_NOW_FLOOR_SECONDS = 30` now lives in the executor);
`BadErrorCodeError` was declared but unused (`/decide` raises it); `research/08-ui-spec.md`
was missing from CLAUDE.md's file map (added). **Still open:** the table is read once at
startup so editing it needs a restart, and there is no auth — research/05 specifies a
Bearer key, the brief excluded it.

## Stage 2 — Simulator and case engine · complete

| Module | Responsible for |
|---|---|
| `src/store/schema.sql` · `db.py` | Five tables, UNIQUE index on `attempts.idempotency_key`, no REAL column. Row dataclasses, derived ids, `set_case_state` as the only path to `cases.state`. |
| `src/simulator/rails.py` | Rail inventory, `derive_rng`, IST peak window, the downtime feed in Razorpay's schema, `RailHealth` lookup. |
| `src/simulator/declines.py` | CONFIG — every rate cited or marked as ours — and the causal cascade turning latent state into an error code. |
| `src/simulator/world.py` | Latent state and the one resolution method. Returns `AttemptOutcome` and nothing else. |
| `src/simulator/generate.py` | Deterministic population + CLI. Cases written at RECEIVED with `arm` unset. |
| `src/executor/state.py` · `runner.py` | Nine states with edges as data and audit-before-state; bounds, diagnosis routing, attempts, status polls, `decide`. |
| `src/api/deps.py` · `routes_cases.py` · `routes_rails.py` | Per-request connection and World; the case lifecycle and the downtime feed. |

**Decisions.** No server clock, so `now` is a required field on `/decide`, `/attempts`,
`/status-poll` and `/stop`; case creation uses `failed_at`, which for a case being opened
*is* the current time. `ESCALATED` and `STOPPED` have no edge back to `SCHEDULED`, making
I-4 structural rather than conventional; `AWAITING_STATUS` keeps its edge, gated on
`status_resolved_at`, because I-6 requires it. The idempotency check runs **before** the
bounds check — a client replaying a request must be told it is a duplicate, not told what
state the original attempt left the case in, or a double-charge attempt hides behind a
422. A failed attempt re-diagnoses against the new code and updates `cases.error_code`;
the original stays on `payments.first_error_code` so Stage 3 can segment by the cause that
opened the case. IDs are derived from stable keys (`blake2b`), never counters or UUIDs,
which is what makes a run byte-identical and `POST /cases` idempotent on `payment_id`
without a lookup-then-insert race. `SWITCH_RAIL` targets come from a fixed map, not
sampled — a rail choice varying per run would break arm parity. Rates were tuned against
the printed distribution until the stream spread across all eight classes with none above
35% (~17% first-attempt failure); tuned, not fitted, and every knob sits in one CONFIG
dict per module.

**Deviations.** `test_no_wall_clock.py` parses the AST rather than grepping — a text grep
fires on `main.py`'s own docstring, which names `datetime.now()` to say it is banned.
`world.attempt` also resolves the original payment (`action="INITIAL"`) and status polls
take the resolution from the caller, both to keep the world at one resolution method.

**Two bugs the tests caught.** `ATTEMPTING -> ESCALATED` was missing, so an attempt
failing with a nudge-class code wedged in SCHEDULED behind a plausible-looking 409; a
static test now asserts every diagnosis destination is reachable from `DIAGNOSED` and
`ATTEMPTING`. `DIAGNOSED -> EXHAUSTED` was missing, so a case whose first wait already
exceeded `drop_dead_at` could not be closed.

**Tests — 303 (228 new).** I-5: a raw SQL insert with a duplicate key raises
`IntegrityError` with no application code in the way; over HTTP, 201 then 409 with one
surviving attempt row. I-6: all five AWAIT_STATUS codes land in `AWAITING_STATUS` with
`scheduled_at` null, an attempt returns 423 and writes nothing, a resolver that raises on
call proves the world is never consulted, and after a poll the same attempt returns 201.
I-7: all three bounds 422, and a refused attempt writes no rows. I-8: a stub resolver
queries the audit table when called and finds the `ATTEMPTING` row already there. I-12:
`AttemptOutcome` has four fields, no `__dict__`, no latent-token field, and the decision
packages are AST-scanned for `src.simulator` imports. I-13: byte-identical database, and
attempt 3 of one case resolves the same whether or not 50 others ran first.

**Carried into Stage 3 and resolved there:** nudges had no recovery path, there was no
tick loop, and `cases` lacked the observable customer columns. **Still open:**
`psp_app_ not_available` has a space in it in `error_policy.json` — left as ground truth
and not emitted by the simulator, but flag it if the table is ever regenerated.

## Stage 3 — Two arms and the eval harness · complete · **SHIPPABLE**

### What was built

| Module | Responsible for |
|---|---|
| `src/arms/base.py` | `Arm` protocol, `ArmDecision`, and `CaseSnapshot` — the curated observable view an arm is given. |
| `src/arms/control.py` | Fixed +24h × 3 retry for every code. Reads neither the table nor rail health. |
| `src/arms/baseline.py` | The policy table acted on directly: eight classes, flat `min_wait_hours`, rail-health check before any switch. |
| `eval/run_arms.py` | Tick loop: generates once, assigns by stable hash, walks the main + trailing window, writes the `runs` row. |
| `eval/score.py` · `report.py` | I-14/15/16/17, Wilson intervals, two-proportion z, per-code / per-class / per-rail segments, time-to-recovery; renders `eval/report-*.md` with section 6 structural rather than conditional. |
| `src/api/routes_eval.py` | `POST /v1/simulator/run`, `GET /v1/eval/runs`, `GET /v1/eval/report/{id}`. |
| prerequisites | `cases` gained `city_tier`, `vpa_handle`, `payer_bank`, `nudge_sent_at`; the world gained a `NUDGE` resolution consuming `nudge_responsiveness_per_hour`. |

### The result

2000 payments over 30 days + 7 trailing, seed 42. Baseline recovers ~2.3× as many
payments on a sixth of the attempts (74 against 454).

| scenario | baseline | control | gap |
|---|---|---|---|
| `normal` | 34.3% (57/166) | 15.1% (28/185) | **+19.2pp**, p < 0.001 |
| `bank_outage` | 34.3% (57/166) | 15.6% (29/186) | **+18.7pp**, p < 0.001 |

The gap is carried by `AWAIT_STATUS` (91% vs 0% — control has no status poll, so I-6
blocks it and its cases expire) and `NUDGE_CUSTOMER` (25% vs 6%). On the three terminal
classes both recover nothing; baseline stops at once, control spends its budget.

**Baseline LOSES on `SWITCH_RAIL`** — 14/17 against control's 13/13 under `normal`, in
section 6 of both reports and not tuned away. Every simulated outage lasts 45–360 minutes,
all shorter than control's 24-hour wait, so "wait a day" accidentally dominates the class
the taxonomy exists to fix, while baseline switches at once onto an alternate rail that
can fail for unrelated reasons. The real-world case for switching is that the *customer*
will not wait a day; abandonment is not modelled, so the lever's main benefit is invisible
here. Fixing that means modelling abandonment, not retuning the arm.

**The two scenarios barely differ.** A 24-hour high-severity window on one PSP handle is
~3% of the timeline on ~1/6 of one rail — arithmetically a fraction of a point at
population level, not a null finding about rail health.

### Decisions not specified in CLAUDE.md or research/

**Arms decide, the runner executes.** `Runner.apply` takes the action from the arm so
control can be genuinely naive; the bounds (I-7), the idempotency guard (I-5) and the
AWAIT_STATUS block (I-6) sit above every arm. Control's pending cases are blocked by the
executor, not by control knowing better. `ArmDecision.policy_routed` exists because
without it a control retry failing with `card_expired` would be re-diagnosed into
`ESCALATED` and control would silently become the baseline while still producing numbers.
Arms see a `CaseSnapshot`, not the raw row, so an arm never queries the store.

Nudges are not attempts: no attempt row, no key, no schedule, and cost reported
separately (₹0.20 against ₹2.00 per attempt, both assumptions). Nudge conversion is an
hourly hazard keyed on the hour rather than a total, so a half-hourly loop recovers the
same customers at the same hour as an hourly one — calibrated to ~25% over 48 hours before
any arm was run and not retuned since. Run ids are derived from the run's parameters,
making reruns idempotent; each run owns one SQLite file under `eval/runs/`.
`runs.created_at` holds the simulated start, the only field that would otherwise differ
between two identical runs. `POST /v1/simulator/run` runs synchronously and returns 202
`completed` — a queue would add a dependency for a few seconds' work.

### Three bugs the first run caught

- **The simulator let a blind retry fix a wrong PIN.** Control was recovering 9/9
  `NUDGE_CUSTOMER`, 5/7 `SWITCH_INSTRUMENT` and 12/13 `MERCHANT_ALERT`, directly
  contradicting CLAUDE.md's own "a retry can never work": the cause model re-rolled every
  gate per attempt. Fixed by splitting gates into **persistent** (merchant
  misconfiguration, risk block, customer error — keyed on the payment) and
  **time-varying** (outage, balance, peak, daily limit — keyed per attempt).
- **`bank_outage` regenerated the whole downtime timeline** rather than adding one window
  to `normal`'s, because the scenario sat in the RNG key: the two shared zero downtime ids
  and were not comparable. It is now exactly `normal` + one event.
- **`SCHEDULED -> AWAITING_STATUS` was missing**, so a control case re-diagnosed to
  `payment_pending` mid-flight wedged behind a plausible-looking 409.

### Tests — 409 passing (97 new)

**I-13** — the assignment hash is stable and order-independent, arms hold disjoint case
sets, and baseline's outcomes are identical whether control ran first, second or not at
all. **I-14/15/16/17** — hand-built stores with known answers: four attempts count as one
payment and attempts are never the denominator; a day-33 recovery counts, a day-40 one
does not; a losing code reaches `by_error_code`, `losses` and the API payload, and sorting
by n keeps it at the top; attempt and nudge cost are both reported. **Arms** — control
gives one answer for all eight classes and raises if handed an engine or health feed it
touches; baseline names exactly the table's class for all 110 codes and never carries a
`scheduled_at` on a non-retrying class. **Nudges** — escalate without an attempt row or
key, recover on landing, exhaust at the window, and a control-only run sends zero.
**Determinism** — two runs at one seed give identical report JSON *and* markdown.

### Open questions carried into Stage 4

- **The simulator and the taxonomy make the same causal claim.** The persistent /
  time-varying split says a wrong PIN does not fix itself, which is also what
  `NUDGE_CUSTOMER` asserts. The evaluation therefore tests whether *acting* on that claim
  beats ignoring it, not whether the claim is true. In every report's caveats, and the
  most important limitation to say out loud in the video.
- **Abandonment is not modelled**, which is why `SWITCH_RAIL` loses. The highest-value
  simulator change left; it would move results, so it belongs before Stage 4 or not at all.
- **Control recovers 6% of `NUDGE_CUSTOMER`.** The sticky draw is fixed but the threshold
  it is compared against still moves with the peak and retry multipliers, so a small
  residual leaks. Real but minor; left rather than special-cased.
- **Small samples.** ~170 cases per arm, per-code rows routinely n < 10, `RETRY_NOW` n = 1.
  Stage 4 needs more payments per run, not more days.

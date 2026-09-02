# CLAUDE.md — TRIAGE

Operating context for building this project. Read this before writing any code.
Detailed specifications live in `research/`; this file holds the rules that must
never be violated and the map for finding everything else.

---

## What TRIAGE is

Razorpay publishes **110 distinct payment failure reasons** and tells merchants what each
one means. It does not tell them what to *do* about it. TRIAGE is the missing decision
layer: it reads a failure reason, checks whether the rail is currently degraded, and
returns a bounded, auditable action — retry now, retry at a predicted time, switch rail,
switch instrument, nudge the customer, wait for status, stop, or alert the merchant.

The finding that justifies the whole build: **only 27 of the 110 codes are recoverable
without human intervention.** A naive "retry three times" loop is therefore wrong on
roughly three quarters of failures.

Built for the Razorpay Buildathon, Track 03 — AI Revenue Recovery. Deadline: **5 September**.

---

## Status

| Stage | Scope | State |
|---|---|---|
| 0 | Research and taxonomy | **Done** — `research/`, `error_policy.json` |
| 1 | Skeleton + policy engine | **Done** — `src/policy/`, `src/api/`, 75 tests green |
| 2 | Simulator + case engine | **Done** — `src/simulator/`, `src/store/`, `src/executor/`, 303 tests green |
| 3 | Two arms + eval harness | **Done — SHIPPABLE CHECKPOINT REACHED.** 409 tests green, both scenario reports generated |
| 4 | Features + LightGBM | Not started |
| 5 | Dashboard + README + video | Not started |

Update this table as stages complete. It is the fastest way to re-orient at the start of
a session.

**Stage 3 is the shippable checkpoint.** At the end of it, TRIAGE is a complete and
defensible submission with zero machine learning in it. Stages 4 and 5 are upside, not
dependencies. If time runs out, submit what exists at the end of Stage 3.

---

## The eight action classes

Referenced constantly. Memorise these; do not re-read `research/03` and `research/04`
for the basics.

| Action | Codes | Meaning | Schedules a retry? |
|---|---|---|---|
| `RETRY_NOW` | 3 | Transient glitch, re-attempt in seconds | Yes |
| `RETRY_SCHEDULED` | 9 | Will plausibly succeed later; predict when | Yes |
| `SWITCH_RAIL` | 15 | Rail degraded; same instrument, different route | Yes |
| `SWITCH_INSTRUMENT` | 28 | Instrument unusable; a retry can never work | **No** |
| `NUDGE_CUSTOMER` | 23 | Blocked on a human action | **No** |
| `AWAIT_STATUS` | 5 | Outcome unknown; retrying risks a **double charge** | **No** |
| `STOP` | 4 | Retrying is unsafe or penalised | **No** |
| `MERCHANT_ALERT` | 23 | Merchant misconfiguration, not a customer failure | **No** |

Silently recoverable = `RETRY_NOW` + `RETRY_SCHEDULED` + `SWITCH_RAIL` = **27 of 110**.

Families: **A** cards + netbanking (37) · **B** UPI + wallets (28) · **S** shared (22) ·
**X** merchant (23).

---

## HARD INVARIANTS

These are not style preferences. Each one, if violated, produces a bug that survives to
the demo and gets caught by a judge instead of by you. Each is paired with the test that
enforces it.

### Decision safety

**I-1 — The policy table decides the action class. The model never does.**
`error_policy.json` is the single source of truth for *what kind* of action is permitted.
The LightGBM model is consulted **only** for `RETRY_SCHEDULED` and `SWITCH_RAIL`, and only
to rank candidate executions *within* an already-permitted class. A model error can
therefore never cause an unrecoverable failure to be retried. Safety is structural, not
learned.
→ `tests/test_model_scope.py`

**I-2 — An unknown error code raises. It never falls back to a retry.**
Defaulting an unrecognised code to `RETRY_SCHEDULED` is the single most dangerous
shortcut available here. **In the decision path** — anywhere a code is submitted as
input and an action comes back — an unknown code is `BAD_REQUEST_ERROR`, HTTP 400.
That is `POST /v1/recovery/decide` and case creation. The read-only taxonomy lookup
`GET /v1/errors/{code}` is not a decision: asking for a row that does not exist is
`NOT_FOUND_ERROR`, HTTP 404. Both raise; neither ever defaults.
→ `tests/test_policy_coverage.py`, `tests/test_api_errors.py`

**I-3 — No LLM output ever reaches a money decision.**
LLMs write explanations and customer message copy. They do not choose actions, times,
rails, or amounts. Anything an LLM produces is display text.
→ enforced by module boundary: `src/explain/` imports from `src/policy/`, never the
reverse.

**I-4 — `SWITCH_INSTRUMENT`, `NUDGE_CUSTOMER`, `AWAIT_STATUS`, `STOP` and
`MERCHANT_ALERT` never schedule a retry.**
`decision.scheduled_at` must be `None` for all five.

**A nudge that lands is not a retry.** `NUDGE_CUSTOMER` reaches `RECOVERED` through
`ESCALATED`, and that does not violate I-4. I-4 forbids *scheduling a retry* on these
classes; a nudge that lands means the customer went and paid themselves — no attempt is
scheduled, no idempotency key is consumed, no attempt row is written, and this system
charges nothing. The structural guarantee is unchanged and is the thing to check:
`ESCALATED` and `STOPPED` have no edge to `SCHEDULED` or `ATTEMPTING`, so a non-retrying
class cannot reach an attempt however a caller misuses the runner. Settled in Stage 3 —
do not re-argue it.

**Control is exempt, by construction.** The control arm retries every code on a fixed
schedule precisely because it does not read the table — that is the behaviour being
measured against. I-4 binds the policy-driven path: `Runner.decide()`, the baseline arm,
and Stage 4's treatment arm.
→ `tests/test_arms.py`, `tests/test_nudge.py`, `tests/test_await_status.py`

### Money safety

**I-5 — Every attempt carries idempotency key `{case_id}:{attempt_number}`, enforced by a
unique database index.**
Not by application logic. Not by an `if` statement. A `UNIQUE` constraint at the schema
level, so a race condition cannot produce a double charge. Duplicate → HTTP `409`.
→ `tests/test_idempotency.py`

**I-6 — `AWAIT_STATUS` cases cannot enter `ATTEMPTING` without a resolved status poll.**
Razorpay's own docs note that pending transactions may authorize late, and a deemed
transaction's outcome is unknown until the next day. Retrying these double-charges the
customer. Blocked attempt → HTTP `423 AWAITING_STATUS`.
→ `tests/test_await_status.py`

**I-7 — No action fires past `max_attempts` or `drop_dead_at`.**
Both are enforced in the executor before any attempt is constructed. Violation →
HTTP `422 POLICY_VIOLATION`.
→ `tests/test_bounds.py`

**I-8 — Every state transition is written to the audit log before the action executes.**
Append-only. Records `from`, `to`, `actor`, `reason`, `idempotency_key`, timestamp.
An action with no preceding audit row is a bug.
→ `tests/test_audit_completeness.py`

### ML integrity

**I-9 — Every rolling feature filters on `event_time < as_of`. No exceptions.**
Computing `cust_hist_success_rate` over the full dataset leaks the outcome and produces
an AUC that collapses on deployment. The feature builder takes `as_of` as a required
argument. This is the first thing a payments ML reviewer will check.
→ `tests/test_no_leakage.py` — **the most important test in the repo**

**I-10 — Train/test splits are temporal. Never random.**
Train days 1–21, validate 22–26, test 27–30. A random split on time-series payment data
is leakage by another name.
→ `tests/test_temporal_split.py`

**I-11 — The label is the attempt outcome. One row per attempt, not per payment.**
Payment-level rows conflate multiple decisions into one target.

**I-12 — The simulator's hidden state is never readable by the policy engine or the model.**
`src/simulator/world.py` knows the customer's true balance, real salary date, and actual
card validity. The decision path sees only what a real system would see: the error code,
the rail health feed, and observable history. If the policy can read hidden state, the
whole evaluation is circular and worthless.
→ `tests/test_hidden_state.py`

### Evaluation integrity

**I-13 — All arms consume identical payment streams.**
Same seed, same generated population, split by assignment not by regeneration. Otherwise
you are comparing arms against different worlds.
→ `tests/test_arm_parity.py`

**I-14 — Scoring deduplicates to the payment, on final outcome.**
One payment attempted four times is **one** payment, scored on whether it eventually
succeeded. Counting attempts inflates recovery numbers. This follows Stripe's published
A/B methodology.

**I-15 — A trailing window is applied before scoring.**
A retry scheduled on day 28 may land on day 33. Cutting measurement at day 30
systematically undercounts the treatment arm.

**I-16 — The per-error-code breakdown is always published, including negative rows.**
Segments where the treatment arm *loses* are published alongside the wins. This is the
project's core credibility claim and the demo's closing beat. Suppressing them defeats
the entire premise.

**I-17 — Attempt counts and cost are reported alongside recovery rates.**
An arm that recovers 3% more using twice the attempts has not necessarily won.

---

## File map

```
TRIAGE/
├── CLAUDE.md                       ← this file
├── error_policy.json               ← MOVE HERE from research/ in Stage 1
├── README.md                       ← written in Stage 5, not before
│
├── research/                       ← reference only. Never imported by code.
│   ├── 01-problem-and-solution.md
│   ├── 02-architecture.md
│   ├── 03-errors-cards-netbanking.md
│   ├── 04-errors-upi-wallets.md
│   ├── 05-api-reference.md
│   ├── 06-ml-lightgbm.md
│   ├── 07-build-plan.md
│   ├── 08-ui-spec.md
│   └── README.md
│
├── src/
│   ├── policy/
│   │   └── engine.py               S1 · loads error_policy.json, resolves code → action
│   ├── api/
│   │   ├── main.py                 S1 · FastAPI app
│   │   ├── errors.py               S1 · Razorpay error envelope
│   │   ├── schemas.py              S1 · Pydantic wire shapes
│   │   ├── deps.py                 S2 · per-request conn, runner, world
│   │   ├── routes_errors.py        S1 · GET /v1/errors, /v1/errors/{code}
│   │   ├── routes_cases.py         S2 · cases, decide, attempts, status-poll
│   │   ├── routes_rails.py         S2 · rail health
│   │   └── routes_eval.py          S3 · simulator run, runs list, eval report
│   ├── simulator/
│   │   ├── generate.py             S2 · payment stream
│   │   ├── declines.py             S2 · error code sampler
│   │   ├── world.py                S2 · HIDDEN STATE — see I-12
│   │   └── rails.py                S2 · downtime event feed
│   ├── store/
│   │   ├── schema.sql              S2 · payments, cases, attempts, audit, downtimes
│   │   └── db.py                   S2 · SQLite access, row types, derived ids
│   ├── executor/
│   │   ├── state.py                S2 · state machine
│   │   └── runner.py               S2 · bounded executor, idempotency
│   ├── arms/
│   │   ├── base.py                 S3 · Arm protocol, CaseSnapshot, ArmDecision
│   │   ├── control.py              S3 · fixed retry +24h ×3, ignores the table
│   │   ├── baseline.py             S3 · policy table only
│   │   └── treatment.py            S4 · policy + model
│   ├── features/
│   │   └── build.py                S4 · 26 features, as_of enforced
│   ├── model/
│   │   └── train.py                S4 · LightGBM
│   └── explain/
│       ├── templates.py            S5 · templated explanations (default)
│       └── llm.py                  S5 · optional, text only
│
├── eval/
│   ├── run_arms.py                 S3 · tick loop, arm assignment
│   ├── score.py                    S3 · dedup, trailing window, per-code, CIs
│   ├── report.py                   S3 · renders the markdown
│   ├── report-normal.md            S3 · generated — do not hand-edit
│   ├── report-bank-outage.md       S3 · generated — do not hand-edit
│   └── runs/                       S3 · one SQLite file per run (gitignored)
│
├── tests/                          see invariants above
│
└── dashboard/                      S5 · Next.js, four screens max
```

**Stage 1 first action:** `git mv research/error_policy.json ./error_policy.json`.
It is a runtime data file, not research. Code loads it from the repo root.

---

## Conventions

**Money is integer paise. Never float.** `499000` is ₹4,990.00. Float arithmetic on
currency is a defect regardless of whether it shows up in this prototype.

**Timestamps are Unix seconds, integer.** Matches Razorpay's API convention.

**Entity IDs follow Razorpay prefixes:** `case_`, `att_`, `dec_`, `down_`, `run_`,
followed by a short base62 string.

**Error responses use the Razorpay envelope:**

```json
{ "error": { "code": "BAD_REQUEST_ERROR", "description": "...",
             "field": "error_code", "source": "business",
             "step": "recovery_decide", "reason": "unknown_error_code" } }
```

| HTTP | Code | When |
|---|---|---|
| 400 | `BAD_REQUEST_ERROR` | Unknown or malformed error code |
| 409 | `IDEMPOTENCY_CONFLICT` | Idempotency key reused — I-5 |
| 422 | `POLICY_VIOLATION` | Would breach `max_attempts` or `drop_dead_at` — I-7 |
| 423 | `AWAITING_STATUS` | Prior outcome unresolved — I-6 |
| 500 | `SERVER_ERROR` | Internal |

**Python:** type hints on public functions. No bare `except`. Dataclasses or Pydantic for
anything crossing a module boundary. `snake_case` filenames.

**Determinism:** every simulator entry point takes a `seed`. Two runs with the same seed
produce byte-identical output. Without this, arm comparison is meaningless.

**No secrets in the repo.** LLM keys come from the environment.

---

## Stage checklists

### Stage 1 — Skeleton + policy engine · ~3h · **DONE**
- [x] `git mv research/error_policy.json ./error_policy.json`
- [x] `src/policy/engine.py` — load, validate, resolve, raise on unknown
- [x] `src/api/main.py` + `routes_errors.py` + `errors.py` + `schemas.py`
- [x] `tests/test_policy_coverage.py` — all 110 resolve, no `UNMAPPED`
- [x] `tests/test_api_errors.py` — 404 and 400 envelope shapes
- **Done when:** `pytest` green, `/v1/errors/insufficient_funds` returns policy, `/docs` renders
- See `SUMMARY.md` for decisions and carried-over gaps.

### Stage 2 — Simulator + case engine · ~5h · **DONE**
- [x] Simulator with hidden world state; scenarios `normal` and `bank_outage` only
- [x] `schema.sql` with the `UNIQUE` idempotency index
- [x] State machine + bounded executor
- [x] `tests/test_idempotency.py`, `tests/test_await_status.py`, `tests/test_hidden_state.py`
- [x] `tests/test_no_wall_clock.py`, `test_state_machine.py`, `test_bounds.py`,
      `test_determinism.py`, `test_simulator.py`
- **Done when:** 2000 payments over 30 days; duplicate → `409`; pending → `423`
- See `SUMMARY.md` for decisions and carried-over gaps.

### Stage 3 — Two arms + eval · ~3h · **SHIPPABLE — REACHED**
- [x] `base.py`, `control.py`, `baseline.py`
- [x] `run_arms.py` tick loop; population generated once, split by assignment (I-13)
- [x] `score.py` implementing I-14, I-15, I-16, I-17, Wilson CIs, two-proportion z
- [x] `eval/report-normal.md` and `eval/report-bank-outage.md` generated
- [x] `routes_eval.py` — `/v1/simulator/run`, `/v1/eval/runs`, `/v1/eval/report/{id}`
- **Done when:** baseline-vs-control uplift with a per-code table including negative rows
- **Result:** baseline +19.2pp over control (34.3% vs 15.1%, p < 0.001) on a sixth of
  the attempts. Baseline **loses** on `SWITCH_RAIL` — published, diagnosed in SUMMARY.md.
- See `SUMMARY.md` for decisions and carried-over gaps.

### Stage 4 — Features + model · ~4h · timeboxed
- [ ] `features/build.py` with required `as_of`
- [ ] `tests/test_no_leakage.py` — must fail if a future event is referenced
- [ ] LightGBM, temporal split, early stopping
- [ ] `treatment.py`, three-arm report
- **Done when:** leakage test passes, both gaps reported separately
- **If the model does not beat baseline, report that and move on.** An honest null result
  is a stronger submission than a fabricated win.

### Stage 5 — Interface + submission · ~4h
- [ ] Dashboard: triage board, case list, case detail with audit trail, eval report. **Four screens, hard limit.**
- [ ] `README.md` — finding, architecture, results, limitations, how to run
- [ ] Architecture diagram
- [ ] 5-minute video
- **Fallback if the dashboard slips:** ship the CLI plus `eval/report.md`. The report
  carries the evidence; the dashboard only presents it.

---

## Never cut

Under any time pressure, these five survive:

1. The audit trail
2. The idempotency guard
3. The `AWAIT_STATUS` block
4. The leakage test
5. The multi-arm evaluation

Everything else is presentation. These are the submission.

---

## Anti-patterns

Things that will silently break this project.

| Anti-pattern | Why it kills you |
|---|---|
| Defaulting unknown codes to retry | Violates I-2. Unbounded loops on merchant misconfiguration. |
| Computing features over the full dataset | Violates I-9. Inflated metrics that collapse under scrutiny. |
| Random train/test split | Violates I-10. Leakage by another name. |
| Letting the LLM pick actions | Violates I-3. Non-reproducible, unauditable, un-A/B-testable. |
| Reporting gross recovery | Uninterpretable. Some of it would have succeeded on dumb retries. |
| Hiding losing segments | Violates I-16. Destroys the project's central claim. |
| Policy reading simulator hidden state | Violates I-12. Makes the entire evaluation circular. |
| Regenerating payments per arm | Violates I-13. Comparing different worlds. |
| Float money | Rounding defects in an audit trail. |
| Building the dashboard before Stage 3 | Presentation before evidence. Wrong order. |

---

## Index — where to look

| Question | File |
|---|---|
| Why does this project exist? What did Stripe do? | `research/01-problem-and-solution.md` |
| Component diagram, state machine, evaluation design | `research/02-architecture.md` |
| Cards + netbanking codes, Stripe mechanism port | `research/03-errors-cards-netbanking.md` |
| UPI + wallets codes, rail-switch lever, mandates | `research/04-errors-upi-wallets.md` |
| Endpoint shapes, request/response JSON, error codes | `research/05-api-reference.md` |
| The 26 features, training config, decision function | `research/06-ml-lightgbm.md` |
| Dashboard screens, components, states | `research/08-ui-spec.md` |
| Stage detail, cut list, demo script, interview Q&A | `research/07-build-plan.md` |
| Machine-readable decision table | `error_policy.json` |

---

## Known boundaries

State these plainly in the README and the video. Naming them is stronger than concealing
them, and a reviewer will find them anyway.

- **Data is synthetic.** No public NPCI decline dataset exists. Simulator parameters are
  grounded in Razorpay's published figures and cited. The arm comparison is robust to the
  simulator's absolute level because all arms face identical data.
- **Adaptive Acceptance cannot be replicated.** It reformats the authorization message,
  which requires issuer access. TRIAGE models the *decision*, not the reformatting.
- **Network Tokens are out of scope.** Requires network certification.
- **Card Account Updater is simulated.** Requires network membership.
- **The Downtime API requires account enablement.** Where unavailable, TRIAGE emits a
  simulated feed conforming to the published schema.

---

## Interview framing

> Razorpay publishes 110 failure reasons and tells merchants what each one means. It
> doesn't tell them what to do. I classified all 110 by recoverable action, found only 27
> can be fixed without human intervention, and built the decision layer that reads the
> failure, checks live rail health, and returns a bounded action — with a multi-arm
> evaluation proving it beats naive retries, including the segments where it doesn't.

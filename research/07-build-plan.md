# 7. TRIAGE — Build Plan

**Five stages. Two days.** Compressed from the original eight-day schedule.

The ordering rule is unchanged: a complete, submittable system must exist before the
model is attempted. **Stage 3 is the shippable checkpoint.** Stages 4 and 5 are upside.

---

## The name

Medical triage sorts casualties by the intervention they need — immediate, delayed,
minor, expectant. TRIAGE does the same to payment failures: 110 reasons sorted into
eight action classes, of which only 27 are recoverable without human intervention.

The name is the thesis. Use it in the demo.

| Triage category | TRIAGE action class | Colour |
|---|---|---|
| Immediate | `RETRY_NOW`, `SWITCH_RAIL` | Red |
| Delayed | `RETRY_SCHEDULED`, `AWAIT_STATUS` | Yellow |
| Minor | `NUDGE_CUSTOMER` | Green |
| Expectant | `SWITCH_INSTRUMENT`, `STOP` | Black |
| Not a casualty | `MERCHANT_ALERT` | Grey |

---

## Stage 1 — Skeleton and policy engine · ~3h · Day 1 morning

The taxonomy already exists. This stage wires it up.

| File | Purpose |
|---|---|
| `error_policy.json` | **Already built.** Copy in. |
| `src/policy/engine.py` | Load policy, resolve code to action + constraints |
| `src/api/main.py` | FastAPI app, health check |
| `src/api/routes_errors.py` | `GET /v1/errors`, `GET /v1/errors/{code}` |
| `tests/test_policy_coverage.py` | Assert all 110 codes resolve, no `UNMAPPED` |
| `docs/`, `README.md` | Copy the documentation set in |

**Done when:** `pytest` green, `/v1/errors/insufficient_funds` returns the policy,
`/docs` renders the OpenAPI page.

**Do not** start the simulator until this passes. Everything downstream reads this table.

---

## Stage 2 — Simulator and case engine · ~5h · Day 1 afternoon

The largest stage. Two halves that must agree on a schema.

### 2a — Simulator

| File | Purpose |
|---|---|
| `src/simulator/generate.py` | Payment stream: customers, merchants, rails, amounts |
| `src/simulator/declines.py` | Sample error codes with a realistic distribution |
| `src/simulator/world.py` | **Hidden state** — true balance, salary date, card validity, rail health |
| `src/simulator/rails.py` | Downtime event feed matching the Razorpay schema |

The hidden state is the point. The world knows the customer's real balance and salary
date; the policy engine never sees them and must infer from `day_of_month`. Without
hidden state you are grading a lookup table.

**Two scenarios only:** `normal` and `bank_outage`. Cut `festival_peak` and `mandate_wave`.

### 2b — Case engine

| File | Purpose |
|---|---|
| `src/store/schema.sql` | `cases`, `attempts`, `audit`, `downtimes` (SQLite) |
| `src/executor/state.py` | State machine with the eight transitions |
| `src/executor/runner.py` | Bounded executor: `max_attempts`, `drop_dead_at`, idempotency |
| `src/api/routes_cases.py` | `POST/GET /v1/recovery/cases`, `/decide`, `/attempts` |
| `tests/test_idempotency.py` | Duplicate key returns `409` |
| `tests/test_await_status.py` | `payment_pending` attempt returns `423` |

**Done when:** 2000 payments generate over 30 simulated days, the double-attempt test
returns `409`, and the `AWAIT_STATUS` test returns `423`.

Those two tests are the safety story. They are non-negotiable.

---

## Stage 3 — Two arms and the eval harness · ~3h · Day 1 evening

**This is the shippable checkpoint.** At the end of this stage TRIAGE is a complete,
honest, defensible submission with zero machine learning in it.

| File | Purpose |
|---|---|
| `src/arms/control.py` | Fixed retry: +24h, ×3, stop |
| `src/arms/baseline.py` | Policy table only, no model |
| `eval/run_arms.py` | Split the batch, run both arms, score |
| `eval/score.py` | Dedup by payment, trailing window, per-code breakdown |
| `eval/report.md` | Generated output |

**Measurement rules, enforced in `score.py`:** deduplicate to the payment not the
attempt, apply a trailing window before scoring, report attempt counts alongside
recovery, and break results out per error code including negative rows.

**Done when:** `eval/report.md` shows baseline-vs-control uplift with a per-code table.

**If everything after this stage fails, submit anyway.** A working policy engine with an
honest two-arm comparison beats a half-finished model every time.

---

## Stage 4 — Features and LightGBM · ~4h · Day 2 morning

| File | Purpose |
|---|---|
| `src/features/build.py` | 26 features, `as_of` filtering enforced |
| `tests/test_no_leakage.py` | **Fails if any feature references an event at or after its own timestamp** |
| `src/model/train.py` | LightGBM, temporal split, early stopping |
| `src/arms/treatment.py` | Policy table + model timing |
| `eval/run_arms.py` | Extend to three arms |

**Done when:** the leakage test passes, and `eval/report.md` shows all three arms with
both gaps reported separately — baseline over control, and treatment over baseline.

**Timebox this to four hours.** If the model does not beat baseline, report that finding
and move on. An honest null result is a stronger submission than a fabricated win, and
it is a genuine outcome on synthetic data.

Cut SHAP. LightGBM's native `feature_importance` is enough.

---

## Stage 5 — Interface, README, video · ~4h · Day 2 afternoon

| File | Purpose |
|---|---|
| `dashboard/` | Case list, decision detail, rail health, triage board, eval report |
| `src/explain/templates.py` | Templated explanations — **not** an LLM |
| `src/explain/llm.py` | Optional. Only if the dashboard finished early. |
| `README.md` | Finding, architecture, results, limitations, how to run |
| `docs/architecture.png` | One diagram |
| `demo.mp4` | Five minutes |

**Dashboard scope, hard limit:** four screens. Triage board (the colour-coded taxonomy),
case list, single case with its audit trail, eval report. No auth, no settings, no
pagination polish.

**Fallback if the dashboard slips:** ship the CLI plus the generated `eval/report.md`.
The report is what carries the evidence. A missing dashboard costs presentation points;
a missing evaluation costs the submission.

---

## Cut list

Removed from the original plan, deliberately:

| Cut | Replaced by |
|---|---|
| LLM explainer as a core feature | Templated strings; LLM only if time remains |
| Four simulator scenarios | Two — `normal` and `bank_outage` |
| SHAP explanations | LightGBM native feature importances |
| Postgres | SQLite |
| Card Account Updater simulation | Documented as out of scope |
| Dashboard polish | Four functional screens |

**Never cut:** the audit trail, the idempotency guard, the `AWAIT_STATUS` block, the
leakage test, or the multi-arm evaluation. Those five are the submission.

---

## Demo — five minutes

Open with refusals, not recoveries.

| Time | Beat |
|---|---|
| 0:00 | The finding — only 27 of 110 codes are silently recoverable. Triage board on screen. |
| 0:45 | `card_expired` → refuses to retry, returns `do_not_try_again` |
| 1:15 | `payment_pending` → `423 AWAITING_STATUS`, double charge prevented |
| 1:45 | Inject a downtime event live → next UPI failure reroutes to card |
| 2:30 | `insufficient_funds` → scheduled to a salary date, feature importance shown |
| 3:15 | Arm results, **including the codes where treatment loses** |
| 4:00 | Point-in-time correctness and the leakage test |
| 4:30 | Limitations: synthetic data, no Network Tokens, no real Account Updater |

---

## Risk

The honest one: **the LightGBM arm may not beat the policy baseline on synthetic data.**
The policy table already encodes most of the causal structure the simulator generates, so
there may be little left for a model to learn.

That is a real possibility, not a reason to skip Stage 4. Report the gap either way. A
submission that says *"the taxonomy delivered 6.4 points, the model added 0.3 and here is
why"* demonstrates more judgement than one claiming a win it cannot support.

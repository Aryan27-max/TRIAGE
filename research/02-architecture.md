# 2. Architecture

## 2.1 Principle

> **A language model never decides a money action.**

The decision core is deterministic: a policy table plus a scored model, both reproducible
and auditable. LLMs sit strictly at the edges — turning a structured decision into English,
and drafting customer messages.

This is not a limitation. It is what makes the system A/B testable, explainable to a
merchant, and defensible in review. A non-deterministic decision cannot be evaluated.

## 2.2 Components

```
                    ┌──────────────────────┐
                    │  FAILURE SIMULATOR   │  generates failed payments
                    │  (hidden ground      │  with realistic causes and
                    │   truth state)       │  hidden recovery state
                    └──────────┬───────────┘
                               │  payment.failed
                               ▼
┌───────────────────────────────────────────────────────────┐
│                      INGEST                               │
│  normalise → attach error code, source, rail, amount      │
└──────────────────────────┬────────────────────────────────┘
                           ▼
┌───────────────────────────────────────────────────────────┐
│                     DIAGNOSE                              │
│  error_policy.json lookup → action class + constraints    │
│  Deterministic. No model. No LLM.                         │
└──────────────────────────┬────────────────────────────────┘
                           ▼
┌───────────────────────────────────────────────────────────┐
│                  CONTEXT ASSEMBLY                         │
│  rail health · customer history · calendar features       │
└──────────────────────────┬────────────────────────────────┘
                           ▼
┌───────────────────────────────────────────────────────────┐
│                   SCORE  (LightGBM)                       │
│  P(success | candidate action, candidate time)            │
│  Only invoked for RETRY_SCHEDULED and SWITCH_RAIL         │
└──────────────────────────┬────────────────────────────────┘
                           ▼
┌───────────────────────────────────────────────────────────┐
│                     POLICY                                │
│  argmax expected value − attempt cost, subject to caps    │
└──────────────────────────┬────────────────────────────────┘
                           ▼
┌───────────────────────────────────────────────────────────┐
│                BOUNDED EXECUTOR                           │
│  idempotency keys · max attempts · drop-dead date         │
│  every transition written to the audit log                │
└──────────────────────────┬────────────────────────────────┘
                    ┌──────┴──────┐
                    ▼             ▼
            ┌──────────────┐  ┌──────────────┐
            │ EXPLAINER    │  │ AUDIT LOG    │
            │ (LLM)        │  │ (append-only)│
            └──────────────┘  └──────────────┘
```

## 2.3 Case state machine

```
RECEIVED ──▶ DIAGNOSED ──▶ SCHEDULED ──▶ ATTEMPTING ──┬──▶ RECOVERED   (terminal)
                  │              ▲                     │
                  │              └─────────────────────┤
                  │                                    ├──▶ EXHAUSTED   (terminal)
                  ├──▶ AWAITING_STATUS ────────────────┤
                  ├──▶ ESCALATED ──────────────────────┤   (nudge sent)
                  └──▶ STOPPED ────────────────────────┘   (terminal)
```

**Hard bounds, enforced in code, not prompts:**

- `max_attempts` — default 4, configurable per merchant
- `drop_dead_at` — absolute cutoff timestamp; no action fires after it
- `min_interval` — no two attempts on one case within this window
- `idempotency_key` — `{case_id}:{attempt_number}`, unique constraint at the database level
- `AWAIT_STATUS` cases cannot transition to `ATTEMPTING` until a status poll resolves

The idempotency constraint is the mechanism that makes the double-charge class safe. It is
enforced by a unique index, not by application logic.

## 2.4 Rail health

A sixth signal family that Stripe's five do not include. Razorpay publishes rail
degradation through a Downtime API and webhooks, graded by severity and scoped to a
specific VPA handle, a whole PSP, a netbanking bank, a card network, or all of UPI.

The prototype models this schema faithfully:

```json
{
  "entity": "payment.downtime",
  "method": "upi",
  "scope": "psp",
  "instrument": "@oksbi",
  "severity": "high",
  "status": "started",
  "begin": 1735689600
}
```

Severity maps to policy directly:

| Severity | Meaning | Policy effect |
|---|---|---|
| `high` | Issuer, bank or network is down | Suppress retries on this rail; prefer `SWITCH_RAIL` |
| `medium` | Elevated declines or depressed success | Increase scheduled wait; penalise this rail in scoring |
| `low` | Unknown cause, minimal impact | Feature input only, no hard rule |

**Availability note:** the live Downtime API requires account enablement. Where unavailable,
the prototype emits a simulated feed conforming to the same schema. This is stated
explicitly rather than obscured.

## 2.5 Evaluation: three arms

The measurement design follows Stripe's published A/B methodology for Authorization Boost —
control against treatment, a trailing window to capture late-landing retries, and
deduplication so a payment attempted four times counts once, scored on its final outcome.

| Arm | Behaviour | Purpose |
|---|---|---|
| **Control** | Fixed retry: +24h, ×3, stop | The naive baseline everyone actually runs |
| **Baseline** | Policy table only, no ML | Isolates the value of the taxonomy |
| **Treatment** | Policy table + LightGBM timing | Isolates the value of learning |

Two gaps get reported: *baseline − control* (value of the taxonomy) and
*treatment − baseline* (value of the model).

**The headline metric is uplift over control, not gross recovery.** Gross recovery is
uninterpretable because a portion would have succeeded under naive retries anyway.

**Measurement rules:**

1. Run the batch, then wait a trailing window before scoring — a retry scheduled on day 6
   may land on day 9. Cutting at day 6 undercounts the treatment arm.
2. Deduplicate to the payment, not the attempt. One payment retried four times is one
   payment, scored on whether it eventually succeeded.
3. Report attempt cost alongside recovery. An arm that recovers 3% more using twice the
   attempts has not necessarily won.
4. Segment results by error code and by rail. A blended number hides the cases where the
   treatment arm loses — and those are the ones worth showing.

## 2.6 Stack

| Layer | Choice | Rationale |
|---|---|---|
| API | FastAPI | Async, generates OpenAPI automatically |
| Model | LightGBM | Trains in seconds, native feature importances for explainability |
| Store | SQLite (Postgres-compatible schema) | Zero setup for a prototype |
| Simulator | Python | Full control of hidden ground-truth state |
| Explainer | Hosted LLM, small model | Text only, never decisions |
| Dashboard | Next.js | Razorpay-style case list, decision detail, eval report |

# 5. API Reference

Base URL `http://localhost:8000/v1` · Auth `Authorization: Bearer <key>` ·
All bodies JSON · All timestamps Unix seconds (Razorpay convention)

Entity IDs follow Razorpay's prefix convention: `case_`, `att_`, `down_`, `run_`.

---

## 5.1 Decision endpoints

### `POST /v1/recovery/decide`

Stateless decision. Given a failure, return the action. **This is the core endpoint** —
everything else is storage around it.

**Request**

```json
{
  "error_code": "insufficient_funds",
  "source": "bank",
  "method": "upi",
  "amount": 499000,
  "currency": "INR",
  "attempt_number": 1,
  "first_failed_at": 1735689600,
  "customer": {
    "id": "cust_MkL2p",
    "vpa_handle": "@oksbi",
    "payer_bank": "SBIN",
    "city_tier": 2
  },
  "merchant": { "id": "acc_9xQ", "mcc": "5411" }
}
```

**Response `200`**

```json
{
  "decision_id": "dec_NpQ4vR",
  "action": "RETRY_SCHEDULED",
  "family": "S",
  "scheduled_at": 1735948800,
  "confidence": 0.71,
  "alternatives": [
    { "action": "SWITCH_RAIL", "target": "card", "score": 0.44 },
    { "action": "NUDGE_CUSTOMER", "score": 0.31 }
  ],
  "reason_code": "balance_dependent_salary_cycle",
  "explanation": "Held until Monday 09:00. This code is balance-dependent and the customer's bank credits salary on the 1st; 62% of prior recoveries on this handle landed within 24h of a salary date.",
  "constraints": {
    "attempts_remaining": 3,
    "drop_dead_at": 1736294400,
    "min_interval_hours": 24
  },
  "model": { "name": "lgbm_timing", "version": "1.2.0", "arm": "treatment" }
}
```

**Response `200` — unrecoverable**

```json
{
  "decision_id": "dec_NpQ8xY",
  "action": "SWITCH_INSTRUMENT",
  "scheduled_at": null,
  "reason_code": "instrument_permanently_unusable",
  "explanation": "Card has expired. No retry can succeed. Customer must supply a new instrument.",
  "advice": "do_not_try_again",
  "constraints": { "attempts_remaining": 0 }
}
```

The `advice` field is this project's analogue of Stripe's `advice_code`. Razorpay
publishes no equivalent.

### `GET /v1/errors/{error_code}`

Policy lookup for a single code. Reads `error_policy.json`.

```json
{
  "code": "bank_technical_error",
  "family": "A",
  "action": "SWITCH_RAIL",
  "min_wait_hours": 0,
  "recoverable": true,
  "policy_note": "CBS error at issuer. Rail switch beats waiting.",
  "razorpay_explanation": "The issuing bank was facing technical problems...",
  "razorpay_next_steps": "The customer must try using another bank account or another method."
}
```

### `GET /v1/errors?family=B&action=SWITCH_RAIL`

Filtered list. Query params: `family` (A|B|S|X), `action`, `recoverable`.

---

## 5.2 Case lifecycle

### `POST /v1/recovery/cases`

Open a recovery case from a failed payment. Idempotent on `payment_id`.

```json
{ "payment_id": "pay_MkL2pQr", "order_id": "order_9xQ",
  "error_code": "bank_technical_error", "source": "bank",
  "method": "upi", "amount": 499000, "failed_at": 1735689600,
  "customer": { "id": "cust_MkL2p", "vpa_handle": "@oksbi" } }
```

**Response `201`**

```json
{ "id": "case_NpQ4vR", "status": "DIAGNOSED",
  "decision": { "action": "SWITCH_RAIL", "target": "card" },
  "attempts": [], "created_at": 1735689601 }
```

### `GET /v1/recovery/cases/{id}`

Full case with decision history and audit trail.

```json
{
  "id": "case_NpQ4vR",
  "status": "RECOVERED",
  "arm": "treatment",
  "original_amount": 499000,
  "recovered_amount": 499000,
  "recovered_at": 1735776000,
  "attempts": [
    { "id": "att_1", "action": "SWITCH_RAIL", "target": "card",
      "at": 1735689900, "outcome": "failed", "error_code": "otp_expired" },
    { "id": "att_2", "action": "NUDGE_CUSTOMER", "channel": "sms",
      "at": 1735692000, "outcome": "delivered" },
    { "id": "att_3", "action": "RETRY_SCHEDULED",
      "at": 1735776000, "outcome": "success" }
  ],
  "audit": [
    { "at": 1735689601, "from": "RECEIVED", "to": "DIAGNOSED",
      "actor": "policy_engine", "reason": "error_policy lookup" },
    { "at": 1735689900, "from": "SCHEDULED", "to": "ATTEMPTING",
      "actor": "executor", "idempotency_key": "case_NpQ4vR:1" }
  ]
}
```

### `POST /v1/recovery/cases/{id}/attempts`

Record an attempt outcome. Requires `Idempotency-Key` header.

`409 Conflict` if the key was already used — the double-charge guard.

### `POST /v1/recovery/cases/{id}/stop`

Force-terminate. Body `{ "reason": "customer_cancelled" }`.

### `GET /v1/recovery/cases?status=SCHEDULED&arm=treatment&limit=50`

Filters: `status`, `arm`, `error_code`, `method`, `from`, `to`.

---

## 5.3 Rail health

### `GET /v1/rails/health`

Mirrors Razorpay's Downtime API schema.

```json
{
  "entity": "collection",
  "count": 2,
  "items": [
    { "id": "down_F1cxDoHWD4fkQt", "entity": "payment.downtime",
      "method": "upi", "scope": "psp", "instrument": "@oksbi",
      "severity": "high", "status": "started",
      "begin": 1735689600, "end": null },
    { "id": "down_F1cxDoHWD4fkQu", "entity": "payment.downtime",
      "method": "card", "scope": "issuer", "instrument": "HDFC",
      "severity": "medium", "status": "started",
      "begin": 1735686000, "end": null }
  ]
}
```

### `POST /v1/rails/health` *(prototype only)*

Inject a downtime event. Used by the demo to trigger live rail switching on stage.

---

## 5.4 Simulation and evaluation

### `POST /v1/simulator/run`

```json
{ "n_payments": 2000, "days": 30, "seed": 42,
  "arms": ["control", "baseline", "treatment"],
  "scenario": "normal" }
```

`scenario` accepts `normal`, `bank_outage`, `festival_peak`, `mandate_wave`.

**Response `202`** — `{ "run_id": "run_Np9x", "status": "running" }`

### `GET /v1/eval/report/{run_id}`

```json
{
  "run_id": "run_Np9x",
  "measurement": {
    "window_days": 30,
    "trailing_window_days": 7,
    "dedup": "by_payment_final_outcome"
  },
  "arms": {
    "control":   { "payments": 667, "recovered": 148, "rate": 0.222,
                   "attempts": 2001, "attempt_cost": 4002 },
    "baseline":  { "payments": 667, "recovered": 191, "rate": 0.286,
                   "attempts": 1204, "attempt_cost": 2408 },
    "treatment": { "payments": 666, "recovered": 213, "rate": 0.320,
                   "attempts": 1180, "attempt_cost": 2360 }
  },
  "uplift": {
    "baseline_vs_control":  { "pp": 6.4, "relative": 0.288 },
    "treatment_vs_baseline":{ "pp": 3.4, "relative": 0.119 },
    "treatment_vs_control": { "pp": 9.8, "relative": 0.441 }
  },
  "by_error_code": [
    { "code": "insufficient_funds", "control": 0.31,
      "treatment": 0.52, "pp": 21.0, "n": 148 },
    { "code": "bank_technical_error", "control": 0.19,
      "treatment": 0.61, "pp": 42.0, "n": 96 },
    { "code": "payment_cancelled", "control": 0.22,
      "treatment": 0.17, "pp": -5.0, "n": 71 }
  ]
}
```

**Note the negative row.** `by_error_code` deliberately includes segments where the
treatment arm underperforms. Reporting only the wins is the failure mode this project
is designed against.

*All numbers above are illustrative response shapes, not results.*

---

## 5.5 Errors

Razorpay-style envelope.

```json
{ "error": { "code": "BAD_REQUEST_ERROR",
    "description": "error_code is not a recognised payment failure reason",
    "field": "error_code", "source": "business", "step": "recovery_decide",
    "reason": "unknown_error_code" } }
```

| HTTP | Code | When |
|---|---|---|
| 400 | `BAD_REQUEST_ERROR` | Malformed or unknown `error_code` |
| 409 | `IDEMPOTENCY_CONFLICT` | Idempotency key reused |
| 422 | `POLICY_VIOLATION` | Action would breach `max_attempts` or `drop_dead_at` |
| 423 | `AWAITING_STATUS` | Attempt blocked — prior outcome unresolved |
| 500 | `SERVER_ERROR` | Internal |

`423` is the double-charge guard surfacing at the API boundary.

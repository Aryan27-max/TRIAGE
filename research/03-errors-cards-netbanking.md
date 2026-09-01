# 3. Family A — Cards + Netbanking

**37 codes.** Stripe's playbook ports to this family with minimal adaptation.

## 3.1 Why these two rails are grouped

| Property | Cards | Netbanking | Shared? |
|---|---|---|---|
| Identity | PAN / token | Bank credentials | Credential-based |
| Authentication | 3DS + OTP | Bank login + OTP | Issuer-mediated, OTP second factor |
| Failure semantics | Global decline codes | Bank-specific, similar shape | Yes |
| Credential rot | Card expiry, reissue | Account closure | Yes |
| Limits | Per-txn, daily, credit | Per-txn, daily | Yes |

Both fail through an **issuer** that owns the credential. Stripe's mechanisms are all
issuer-facing, so they transfer.

## 3.2 Porting Stripe's five mechanisms

| Stripe mechanism | Prototype equivalent | Codes it addresses | Feasible? |
|---|---|---|---|
| **Smart Retries** | `RETRY_SCHEDULED` + LightGBM timing model | `insufficient_funds`, `transaction_daily_limit_exceeded`, `otp_attempts_exceeded`, `bank_cutoff_in_progress` | **Yes — core build** |
| **Adaptive Acceptance** | `SWITCH_RAIL` on opaque declines, plus a false-decline flag | `card_declined`, `payment_declined` | **Partial.** Real Adaptive Acceptance reformats the authorization message — impossible without issuer access. The prototype models the *decision*, not the reformatting. Documented as a limitation, not hidden. |
| **Card Account Updater** | `SWITCH_INSTRUMENT` + a simulated updater service | `card_expired`, `debit_instrument_blocked` | **Simulated.** Real CAU requires network membership. |
| **Network Tokens** | Out of scope | — | **No.** Requires network certification. Stated explicitly. |
| **`advice_code`** | `STOP` + `SWITCH_INSTRUMENT` action classes | 28 + 4 codes | **Yes — and this is the original contribution.** Razorpay publishes no retry advisory. |

**Being honest about the two that cannot be built is worth more in a review than
pretending otherwise.** Adaptive Acceptance and Network Tokens need issuer and network
relationships. Naming that boundary demonstrates you understand what the mechanisms
actually do.

## 3.3 The insufficient-funds case

The single highest-value code in this family, and the clearest showcase of the timing model.

```
insufficient_funds
  ├─ action        RETRY_SCHEDULED
  ├─ min_wait      72h
  ├─ key features  day_of_month, days_to_salary_date,
  │                customer_prior_recovery_lag, amount_bucket
  └─ policy        argmax over candidate slots of
                   P(success | slot) × amount − attempt_cost
```

Indian salary cycles concentrate on the 1st and the 7th. `days_to_salary` is expected to
be the dominant feature for this code — and the model should surface that in its feature
importances, which is exactly the kind of finding worth showing rather than asserting.

## 3.4 Full taxonomy — Family A

| Code | Action | Min Wait Hours | Policy Note |
|---|---|---|---|
| `authentication_failed` | **NUDGE_CUSTOMER** | — | 3DS/OTP not completed. |
| `incorrect_card_details` | **NUDGE_CUSTOMER** | — | Card field entry error. |
| `incorrect_card_expiry_date` | **NUDGE_CUSTOMER** | — | Wrong expiry entered. |
| `incorrect_cardholder_name` | **NUDGE_CUSTOMER** | — | Wrong name entered. |
| `incorrect_cvv` | **NUDGE_CUSTOMER** | — | Wrong CVV. |
| `incorrect_otp` | **NUDGE_CUSTOMER** | — | Wrong OTP entered. |
| `otp_expired` | **NUDGE_CUSTOMER** | — | OTP lapsed, regenerate. |
| `bank_cutoff_in_progress` | **RETRY_SCHEDULED** | 2h | CBS cutoff is a scheduled bank event. |
| `otp_attempts_exceeded` | **RETRY_SCHEDULED** | 24h | Issuer temporarily blocks the card. |
| `transaction_daily_count_exceeded` | **RETRY_SCHEDULED** | 24h | Count resets at the day boundary. |
| `transaction_daily_limit_exceeded` | **RETRY_SCHEDULED** | 24h | Limit resets at the day boundary. |
| `amount_less_than_minimum_amount` | **SWITCH_INSTRUMENT** | — | Below the bank's fixed-fee floor. |
| `bank_account_invalid` | **SWITCH_INSTRUMENT** | — | Account closed. |
| `bank_account_validation_failed` | **SWITCH_INSTRUMENT** | — | Account details unverifiable. |
| `beneficiary_account_does_not_exist` | **SWITCH_INSTRUMENT** | — | Beneficiary account missing. |
| `beneficiary_account_dormant` | **SWITCH_INSTRUMENT** | — | Beneficiary account dormant. |
| `card_declined` | **SWITCH_INSTRUMENT** | — | Opaque issuer decline. Reason not shared. |
| `card_expired` | **SWITCH_INSTRUMENT** | — | Account Updater territory. Never retry as-is. |
| `card_not_enrolled` | **SWITCH_INSTRUMENT** | — | Not enrolled for 3DS. |
| `card_number_invalid` | **SWITCH_INSTRUMENT** | — | Not a valid BIN/IIN. |
| `card_type_invalid` | **SWITCH_INSTRUMENT** | — | Card type disallowed for this MCC. |
| `credit_failed` | **SWITCH_INSTRUMENT** | — | Beneficiary-side credit refusal. |
| `credit_limit_exceeded` | **SWITCH_INSTRUMENT** | — | Cardless EMI limit. |
| `credit_limit_expired` | **SWITCH_INSTRUMENT** | — | Cardless EMI limit lapsed. |
| `credit_limit_inactive` | **SWITCH_INSTRUMENT** | — | Cardless EMI limit dormant. |
| `credit_limit_not_approved` | **SWITCH_INSTRUMENT** | — | Cardless EMI not approved. |
| `credit_not_permitted` | **SWITCH_INSTRUMENT** | — | Beneficiary bank refused credit. |
| `debit_declined` | **SWITCH_INSTRUMENT** | — | Account blocked at issuer. |
| `debit_instrument_blocked` | **SWITCH_INSTRUMENT** | — | Instrument blocked by issuer/customer. |
| `debit_instrument_inactive` | **SWITCH_INSTRUMENT** | — | Instrument frozen. |
| `emi_greater_than_max_amount` | **SWITCH_INSTRUMENT** | — | EMI ceiling exceeded. |
| `emi_plan_unavailable` | **SWITCH_INSTRUMENT** | — | EMI plan withdrawn. |
| `transaction_limit_exceeded` | **SWITCH_INSTRUMENT** | — | Per-transaction ceiling on this card. |
| `user_not_eligible` | **SWITCH_INSTRUMENT** | — | Failed credit eligibility. |
| `user_not_registered_for_netbanking` | **SWITCH_INSTRUMENT** | — | Netbanking not activated. |
| `bank_not_available` | **SWITCH_RAIL** | — | Issuer down. Route to another rail now. |
| `bank_technical_error` | **SWITCH_RAIL** | — | CBS error at issuer. Rail switch beats waiting. |


## 3.5 Shared codes

These apply to both families. Rail-agnostic gateway, status and infrastructure failures.

| Code | Action | Min Wait Hours | Policy Note |
|---|---|---|---|
| `capture_failed` | **AWAIT_STATUS** | — | Authorised but not captured. Capture, do not re-charge. |
| `deemed_transaction` | **AWAIT_STATUS** | — | Acquirer does not know the outcome until next day. |
| `payment_pending` | **AWAIT_STATUS** | — | Late authorisation is possible. Poll status before any retry. |
| `record_not_found` | **AWAIT_STATUS** | — | Status-check was never fired for an intent payment. |
| `verification_failed` | **AWAIT_STATUS** | — | Status API failed; true state unknown. |
| `invalid_email` | **NUDGE_CUSTOMER** | — | Invalid email supplied. |
| `payment_cancelled` | **NUDGE_CUSTOMER** | — | Customer aborted deliberately. |
| `payment_session_expired` | **NUDGE_CUSTOMER** | — | Checkout window lapsed. |
| `payment_timed_out` | **NUDGE_CUSTOMER** | — | Customer exceeded the window. |
| `duplicate_rrn_found` | **RETRY_NOW** | — | Rare RRN collision, resolves on immediate re-attempt. |
| `invalid_response_from_gateway` | **RETRY_NOW** | — | Malformed gateway response, not a decline. |
| `request_timed_out` | **RETRY_NOW** | — | No decision reached; safe to re-attempt. |
| `insufficient_funds` | **RETRY_SCHEDULED** | 72h | Balance-dependent. Align to salary-cycle features. |
| `payment_declined_due_to_high_traffic` | **RETRY_SCHEDULED** | 1h | Load spike. Retry off-peak. |
| `compliance_violation` | **STOP** | — | Compliance block. Retrying is unsafe. |
| `payment_risk_check_failed` | **STOP** | — | Risk decline. Retrying invites penalties. |
| `invalid_user_details` | **SWITCH_INSTRUMENT** | — | Customer record absent. |
| `payment_declined` | **SWITCH_INSTRUMENT** | — | Opaque decline, reason withheld. |
| `gateway_technical_error` | **SWITCH_RAIL** | — | Gateway fault. Secondary terminal. |
| `issuer_technical_error` | **SWITCH_RAIL** | — | Issuer-side fault, instrument is fine. |
| `payment_failed` | **SWITCH_RAIL** | — | Unattributed gateway failure. |
| `server_error` | **SWITCH_RAIL** | — | Razorpay-side fault. |


### The `AWAIT_STATUS` class

Five codes carry an unknown outcome: `payment_pending`, `deemed_transaction`,
`record_not_found`, `verification_failed`, `capture_failed`.

Razorpay's own documentation notes that pending transactions may later become authorized —
late authorization — and that a deemed transaction's status is not known to the acquirer
until the following day.

**Retrying these risks charging the customer twice.** They must poll status before any
re-attempt. A naive retry loop has no concept of this state, which is the single most
demonstrable safety failure to show in a demo.

# 4. Family B — UPI + Wallets

**28 codes.** No published Stripe equivalent exists. This is the original half.

## 4.1 Why these two rails are grouped

| Property | UPI | Wallets | Shared? |
|---|---|---|---|
| Identity | VPA (`user@bank`) | Mobile number | Handle-based, not credential-based |
| Authentication | UPI PIN + device binding | Mobile OTP / wallet PIN | App-mediated, no 3DS |
| Intermediary | PSP app | Wallet provider | A party Stripe's model has no slot for |
| Recurring | UPI Autopay mandate | Wallet auto-debit | Mandate-driven |
| Failure semantics | NPCI codes | Provider codes | Non-card taxonomy |

The defining difference: a **PSP sits between customer and bank**. A UPI failure can
originate at the payer's app, the payer's bank, the NPCI switch, the beneficiary bank, or
the gateway. Razorpay's error payload carries a `source` field naming which one.

**A card decline tells you what went wrong. A UPI failure tells you where.** That extra
dimension is what makes rail switching a viable strategy rather than a guess.

## 4.2 The failure chain

```
Customer's UPI app  →  Customer's bank  →  NPCI  →  Merchant's bank  →  Gateway
   (payer PSP)          (remitter)      (switch)    (beneficiary)
        │                    │              │             │            │
    psp_not_available   insufficient   vpa_resolution  credit_    gateway_
    upi_app_technical_  _funds         _failed         not_       technical_
    error               bank_technical                 permitted  error
                        _error
```

Where the break occurred determines the correct action:

| Break point | Action | Reasoning |
|---|---|---|
| Payer PSP | `SWITCH_RAIL` | Another PSP app works immediately |
| Remitter bank | `RETRY_SCHEDULED` or `SWITCH_RAIL` | Depends on funds vs downtime |
| NPCI switch | `SWITCH_RAIL` | System-wide; wait or change method |
| Beneficiary bank | `SWITCH_INSTRUMENT` | Merchant-side, customer cannot fix |
| Gateway | `SWITCH_RAIL` | Secondary terminal |

## 4.3 The rail-switch lever

The strategy Stripe does not have.

**Scenario.** A ₹4,999 UPI payment fails with `bank_technical_error`. The Downtime API
reports `severity: high` for the payer's bank.

| Approach | Decision | Expected outcome |
|---|---|---|
| Stripe-style (timing only) | Retry in 2–4h once the outage clears | Money arrives in hours; customer may abandon |
| Rail switch | Send a card payment link now | Money arrives in seconds; different infrastructure entirely |

Razorpay's documentation already recommends this shape — for partner bank downtime it
points merchants at multi-terminal routing rather than waiting. **This project makes that
recommendation automatic and per-failure instead of manual and static.**

## 4.4 Mandate sub-taxonomy

UPI Autopay mandates fail in ways card subscriptions do not.

| Code | Action | Note |
|---|---|---|
| `mandate_creation_declined` | `NUDGE_CUSTOMER` | Customer must approve in their PSP app |
| `mandate_creation_expired` | `NUDGE_CUSTOMER` | Approval window lapsed |
| `mandate_creation_timeout` | `NUDGE_CUSTOMER` | PSP did not respond in time |
| `reqauth_mandate_not_acknowledged` | `SWITCH_RAIL` | PSP-side silence, retry via another PSP |
| `funds_blocked_by_mandate` | `RETRY_SCHEDULED` | OTM block holding the funds |
| `upi_autopay_not_supported_on_psp` | `SWITCH_RAIL` | Capability gap, not a failure |

**None of these are retryable in the Stripe sense.** Four of six need the customer to act
inside their PSP app. A timing model applied here produces nothing but wasted attempts.

## 4.5 Flow-deprecation note

NPCI is deprecating the UPI Collect flow — manual VPA entry for payments and mandate
registration — with exemptions for MCC 6012 and 6211. Merchants migrate to Intent or QR.

**Design consequence:** `payment_collect_request_expired`, `collect_request_pending` and
`collect_on_mcc_blocked` are a shrinking failure class. They are retained in the taxonomy
for completeness but should not anchor the demo.

## 4.6 India-specific features

Three signals that carry far more weight here than in Stripe's problem:

| Feature | Rationale |
|---|---|
| `is_peak_window` | UPI success sags during evening peaks as banks contend under simultaneous load |
| `city_tier` | Metro and tier-3 success rates differ by more than 25 points |
| `psp_handle` | Success varies by handle (`@oksbi`, `@ybl`, `@paytm`) independently of the underlying bank |

The third has no card analogue at all. The app and the bank are separate entities in UPI,
and both independently affect the outcome.

## 4.7 Full taxonomy — Family B

| Code | Action | Min Wait Hours | Policy Note |
|---|---|---|---|
| `collect_request_pending` | **NUDGE_CUSTOMER** | — | Awaiting customer approval. |
| `incorrect_atm_pin` | **NUDGE_CUSTOMER** | — | Wrong ATM PIN during UPI registration. |
| `incorrect_pin` | **NUDGE_CUSTOMER** | — | Wrong UPI PIN. |
| `invalid_device` | **NUDGE_CUSTOMER** | — | Device binding incomplete (UPI 2FA). |
| `invalid_mobile_number` | **NUDGE_CUSTOMER** | — | Unregistered mobile (wallet). |
| `mandate_creation_declined` | **NUDGE_CUSTOMER** | — | Customer must re-approve the mandate. |
| `mandate_creation_expired` | **NUDGE_CUSTOMER** | — | Mandate approval window lapsed. |
| `mandate_creation_failed` | **NUDGE_CUSTOMER** | — | Mandate rejected by an entity. |
| `mandate_creation_timeout` | **NUDGE_CUSTOMER** | — | Mandate approval timed out. |
| `mobile_number_invalid` | **NUDGE_CUSTOMER** | — | Mobile not mapped to bank account. |
| `payment_collect_request_expired` | **NUDGE_CUSTOMER** | — | Customer never acted. Re-prompt. |
| `pin_not_set` | **NUDGE_CUSTOMER** | — | UPI PIN never configured. |
| `funds_blocked_by_mandate` | **RETRY_SCHEDULED** | 24h | Funds locked by an OTM block. |
| `pin_attempts_exceeded` | **RETRY_SCHEDULED** | 24h | Issuer temporarily blocks the instrument. |
| `transaction_frequency_limit_exceeded` | **RETRY_SCHEDULED** | 24h | NPCI per-day frequency cap. |
| `collect_on_mcc_blocked` | **STOP** | — | NPCI blocks collect on this MCC. Use intent. |
| `mcc_amount_limit_exceeded` | **STOP** | — | NPCI MCC ceiling. No retry path. |
| `invalid_vpa` | **SWITCH_INSTRUMENT** | — | VPA unregistered or invalid. |
| `transaction_on_vpa_restricted` | **SWITCH_INSTRUMENT** | — | VPA blocked by PSP. |
| `authorisation_declined_by_psp` | **SWITCH_RAIL** | — | PSP rejected authorisation. |
| `psp_app_ not_available` | **SWITCH_RAIL** | — | PSP app unavailable. |
| `psp_app_not_supported` | **SWITCH_RAIL** | — | PSP blacklisted for this flow. |
| `psp_not_available` | **SWITCH_RAIL** | — | PSP downtime. Another PSP app works. |
| `psp_not_registered` | **SWITCH_RAIL** | — | PSP not on this device. |
| `reqauth_mandate_not_acknowledged` | **SWITCH_RAIL** | — | PSP silent on mandate auth. |
| `upi_app_technical_error` | **SWITCH_RAIL** | — | Payer PSP fault. |
| `upi_autopay_not_supported_on_psp` | **SWITCH_RAIL** | — | Autopay unsupported on this PSP. |
| `vpa_resolution_failed` | **SWITCH_RAIL** | — | NPCI resolution service fault. |


## 4.8 Merchant-side codes

**23 codes are not payment failures.** They are merchant integration or configuration
defects: `invalid_order_id`, `live_mode_not_enabled`, `payment_method_not_enabled`,
`merchant_not_activated`, `order_amount_mismatch` and similar.

A recovery engine must **never retry these**. It must raise a merchant alert. Retrying a
misconfiguration produces an unbounded loop of guaranteed failures.

Separating this class out is a small piece of the design that a naive implementation
misses entirely — and it accounts for one code in five.

| Code | Action | Min Wait Hours | Policy Note |
|---|---|---|---|
| `bank_not_enabled` | **MERCHANT_ALERT** | — | Merchant configuration or integration defect. |
| `card_network_not_enabled` | **MERCHANT_ALERT** | — | Merchant configuration or integration defect. |
| `duplicate_refund_id` | **MERCHANT_ALERT** | — | Merchant configuration or integration defect. |
| `duplicate_request` | **MERCHANT_ALERT** | — | Merchant configuration or integration defect. |
| `input_validation_failed` | **MERCHANT_ALERT** | — | Merchant configuration or integration defect. |
| `international_transaction_not_allowed` | **MERCHANT_ALERT** | — | Merchant configuration or integration defect. |
| `invalid_amount` | **MERCHANT_ALERT** | — | Merchant configuration or integration defect. |
| `invalid_currency` | **MERCHANT_ALERT** | — | Merchant configuration or integration defect. |
| `invalid_order_id` | **MERCHANT_ALERT** | — | Merchant configuration or integration defect. |
| `invalid_request` | **MERCHANT_ALERT** | — | Merchant configuration or integration defect. |
| `live_mode_not_enabled` | **MERCHANT_ALERT** | — | Merchant configuration or integration defect. |
| `merchant_not_activated` | **MERCHANT_ALERT** | — | Merchant configuration or integration defect. |
| `mismatch_in_transaction_details` | **MERCHANT_ALERT** | — | Merchant configuration or integration defect. |
| `order_already_paid` | **MERCHANT_ALERT** | — | Merchant configuration or integration defect. |
| `order_amount_mismatch` | **MERCHANT_ALERT** | — | Merchant configuration or integration defect. |
| `order_payment_method_mismatch` | **MERCHANT_ALERT** | — | Merchant configuration or integration defect. |
| `payment_amount_tampered` | **MERCHANT_ALERT** | — | Merchant configuration or integration defect. |
| `payment_method_not_enabled` | **MERCHANT_ALERT** | — | Merchant configuration or integration defect. |
| `payment_pending_approval` | **MERCHANT_ALERT** | — | Merchant configuration or integration defect. |
| `recurring_payment_not_enabled` | **MERCHANT_ALERT** | — | Merchant configuration or integration defect. |
| `refund_limit_crossed` | **MERCHANT_ALERT** | — | Merchant configuration or integration defect. |
| `upi_collect_not_enabled` | **MERCHANT_ALERT** | — | Merchant configuration or integration defect. |
| `upi_intent_not_enabled` | **MERCHANT_ALERT** | — | Merchant configuration or integration defect. |


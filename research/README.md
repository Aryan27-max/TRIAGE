# TRIAGE — Payment Recovery Engine

**A decision layer for failed payments on Razorpay-style rails.**

Razorpay tells a merchant *what went wrong*. It does not tell them *what to do about it*.
This project builds the missing layer: it reads the failure reason, checks whether the rail
is currently degraded, and returns a bounded, explainable action — retry now, retry at a
predicted time, switch rail, switch instrument, nudge the customer, wait for status, or stop.

Built for the Razorpay Buildathon, Track 03 — AI Revenue Recovery.

---

## The finding that drives the design

Razorpay publishes **110 distinct payment failure reasons**. Classified by what a
recovery system can actually *do* about each one:

| Action class | Codes | Meaning |
|---|---|---|
| `SWITCH_INSTRUMENT` | 28 | Instrument is unusable. A retry can never work. |
| `NUDGE_CUSTOMER` | 23 | Blocked on a human. Silent retry is pointless. |
| `MERCHANT_ALERT` | 23 | Merchant misconfiguration. Not a customer failure at all. |
| `SWITCH_RAIL` | 15 | Rail is degraded. Same instrument, different route. |
| `RETRY_SCHEDULED` | 9 | Will plausibly succeed later. Predict when. |
| `AWAIT_STATUS` | 5 | Outcome unknown. **Retrying risks a double charge.** |
| `STOP` | 4 | Retrying is unsafe or penalised. |
| `RETRY_NOW` | 3 | Transient glitch. Immediate re-attempt. |

**Only 27 of 110 codes are silently recoverable.** A naive "retry everything three times"
loop is therefore wrong on roughly three-quarters of failures — wasting attempts on 28 dead
instruments, ignoring 23 cases that need a human, hiding 23 merchant bugs, and risking
double charges on 5.

That gap is the product.

---

## Scope

This is an **interview-defensible prototype**, not production infrastructure.

**In scope:** the decision policy, a realistic failure simulator, a LightGBM timing model,
a Razorpay-style dashboard, a three-arm evaluation harness, and full API documentation.

**Out of scope:** real money movement, real Razorpay credentials, PCI scope, horizontal
scale, live bank connectivity.

---

## Docs

| File | Contents |
|---|---|
| `docs/01-problem-and-solution.md` | The problem, Stripe's answer, why India differs |
| `docs/02-architecture.md` | Components, data flow, state machine, evaluation design |
| `docs/03-errors-cards-netbanking.md` | Family A taxonomy + Stripe mechanism port |
| `docs/04-errors-upi-wallets.md` | Family B taxonomy + the India-specific solution |
| `docs/05-api-reference.md` | Full REST API specification |
| `docs/06-ml-lightgbm.md` | Feature set, training, evaluation |
| `docs/07-build-plan.md` | Day-by-day build order |
| `error_policy.json` | The decision table, machine-readable |

---

## Attribution

Error reasons, descriptions and next steps are Razorpay's published documentation.
The **action classification, wait windows and decision policy are this project's
contribution** — Razorpay does not publish a machine-readable retry advisory.

Stripe's `advice_code: do_not_try_again` is the closest published equivalent anywhere.
Razorpay has no counterpart. This project builds it for Indian rails.

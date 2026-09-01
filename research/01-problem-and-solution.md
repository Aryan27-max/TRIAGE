# 1. Problem, Prior Art, and the India Gap

## 1.1 The problem

A payment fails. Razorpay returns a reason code such as `insufficient_funds` or
`bank_technical_error`, plus a `source` field naming which party in the chain failed.

Most merchants respond with fixed-interval retries: wait 24h, retry, three times, give up.

This is wrong in two expensive directions:

- **Over-retrying.** Re-attempting `card_expired` or `invalid_vpa` cannot succeed. Each
  attempt costs money, and card networks penalise excessive retries on dead credentials.
- **Under-retrying.** A customer whose balance was empty on the 5th will pay happily on the
  8th. A fixed schedule that gives up on day 3 loses a customer who never intended to leave.

The underlying cause: **"payment failed" is not a diagnosis.** It is 110 different
situations sharing one label.

## 1.2 Why it is worth solving

Involuntary churn — customers who stop paying because the mechanism broke, not because
they chose to leave — is a large and largely unaddressed leak. Stripe's published finding
is that roughly a quarter of lapsed subscriptions are payment failures rather than
cancellations, and that recovered subscriptions continue for an average of seven further
months. Recovery is therefore economically comparable to acquisition, at a fraction of
the cost.

## 1.3 Prior art: Stripe's five mechanisms

Stripe does not solve this with one tool. Their authorization stack is five distinct
mechanisms, each targeting a different failure class.

| Mechanism | Failure class targeted | Technique |
|---|---|---|
| Smart Retries | Temporary decline (funds, timeout) | ML predicts the optimal re-attempt time, days ahead |
| Adaptive Acceptance | False decline — good payment wrongly rejected | Real-time retry with reformatted authorization request |
| Card Account Updater | Stale credentials | Fetches replacement card details from the issuer |
| Network Tokens | Credential rot, prevented | Token survives card reissue |
| `advice_code` | Unrecoverable | Explicitly signals `do_not_try_again` |

Two design decisions worth importing:

**Latency is not a constraint.** Because the best retry time is often days away, Stripe
could afford a heavy model. Their Smart Retries model consumes 500+ attributes across
five families: customer, business, payment, seasonality, and billing. Adaptive Acceptance
was later migrated from XGBoost to a TabTransformer-based network.

**A large part of the product is refusing to act.** Adaptive Acceptance blocks payments
with a low probability of authorization specifically to avoid network penalties. Knowing
when to stop is a feature, not an omission.

## 1.4 Why India is a different problem

Stripe optimises **one variable**: when to retry. Cards are effectively a single road.

Razorpay operates **four independent rails** — UPI, cards, netbanking, wallets — that share
almost no infrastructure. So the optimisation is two-dimensional:

> **When do I retry, and which rail do I send it down?**

The second dimension does not exist in Stripe's problem. It is the core India-specific
opportunity, and Razorpay's own documentation already points at it: for partner bank
downtime, the recommended remedy is multi-terminal routing, not waiting.

Four further structural differences:

| Factor | Stripe's world | Indian rails |
|---|---|---|
| Rail switching | Marginal | **Primary lever** — an issuer outage does not affect UPI |
| Time-of-day | Weak signal | Strong — bank load sags measurably during evening peaks |
| Geography | Country-level | Metro vs tier-3 success gap exceeds 25 points |
| Failure vocabulary | Card decline codes (global standard) | NPCI codes — no published decision mapping exists |

**Consequence for this project:** the cards/netbanking half is a *port* of a documented
playbook. The UPI/wallets half is *original* — nobody has published the equivalent
decision table for NPCI failure reasons. Both together make it a Razorpay project rather
than a Stripe clone.

## 1.5 The two families

Rails are grouped by whether Stripe's playbook transfers.

| Family | Rails | Why grouped | Codes |
|---|---|---|---|
| **A** | Cards + Netbanking | Issuer-authenticated, credential-based, 3DS/OTP, global decline semantics. Stripe's mechanisms port directly. | 37 |
| **B** | UPI + Wallets | PSP-mediated, VPA/mobile-identified, PIN-authenticated, mandate-driven, NPCI semantics. No Stripe equivalent. | 28 |
| Shared | Both | Rail-agnostic gateway and status failures | 22 |
| Merchant | Neither | Integration and configuration defects | 23 |

# 6. The Model — LightGBM

## 6.1 What the model does, and does not

**Does:** estimate `P(success | failure context, candidate action, candidate time)`.

**Does not:** choose the action. The action class comes from the deterministic policy
table. The model only ranks *candidate executions* within an already-permitted class.

This separation matters. It means an unrecoverable failure can never be retried because of
a model error — the policy table forbids it before the model is consulted. **Safety is
structural, not learned.**

The model is invoked for exactly two action classes: `RETRY_SCHEDULED` and `SWITCH_RAIL`.
The other six are fully determined by policy.

## 6.2 Why LightGBM and not a transformer

| Consideration | Verdict |
|---|---|
| Data | Tabular, mixed categorical/numeric — GBDT's home ground |
| Volume | Tens of thousands of simulated rows. Transformers need orders of magnitude more |
| Explainability | Native feature importances and SHAP. Required by the audit trail |
| Training time | Seconds. Enables rapid iteration under deadline |
| Credibility | Standard in payments risk. Uncontroversial in review |

Stripe eventually moved Adaptive Acceptance to a TabTransformer-based network — but at
billions of transactions. **At prototype scale, gradient boosting is the correct choice,
and saying why demonstrates better judgement than reaching for the fancier architecture.**

## 6.3 Feature set

Organised along Stripe's five families, plus the sixth that India requires.

### Customer
| Feature | Type | Note |
|---|---|---|
| `payer_bank` | categorical | Issuer or remitter |
| `psp_handle` | categorical | `@oksbi`, `@ybl` — no card analogue |
| `city_tier` | ordinal | Metro / tier-2 / tier-3 |
| `cust_hist_success_rate` | float | Rolling, point-in-time |
| `cust_prior_failures_30d` | int | |
| `cust_prior_recovery_lag_h` | float | Their historical recovery latency |

### Business
| Feature | Type | Note |
|---|---|---|
| `mcc` | categorical | Merchant category |
| `ticket_size_band` | ordinal | Amount bucket |
| `is_recurring` | bool | Mandate vs one-time |

### Payment
| Feature | Type | Note |
|---|---|---|
| `error_code` | categorical | 110 levels |
| `error_source` | categorical | Which party failed — UPI-specific richness |
| `action_class` | categorical | From the policy table |
| `method` | categorical | upi / card / netbanking / wallet |
| `attempt_number` | int | |
| `hours_since_first_failure` | float | |
| `amount` | float | Log-scaled |

### Seasonality
| Feature | Type | Note |
|---|---|---|
| `hour_of_day` | cyclical | sin/cos encoded |
| `day_of_week` | cyclical | |
| `day_of_month` | int | |
| `days_to_salary_date` | int | India-specific: 1st and 7th |
| `is_peak_window` | bool | Evening bank contention |
| `is_month_end` | bool | |

### Billing
| Feature | Type | Note |
|---|---|---|
| `mandate_type` | categorical | autopay / enach / si / none |
| `billing_cycle_day` | int | |

### Rail health — the sixth family
| Feature | Type | Note |
|---|---|---|
| `rail_downtime_active` | bool | From the Downtime feed |
| `rail_downtime_severity` | ordinal | high / medium / low / none |
| `rail_success_rate_1h` | float | Rolling window for this rail |
| `alt_rail_success_rate_1h` | float | Drives the switch decision |

**26 features.** Stripe uses 500+ because they have billions of real transactions. Claiming
a comparable count on simulated data would be dishonest and would overfit.

## 6.4 Point-in-time correctness

**The single most important implementation constraint.**

Every rolling feature must be computed using only data available *strictly before* the
attempt timestamp. Computing `cust_hist_success_rate` over the whole dataset leaks the
outcome and produces an AUC that collapses on deployment.

Enforcement:
- Feature builder takes `as_of` and filters `WHERE event_time < as_of`
- A unit test asserts that no feature row references an event at or after its own timestamp
- Train/test split is **temporal**, never random

State this explicitly in the README. It is the first thing a payments ML reviewer checks,
and the fact that Vulcan's own design emphasises never learning from what was not knowable
at the time makes it a strong point of contact in an interview.

## 6.5 Training

```python
import lightgbm as lgb

params = {
    "objective": "binary",
    "metric": ["auc", "average_precision"],
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_data_in_leaf": 50,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "is_unbalance": True,
    "verbosity": -1,
}

model = lgb.train(
    params, train_set,
    valid_sets=[valid_set],
    num_boost_round=800,
    callbacks=[lgb.early_stopping(50), lgb.log_evaluation(100)],
)
```

**Label:** `1` if the attempt succeeded, else `0`. One row per *attempt*, not per payment.

**Split:** temporal. Train on days 1–21, validate 22–26, test 27–30.

## 6.6 From score to decision

```python
def choose(case, policy, model, now):
    action = policy[case.error_code].action          # deterministic
    if action not in ("RETRY_SCHEDULED", "SWITCH_RAIL"):
        return Decision(action, scheduled_at=None)   # model not consulted

    candidates = enumerate_slots(case, action, now)  # (action, target, ts)
    best, best_ev = None, -1
    for c in candidates:
        p  = model.predict(features(case, c))
        ev = p * case.amount - ATTEMPT_COST
        if ev > best_ev and satisfies_constraints(case, c):
            best, best_ev = c, ev
    return Decision(best, expected_value=best_ev) if best_ev > 0 \
           else Decision("STOP", reason="negative_expected_value")
```

**The `STOP` fallback is the point.** When no candidate has positive expected value, the
system declines to act. That is the local equivalent of Stripe blocking payments unlikely
to be authorized — refusing to spend an attempt is a decision, and the audit log records it
as one.

## 6.7 Evaluation

**Model level:** PR-AUC (primary — the classes are imbalanced), ROC-AUC, calibration curve,
feature importance, and a temporal holdout.

**System level:** the three-arm comparison in `docs/02-architecture.md`. This is what the
submission reports. Model metrics are diagnostic; recovery uplift is the result.

**Honesty requirements, non-negotiable:**

1. Report `treatment_vs_baseline` separately. If the model adds nothing over the policy
   table, say so. That is a legitimate finding and a stronger submission than an
   unsupported win.
2. Publish the per-error-code breakdown including negative rows.
3. Report attempt counts and cost alongside recovery rates.
4. Include a stress scenario the model never trained on — `bank_outage` — and report
   the degradation.

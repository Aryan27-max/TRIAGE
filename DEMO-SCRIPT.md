# Demo script — five minutes

Updated for the Stage 4 null result. **The null is the strongest thing in this
submission** — it is not buried at the end, and it is not apologised for.

Setup before recording:

```bash
uv run python -m src.simulator.generate --n 8000 --days 30 --seed 42
uv run uvicorn src.api.main:app                    # terminal 1
cd dashboard && npm run dev                        # terminal 2
```

Open the **Taxonomy** screen. Have the scenario menu ready on the Live screen.

---

## 0:00 — the finding

**Taxonomy screen.**

> Razorpay publishes 110 distinct payment failure reasons, and for each one it tells the
> merchant what it means. It does not tell them what to *do*.
>
> I classified all 110 by recoverable action. This is that classification. Red and amber
> at the top are the codes a system can fix by itself. Everything hollow or dashed below
> cannot be fixed by any retry — the instrument is dead, or a human has to act, or the
> merchant has a configuration fault.
>
> **Twenty-seven of a hundred and ten.** That is the whole premise. If you run a "retry
> three times" loop, you are wrong on roughly three quarters of your failures — and on
> five of them you risk charging the customer twice.

*Hover one code — `insufficient_funds` — to show Razorpay's explanation next to the
policy note.*

---

## 0:45 — a retry that can never work

**Live screen.** Scenario menu → `card_expired`. Pay.

> The card has expired. Watch the Inspector.
>
> `policy.resolve` returns SWITCH_INSTRUMENT with advice `do_not_try_again` — this
> project's analogue of Stripe's `advice_code`, which Razorpay publishes no equivalent
> for. `scheduled_at` is null. Nothing is scheduled, no attempt is spent, no idempotency
> key is consumed.
>
> And look at `model.score`. **Hollow. Not invoked.** The model is never consulted here,
> because the policy table has already decided. That absence is the safety guarantee: a
> model error cannot cause an unrecoverable failure to be retried.

*Expand the `model.score` row to show the reason text.*

---

## 1:15 — the double-charge guard

**Live screen.** Scenario menu → `payment_pending`. Pay.

> This is the one that matters most. Razorpay's own documentation says a pending
> transaction may authorize late, and a deemed transaction's outcome is not known to the
> acquirer until the following day.
>
> **Retrying this charges the customer twice.** So the executor refuses. The case goes to
> AWAITING_STATUS and stays there until a status poll resolves it.

*Switch to a terminal:*

```bash
curl -s -X POST localhost:8000/v1/recovery/cases/CASE_ID/attempts \
  -H 'Idempotency-Key: demo' -d '{"now":1737028800}'
```

> **423 AWAITING_STATUS.** Not a warning, not a log line — the API refuses. Five of the
> 110 codes carry this, and a naive retry loop has no concept of it at all.

---

## 1:45 — the India-specific lever, live

**Live screen.** Scenario menu → `bank_technical_error`, method UPI. Pay.

> Bank technical error on UPI. The policy says SWITCH_RAIL — same instrument, different
> route. Razorpay's own docs recommend multi-terminal routing for partner bank downtime;
> this makes that automatic and per-failure instead of manual and static. The Inspector
> shows the target: card.
>
> Now watch what happens when the alternate rail is also down.

*Scenario menu → "Inject a downtime event" → `card · high`. Pay the same failure again.*

> The rail-health strip has gone red. And the decision changed: instead of
> `rail_degraded` it now reads `both_rails_degraded_waiting`. It is holding rather than
> switching into a second outage.
>
> That is not a hard-coded demo path. It read the Downtime feed, applied
> research/02's severity rule, and changed its mind.

---

## 2:30 — the null, stated plainly

**Results screen.**

> Three arms on one population, split by a stable hash. Control is fixed retry, +24 hours,
> three times — what merchants actually run. Baseline is the policy table. Treatment is
> the policy table plus a LightGBM model.
>
> Two gaps, reported separately, because blending them would hide which half did the work.
>
> **The taxonomy is worth twenty-one points.** 13.5% to 34.7%, p under 0.001, using a
> seventh of the attempts.
>
> **The model added nothing. Plus 0.2 points, p equals 0.94.** On the surface where it's
> even consulted, it's *minus* four and a half.

*Scroll to Model diagnostics.*

> And here's why, which is the more interesting half.
>
> The model works. PR-AUC 0.97 on a held-out temporal split. Look at what it found:
> `days_to_salary_date` and `day_of_month`, twenty-one and thirteen percent of gain. **It
> discovered the Indian salary cycle from the calendar alone**, without being told it
> exists. That was the prediction in my research notes and it came out.
>
> Now look at `candidate_delay_hours`. **Zero.**
>
> The training data comes from the baseline arm, which schedules every retry at exactly
> `min_wait_hours`. There is no variation in the timing decision — so the model never had
> a chance to learn what a different delay would do. It can rank *which* cases recover. It
> cannot rank *when* to retry.
>
> That's off-policy learning without exploration. No amount of training fixes it. The fix
> is an explorer arm, and that's a simulator change, not a model change.
>
> I could have widened the model's scope until the number went positive. I didn't.

---

## 3:15 — where it loses

**Results screen**, scroll to "Where the policy arm loses".

> Every per-code row is published, including the ones where the policy arm is worse.
>
> The interesting one is `SWITCH_RAIL` — baseline loses to control. Every outage in my
> simulator lasts under six hours, and control waits twenty-four. So "wait a day"
> accidentally beats "switch immediately", on the exact class the taxonomy exists to fix.
>
> The real argument for switching is that the *customer* won't wait a day. I don't model
> abandonment, so the lever's main benefit is invisible to my own evaluation. That's a
> limitation of the measurement, not a defence of the result.

---

## 4:00 — why you can believe the numbers

**Terminal.**

```bash
uv run pytest tests/test_no_leakage.py -q
```

> The most important test in the repo. Point-in-time correctness: every rolling feature
> filters strictly before its own timestamp.
>
> It has four checks and only one of them catches a real leak. Build the features at time
> T. Insert a pile of attempts, recoveries and downtime events dated *after* T for the
> same customer and the same rail. Rebuild at the same cutoff. **Every value must be
> byte-identical.**
>
> A naive implementation that aggregates over the whole table passes the other three and
> fails that one. It's also how I caught two real bugs in my own feature builder — the
> error code and the attempt number were both being read from current state instead of
> being reconstructed as of the cutoff.
>
> The split is temporal, never random. Train days 1–21, validate 22–26, test 27–30, and
> no case appears in two splits.

---

## 4:30 — limitations, named first

> Three things I'd want a reviewer to know before they find them.
>
> **The data is synthetic.** No public NPCI decline dataset exists. Every rate in the
> simulator is either cited to Razorpay's published material or explicitly marked as my
> assumption.
>
> **The simulator and the taxonomy make the same causal claim.** Both say a wrong PIN
> doesn't fix itself. So this tests whether *acting* on that claim beats ignoring it — not
> whether the claim is true. That's the honest frame.
>
> **The negative-expected-value stop never fired.** It's built and tested, but at Indian
> ticket sizes — two rupees of attempt cost against a five-thousand-rupee payment — the
> model would have to predict below 0.04% for it to trigger. I reported zero rather than
> inflating the cost to make it fire.
>
> The taxonomy delivered twenty-one points. The model added nothing, and I can tell you
> exactly why. That's the submission.

---

## If something breaks

| Symptom | Fix |
|---|---|
| Dashboard shows "waking the API" | Free-tier cold start, ~50s. Locally, check uvicorn is on :8000. |
| Downtime injection returns 503 | The instance is read-only. Run locally to drive the rails. |
| Cases screen is empty | `uv run python -m src.simulator.generate --n 8000 --days 30 --seed 42` |
| Results screen has no 3-arm run | `uv run python -m eval.run_arms --seed 42 --scenario normal --arms control,baseline,treatment` |

**Fallback if the dashboard fails entirely:** the evidence is in
`eval/report-normal.md`, `eval/report-bank-outage.md` and `eval/PRODUCTION-CHECK.md`. The
report carries the argument; the dashboard only presents it.

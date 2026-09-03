"""Report generation. `eval/report-*.md` is written by this file and never edited.

Section 6 — where the baseline arm loses — is not optional and not a footnote. A
comparison that only shows its wins is not a comparison, and a reviewer will assume
the losses were hidden rather than absent. Publishing them is the credibility of the
whole submission.
"""

from __future__ import annotations

from pathlib import Path

from eval.score import (
    ATTEMPT_COST_PAISE,
    NUDGE_COST_PAISE,
    Scorecard,
    SegmentRow,
    score,
)
from src.model.dataset import DATA_DIR as MODEL_DIR
from src.policy.engine import PolicyEngine
from src.store import db


# What each adjacent gap in the arm chain actually isolates. research/02 §2.5.
GAP_MEANING: dict[tuple[str, str], str] = {
    ("baseline", "control"): "the value of the **taxonomy**",
    ("treatment", "baseline"): "the value of the **model**",
    ("treatment", "control"): "both together, for context",
}


def rupees(paise: int) -> str:
    return f"₹{paise / 100:,.2f}"


def duration(seconds: int) -> str:
    if seconds <= 0:
        return "—"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _rate_cell(row: SegmentRow, arm: str) -> str:
    rate = row.rate(arm)
    if rate is None:
        return "—"
    n, k = row.counts[arm]
    return f"{rate:.0%} ({k}/{n})"


def _pp_cell(pp: float | None) -> str:
    if pp is None:
        return "—"
    if pp < 0:
        return f"**{pp:+.1f}** ⚠"
    return f"{pp:+.1f}"


def _segment_table(card: Scorecard, rows: list[SegmentRow], first: str) -> list[str]:
    has_model = any(row.pp_model is not None for row in rows)
    headline = f"pp<br>{card.focus}−{card.reference}"
    header = f"| {first} | n | " + " | ".join(card.arms) + f" | {headline} |"
    if has_model:
        header += " pp<br>treatment−baseline |"
    width = len(card.arms) + 3 + (1 if has_model else 0)
    out = [header, "|" + "---|" * width]
    for row in rows:
        label = f"`{row.key}`" + (f" · {row.label}" if row.label else "")
        cells = " | ".join(_rate_cell(row, arm) for arm in card.arms)
        line = f"| {label} | {row.n} | {cells} | {_pp_cell(row.pp)} |"
        if has_model:
            line += f" {_pp_cell(row.pp_model)} |"
        out.append(line)
    return out


def _model_sections(card: Scorecard) -> list[str]:
    """Sections 8 and 9: where the model can act at all, and how well it predicts.

    Section 8 exists because the overall gap understates or overstates the model
    depending on how much of the stream it is even allowed to touch. Reporting the
    blended number alone would be misleading in both directions.
    """
    eligible = card.model_eligible
    if not eligible or "treatment" not in card.arms:
        return []

    lines = [
        "## 8. Model-eligible codes only",
        "",
        f"The model is consulted for **{', '.join(eligible['actions'])}** and nothing "
        f"else. (I-1) That is **{eligible['codes']} of {eligible['total_codes']}** "
        f"codes — on the other "
        f"{eligible['total_codes'] - eligible['codes']}, `treatment` delegates to "
        f"`baseline` and the two arms are identical by construction.",
        "",
        f"This section restricts to the {eligible['payments']} payments whose opening "
        "failure carries a model-eligible code. It is the only surface on which the "
        "model can move anything, and it bounds the achievable uplift before a single "
        "row is scored.",
        "",
        "| arm | payments | recovered | rate | 95% CI |",
        "|---|---|---|---|---|",
    ]
    for arm in card.arms:
        block = eligible["arms"].get(arm)
        if not block:
            continue
        lines.append(
            f"| `{arm}` | {block['payments']} | {block['recovered']} | "
            f"**{block['rate']:.1%}** | {block['ci_low']:.1%} – {block['ci_high']:.1%} |"
        )
    lines += ["", "| gap | pp | z | p |", "|---|---|---|---|"]
    for gap in eligible["gaps"]:
        lines.append(
            f"| `{gap['focus']}` − `{gap['reference']}` | {gap['pp']:+.1f} | "
            f"{gap['z']:.2f} | {gap['p_value']:.3f} |"
        )
    lines.append("")

    diagnostics = card.diagnostics
    metrics = diagnostics.get("metrics") or {}
    if not metrics:
        lines += [
            "## 9. Model diagnostics",
            "",
            "No training artefacts found in `eval/model/`. Run "
            "`python -m src.model.train`.",
            "",
        ]
        return lines

    dataset = diagnostics.get("dataset") or {}
    lines += [
        "## 9. Model diagnostics",
        "",
        "Diagnostic only. The result is the recovery uplift above; these numbers exist "
        "to say *why* it came out that way. A PR-AUC at the base rate means nothing "
        "was learnable; good discrimination with no uplift points at the decision "
        "layer rather than the model.",
        "",
        f"Trained on run `{metrics.get('run_id')}` "
        f"(`{metrics.get('scenario')}`, seed {metrics.get('seed')}), "
        f"{metrics.get('trained_on_arm')} attempts only — control's action choice is "
        "uncorrelated with the error code, so its rows teach nothing "
        "action-conditional. Stopped at iteration "
        f"{metrics.get('best_iteration')}.",
        "",
    ]
    if dataset:
        lines += [
            f"Dataset: {dataset.get('n_rows')} rows, one per attempt (I-11), "
            f"{dataset.get('n_positive')} positive "
            f"({dataset.get('positive_rate', 0):.1%}). Temporal split (I-10), days "
            f"{dataset.get('split_days', {}).get('train')} / "
            f"{dataset.get('split_days', {}).get('valid')} / "
            f"{dataset.get('split_days', {}).get('test')}.",
            "",
        ]

    lines += ["| split | n | positives | base rate | PR-AUC | ROC-AUC | Brier |",
              "|---|---|---|---|---|---|---|"]
    for name, block in (metrics.get("splits") or {}).items():
        if not block.get("n"):
            lines.append(f"| {name} | 0 | — | — | — | — | — |")
            continue
        lines.append(
            f"| {name} | {block['n']} | {block['positives']} | "
            f"{block['base_rate']:.3f} | **{block['pr_auc']:.3f}** | "
            f"{block['roc_auc']:.3f} | {block['brier']:.3f} |"
        )
    lines.append("")
    for warning in metrics.get("warnings", []):
        lines.append(f"> **Warning:** {warning}")
    if metrics.get("warnings"):
        lines.append("")

    calibration = metrics.get("calibration_test") or []
    if calibration:
        lines += [
            "### Calibration, test split",
            "",
            "Predicted probability against observed rate, by decile. Monotone ordering "
            "means the ranking is real; a gap between the two columns means the "
            "*level* is off, which matters because the expected-value argmax multiplies "
            "the predicted probability by the amount.",
            "",
            "| decile | n | predicted | observed |",
            "|---|---|---|---|",
        ]
        for row in calibration:
            lines.append(
                f"| {row['decile']} | {row['n']} | {row['predicted_mean']:.3f} | "
                f"{row['observed_rate']:.3f} |"
            )
        lines.append("")

    importances = diagnostics.get("importances") or []
    if importances:
        total = sum(r["gain"] for r in importances) or 1.0
        lines += [
            "### Top features by gain",
            "",
            "| feature | gain share | splits |",
            "|---|---|---|",
        ]
        for row in importances:
            lines.append(
                f"| `{row['feature']}` | {100 * row['gain'] / total:.1f}% | "
                f"{row['split']} |"
            )
        lines.append("")
    zero_gain = metrics.get("zero_gain_features") or []
    if zero_gain:
        lines += [
            "**Features the model never used:** "
            + ", ".join(f"`{f}`" for f in zero_gain)
            + ".",
            "",
        ]
    return lines


def render(card: Scorecard) -> str:
    run = card.run
    focus = next((g.focus for g in card.gaps), None)
    lines: list[str] = [
        f"# TRIAGE — evaluation report · `{run.scenario}`",
        "",
        "Generated by `eval/report.py`. Do not edit by hand — rerun the harness.",
        "",
        "## 1. Run parameters",
        "",
        "| | |",
        "|---|---|",
        f"| run_id | `{run.run_id}` |",
        f"| seed | {run.seed} |",
        f"| scenario | `{run.scenario}` |",
        f"| payments generated | {run.n_payments} |",
        f"| main window | {run.days} days |",
        f"| trailing window | {run.trailing_days} days (executed, not just tolerated) |",
        f"| tick size | {run.tick_seconds}s |",
        f"| arms | {', '.join(card.arms)} |",
        f"| reference arm | `{card.reference}` |",
        f"| git sha | `{run.git_sha or 'unavailable'}` |",
        "",
        "The run id is derived from these parameters, so an identical run reproduces "
        "to the same id and the same numbers.",
        "",
    ]

    # -- 2. headline ----------------------------------------------------------
    lines += [
        "## 2. Headline — recovery rate per arm",
        "",
        "Deduplicated to the payment and scored on final outcome: a payment attempted "
        "four times is one payment. (I-14) Intervals are Wilson score at 95%.",
        "",
        "| arm | payments | recovered | rate | 95% CI |",
        "|---|---|---|---|---|",
    ]
    for arm in card.arms:
        s = card.scores[arm]
        lines.append(
            f"| `{arm}` | {s.payments} | {s.recovered} | **{s.rate:.1%}** | "
            f"{s.ci_low:.1%} – {s.ci_high:.1%} |"
        )
    lines.append("")

    if card.gaps:
        lines += [
            "Two contributions, reported separately. Blending them into a single "
            "`treatment − control` number would hide which half did the work.",
            "",
            "| gap | measures | pp | relative | z | p |",
            "|---|---|---|---|---|---|",
        ]
        for gap in card.gaps:
            lines.append(
                f"| `{gap.focus}` − `{gap.reference}` | {GAP_MEANING.get((gap.focus, gap.reference), '—')} "
                f"| {gap.pp:+.1f} | {gap.relative:+.1%} | {gap.z:.2f} | {gap.p_value:.3f} |"
            )
        lines.append("")
        for gap in card.gaps:
            verdict = (
                "statistically significant at the 5% level"
                if gap.p_value < 0.05
                else "**not** statistically significant at the 5% level"
            )
            lines.append(
                f"- `{gap.focus}` vs `{gap.reference}`: {gap.pp:+.1f}pp, p = "
                f"{gap.p_value:.3f} — {verdict}."
            )
        lines += [
            "",
            "The confidence intervals overlap-check is the honest reading here: with a "
            "few hundred cases per arm these estimates are wide, and the point of "
            "printing the interval is to show that rather than imply precision that is "
            "not present.",
            "",
        ]

    # -- 3. cost --------------------------------------------------------------
    lines += [
        "## 3. Attempts and cost",
        "",
        f"I-17. An arm that recovers more using twice the attempts has not necessarily "
        f"won. Attempt cost is {rupees(ATTEMPT_COST_PAISE)} each and a nudge "
        f"{rupees(NUDGE_COST_PAISE)} — both our assumptions, and identical across arms, "
        f"so the comparison does not depend on the absolute figures.",
        "",
        "| arm | attempts | per payment | nudges | negative-EV stops | total cost | "
        "cost per recovery | amount recovered |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for arm in card.arms:
        s = card.scores[arm]
        lines.append(
            f"| `{arm}` | {s.attempts} | {s.attempts_per_payment:.2f} | {s.nudges} | "
            f"{s.negative_ev_stops} | {rupees(s.total_cost_paise)} | "
            f"{rupees(s.cost_per_recovery_paise)} | "
            f"{rupees(s.amount_recovered_paise)} |"
        )
    if any(card.scores[a].negative_ev_stops for a in card.arms):
        lines += [
            "",
            "**Negative-EV stops** are decisions not to spend an attempt: no candidate "
            "execution had `P(success) × amount > attempt cost`. research/06 §6.6 calls "
            "this the local equivalent of Stripe declining a payment it does not expect "
            "to be authorised. Declining to act is a decision, and the audit log "
            "records it as one.",
        ]
    lines += [
        "",
        "### Trailing window effect (I-15)",
        "",
        "| arm | recovered by day "
        f"{card.window_days} | recovered by day {card.window_days + card.trailing_days}"
        " | late |",
        "|---|---|---|---|",
    ]
    for arm in card.arms:
        s = card.scores[arm]
        late = s.recovered - s.recovered_without_trailing_window
        lines.append(
            f"| `{arm}` | {s.recovered_without_trailing_window} | {s.recovered} | "
            f"+{late} |"
        )
    lines += [
        "",
        "Cutting measurement at the end of the main window would have discarded the "
        "'late' column. That is the undercount I-15 exists to prevent.",
        "",
        "### Where cases ended up",
        "",
        "| arm | " + " | ".join(sorted({s for a in card.arms for s in card.scores[a].states})) + " |",
    ]
    all_states = sorted({s for a in card.arms for s in card.scores[a].states})
    lines.append("|" + "---|" * (len(all_states) + 1))
    for arm in card.arms:
        s = card.scores[arm]
        cells = " | ".join(str(s.states.get(state, 0)) for state in all_states)
        lines.append(f"| `{arm}` | {cells} |")
    lines.append("")

    # -- 4. per action class --------------------------------------------------
    lines += [
        "## 4. By action class",
        "",
        "The taxonomy's value shows up most clearly here: the eight classes are the "
        "thing the policy table adds, and control is blind to all of them.",
        "",
    ]
    lines += _segment_table(card, card.by_action, "action class")
    lines += ["", "## 4b. By rail", ""]
    lines += _segment_table(card, card.by_rail, "method")
    lines.append("")

    # -- 5. per error code ----------------------------------------------------
    lines += [
        "## 5. By error code",
        "",
        "I-16. Every code that produced a case, sorted by sample size. Rows where the "
        f"`{focus or 'focus'}` arm underperforms are marked ⚠ and are **not** filtered "
        "out — see section 6.",
        "",
    ]
    lines += _segment_table(card, card.by_error_code, "code")
    lines.append("")

    # -- 6. losses ------------------------------------------------------------
    lines += [
        f"## 6. Where `{focus or 'the focus arm'}` LOSES",
        "",
    ]
    if not card.gaps:
        lines.append("Only one arm was run; there is nothing to compare.")
    elif not card.losses:
        lines += [
            f"No error code in this run shows `{focus}` below `{card.reference}`.",
            "",
            "That is a claim about **this run at this sample size**, not a claim that "
            "no such segment exists. Several codes below carry single-digit n, where a "
            "one-case difference moves the rate by tens of points. Read the absence of "
            "losing rows as 'not detected here', not as 'does not happen'.",
        ]
    else:
        lines += [
            f"{len(card.losses)} segment(s) where `{focus}` recovers a **smaller** "
            f"share than `{card.reference}`. Published because suppressing them would "
            "defeat the premise of the project.",
            "",
        ]
        lines += _segment_table(card, card.losses, "code")
        lines += [
            "",
            "These are the rows worth arguing about. A losing segment is either a "
            "wrong action mapping in `error_policy.json`, a response window that is "
            "too short, or small-sample noise — and the n column is the first thing "
            "to check before treating any of them as a finding.",
        ]
    lines.append("")

    # -- 7. time to recovery --------------------------------------------------
    lines += [
        "## 7. Time to recovery",
        "",
        "Measured from the original failure to the recovery, over recovered payments "
        "only.",
        "",
        "| arm | n | p25 | median | p75 | mean |",
        "|---|---|---|---|---|---|",
    ]
    for arm in card.arms:
        t = card.scores[arm].time_to_recovery
        lines.append(
            f"| `{arm}` | {t.n} | {duration(t.p25_s)} | {duration(t.median_s)} | "
            f"{duration(t.p75_s)} | {duration(t.mean_s)} |"
        )
    lines.append("")
    bucket_names = list(next(iter(card.scores.values())).time_to_recovery.buckets)
    lines += [
        "| arm | " + " | ".join(bucket_names) + " |",
        "|" + "---|" * (len(bucket_names) + 1),
    ]
    for arm in card.arms:
        t = card.scores[arm].time_to_recovery
        lines.append(
            f"| `{arm}` | " + " | ".join(str(t.buckets[b]) for b in bucket_names) + " |"
        )
    lines.append("")

    # -- 8. model-eligible surface --------------------------------------------
    lines += _model_sections(card)

    # -- 9. caveats -----------------------------------------------------------
    smallest = min((card.scores[a].payments for a in card.arms), default=0)
    lines += [
        "## 10. Caveats",
        "",
        "- **The data is synthetic.** No public NPCI decline dataset exists. The "
        "simulator's rates are grounded in Razorpay's published material where it says "
        "anything, and explicitly marked as our assumptions where it does not — see "
        "the CONFIG dicts in `src/simulator/declines.py`, `rails.py` and `world.py`. "
        "The arm comparison is robust to the simulator's absolute level because both "
        "arms face the identical generated population, but the absolute recovery rates "
        "here are not forecasts.",
        f"- **The samples are small.** The smaller arm holds {smallest} payments. "
        "Per-code rows frequently carry n < 10, where one case moves the rate by more "
        "than ten points. Treat the per-code table as directional and the headline "
        "interval as the real precision.",
        "- **The simulator and the policy table share an author.** The cause model is "
        "built from latent state rather than from a hand-picked code distribution "
        "specifically to avoid grading the table against its own answer key, but this "
        "is not an independent evaluation and should not be read as one.",
        "- **What this does not measure:** customer lifetime effects of repeated "
        "retries or nudges, real issuer behaviour, the cost of a false nudge, or "
        "anything about Adaptive Acceptance and Network Tokens, which need issuer and "
        "network access this project does not have.",
        "- **No machine learning is involved in these numbers.** Both arms are "
        "deterministic. `RETRY_SCHEDULED` waits a flat `min_wait_hours` from the "
        "table. The model arrives in Stage 4 and will be reported as a separate gap.",
        "",
    ]
    return "\n".join(lines) + "\n"


def build(
    db_path: Path | str,
    run_id: str,
    *,
    reference: str = "control",
    model_dir: Path | str | None = MODEL_DIR,
) -> Scorecard:
    conn = db.connect(db_path)
    try:
        run = db.get_run(conn, run_id)
        if run is None:
            raise ValueError(f"no run {run_id!r} in {db_path}")
        return score(
            conn,
            run,
            PolicyEngine().load(),
            reference=reference,
            model_dir=model_dir,
        )
    finally:
        conn.close()


def write_report(
    db_path: Path | str,
    run_id: str,
    out_path: Path | str,
    *,
    reference: str = "control",
    model_dir: Path | str | None = MODEL_DIR,
) -> Scorecard:
    card = build(db_path, run_id, reference=reference, model_dir=model_dir)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(card), encoding="utf-8")
    return card

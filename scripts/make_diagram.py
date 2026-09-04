"""Render docs/architecture.png.

One diagram, and the thing it has to make obvious is which path the model touches and
which it does not: 24 of the 110 codes reach the scorer, 86 are decided by the table
and never go near it. Drawn with matplotlib so it regenerates from source rather than
being a screenshot nobody can update.

    uv run python scripts/make_diagram.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import matplotlib

matplotlib.use("Agg")
import matplotlib.patches as patches  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

INK = "#10151C"
PANEL = "#171E28"
LINE = "#2B3644"
FG = "#E4EAF2"
DIM = "#7D8CA1"
IMMEDIATE = "#E5484D"
DELAYED = "#F5A524"
MINOR = "#2FA84F"
EXPECTANT = "#5C6470"

W, H = 15.0, 8.6


def box(ax, x, y, w, h, title, lines, *, edge=LINE, face=PANEL, title_color=FG, dashed=False):
    ax.add_patch(
        patches.FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.02,rounding_size=0.12",
            linewidth=1.6,
            edgecolor=edge,
            facecolor=face,
            linestyle="--" if dashed else "-",
        )
    )
    ax.text(x + 0.2, y + h - 0.32, title, color=title_color, fontsize=11, fontweight="600")
    for index, line in enumerate(lines):
        ax.text(
            x + 0.2,
            y + h - 0.62 - index * 0.26,
            line,
            color=DIM,
            fontsize=8.4,
            family="monospace",
        )


def arrow(ax, start, end, *, color=LINE, label=None, label_dy=0.14, style="-|>", dashed=False):
    ax.annotate(
        "",
        xy=end,
        xytext=start,
        arrowprops=dict(
            arrowstyle=style,
            color=color,
            linewidth=1.6,
            linestyle="--" if dashed else "-",
            shrinkA=2,
            shrinkB=2,
        ),
    )
    if label:
        ax.text(
            (start[0] + end[0]) / 2,
            (start[1] + end[1]) / 2 + label_dy,
            label,
            color=color,
            fontsize=8,
            ha="center",
            family="monospace",
        )


def main() -> int:
    fig, ax = plt.subplots(figsize=(W, H), dpi=170)
    fig.patch.set_facecolor(INK)
    ax.set_facecolor(INK)
    ax.set_xlim(0, W)
    ax.set_ylim(0, H)
    ax.axis("off")

    ax.text(0.4, H - 0.5, "TRIAGE", color=FG, fontsize=19, fontweight="700")
    ax.text(
        0.4,
        H - 0.85,
        "the decision layer between a failed payment and what to do about it",
        color=DIM,
        fontsize=10,
    )
    ax.text(
        0.4,
        H - 1.15,
        "A language model never decides a money action. The policy table does.",
        color=DIM,
        fontsize=9,
        style="italic",
    )

    row = 5.55
    box(ax, 0.4, row, 2.5, 1.35, "payment.failed",
        ["error_code", "source · method", "amount · customer"])
    box(ax, 3.3, row, 2.7, 1.35, "policy.resolve", ["error_policy.json", "110 codes → 8 classes",
                                                    "raises on unknown (I-2)"], edge=MINOR)
    box(ax, 6.4, row, 2.6, 1.35, "rails.health", ["Downtime API schema", "severity → policy",
                                                  "observable, not latent"])
    box(ax, 9.4, row, 2.6, 1.35, "model.score", ["LightGBM", "RETRY_SCHEDULED", "SWITCH_RAIL only"],
        edge=DELAYED)
    box(ax, 12.4, row, 2.2, 1.35, "executor", ["max_attempts", "drop_dead_at", "idempotency key"],
        edge=IMMEDIATE)

    arrow(ax, (2.9, row + 0.68), (3.3, row + 0.68))
    arrow(ax, (6.0, row + 0.68), (6.4, row + 0.68))
    arrow(ax, (9.0, row + 0.68), (9.4, row + 0.68), color=DELAYED, label="24 of 110", label_dy=0.16)
    arrow(ax, (12.0, row + 0.68), (12.4, row + 0.68), color=DELAYED)

    # The bypass. This is the line the diagram exists to show.
    ax.annotate(
        "",
        xy=(12.9, row + 1.35),
        xytext=(7.7, row + 1.35),
        arrowprops=dict(
            arrowstyle="-|>",
            color=EXPECTANT,
            linewidth=1.8,
            linestyle=(0, (5, 3)),
            connectionstyle="arc3,rad=-0.28",
        ),
    )
    ax.text(
        10.3,
        row + 2.12,
        "86 of 110 codes never reach the model",
        color=EXPECTANT,
        fontsize=9.5,
        ha="center",
        fontweight="600",
    )
    ax.text(
        10.3,
        row + 1.88,
        "the table is final; a model error cannot cause an unrecoverable failure to be retried  (I-1)",
        color=DIM,
        fontsize=8,
        ha="center",
    )

    # The eight classes, coloured by triage category.
    classes = [
        ("RETRY_NOW", "3", IMMEDIATE, True),
        ("RETRY_SCHEDULED", "9", DELAYED, True),
        ("SWITCH_RAIL", "15", IMMEDIATE, True),
        ("SWITCH_INSTRUMENT", "28", EXPECTANT, False),
        ("NUDGE_CUSTOMER", "23", MINOR, False),
        ("AWAIT_STATUS", "5", DELAYED, False),
        ("STOP", "4", EXPECTANT, False),
        ("MERCHANT_ALERT", "23", "#8892A4", False),
    ]
    y = 3.55
    ax.text(0.4, y + 1.0, "the eight action classes", color=FG, fontsize=11, fontweight="600")
    ax.text(
        0.4,
        y + 0.72,
        "only the first three recover without a human — 27 of 110",
        color=DIM,
        fontsize=8.6,
    )
    x = 0.4
    for name, count, colour, recoverable in classes:
        width = 1.72
        hollow = colour in (EXPECTANT, "#8892A4")
        ax.add_patch(
            patches.FancyBboxPatch(
                (x, y),
                width,
                0.52,
                boxstyle="round,pad=0.01,rounding_size=0.08",
                linewidth=1.4,
                edgecolor=colour,
                facecolor="none" if hollow else colour + "33",
                linestyle="--" if colour == "#8892A4" else "-",
            )
        )
        ax.text(x + 0.1, y + 0.29, name, color=colour, fontsize=7.4, family="monospace")
        ax.text(x + 0.1, y + 0.10, f"{count} codes", color=DIM, fontsize=7)
        if recoverable:
            ax.text(x + width - 0.22, y + 0.30, "●", color=colour, fontsize=7)
        x += width + 0.11

    # Audit and the guards.
    box(ax, 0.4, 1.55, 6.6, 1.5, "audit log — append-only",
        ["every transition written BEFORE its action executes  (I-8)",
         "from · to · actor · reason · idempotency_key · timestamp",
         "an action with no preceding audit row is a bug"], edge=MINOR)
    box(ax, 7.4, 1.55, 7.2, 1.5, "the guards the model cannot override",
        ["I-5  idempotency key, UNIQUE index → 409     I-6  AWAIT_STATUS blocked → 423",
         "I-7  max_attempts · drop_dead_at → 422       I-4  five classes never schedule",
         "safety is structural, not learned"], edge=IMMEDIATE)

    arrow(ax, (13.5, row), (13.5, 3.1), color=MINOR)
    arrow(ax, (3.7, y), (3.7, 3.05), color=LINE, dashed=True)

    ax.text(
        0.4,
        0.75,
        "measured over 8 000 simulated payments, three arms, 30 days + 7 trailing:",
        color=DIM,
        fontsize=8.8,
    )
    ax.text(
        0.4,
        0.42,
        "the taxonomy is worth +21.2pp over a fixed-retry control (p < 0.001).  "
        "the model adds +0.2pp (p = 0.94) — an honest null.",
        color=FG,
        fontsize=9.6,
        fontweight="600",
    )
    ax.text(
        0.4,
        0.12,
        "candidate_delay_hours carries zero gain: the training data is on-policy and "
        "contains no variation in the timing decision.",
        color=DIM,
        fontsize=8.4,
    )

    out = ROOT / "docs" / "architecture.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=INK, bbox_inches="tight", pad_inches=0.28)
    print(f"wrote {out}  ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

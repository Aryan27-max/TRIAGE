"use client";

import { useState } from "react";
import clsx from "clsx";

/**
 * The LightGBM / model-scope FAQ, as a floating bottom-right tab — present on every
 * screen, not just Results, since the "why is the model bounded like this" question
 * comes up from the Live inspector too. Every answer restates I-1 in one form or
 * another — the policy table decides the action class, the model only ranks within
 * a class the table already permitted. Individual rows use native <details> for
 * retractable Q&As with no extra state; only the panel's own open/closed state is
 * wired up, so it can render as a tab when collapsed.
 */

type Entry = { q: string; a: string };

const ENTRIES: Entry[] = [
  {
    q: "Why LightGBM?",
    a: "Fast, accurate, and well-suited for structured/tabular financial data.",
  },
  {
    q: "Why use it in TRIAGE?",
    a: "To predict the probability that a permitted recovery attempt will succeed.",
  },
  {
    q: "Why not let LightGBM choose the action?",
    a: "To keep safety deterministic and prevent ML from making unrestricted decisions.",
  },
  {
    q: "Why deterministic policy first?",
    a: "To ensure unrecoverable failures can never be retried by the ML model.",
  },
  {
    q: "Why only RETRY_SCHEDULED and SWITCH_RAIL?",
    a: "These are the actions where ranking different execution options provides value.",
  },
  {
    q: "Why LightGBM instead of an LLM/Transformer?",
    a: "The problem is tabular, not language-based, and LightGBM performs well with relatively small datasets.",
  },
  {
    q: "Why these features?",
    a: "They capture customer history, payment context, business information, timing, billing behavior, and rail health.",
  },
  {
    q: "Why include rail health?",
    a: "Because retrying through a currently degraded payment rail can waste attempts and reduce recovery probability.",
  },
  {
    q: "Why is LightGBM suitable for financial systems?",
    a: "It is mature, fast, deterministic in controlled configurations, and commonly used for structured risk/fraud prediction.",
  },
  {
    q: "Why is the system explainable?",
    a: "Decisions are bounded by explicit policies, candidate actions, features, and expected-value calculations.",
  },
  {
    q: "Why fail loudly if the model is missing?",
    a: "To avoid silently making financial decisions using a fake or default prediction.",
  },
  {
    q: "Why detect feature drift?",
    a: "To prevent the model from producing predictions when the input schema no longer matches what it was trained on.",
  },
  {
    q: "Why pin categorical levels?",
    a: "To ensure the same categorical mapping is used during training and inference.",
  },
  {
    q: "Why use a temporal split?",
    a: "To evaluate the model on future-like data and reduce the risk of temporal leakage.",
  },
  {
    q: "Why use PR-AUC, ROC-AUC, Brier score, etc.?",
    a: "To measure discrimination as well as whether predicted probabilities are properly calibrated.",
  },
  {
    q: "Why calculate expected value?",
    a: "Because a high probability of success is not enough; the recovery must also be economically worthwhile.",
  },
];

export function Faq() {
  const [open, setOpen] = useState(false);

  return (
    <div className="fixed bottom-5 right-5 z-40 flex flex-col items-end">
      {open && (
        <div className="mb-3 max-h-[70vh] w-[min(420px,calc(100vw-2.5rem))] overflow-y-auto rounded-card border border-ink-line bg-ink-800 shadow-2xl shadow-black/40">
          <div className="sticky top-0 flex items-center justify-between border-b border-ink-line bg-ink-800 px-4 py-3">
            <span className="text-sm font-medium text-fg">
              FAQ — why LightGBM, why bounded like this
            </span>
            <button
              type="button"
              onClick={() => setOpen(false)}
              aria-label="Close FAQ"
              className="rounded-control px-1.5 py-0.5 text-fg-dim transition-colors hover:bg-ink-700 hover:text-fg"
            >
              ✕
            </button>
          </div>
          <div className="px-4 pb-1 pt-2">
            <div className="divide-y divide-ink-line">
              {ENTRIES.map((entry) => (
                <details key={entry.q} className="group/item py-2.5">
                  <summary className="flex cursor-pointer list-none items-start justify-between gap-3 select-none">
                    <span className="text-sm text-fg">{entry.q}</span>
                    <span className="mt-0.5 shrink-0 font-mono text-xs text-fg-dim transition-transform group-open/item:rotate-45">
                      +
                    </span>
                  </summary>
                  <p className="mt-1.5 text-xs leading-relaxed text-fg-dim">{entry.a}</p>
                </details>
              ))}
            </div>
            <p className="my-3 rounded-control border border-tri-delayed/30 bg-tri-delayed/[0.06] px-3 py-2.5 text-xs leading-relaxed text-fg">
              <span className="font-medium">Core reason: </span>
              the policy engine decides what is allowed. LightGBM only decides which
              allowed option is most likely to work.
            </p>
          </div>
        </div>
      )}

      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={clsx(
          "flex items-center gap-2 rounded-control border px-4 py-2.5 text-sm font-medium shadow-lg shadow-black/30 transition-colors",
          open
            ? "border-ink-line bg-ink-700 text-fg"
            : "border-ink-line bg-ink-800 text-fg hover:bg-ink-700",
        )}
      >
        <span className="font-mono text-xs text-fg-dim">?</span>
        FAQ
      </button>
    </div>
  );
}

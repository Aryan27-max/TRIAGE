"use client";

import { useEffect, useMemo, useState } from "react";
import clsx from "clsx";
import {
  ACTION_ORDER,
  TRIAGE_OF_ACTION,
  api,
  type ErrorPolicy,
  type TriageCategory,
} from "@/lib/api";
import { TriageLegend } from "@/components/TriageChip";

/**
 * The 27-of-110 finding as an image.
 *
 * All 110 codes, grouped by action class and coloured by triage category. It should be
 * legible in two seconds from across a room — the recoverable 27 are clustered at the
 * top and visibly outnumbered by everything below them.
 */

const CELL: Record<TriageCategory, string> = {
  immediate: "bg-tri-immediate/85 text-white border-tri-immediate",
  delayed: "bg-tri-delayed/85 text-[#221704] border-tri-delayed",
  minor: "bg-tri-minor/85 text-white border-tri-minor",
  expectant: "bg-transparent text-tri-expectant border-tri-expectant",
  merchant: "bg-transparent text-tri-merchant border-dashed border-tri-merchant",
};

export default function TaxonomyPage() {
  const [codes, setCodes] = useState<ErrorPolicy[]>([]);
  const [hovered, setHovered] = useState<ErrorPolicy | null>(null);

  useEffect(() => {
    api.errors().then((r) => setCodes(r.items)).catch(() => setCodes([]));
  }, []);

  const groups = useMemo(
    () =>
      ACTION_ORDER.map((action) => ({
        action,
        recoverable: ["RETRY_NOW", "RETRY_SCHEDULED", "SWITCH_RAIL"].includes(action),
        items: codes.filter((c) => c.action === action),
      })),
    [codes],
  );

  const recoverable = codes.filter((c) => c.recoverable).length;

  return (
    <div className="pt-6">
      <header className="mb-6">
        <h1 className="text-xl font-semibold tracking-tight text-fg">
          The taxonomy — {recoverable || 27} of {codes.length || 110} are silently
          recoverable
        </h1>
        <p className="mt-1 max-w-3xl text-sm leading-relaxed text-fg-dim">
          Razorpay publishes {codes.length || 110} distinct payment failure reasons and
          explains what each one means. It does not say what to do about them. Classified
          by recoverable action, only the top three groups can be fixed without a human —
          so a &ldquo;retry three times&rdquo; loop is wrong on roughly three quarters of
          failures.
        </p>
      </header>

      <TriageLegend className="mb-6 rounded-card border border-ink-line bg-ink-800 px-4 py-3" />

      <div className="space-y-5">
        {groups.map((group) => (
          <section key={group.action}>
            <div className="mb-2 flex items-baseline gap-3">
              <h2 className="font-mono text-sm font-medium text-fg">{group.action}</h2>
              <span className="text-xs text-fg-dim">{group.items.length} codes</span>
              {group.recoverable && (
                <span className="rounded-full bg-tri-minor/15 px-2 py-0.5 text-[11px] text-tri-minor">
                  silently recoverable
                </span>
              )}
            </div>
            <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 xl:grid-cols-8">
              {group.items.map((entry) => (
                <button
                  key={entry.code}
                  type="button"
                  onMouseEnter={() => setHovered(entry)}
                  onFocus={() => setHovered(entry)}
                  className={clsx(
                    "truncate rounded-control border px-2 py-1.5 text-left font-mono text-[10.5px] transition-transform hover:scale-[1.03]",
                    CELL[TRIAGE_OF_ACTION[entry.action] ?? "merchant"],
                  )}
                  title={entry.code}
                >
                  {entry.code}
                </button>
              ))}
            </div>
          </section>
        ))}
      </div>

      {hovered && (
        <aside className="sticky bottom-4 mt-6 rounded-card border border-ink-line bg-ink-800 p-4 shadow-2xl">
          <div className="flex flex-wrap items-baseline gap-3">
            <span className="font-mono text-sm text-fg">{hovered.code}</span>
            <span className="font-mono text-xs text-fg-dim">{hovered.action}</span>
            <span className="text-xs text-fg-dim">family {hovered.family}</span>
            {hovered.min_wait_hours > 0 && (
              <span className="text-xs text-fg-dim">
                min wait {hovered.min_wait_hours}h
              </span>
            )}
            {hovered.is_model_eligible && (
              <span className="rounded-full bg-tri-delayed/15 px-2 py-0.5 text-[11px] text-tri-delayed">
                model-eligible
              </span>
            )}
          </div>
          <p className="mt-2 text-xs leading-relaxed text-fg-dim">
            <span className="text-fg">Razorpay:</span> {hovered.razorpay_explanation}
          </p>
          <p className="mt-1 text-xs leading-relaxed text-fg-dim">
            <span className="text-fg">TRIAGE:</span> {hovered.policy_note}
          </p>
        </aside>
      )}
    </div>
  );
}

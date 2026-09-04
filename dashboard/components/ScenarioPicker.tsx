"use client";

import { useMemo, useState } from "react";
import clsx from "clsx";
import { Search, Zap } from "lucide-react";
import { ACTION_ORDER, type ErrorPolicy } from "@/lib/api";
import { TriageChip } from "./TriageChip";

/**
 * The control the demo is actually driven from.
 *
 * Not in Razorpay's real UI — it lives behind the `⋯` menu, which is empty space
 * there. Choosing which of the 110 codes to inject, or firing a downtime event, is
 * what turns a static replica into a demo instrument. research/08 §8.8 lists this as
 * one of the two things that must never be cut.
 */
export function ScenarioPicker({
  codes,
  selected,
  onSelect,
  onInjectDowntime,
  onClose,
}: {
  codes: ErrorPolicy[];
  selected: string;
  onSelect: (code: string) => void;
  onInjectDowntime: (method: string, severity: string) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");

  const grouped = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const filtered = needle
      ? codes.filter(
          (c) =>
            c.code.toLowerCase().includes(needle) ||
            c.action.toLowerCase().includes(needle),
        )
      : codes;
    return ACTION_ORDER.map((action) => ({
      action,
      items: filtered.filter((c) => c.action === action),
    })).filter((group) => group.items.length > 0);
  }, [codes, query]);

  return (
    <div className="absolute right-4 top-14 z-20 w-[380px] overflow-hidden rounded-card border border-black/10 bg-white shadow-2xl">
      <div className="border-b border-black/[0.06] p-3">
        <p className="text-xs font-medium text-rzp-ink">Inject a failure</p>
        <p className="mt-0.5 text-[11px] text-rzp-ink-dim">
          Any of the 110 published reasons. The next payment fails with it.
        </p>
        <div className="mt-2 flex items-center gap-2 rounded-control border border-black/10 px-2">
          <Search size={13} className="text-rzp-ink-dim" />
          <input
            autoFocus
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search 110 codes…"
            className="w-full bg-transparent py-1.5 font-mono text-xs text-rzp-ink outline-none placeholder:text-rzp-ink-dim"
          />
        </div>
      </div>

      <div className="max-h-[300px] overflow-y-auto">
        {grouped.map((group) => (
          <div key={group.action}>
            <div className="sticky top-0 flex items-center justify-between bg-black/[0.03] px-3 py-1.5 backdrop-blur">
              <span className="font-mono text-[10px] font-medium text-rzp-ink">
                {group.action}
              </span>
              <span className="text-[10px] text-rzp-ink-dim">{group.items.length}</span>
            </div>
            {group.items.map((entry) => (
              <button
                key={entry.code}
                type="button"
                onClick={() => onSelect(entry.code)}
                className={clsx(
                  "flex w-full items-center justify-between gap-2 px-3 py-1.5 text-left transition-colors hover:bg-rzp-method-active",
                  entry.code === selected && "bg-rzp-method-active",
                )}
              >
                <span className="truncate font-mono text-[11px] text-rzp-ink">
                  {entry.code}
                </span>
                <TriageChip action={entry.action} />
              </button>
            ))}
          </div>
        ))}
        {grouped.length === 0 && (
          <p className="px-3 py-6 text-center text-xs text-rzp-ink-dim">
            No code matches “{query}”.
          </p>
        )}
      </div>

      <div className="border-t border-black/[0.06] p-3">
        <p className="flex items-center gap-1.5 text-xs font-medium text-rzp-ink">
          <Zap size={13} /> Inject a downtime event
        </p>
        <p className="mt-0.5 text-[11px] leading-relaxed text-rzp-ink-dim">
          Degrading a rail changes the next decision on it. Take{" "}
          <span className="font-mono">card</span> down and a UPI rail-switch will wait
          instead of switching into a second outage.
        </p>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {["upi", "card", "netbanking", "wallet"].map((method) => (
            <button
              key={method}
              type="button"
              onClick={() => onInjectDowntime(method, "high")}
              className="rounded-control border border-black/10 px-2 py-1 font-mono text-[11px] text-rzp-ink-dim transition-colors hover:border-tri-immediate hover:text-tri-immediate"
            >
              {method} · high
            </button>
          ))}
        </div>
      </div>

      <button
        type="button"
        onClick={onClose}
        className="w-full border-t border-black/[0.06] py-2 text-[11px] text-rzp-ink-dim hover:bg-black/[0.02]"
      >
        Close
      </button>
    </div>
  );
}

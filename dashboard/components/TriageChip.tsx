import clsx from "clsx";
import { TRIAGE_LABEL, TRIAGE_OF_ACTION, type TriageCategory } from "@/lib/api";

/**
 * The five-category scale, treated per research/08 §8.3.
 *
 * Expectant renders hollow rather than filled because true black is invisible on a
 * dark background — and the hollow treatment carries the meaning better anyway:
 * nothing will be spent on this one. Merchant is dashed: not a casualty at all.
 */
const STYLES: Record<TriageCategory, string> = {
  immediate: "bg-tri-immediate/15 text-tri-immediate border border-tri-immediate/40",
  delayed: "bg-tri-delayed/15 text-tri-delayed border border-tri-delayed/40",
  minor: "bg-tri-minor/15 text-tri-minor border border-tri-minor/40",
  expectant: "bg-transparent text-tri-expectant border border-tri-expectant",
  merchant: "bg-transparent text-tri-merchant border border-dashed border-tri-merchant",
};

export function TriageChip({
  action,
  showAction = false,
  className,
}: {
  action: string;
  showAction?: boolean;
  className?: string;
}) {
  const category = TRIAGE_OF_ACTION[action] ?? "merchant";
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs whitespace-nowrap",
        STYLES[category],
        className,
      )}
      title={`${action} · ${TRIAGE_LABEL[category]}`}
    >
      {showAction && <span className="font-medium">{action}</span>}
      <span className={showAction ? "opacity-70" : "font-medium"}>
        {TRIAGE_LABEL[category]}
      </span>
    </span>
  );
}

export function TriageLegend({ className }: { className?: string }) {
  const entries: [TriageCategory, string][] = [
    ["immediate", "RETRY_NOW · SWITCH_RAIL"],
    ["delayed", "RETRY_SCHEDULED · AWAIT_STATUS"],
    ["minor", "NUDGE_CUSTOMER"],
    ["expectant", "SWITCH_INSTRUMENT · STOP"],
    ["merchant", "MERCHANT_ALERT"],
  ];
  return (
    <div className={clsx("flex flex-wrap items-center gap-x-5 gap-y-2", className)}>
      {entries.map(([category, actions]) => (
        <div key={category} className="flex items-center gap-2">
          <span
            className={clsx(
              "inline-block h-3 w-3 rounded-sm",
              category === "immediate" && "bg-tri-immediate",
              category === "delayed" && "bg-tri-delayed",
              category === "minor" && "bg-tri-minor",
              category === "expectant" && "border border-tri-expectant",
              category === "merchant" && "border border-dashed border-tri-merchant",
            )}
          />
          <span className="text-xs text-fg">{TRIAGE_LABEL[category]}</span>
          <span className="font-mono text-[11px] text-fg-dim">{actions}</span>
        </div>
      ))}
    </div>
  );
}

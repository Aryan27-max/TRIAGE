"use client";

import { useState } from "react";
import clsx from "clsx";
import { ChevronRight, Circle, CircleDot } from "lucide-react";
import { Json } from "./Json";
import { TriageChip } from "./TriageChip";

/**
 * The Inspector. Events pin to a vertical rail rather than sitting in a table: a table
 * would show the same data, but the rail shows the *shape* of the decision — where it
 * paused, where it branched, where it stopped.
 *
 * Every row expands to the API's real payload. The Inspector renders what the service
 * actually returned; it does not invent a display format.
 */

export type InspectorEvent = {
  id: string;
  at: string;
  name: string;
  duration: string;
  summary: string;
  action?: string;
  /** Hollow node: the step did not fire. The absence is the point on `model.score`. */
  hollow?: boolean;
  /** The one row that is visually distinct — the only place a model appears at all. */
  emphasis?: boolean;
  payload?: unknown;
};

export function Inspector({
  caseId,
  events,
  live,
  railSeverity,
}: {
  caseId: string | null;
  events: InspectorEvent[];
  live: boolean;
  railSeverity: string | null;
}) {
  return (
    <section className="flex h-full flex-col rounded-shell border border-ink-line bg-ink-800">
      <header className="flex items-center justify-between border-b border-ink-line px-5 py-3">
        <div className="flex items-baseline gap-3">
          <h2 className="text-sm font-semibold tracking-tight text-fg">INSPECTOR</h2>
          <span className="text-[11px] text-fg-dim">
            what the merchant never sees
          </span>
        </div>
        <div className="flex items-center gap-3">
          {caseId && <span className="font-mono text-[11px] text-fg-dim">{caseId}</span>}
          <span
            className={clsx(
              "flex items-center gap-1.5 text-[11px]",
              live ? "text-tri-minor" : "text-fg-dim",
            )}
          >
            <span
              className={clsx(
                "inline-block h-1.5 w-1.5 rounded-full",
                live ? "bg-tri-minor" : "bg-fg-dim",
              )}
            />
            {live ? "LIVE" : "IDLE"}
          </span>
        </div>
      </header>

      {/* Persistent rail-health strip; amber or red on an active downtime. */}
      <div
        className={clsx(
          "flex items-center gap-2 border-b px-5 py-1.5 text-[11px]",
          railSeverity === "high"
            ? "border-tri-immediate/30 bg-tri-immediate/10 text-tri-immediate"
            : railSeverity
              ? "border-tri-delayed/30 bg-tri-delayed/10 text-tri-delayed"
              : "border-ink-line bg-ink-900/40 text-fg-dim",
        )}
      >
        <span className="font-medium">Rail health</span>
        <span className="font-mono">
          {railSeverity ? `severity: ${railSeverity}` : "no active downtime"}
        </span>
      </div>

      <div className="flex-1 overflow-y-auto px-5 py-4">
        {events.length === 0 ? (
          <p className="py-16 text-center text-sm text-fg-dim">
            Submit a payment to see the decision chain resolve.
          </p>
        ) : (
          <ol className="relative">
            {events.map((event, index) => (
              <EventRow
                key={event.id}
                event={event}
                last={index === events.length - 1}
                delay={index * 40}
              />
            ))}
          </ol>
        )}
      </div>
    </section>
  );
}

function EventRow({
  event,
  last,
  delay,
}: {
  event: InspectorEvent;
  last: boolean;
  delay: number;
}) {
  const [open, setOpen] = useState(false);
  const expandable = event.payload !== undefined;

  return (
    <li
      className="animate-row-in relative pl-7"
      style={{ animationDelay: `${delay}ms` }}
    >
      {/* the rail */}
      {!last && (
        <span className="absolute left-[5px] top-4 h-full w-px bg-ink-line" aria-hidden />
      )}
      <span className="absolute left-0 top-[6px] text-fg-dim" aria-hidden>
        {event.hollow ? (
          <Circle size={11} className="text-tri-expectant" />
        ) : (
          <CircleDot
            size={11}
            className={event.emphasis ? "text-tri-delayed" : "text-fg"}
          />
        )}
      </span>

      <div
        className={clsx(
          "mb-3 rounded-card px-3 py-2 transition-colors",
          event.emphasis && "bg-ink-700/60 ring-1 ring-tri-delayed/25",
          expandable && "cursor-pointer hover:bg-ink-700",
        )}
        onClick={() => expandable && setOpen((value) => !value)}
      >
        <div className="flex items-baseline gap-3">
          <span className="font-mono text-[11px] tabular-nums text-fg-dim">
            {event.at}
          </span>
          <span
            className={clsx(
              "text-sm",
              event.hollow ? "text-fg-dim" : "font-medium text-fg",
            )}
          >
            {event.name}
          </span>
          {event.action && <TriageChip action={event.action} showAction />}
          <span className="ml-auto font-mono text-[11px] tabular-nums text-fg-dim">
            {event.duration}
          </span>
          {expandable && (
            <ChevronRight
              size={13}
              className={clsx(
                "shrink-0 text-fg-dim transition-transform",
                open && "rotate-90",
              )}
            />
          )}
        </div>
        <p
          className={clsx(
            "mt-0.5 text-[12px] leading-relaxed",
            event.hollow ? "text-tri-expectant" : "text-fg-dim",
          )}
        >
          {event.summary}
        </p>

        {open && expandable && (
          <div className="mt-2 rounded-control border border-ink-line bg-ink-900 p-3">
            <Json value={event.payload} />
          </div>
        )}
      </div>
    </li>
  );
}

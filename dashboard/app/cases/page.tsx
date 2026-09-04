"use client";

import { useEffect, useState } from "react";
import clsx from "clsx";
import { api, clock, rupees, type CaseSummary } from "@/lib/api";
import { Json } from "@/components/Json";

/**
 * Case history from the simulator runs, filterable, with the audit trail one click
 * away. The audit trail is the thing worth showing here: every state transition,
 * timestamped, attributed, and written before its action executed.
 */

const STATES = [
  "",
  "RECEIVED",
  "DIAGNOSED",
  "SCHEDULED",
  "AWAITING_STATUS",
  "ESCALATED",
  "RECOVERED",
  "EXHAUSTED",
  "STOPPED",
];

const STATE_TONE: Record<string, string> = {
  RECOVERED: "text-tri-minor",
  EXHAUSTED: "text-tri-expectant",
  STOPPED: "text-tri-expectant",
  AWAITING_STATUS: "text-tri-delayed",
  ESCALATED: "text-tri-minor",
  SCHEDULED: "text-tri-immediate",
};

export default function CasesPage() {
  const [cases, setCases] = useState<CaseSummary[]>([]);
  const [count, setCount] = useState(0);
  const [state, setState] = useState("");
  const [arm, setArm] = useState("");
  const [code, setCode] = useState("");
  const [detail, setDetail] = useState<any>(null);
  const [empty, setEmpty] = useState(false);

  useEffect(() => {
    api
      .cases({ state, arm, error_code: code, limit: 100 })
      .then((r) => {
        setCases(r.items);
        setCount(r.count);
        setEmpty(r.count === 0);
      })
      .catch(() => setCases([]));
  }, [state, arm, code]);

  return (
    <div className="pt-6">
      <header className="mb-4">
        <h1 className="text-xl font-semibold tracking-tight text-fg">Cases</h1>
        <p className="mt-1 text-sm text-fg-dim">
          {count} case{count === 1 ? "" : "s"} in the working store. Click a row for its
          audit trail.
        </p>
      </header>

      <div className="mb-4 flex flex-wrap gap-2">
        <select
          value={state}
          onChange={(e) => setState(e.target.value)}
          className="rounded-control border border-ink-line bg-ink-800 px-3 py-1.5 font-mono text-xs text-fg outline-none"
        >
          {STATES.map((s) => (
            <option key={s} value={s}>
              {s || "all states"}
            </option>
          ))}
        </select>
        <select
          value={arm}
          onChange={(e) => setArm(e.target.value)}
          className="rounded-control border border-ink-line bg-ink-800 px-3 py-1.5 font-mono text-xs text-fg outline-none"
        >
          {["", "control", "baseline", "treatment"].map((a) => (
            <option key={a} value={a}>
              {a || "all arms"}
            </option>
          ))}
        </select>
        <input
          value={code}
          onChange={(e) => setCode(e.target.value)}
          placeholder="error code…"
          className="rounded-control border border-ink-line bg-ink-800 px-3 py-1.5 font-mono text-xs text-fg outline-none placeholder:text-fg-dim"
        />
      </div>

      {empty && (
        <p className="rounded-card border border-ink-line bg-ink-800 px-4 py-6 text-center text-sm text-fg-dim">
          No cases in the working store. Generate a population locally with{" "}
          <span className="font-mono text-fg">
            python -m src.simulator.generate --n 2000
          </span>
          , or open the Results screen for the pre-computed evaluation runs.
        </p>
      )}

      <div className="overflow-x-auto rounded-card border border-ink-line">
        <table className="w-full text-sm">
          <thead className="bg-ink-800 text-left text-xs text-fg-dim">
            <tr>
              <th className="px-4 py-2 font-medium">case</th>
              <th className="px-4 py-2 font-medium">state</th>
              <th className="px-4 py-2 font-medium">arm</th>
              <th className="px-4 py-2 font-medium">error code</th>
              <th className="px-4 py-2 font-medium">method</th>
              <th className="px-4 py-2 font-medium">amount</th>
              <th className="px-4 py-2 font-medium">attempts</th>
              <th className="px-4 py-2 font-medium">recovered</th>
            </tr>
          </thead>
          <tbody className="font-mono text-[12.5px]">
            {cases.map((row) => (
              <tr
                key={row.id}
                onClick={() => api.caseDetail(row.id).then(setDetail)}
                className="cursor-pointer border-t border-ink-line hover:bg-ink-800"
              >
                <td className="px-4 py-1.5 text-fg">{row.id}</td>
                <td className={clsx("px-4 py-1.5", STATE_TONE[row.status] ?? "text-fg-dim")}>
                  {row.status}
                </td>
                <td className="px-4 py-1.5 text-fg-dim">{row.arm ?? "—"}</td>
                <td className="px-4 py-1.5 text-fg-dim">{row.error_code}</td>
                <td className="px-4 py-1.5 text-fg-dim">{row.method}</td>
                <td className="px-4 py-1.5 text-fg-dim">{rupees(row.amount_paise)}</td>
                <td className="px-4 py-1.5 text-fg-dim">{row.attempt_count}</td>
                <td className="px-4 py-1.5 text-fg-dim">
                  {row.recovered_at ? clock(row.recovered_at) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {detail && (
        <div
          className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 p-6"
          onClick={() => setDetail(null)}
        >
          <div
            className="max-h-[85vh] w-full max-w-3xl overflow-y-auto rounded-shell border border-ink-line bg-ink-800 p-5"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-3 flex items-baseline justify-between">
              <h2 className="font-mono text-sm text-fg">{detail.id}</h2>
              <button
                type="button"
                onClick={() => setDetail(null)}
                className="text-xs text-fg-dim hover:text-fg"
              >
                close
              </button>
            </div>

            <h3 className="mb-1.5 text-xs font-medium text-fg">
              Audit trail — every transition, written before its action executed
            </h3>
            <ol className="mb-4 space-y-1">
              {detail.audit?.map((row: any, index: number) => (
                <li
                  key={index}
                  className="flex flex-wrap items-baseline gap-2 rounded-control bg-ink-900 px-3 py-1.5 font-mono text-[11.5px]"
                >
                  <span className="text-fg-dim">{clock(row.at)}</span>
                  <span className="text-fg">
                    {row.from_state ?? "∅"} → {row.to_state}
                  </span>
                  <span className="text-fg-dim">{row.actor}</span>
                  <span className="text-fg-dim">· {row.reason}</span>
                  {row.idempotency_key && (
                    <span className="ml-auto text-tri-delayed">
                      key {row.idempotency_key}
                    </span>
                  )}
                </li>
              ))}
            </ol>

            {detail.attempts?.length > 0 && (
              <>
                <h3 className="mb-1.5 text-xs font-medium text-fg">Attempts</h3>
                <div className="mb-4 rounded-control border border-ink-line bg-ink-900 p-3">
                  <Json value={detail.attempts} />
                </div>
              </>
            )}

            <h3 className="mb-1.5 text-xs font-medium text-fg">Current decision</h3>
            <div className="rounded-control border border-ink-line bg-ink-900 p-3">
              <Json value={detail.decision} />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

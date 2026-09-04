"use client";

import { useEffect, useState } from "react";
import clsx from "clsx";
import { api, type RunSummary } from "@/lib/api";
import { TriageChip } from "@/components/TriageChip";

/**
 * The arm comparison, rendered from GET /v1/eval/report/{id}.
 *
 * Both gaps are reported separately — baseline − control is the value of the taxonomy,
 * treatment − baseline is the value of the model. Blending them into one
 * treatment − control number would hide which half did the work, and in this project
 * the answer to that question is the result.
 *
 * The null is not softened anywhere on this page.
 */

const GAP_MEANING: Record<string, string> = {
  baseline_vs_control: "the value of the taxonomy",
  treatment_vs_baseline: "the value of the model",
  treatment_vs_control: "both together, for context",
};

const pct = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`;

export default function ResultsPage() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  const [report, setReport] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .runs()
      .then((r) => {
        const threeArm = r.items.filter((run) => run.arms.length >= 3);
        const pool = threeArm.length ? threeArm : r.items;
        setRuns(pool);
        if (pool.length) setRunId(pool[0].run_id);
      })
      .catch(() => setError("Could not list runs."));
  }, []);

  useEffect(() => {
    if (!runId) return;
    setReport(null);
    api.report(runId).then(setReport).catch(() => setError("Could not load the report."));
  }, [runId]);

  const arms: string[] = report ? Object.keys(report.arms) : [];

  return (
    <div className="pt-6">
      <header className="mb-5 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-fg">
            Arm comparison
          </h1>
          <p className="mt-1 max-w-3xl text-sm leading-relaxed text-fg-dim">
            Deduplicated to the payment and scored on final outcome, with a trailing
            window executed before scoring. Both gaps are reported separately.
          </p>
        </div>
        <div className="flex gap-1.5">
          {runs.map((run) => (
            <button
              key={run.run_id}
              type="button"
              onClick={() => setRunId(run.run_id)}
              className={clsx(
                "rounded-control px-3 py-1.5 font-mono text-xs transition-colors",
                run.run_id === runId
                  ? "bg-ink-700 text-fg"
                  : "text-fg-dim hover:bg-ink-800",
              )}
            >
              {run.scenario}
            </button>
          ))}
        </div>
      </header>

      {error && <p className="text-sm text-tri-immediate">{error}</p>}
      {!report && !error && <p className="text-sm text-fg-dim">Loading the report…</p>}

      {report && (
        <div className="space-y-8">
          {/* headline */}
          <section>
            <h2 className="mb-2 text-sm font-medium text-fg">Recovery rate per arm</h2>
            <div className="overflow-x-auto rounded-card border border-ink-line">
              <table className="w-full text-sm">
                <thead className="bg-ink-800 text-left text-xs text-fg-dim">
                  <tr>
                    <th className="px-4 py-2 font-medium">arm</th>
                    <th className="px-4 py-2 font-medium">payments</th>
                    <th className="px-4 py-2 font-medium">recovered</th>
                    <th className="px-4 py-2 font-medium">rate</th>
                    <th className="px-4 py-2 font-medium">95% CI</th>
                    <th className="px-4 py-2 font-medium">attempts</th>
                    <th className="px-4 py-2 font-medium">nudges</th>
                  </tr>
                </thead>
                <tbody className="font-mono text-[13px]">
                  {arms.map((arm) => {
                    const row = report.arms[arm];
                    return (
                      <tr key={arm} className="border-t border-ink-line">
                        <td className="px-4 py-2 text-fg">{arm}</td>
                        <td className="px-4 py-2 text-fg-dim">{row.payments}</td>
                        <td className="px-4 py-2 text-fg-dim">{row.recovered}</td>
                        <td className="px-4 py-2 font-semibold text-fg">
                          {pct(row.rate)}
                        </td>
                        <td className="px-4 py-2 text-fg-dim">
                          {pct(row.ci_low)} – {pct(row.ci_high)}
                        </td>
                        <td className="px-4 py-2 text-fg-dim">{row.attempts}</td>
                        <td className="px-4 py-2 text-fg-dim">{row.nudges}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>

          {/* the two gaps, separately */}
          <section>
            <h2 className="mb-2 text-sm font-medium text-fg">
              The two contributions, reported separately
            </h2>
            <div className="grid gap-3 md:grid-cols-3">
              {Object.entries(report.uplift).map(([key, gap]: [string, any]) => {
                const significant = gap.p_value < 0.05;
                const isModel = key === "treatment_vs_baseline";
                return (
                  <div
                    key={key}
                    className={clsx(
                      "rounded-card border p-4",
                      isModel
                        ? "border-tri-delayed/40 bg-tri-delayed/[0.06]"
                        : "border-ink-line bg-ink-800",
                    )}
                  >
                    <p className="font-mono text-xs text-fg-dim">
                      {key.replace(/_/g, " ")}
                    </p>
                    <p className="mt-0.5 text-xs text-fg-dim">{GAP_MEANING[key]}</p>
                    <p
                      className={clsx(
                        "mt-2 font-mono text-2xl font-semibold",
                        gap.pp > 0.5 ? "text-tri-minor" : "text-fg",
                      )}
                    >
                      {gap.pp > 0 ? "+" : ""}
                      {gap.pp.toFixed(1)}pp
                    </p>
                    <p className="mt-1 font-mono text-xs text-fg-dim">
                      p = {gap.p_value.toFixed(3)} ·{" "}
                      {significant ? "significant at 5%" : "not significant at 5%"}
                    </p>
                  </div>
                );
              })}
            </div>
            <p className="mt-3 max-w-4xl text-xs leading-relaxed text-fg-dim">
              The taxonomy is worth roughly twenty points. The model is worth nothing
              measurable — and that is reported as it came out. The diagnosis is in the
              next section: the model is consulted on 24 of 110 codes, and the training
              data contains no variation in the timing decision it was built to make.
            </p>
          </section>

          {/* model-eligible surface */}
          {report.model_eligible?.arms && (
            <section>
              <h2 className="mb-2 text-sm font-medium text-fg">
                Model-eligible codes only
              </h2>
              <p className="mb-3 max-w-4xl text-xs leading-relaxed text-fg-dim">
                The model is consulted for{" "}
                <span className="font-mono text-fg">
                  {report.model_eligible.actions.join(", ")}
                </span>{" "}
                and nothing else — {report.model_eligible.codes} of{" "}
                {report.model_eligible.total_codes} codes. On the other{" "}
                {report.model_eligible.total_codes - report.model_eligible.codes},
                treatment delegates to baseline and the two arms are identical by
                construction. This section restricts to the{" "}
                {report.model_eligible.payments} payments where the model can move
                anything at all.
              </p>
              <div className="grid gap-3 md:grid-cols-2">
                <div className="overflow-x-auto rounded-card border border-ink-line">
                  <table className="w-full text-sm">
                    <thead className="bg-ink-800 text-left text-xs text-fg-dim">
                      <tr>
                        <th className="px-4 py-2 font-medium">arm</th>
                        <th className="px-4 py-2 font-medium">payments</th>
                        <th className="px-4 py-2 font-medium">rate</th>
                      </tr>
                    </thead>
                    <tbody className="font-mono text-[13px]">
                      {arms.map((arm) => (
                        <tr key={arm} className="border-t border-ink-line">
                          <td className="px-4 py-2 text-fg">{arm}</td>
                          <td className="px-4 py-2 text-fg-dim">
                            {report.model_eligible.arms[arm]?.payments}
                          </td>
                          <td className="px-4 py-2 font-semibold text-fg">
                            {pct(report.model_eligible.arms[arm]?.rate)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="rounded-card border border-ink-line bg-ink-800 p-4">
                  {report.model_eligible.gaps.map((gap: any) => (
                    <div
                      key={`${gap.focus}-${gap.reference}`}
                      className="flex items-baseline justify-between border-b border-ink-line py-1.5 last:border-0"
                    >
                      <span className="font-mono text-xs text-fg-dim">
                        {gap.focus} − {gap.reference}
                      </span>
                      <span
                        className={clsx(
                          "font-mono text-sm",
                          gap.pp < 0 ? "text-tri-immediate" : "text-fg",
                        )}
                      >
                        {gap.pp > 0 ? "+" : ""}
                        {gap.pp.toFixed(1)}pp · p = {gap.p_value.toFixed(3)}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            </section>
          )}

          {/* model diagnostics */}
          {report.model_diagnostics?.top_features?.length > 0 && (
            <section>
              <h2 className="mb-2 text-sm font-medium text-fg">Model diagnostics</h2>
              <div className="grid gap-3 md:grid-cols-2">
                <div className="rounded-card border border-ink-line bg-ink-800 p-4">
                  <p className="mb-2 text-xs text-fg-dim">
                    Held-out temporal test split
                  </p>
                  <table className="w-full font-mono text-[12px]">
                    <tbody>
                      {Object.entries(report.model_diagnostics.splits).map(
                        ([name, s]: [string, any]) =>
                          s.n ? (
                            <tr key={name} className="border-b border-ink-line last:border-0">
                              <td className="py-1 text-fg">{name}</td>
                              <td className="py-1 text-fg-dim">n={s.n}</td>
                              <td className="py-1 text-fg-dim">base {s.base_rate}</td>
                              <td className="py-1 text-fg">PR-AUC {s.pr_auc}</td>
                            </tr>
                          ) : null,
                      )}
                    </tbody>
                  </table>
                </div>
                <div className="rounded-card border border-ink-line bg-ink-800 p-4">
                  <p className="mb-2 text-xs text-fg-dim">Top features by gain</p>
                  {(() => {
                    const features = report.model_diagnostics.top_features;
                    const total =
                      features.reduce((a: number, f: any) => a + f.gain, 0) || 1;
                    return features.slice(0, 8).map((f: any) => (
                      <div key={f.feature} className="mb-1 flex items-center gap-2">
                        <span className="w-52 shrink-0 truncate font-mono text-[11px] text-fg">
                          {f.feature}
                        </span>
                        <span className="h-1.5 flex-1 rounded-full bg-ink-700">
                          <span
                            className="block h-full rounded-full bg-tri-delayed"
                            style={{ width: `${(100 * f.gain) / total}%` }}
                          />
                        </span>
                        <span className="w-12 shrink-0 text-right font-mono text-[11px] text-fg-dim">
                          {((100 * f.gain) / total).toFixed(1)}%
                        </span>
                      </div>
                    ));
                  })()}
                  <p className="mt-3 text-[11px] leading-relaxed text-fg-dim">
                    <span className="text-fg">candidate_delay_hours carries zero gain.</span>{" "}
                    The training data comes from an arm that always schedules at the same
                    delay, so the model never saw the timing decision vary. It can rank
                    which cases recover; it cannot rank when to retry.
                  </p>
                </div>
              </div>
            </section>
          )}

          {/* losses */}
          <section>
            <h2 className="mb-2 text-sm font-medium text-fg">
              Where the policy arm loses
            </h2>
            {report.losing_segments?.length ? (
              <>
                <p className="mb-3 max-w-4xl text-xs leading-relaxed text-fg-dim">
                  {report.losing_segments.length} segment(s) where the focus arm recovers
                  a smaller share than control. Published because suppressing them would
                  defeat the premise of the project.
                </p>
                <div className="overflow-x-auto rounded-card border border-tri-immediate/30">
                  <table className="w-full text-sm">
                    <thead className="bg-tri-immediate/10 text-left text-xs text-fg-dim">
                      <tr>
                        <th className="px-4 py-2 font-medium">code</th>
                        <th className="px-4 py-2 font-medium">n</th>
                        <th className="px-4 py-2 font-medium">pp</th>
                      </tr>
                    </thead>
                    <tbody className="font-mono text-[13px]">
                      {report.losing_segments.map((row: any) => (
                        <tr key={row.code} className="border-t border-ink-line">
                          <td className="px-4 py-2 text-fg">{row.code}</td>
                          <td className="px-4 py-2 text-fg-dim">{row.n}</td>
                          <td className="px-4 py-2 font-semibold text-tri-immediate">
                            {row.pp.toFixed(1)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            ) : (
              <p className="text-xs text-fg-dim">
                No code shows the focus arm below control in this run. That is a claim
                about this run at this sample size, not that no such segment exists.
              </p>
            )}
          </section>

          {/* per-code */}
          <section>
            <h2 className="mb-2 text-sm font-medium text-fg">By error code</h2>
            <p className="mb-3 text-xs text-fg-dim">
              Every code that produced a case, sorted by sample size. Negative rows are
              marked and never filtered.
            </p>
            <div className="max-h-[460px] overflow-auto rounded-card border border-ink-line">
              <table className="w-full text-sm">
                <thead className="sticky top-0 bg-ink-800 text-left text-xs text-fg-dim">
                  <tr>
                    <th className="px-4 py-2 font-medium">code</th>
                    <th className="px-4 py-2 font-medium">action</th>
                    <th className="px-4 py-2 font-medium">n</th>
                    {arms.map((arm) => (
                      <th key={arm} className="px-4 py-2 font-medium">
                        {arm}
                      </th>
                    ))}
                    <th className="px-4 py-2 font-medium">pp</th>
                  </tr>
                </thead>
                <tbody className="font-mono text-[12.5px]">
                  {report.by_error_code.map((row: any) => (
                    <tr key={row.code} className="border-t border-ink-line">
                      <td className="px-4 py-1.5 text-fg">{row.code}</td>
                      <td className="px-4 py-1.5">
                        <TriageChip action={row.action} />
                      </td>
                      <td className="px-4 py-1.5 text-fg-dim">{row.n}</td>
                      {arms.map((arm) => (
                        <td key={arm} className="px-4 py-1.5 text-fg-dim">
                          {pct(row[arm])}
                        </td>
                      ))}
                      <td
                        className={clsx(
                          "px-4 py-1.5",
                          row.pp < 0 ? "font-semibold text-tri-immediate" : "text-fg-dim",
                        )}
                      >
                        {row.pp === null ? "—" : `${row.pp > 0 ? "+" : ""}${row.pp.toFixed(1)}`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section className="rounded-card border border-ink-line bg-ink-800 p-4">
            <h2 className="mb-2 text-sm font-medium text-fg">Caveats</h2>
            <ul className="space-y-1.5 text-xs leading-relaxed text-fg-dim">
              <li>
                <span className="text-fg">The data is synthetic.</span> No public NPCI
                decline dataset exists. Rates are grounded in Razorpay's published
                material where it says anything and marked as our assumptions where it
                does not.
              </li>
              <li>
                <span className="text-fg">
                  The simulator and the taxonomy make the same causal claim.
                </span>{" "}
                Both say a wrong PIN does not fix itself. This tests whether acting on
                that claim beats ignoring it — not whether the claim is true.
              </li>
              <li>
                <span className="text-fg">Samples are small.</span> Per-code rows
                routinely carry n &lt; 10, where one case moves the rate by ten points.
              </li>
            </ul>
          </section>
        </div>
      )}
    </div>
  );
}

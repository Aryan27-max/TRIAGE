"use client";

import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  api,
  rupees,
  type Decision,
  type Downtime,
  type ErrorPolicy,
} from "@/lib/api";
import { Checkout, type CheckoutSubmission } from "@/components/Checkout";
import { Inspector, type InspectorEvent } from "@/components/Inspector";

// The simulated clock. Every write endpoint takes `now` explicitly — there is no
// server clock anywhere in src/ — so the demo picks an instant inside the run window
// and moves it forward as it goes.
const BASE_NOW = 1737025200;

export default function LivePage() {
  const [codes, setCodes] = useState<ErrorPolicy[]>([]);
  const [events, setEvents] = useState<InspectorEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [caseId, setCaseId] = useState<string | null>(null);
  const [railSeverity, setRailSeverity] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<{ action: string; message: string } | null>(null);
  const [now, setNow] = useState(BASE_NOW);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.errors().then((r) => setCodes(r.items)).catch(() => setCodes([]));
  }, []);

  const refreshRails = useCallback(
    async (at: number) => {
      try {
        const rails = await api.rails(at);
        const worst = rails.items.reduce<string | null>((acc, item) => {
          const rank = { low: 1, medium: 2, high: 3 } as Record<string, number>;
          if (!acc || rank[item.severity] > rank[acc]) return item.severity;
          return acc;
        }, null);
        setRailSeverity(worst);
        return rails.items;
      } catch {
        return [] as Downtime[];
      }
    },
    [],
  );

  useEffect(() => {
    refreshRails(now);
  }, [now, refreshRails]);

  const submit = async (submission: CheckoutSubmission) => {
    setBusy(true);
    setError(null);
    setEvents([]);
    const at = now;
    // A confirming overlay long enough to read: this is the moment failure is injected.
    await new Promise((resolve) => setTimeout(resolve, 900));

    try {
      const active = await refreshRails(at);
      const decision = await api.decide({
        error_code: submission.errorCode,
        now: at,
        method: submission.method,
        vpa_handle: submission.method === "upi" ? submission.vpaHandle : undefined,
      });
      const derivedCase = `case_${submission.errorCode.slice(0, 6)}${at % 10000}`;
      setCaseId(derivedCase);
      setEvents(buildChain(decision, submission, at, active, derivedCase));
      setOutcome({ action: decision.action, message: merchantCopy(decision) });
      setNow(at + 1);
    } catch (caught) {
      const message =
        caught instanceof ApiError
          ? `${caught.status} · ${caught.envelope?.code ?? "error"} — ${caught.message}`
          : "Could not reach the API.";
      setError(message);
    } finally {
      setBusy(false);
    }
  };

  const injectDowntime = async (method: string, severity: string) => {
    setError(null);
    try {
      await api.injectDowntime({
        method,
        severity,
        scope: "all",
        begin: now - 3600,
        end: now + 86400,
      });
      await refreshRails(now);
      setOutcome({
        action: "DOWNTIME",
        message: `Injected a ${severity}-severity downtime on ${method}. The next decision on that rail will see it.`,
      });
    } catch (caught) {
      setError(
        caught instanceof ApiError && caught.status === 503
          ? "This instance is read-only — downtime injection is disabled. Run it locally to drive the rails."
          : "Could not inject the downtime event.",
      );
    }
  };

  return (
    <div className="pt-6">
      <p className="mb-4 rounded-card border border-ink-line bg-ink-800 px-4 py-2.5 text-xs leading-relaxed text-fg-dim">
        <span className="font-medium text-fg">Simulated environment.</span> No real
        payment is made and no real bank is contacted. The failure reasons are
        Razorpay's published documentation; the action classification and the decision
        policy are this project's contribution.
      </p>

      {error && (
        <p className="mb-4 rounded-card border border-tri-immediate/30 bg-tri-immediate/10 px-4 py-2.5 text-xs text-tri-immediate">
          {error}
        </p>
      )}

      <div className="grid gap-5 lg:grid-cols-[minmax(0,40%)_minmax(0,60%)]">
        <Checkout
          codes={codes}
          onSubmit={submit}
          onInjectDowntime={injectDowntime}
          busy={busy}
          lastOutcome={outcome}
        />
        <Inspector
          caseId={caseId}
          events={events}
          live={events.length > 0}
          railSeverity={railSeverity}
        />
      </div>
    </div>
  );
}

/** What the merchant is told. States what happened and what comes next; no apology. */
function merchantCopy(decision: Decision): string {
  switch (decision.action) {
    case "SWITCH_RAIL":
      return decision.rail_health?.switch_blocked
        ? `The bank declined this and the alternate rail is also degraded. TRIAGE is holding for ${Math.round((decision.constraints as any).min_wait_hours || 4)}h rather than switching into a second outage.`
        : `The bank declined this. TRIAGE is routing to ${decision.target_rail}.`;
    case "RETRY_SCHEDULED":
      return `This can succeed later. TRIAGE will retry in ${decision.min_wait_hours}h.`;
    case "RETRY_NOW":
      return "A transient glitch. TRIAGE is re-attempting in seconds.";
    case "SWITCH_INSTRUMENT":
      return "This instrument cannot be charged. No retry can succeed — a different card or method is needed.";
    case "NUDGE_CUSTOMER":
      return "This needs the customer to act. TRIAGE is sending them a message rather than retrying.";
    case "AWAIT_STATUS":
      return "The outcome is unknown. TRIAGE will not retry until a status poll resolves it — retrying now risks a double charge.";
    case "STOP":
      return "Retrying this is unsafe or penalised. TRIAGE is stopping.";
    case "MERCHANT_ALERT":
      return "This is a merchant configuration fault, not a customer failure. TRIAGE is alerting the merchant.";
    default:
      return decision.explanation;
  }
}

/**
 * The decision chain, built from the API's real response.
 *
 * The `model.score` row is the one that matters. On the 86 ineligible codes it renders
 * hollow and says *not invoked* — its absence is the proof of I-1. On the 24 eligible
 * ones it renders filled and carries the candidate set the treatment arm would rank.
 */
function buildChain(
  decision: Decision,
  submission: CheckoutSubmission,
  at: number,
  active: Downtime[],
  caseId: string,
): InspectorEvent[] {
  const stamp = (offsetMs: number) => {
    const base = new Date(at * 1000);
    base.setMilliseconds(offsetMs);
    return `${base.toISOString().slice(11, 19)}.${String(offsetMs % 1000).padStart(3, "0")}`;
  };
  const rail = decision.rail_health;

  const chain: InspectorEvent[] = [
    {
      id: "failed",
      at: stamp(204),
      name: "payment.failed",
      duration: "302ms",
      summary: `${decision.error_code} · source: ${submission.method === "upi" ? "bank" : "gateway"} · ${submission.method} · ${rupees(submission.amountPaise)}`,
      payload: {
        error_code: decision.error_code,
        method: submission.method,
        amount: submission.amountPaise,
        currency: "INR",
        vpa_handle: submission.method === "upi" ? submission.vpaHandle : null,
      },
    },
    {
      id: "policy",
      at: stamp(208),
      name: "policy.resolve",
      duration: "4ms",
      summary: `${decision.action} · family ${decision.family} · ${decision.recoverable ? "recoverable" : "not silently recoverable"}`,
      action: decision.action,
      payload: {
        action: decision.action,
        family: decision.family,
        recoverable: decision.recoverable,
        min_wait_hours: decision.min_wait_hours,
        reason_code: decision.reason_code,
        advice: decision.advice,
        razorpay_explanation: decision.explanation,
        razorpay_next_steps: decision.next_steps,
      },
    },
    {
      id: "rails",
      at: stamp(220),
      name: "rails.health",
      duration: "12ms",
      summary: rail?.severity
        ? `${rail.method} · severity: ${rail.severity} · ${rail.active_events} active event(s)`
        : `${rail?.active_events ?? 0} active event(s) · nothing affecting ${submission.method}`,
      hollow: !rail?.severity,
      payload: { rail_health: rail, active: active.slice(0, 4) },
    },
    {
      id: "model",
      at: stamp(221),
      name: "model.score",
      duration: decision.model?.eligible ? "18ms" : "not invoked",
      hollow: !decision.model?.eligible,
      emphasis: true,
      summary: decision.model?.eligible
        ? `${decision.action} is model-eligible — LightGBM ranks candidate executions within the class the table already permitted`
        : `not invoked — ${decision.action} is decided by the policy table alone (I-1)`,
      payload: {
        eligible: decision.model?.eligible,
        eligible_actions: decision.model?.eligible_actions,
        reason: decision.model?.reason,
        note:
          "The model is consulted for 24 of the 110 codes. It ranks executions " +
          "within a permitted class; it never chooses the class. A model error " +
          "therefore cannot cause an unrecoverable failure to be retried.",
      },
    },
    {
      id: "executor",
      at: stamp(227),
      name: "executor.decide",
      duration: "6ms",
      summary:
        decision.scheduled_at === null
          ? `no retry scheduled · advice: ${decision.advice}`
          : `${decision.target_rail ? `route to ${decision.target_rail} · ` : ""}fires at +${decision.min_wait_hours || "0"}h · attempt 1 of ${(decision.constraints as any).max_attempts}`,
      payload: { decision_id: decision.decision_id, ...decision.constraints, scheduled_at: decision.scheduled_at },
    },
    {
      id: "audit",
      at: stamp(229),
      name: "audit.write",
      duration: "2ms",
      summary: `RECEIVED → DIAGNOSED · key ${caseId}:1`,
      payload: {
        from: "RECEIVED",
        to: "DIAGNOSED",
        actor: "policy_engine",
        reason: "error_policy lookup",
        idempotency_key: `${caseId}:1`,
      },
    },
  ];
  return chain;
}

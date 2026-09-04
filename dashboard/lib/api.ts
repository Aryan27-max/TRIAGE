// The API client. One module, no data-fetching library: four screens do not need one.

export const API_URL =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") || "http://127.0.0.1:8000";

export type Health = {
  status: string;
  policy_codes_loaded: number;
  policy_version: string;
  read_only: boolean;
};

export type ErrorPolicy = {
  code: string;
  family: string;
  action: string;
  min_wait_hours: number;
  recoverable: boolean;
  is_retrying: boolean;
  is_model_eligible: boolean;
  policy_note: string;
  razorpay_explanation: string;
  razorpay_next_steps: string;
};

export type RailHealthSnapshot = {
  method: string | null;
  severity: string | null;
  target_rail: string | null;
  target_severity: string | null;
  active_events: number;
  switch_blocked: boolean;
};

export type ModelDisposition = {
  eligible: boolean;
  consulted: boolean;
  reason: string;
  eligible_actions: string[];
};

export type Decision = {
  decision_id: string;
  error_code: string;
  action: string;
  family: string;
  recoverable: boolean;
  scheduled_at: number | null;
  target_rail: string | null;
  min_wait_hours: number;
  reason_code: string;
  advice: string;
  explanation: string;
  next_steps: string;
  model_eligible: boolean;
  constraints: Record<string, unknown>;
  rail_health: RailHealthSnapshot | null;
  model: ModelDisposition | null;
};

export type Downtime = {
  id: string;
  entity: string;
  method: string;
  scope: string;
  instrument: string | null;
  severity: string;
  status: string;
  begin: number;
  end: number | null;
};

export type RunSummary = {
  run_id: string;
  scenario: string;
  seed: number;
  n_payments: number;
  days: number;
  trailing_days: number;
  arms: string[];
};

export type CaseSummary = {
  id: string;
  payment_id: string;
  status: string;
  arm: string | null;
  method: string;
  rail: string;
  amount_paise: number;
  error_code: string;
  failed_at: number;
  attempt_count: number;
  recovered_at: number | null;
};

export class ApiError extends Error {
  constructor(
    public status: number,
    public envelope: Record<string, string> | null,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    cache: "no-store",
  });
  if (!response.ok) {
    let envelope: Record<string, string> | null = null;
    let description = response.statusText;
    try {
      const body = await response.json();
      envelope = body?.error ?? null;
      description = envelope?.description ?? description;
    } catch {
      /* a non-JSON body is still a failure; the status carries the meaning */
    }
    throw new ApiError(response.status, envelope, description);
  }
  return response.json() as Promise<T>;
}

export const api = {
  health: () => request<Health>("/health"),
  errors: () => request<{ count: number; items: ErrorPolicy[] }>("/v1/errors"),
  coverage: () =>
    request<{
      total_codes: number;
      recoverable_codes: number;
      unrecoverable_codes: number;
      headline: string;
      by_action: { action: string; count: number; recoverable: boolean; description: string }[];
    }>("/v1/errors/meta/coverage"),
  decide: (body: {
    error_code: string;
    now: number;
    method?: string;
    vpa_handle?: string;
    payer_bank?: string;
  }) => request<Decision>("/v1/recovery/decide", { method: "POST", body: JSON.stringify(body) }),
  rails: (at?: number) =>
    request<{ count: number; items: Downtime[] }>(
      `/v1/rails/health${at ? `?at=${at}` : ""}`,
    ),
  injectDowntime: (body: {
    method: string;
    severity: string;
    begin: number;
    end?: number;
    scope?: string;
    instrument?: string | null;
  }) => request<Downtime>("/v1/rails/health", { method: "POST", body: JSON.stringify(body) }),
  runs: () => request<{ count: number; items: RunSummary[] }>("/v1/eval/runs"),
  report: (runId: string) => request<any>(`/v1/eval/report/${runId}`),
  cases: (params: Record<string, string | number | undefined>) => {
    const query = Object.entries(params)
      .filter(([, v]) => v !== undefined && v !== "")
      .map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`)
      .join("&");
    return request<{ count: number; items: CaseSummary[] }>(
      `/v1/recovery/cases${query ? `?${query}` : ""}`,
    );
  },
  caseDetail: (id: string) => request<any>(`/v1/recovery/cases/${id}`),
};

// -- the triage scale ---------------------------------------------------------
// Medical triage sorts casualties by the intervention they need. The five categories
// are where the project's name comes from, and the same five colours run through the
// taxonomy board, the inspector rows and the case list.

export type TriageCategory =
  | "immediate"
  | "delayed"
  | "minor"
  | "expectant"
  | "merchant";

export const TRIAGE_OF_ACTION: Record<string, TriageCategory> = {
  RETRY_NOW: "immediate",
  SWITCH_RAIL: "immediate",
  RETRY_SCHEDULED: "delayed",
  AWAIT_STATUS: "delayed",
  NUDGE_CUSTOMER: "minor",
  SWITCH_INSTRUMENT: "expectant",
  STOP: "expectant",
  MERCHANT_ALERT: "merchant",
};

export const TRIAGE_LABEL: Record<TriageCategory, string> = {
  immediate: "Immediate",
  delayed: "Delayed",
  minor: "Minor",
  expectant: "Expectant",
  merchant: "Not a casualty",
};

export const ACTION_ORDER = [
  "RETRY_NOW",
  "RETRY_SCHEDULED",
  "SWITCH_RAIL",
  "SWITCH_INSTRUMENT",
  "NUDGE_CUSTOMER",
  "AWAIT_STATUS",
  "STOP",
  "MERCHANT_ALERT",
];

export const rupees = (paise: number) =>
  `₹${(paise / 100).toLocaleString("en-IN", { minimumFractionDigits: 2 })}`;

export const clock = (ts: number, withMillis = false) => {
  const d = new Date(ts * 1000);
  const base = d.toISOString().slice(11, 19);
  return withMillis ? `${base}.${String(d.getUTCMilliseconds()).padStart(3, "0")}` : base;
};

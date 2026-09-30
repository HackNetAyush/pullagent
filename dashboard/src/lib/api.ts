/**
 * Typed client for the dashboard API.
 *
 * Every list endpoint returns a `Page<T>` — items plus the real total — so a
 * table can say "25 of 3,214" instead of quietly showing the first page and
 * letting the reader assume that is everything.
 */

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface Run {
  id: number;
  repo: string;
  pr: number | null;
  tier: string;
  model: string;
  source: string;
  actor: string;
  status: "running" | "done" | "failed" | "stalled" | "cancelled";
  stage: string;
  stage_index: number;
  stages: string[];
  posted: number;
  killed: number;
  cost: number;
  elapsed: number;
  input_tokens: number;
  output_tokens: number;
  cache_read: number;
  cache_write: number;
  cache_hit: boolean;
  cached_cost_usd: number;
  judge_cost: number;
  error: string;
  started_at: string | null;
}

export interface Finding {
  id: number;
  run_id?: number;
  repo?: string;
  pr?: number | null;
  tier?: string;
  claim: string;
  failure_scenario: string;
  category: string;
  severity: string;
  confidence: number;
  file: string;
  line: number;
  found_by: string;
  posted: boolean;
  fingerprint: string;
}

export interface RunDetail extends Run {
  findings: Finding[];
}

export interface DayPoint {
  date: string;
  cost: number;
  runs: number;
  posted: number;
}

export interface Overview {
  window_days: number;
  runs: number;
  active: number;
  posted: number;
  killed: number;
  kill_rate: number;
  kill_rate_sample: number;
  kill_rate_reliable: boolean;
  cost: number;
  cost_per_review: number;
  cache_hit: number;
  suppressions: number;
  suppression_hits: number;
  series: DayPoint[];
  severity: Record<string, number>;
  cost_by_model: Record<string, number>;
  cost_by_source: Record<string, number>;
}

export interface Suppression {
  id: number;
  repo: string;
  fingerprint: string;
  reason: string;
  file: string;
  claim: string;
  note: string;
  hits: number;
  pr: number | null;
}

export interface RepoRow {
  repo: string;
  runs: number;
  cost: number;
  posted: number;
  killed: number;
  suppressions: number;
  pull_requests: number;
  last_run: string | null;
}

export interface Job {
  id: number;
  kind: string;
  key: string;
  repo: string;
  status: string;
  attempts: number;
  error: string;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface Me {
  signed_in: boolean;
  sign_in_configured: boolean;
  login?: string;
  name?: string;
  avatar_url?: string;
  is_admin?: boolean;
}

export interface AppStatus {
  configured: boolean;
  app_id: string;
  slug: string;
  webhook_secret_set: boolean;
  public_url: string;
  webhook_url?: string;
  incremental: boolean;
  review_forks: boolean;
  review_drafts: boolean;
  debounce_s: number;
  installations: { id: number; account: string; repos: string[]; suspended: boolean }[];
  queue: { waiting: { key: string; kind: string; in_s: number }[]; running: string[]; concurrency: number };
}

export interface AccountRow {
  login: string;
  status: string;
  account_type: string;
  note: string;
  requested_by: string;
  decided_by: string;
  decided_at: string | null;
  blocked_events: number;
  last_blocked_at: string | null;
}

export interface Facets {
  repos: string[];
  statuses: string[];
  sources: string[];
  tiers: string[];
}

/** Thrown with the status attached so a 401 can drive the sign-in gate
 *  rather than surfacing as an anonymous "something went wrong". */
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

type Params = Record<string, string | number | boolean | undefined | null>;

function qs(params: Params = {}): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, {
    ...init,
    headers: { Accept: "application/json", ...(init?.body ? { "Content-Type": "application/json" } : {}), ...init?.headers },
  });
  if (!r.ok) {
    let detail = r.statusText;
    try {
      const body = await r.json();
      detail = body.detail || body.error || detail;
    } catch {
      /* a non-JSON error body is still an error; keep the status text */
    }
    throw new ApiError(r.status, detail);
  }
  return (await r.json()) as T;
}

export const api = {
  me: () => request<Me>("/api/me"),
  logout: () => request<{ ok: boolean }>("/auth/logout", { method: "POST" }),

  overview: (days: number) => request<Overview>(`/api/overview${qs({ days })}`),
  config: () => request<Record<string, unknown>>("/api/config"),

  runs: (params: Params) => request<Page<Run>>(`/api/runs${qs(params)}`),
  runFacets: () => request<Facets>("/api/runs/facets"),
  activeRuns: () => request<Run[]>("/api/runs/active"),
  run: (id: number) => request<RunDetail>(`/api/runs/${id}`),

  findings: (params: Params) => request<Page<Finding>>(`/api/findings${qs(params)}`),
  repos: (params: Params) => request<Page<RepoRow>>(`/api/repos${qs(params)}`),
  suppressions: (params: Params) => request<Page<Suppression>>(`/api/suppressions${qs(params)}`),

  appStatus: () => request<AppStatus>("/api/app/status"),
  jobs: (params: Params) => request<Page<Job>>(`/api/app/jobs${qs(params)}`),

  accounts: (status?: string) => request<AccountRow[]>(`/api/admin/accounts${qs({ status })}`),
  decideAccount: (login: string, status: string, note?: string) =>
    request<{ login: string; status: string }>(`/api/admin/accounts/${encodeURIComponent(login)}`, {
      method: "POST",
      body: JSON.stringify({ status, note }),
    }),
  myAccount: () => request<{ login: string; status: string }>("/api/me/accounts"),
  requestAccess: (login?: string, note?: string) =>
    request<{ login: string; status: string; changed: boolean }>("/api/access-requests", {
      method: "POST",
      body: JSON.stringify({ login, note }),
    }),
};

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
  billing: "managed" | "byok";
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
  /** Spend on CR credits. */
  managed: number;
  /** Spend on customers' own API keys. */
  byok: number;
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
  byok: ByokSpend;
}

export interface ByokModelSpend {
  /** Model name and provider, e.g. "gpt-oss-120b · Groq". */
  label: string;
  cost: number;
  input_tokens: number;
  output_tokens: number;
  /** False when some of it ran without a known price: cost is a floor. */
  priced: boolean;
}

export interface ByokSpend {
  cost: number;
  runs: number;
  by_model: ByokModelSpend[];
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

export type Effort = "low" | "medium" | "high" | "xhigh" | "max";
export type Slot = "T1" | "T2" | "T3";
export type CacheMode = "explicit" | "automatic" | "none";

export interface WorkspaceRef {
  login: string;
  kind: "personal" | "organization";
  /** "owner" (your own account), "admin" or "member" of an organisation. */
  role: "owner" | "admin" | "member";
  /** Owners and org admins change keys, tiers and routing; members only view. */
  can_manage: boolean;
  /** The App is installed on this account, so its pull requests get reviewed. */
  installed: boolean;
}

export interface WorkspaceList {
  login: string;
  is_admin: boolean;
  workspaces: WorkspaceRef[];
  /** Where to install the App on another account; "" when unknown. */
  install_url: string;
}

export interface ProviderOption {
  id: string;
  label: string;
  host: string;
  needs_resource: boolean;
  docs_url: string;
  /** What to type in the models field for this provider. */
  model_hint: string;
  /** Catalog models offered as one-click suggestions; any other name works too. */
  suggestions: { name: string; label: string; pricing: { input: number; output: number } | null }[];
}

export interface ConnectionModel {
  /** How a tier refers to this model: conn:<id>:<name>. */
  ref: string;
  name: string;
  label: string;
  /** In CR's catalog (known pricing); false for custom and deployment names. */
  listed: boolean;
  effort_levels: Effort[];
  /** The price spend is tracked at: the customer's own, else the catalog's. */
  pricing: ModelPrice | null;
  price_source: "custom" | "catalog" | null;
  catalog_pricing: ModelPrice | null;
  spend_30d: number;
  tested_at: string | null;
}

/** USD per million tokens. */
export interface ModelPrice {
  input: number;
  output: number;
}

/** A provider account the customer brought: never the key, only its last four. */
export interface Connection {
  id: number;
  provider: string;
  provider_label: string;
  label: string;
  host: string;
  resource: string;
  hint: string;
  models: ConnectionModel[];
  tested_at: string | null;
  used_by: string[];
  spend_30d: number;
}

export interface ConnectionDraft {
  provider: string;
  /** Blank when editing: keep the saved key. */
  api_key: string;
  resource: string;
  label: string;
  models: string[];
  /** Optional per-model prices; changing only these never needs a re-test. */
  prices: Record<string, ModelPrice | null>;
}

export interface ModelTestResult {
  model: string;
  label: string;
  listed: boolean;
  ok: boolean;
  effort: boolean;
  latency_ms: number;
  cost_usd: number;
  detail: string;
}

export interface SampleReviewResult {
  model: string;
  /** A finder reported the planted bug. */
  found: boolean;
  /** ...and it survived verification, so it would have been posted. */
  verified: boolean;
  claim: string;
  other_findings: number;
  cost_usd: number;
  elapsed_s: number;
  errors: string[];
}

export interface ConnectionTest {
  ok: boolean;
  results: ModelTestResult[];
  /** Present only when every model passed; the save call must send it back. */
  receipt: string | null;
}

/** A managed tier, described by what it does - never by what serves it. */
export interface Preset {
  slot: Slot;
  label: string;
  when: string;
  finders: string[];
  verifiers: string[];
  effort: Effort;
  max_comments: number;
}

export interface Lens {
  id: string;
  description: string;
}

export interface StageChoice {
  model: string;
  effort: Effort;
}

export interface LensChoice {
  lens: string;
  /** null: use the stage default. */
  model?: string | null;
  effort?: Effort | null;
}

/** A lens the workspace wrote itself. */
export interface CustomLens {
  id: string;
  name: string;
  instruction: string;
  model?: string | null;
  effort?: Effort | null;
}

export interface TierSpec {
  name: string;
  finder: StageChoice;
  verifier: StageChoice;
  finders: LensChoice[];
  verifiers: LensChoice[];
  custom_finders: CustomLens[];
  custom_verifiers: CustomLens[];
  max_comments: number;
}

export interface CustomTier {
  id: number;
  name: string;
  spec: TierSpec;
  /** Why the tier cannot run right now; empty when it can. */
  problems: string[];
  /** Per custom finder lens: wording likely to make the model drop findings. */
  warnings: Record<string, string[]>;
  updated_at: string | null;
}

export interface Routing {
  /** Tier for every repository; null runs CR's managed review. */
  all: number | null;
  repos: Record<string, number>;
}

export interface Workspace {
  account: string;
  /** False for org members: they see the settings but cannot change them. */
  can_manage: boolean;
  vault_ready: boolean;
  providers: ProviderOption[];
  connections: Connection[];
  presets: Preset[];
  lenses: { finders: Lens[]; verifiers: Lens[] };
  tiers: CustomTier[];
  /** Where the account forces its own tiers. A repository rule beats `all`. */
  routing: Routing;
  /** The account's known repositories, for the picker. */
  repos: string[];
  /** Review guidelines per repository (lower-cased owner/name). */
  guidelines: Record<string, string>;
  limits: {
    max_comments: number;
    max_models: number;
    max_guidelines: number;
    max_custom_finders: number;
    max_custom_verifiers: number;
  };
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

const enc = encodeURIComponent;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, {
    ...init,
    headers: { Accept: "application/json", ...(init?.body ? { "Content-Type": "application/json" } : {}), ...init?.headers },
  });
  if (!r.ok) {
    let detail = r.statusText;
    try {
      const body = await r.json();
      const raw = body.detail || body.error || detail;
      // FastAPI reports body validation as a list of {loc, msg}; show the messages.
      detail = Array.isArray(raw)
        ? raw.map((d: { msg?: string }) => d.msg || "").filter(Boolean).join("; ") || detail
        : String(raw);
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

  // Every read below is scoped to one workspace; the server enforces it.
  overview: (days: number, account: string) =>
    request<Overview>(`/api/overview${qs({ days, account })}`),
  config: () => request<Record<string, unknown>>("/api/config"),

  runs: (params: Params) => request<Page<Run>>(`/api/runs${qs(params)}`),
  runFacets: (account: string) => request<Facets>(`/api/runs/facets${qs({ account })}`),
  activeRuns: (account: string) => request<Run[]>(`/api/runs/active${qs({ account })}`),
  run: (id: number) => request<RunDetail>(`/api/runs/${id}`),

  findings: (params: Params) => request<Page<Finding>>(`/api/findings${qs(params)}`),
  repos: (params: Params) => request<Page<RepoRow>>(`/api/repos${qs(params)}`),
  suppressions: (params: Params) => request<Page<Suppression>>(`/api/suppressions${qs(params)}`),

  appStatus: (account: string) => request<AppStatus>(`/api/app/status${qs({ account })}`),
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
  workspaces: () => request<WorkspaceList>("/api/workspaces"),
  checkInstallation: (account: string) =>
    request<{ account: string; installed: boolean }>(
      `/api/workspaces/${enc(account)}/installation/check`,
      { method: "POST" },
    ),
  workspace: (account: string) => request<Workspace>(`/api/workspaces/${enc(account)}`),
  testConnection: (account: string, draft: ConnectionDraft, id?: number) =>
    request<ConnectionTest>(
      `/api/workspaces/${enc(account)}/connections${id ? `/${id}` : ""}/test`,
      { method: "POST", body: JSON.stringify(draft) },
    ),
  saveConnection: (account: string, draft: ConnectionDraft, receipt: string, id?: number) =>
    request<Connection>(`/api/workspaces/${enc(account)}/connections${id ? `/${id}` : ""}`, {
      method: id ? "PUT" : "POST",
      body: JSON.stringify({ ...draft, receipt }),
    }),
  sampleReview: (account: string, id: number, model: string) =>
    request<SampleReviewResult>(
      `/api/workspaces/${enc(account)}/connections/${id}/sample-review`,
      { method: "POST", body: JSON.stringify({ model }) },
    ),
  saveGuidelines: (account: string, repo: string, text: string) =>
    request<{ repo: string; text: string }>(`/api/workspaces/${enc(account)}/guidelines`, {
      method: "PUT",
      body: JSON.stringify({ repo, text }),
    }),
  deleteConnection: (account: string, id: number) =>
    request<{ deleted: number }>(`/api/workspaces/${enc(account)}/connections/${id}`, {
      method: "DELETE",
    }),
  createTier: (account: string, spec: TierSpec) =>
    request<CustomTier>(`/api/workspaces/${enc(account)}/tiers`, {
      method: "POST",
      body: JSON.stringify(spec),
    }),
  updateTier: (account: string, id: number, spec: TierSpec) =>
    request<CustomTier>(`/api/workspaces/${enc(account)}/tiers/${id}`, {
      method: "PUT",
      body: JSON.stringify(spec),
    }),
  deleteTier: (account: string, id: number) =>
    request<{ deleted: number }>(`/api/workspaces/${enc(account)}/tiers/${id}`, { method: "DELETE" }),
  saveRouting: (account: string, routing: Routing) =>
    request<{ routing: Routing }>(`/api/workspaces/${enc(account)}/routing`, {
      method: "PUT",
      body: JSON.stringify(routing),
    }),
};

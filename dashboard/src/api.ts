export interface Run {
  id: number;
  repo: string;
  pr: number | null;
  tier: string;
  model: string;
  status: "running" | "done" | "failed" | "stalled";
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
  error: string;
  started_at: string | null;
}

export interface Finding {
  id: number;
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
}

export interface Suppression {
  id: number;
  repo: string;
  fingerprint: string;
  reason: string;
  file: string;
  claim: string;
  hits: number;
  pr: number | null;
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path, { headers: { Accept: "application/json" } });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return (await r.json()) as T;
}

export const api = {
  overview: (days: number) => get<Overview>(`/api/overview?days=${days}`),
  runs: (limit = 50) => get<Run[]>(`/api/runs?limit=${limit}`),
  active: () => get<Run[]>("/api/runs/active"),
  run: (id: number) => get<RunDetail>(`/api/runs/${id}`),
  suppressions: () => get<Suppression[]>("/api/suppressions"),
};

export const fmtUSD = (n: number) =>
  n >= 100 ? `$${n.toFixed(0)}` : n >= 1 ? `$${n.toFixed(2)}` : `$${n.toFixed(4)}`;

export const fmtInt = (n: number) => n.toLocaleString();

export const fmtPct = (n: number) => `${Math.round(n * 100)}%`;

export function relTime(iso: string | null): string {
  if (!iso) return "—";
  const secs = (Date.now() - new Date(iso).getTime()) / 1000;
  if (secs < 60) return `${Math.max(0, Math.round(secs))}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
}

/** Ordinal blue ramp — severity is an ordered scale, not distinct identities. */
export const SEVERITY_ORDER = ["critical", "high", "medium", "low"] as const;
export const SEVERITY_VAR: Record<string, string> = {
  critical: "var(--ord-1)",
  high: "var(--ord-2)",
  medium: "var(--ord-3)",
  low: "var(--ord-4)",
};

/** Run state uses the reserved status palette, always with a text label beside it. */
export const STATUS_VAR: Record<string, string> = {
  running: "var(--series-1)",
  done: "var(--good)",
  failed: "var(--critical)",
  stalled: "var(--warning)",
};

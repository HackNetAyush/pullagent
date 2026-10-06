import type { Effort } from "../../lib/api";

/** Per-million prices: enough precision for $0.075 without "$10.000". */
export function fmtRate(n: number): string {
  return `$${n.toLocaleString(undefined, { maximumFractionDigits: 3 })}`;
}

export const EFFORT_LABEL: Record<Effort, string> = {
  low: "Low",
  medium: "Medium",
  high: "High",
  xhigh: "Extra high",
  max: "Max",
};

const LENS_NAMES: Record<string, string> = {
  correctness: "Correctness",
  security: "Security",
  concurrency: "Concurrency",
  api_contract: "API contracts",
  test_coverage: "Test coverage",
  performance: "Performance",
  state_and_security: "State and security",
  reachability: "Reachability",
  evidence: "Evidence",
};

export function lensName(id: string): string {
  return LENS_NAMES[id] || id.replace(/_/g, " ");
}

/**
 * Wording that asks a finder to filter rather than to find. Mirrors the
 * server's check (cr.app.workspace.filter_warnings) so the warning shows while
 * typing; the server reports the same on save.
 */
const FILTER_WORDING: [RegExp, string][] = [
  [
    /\bonly\s+(report|flag|mention|comment|raise|surface)\b/i,
    '"Only report..." makes the model stay silent about real bugs it found.',
  ],
  [
    /\b(don'?t|do not|never|ignore|skip)\b[^.\n]{0,40}\b(minor|small|low|nit|nits|trivial|style)\b/i,
    "Asking it to skip small issues drops findings; severity already ranks them.",
  ],
  [
    /\b(high|critical)[- ]severity\s+only\b|\bjust\s+(report|flag)\b/i,
    "Filtering by severity belongs to the verifier and the comment limit, not the finder.",
  ],
];

export function filterWarnings(text: string): string[] {
  return FILTER_WORDING.filter(([re]) => re.test(text || "")).map(([, why]) => why);
}

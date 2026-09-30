import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** Merge Tailwind classes so a caller's `className` reliably wins. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export const fmtUSD = (n: number) =>
  n >= 100 ? `$${n.toFixed(0)}` : n >= 1 ? `$${n.toFixed(2)}` : `$${n.toFixed(4)}`;

export const fmtInt = (n: number) => (n ?? 0).toLocaleString();

/** Axis ticks need to be short, not precise — "$0.0000" clips the gutter. */
export const fmtAxisUSD = (n: number) =>
  n === 0 ? "$0" : n >= 1 ? `$${Math.round(n)}` : `$${n.toFixed(2)}`;

export const fmtPct = (n: number) => `${Math.round((n ?? 0) * 100)}%`;

export const fmtSecs = (n: number) =>
  n < 60 ? `${Math.round(n)}s` : `${Math.floor(n / 60)}m ${Math.round(n % 60)}s`;

export function relTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const secs = (Date.now() - new Date(iso).getTime()) / 1000;
  if (secs < 60) return `${Math.max(0, Math.round(secs))}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/**
 * Severity is an ordered scale, so it gets one hue that darkens with
 * seriousness — not four unrelated identities. Colour never travels alone:
 * every use pairs it with the severity word.
 */
export const SEVERITY_ORDER = ["critical", "high", "medium", "low"] as const;
export const SEVERITY_VAR: Record<string, string> = {
  critical: "var(--sev-critical)",
  high: "var(--sev-high)",
  medium: "var(--sev-medium)",
  low: "var(--sev-low)",
};

/** Reserved status palette. Always rendered with its label beside it. */
export const STATUS_VAR: Record<string, string> = {
  done: "var(--good)",
  running: "var(--series-1)",
  queued: "var(--series-1)",
  failed: "var(--critical)",
  stalled: "var(--warning)",
  cancelled: "var(--axis)",
  superseded: "var(--axis)",
  skipped: "var(--axis)",
  approved: "var(--good)",
  pending: "var(--warning)",
  denied: "var(--critical)",
};

/** Fixed categorical order. A 7th series folds into "Other" rather than
 *  inventing a hue — see the dataviz non-negotiables. */
export const SERIES_VARS = [
  "var(--series-1)",
  "var(--series-2)",
  "var(--series-3)",
  "var(--series-4)",
  "var(--series-5)",
  "var(--series-6)",
];

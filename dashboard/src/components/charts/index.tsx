/**
 * Charts.
 *
 * Form is chosen per the data's job, and colour comes last from the validated
 * tokens in theme.css — never picked here:
 *
 *   cost over time      → area, one series, change-over-time (no legend: the
 *                         title names it; a legend box for one series is noise)
 *   findings by severity → horizontal bars on the ordinal ramp, direct-labelled
 *   spend by dimension   → horizontal bars, categorical slots in fixed order
 *
 * There is deliberately no dual-axis chart anywhere: cost and run-count are
 * different scales, so they are two charts rather than two y-axes on one.
 */
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip as RTooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { DayPoint } from "../../lib/api";
import {
  SERIES_VARS,
  SEVERITY_ORDER,
  SEVERITY_VAR,
  fmtAxisUSD,
  fmtInt,
  fmtUSD,
} from "../../lib/utils";
import { EmptyState } from "../ui";

const AXIS = { fontSize: 11, fill: "var(--axis)" };

function TooltipBox({ rows, label }: { rows: { name: string; value: string; color?: string }[]; label?: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-2.5 py-2 text-[12px] shadow-lg dark:border-slate-700 dark:bg-[#151b28]">
      {label && <p className="mb-1 font-600 text-slate-900 dark:text-slate-100">{label}</p>}
      {rows.map((r) => (
        <p key={r.name} className="flex items-center gap-1.5 text-slate-600 dark:text-slate-300">
          {r.color && (
            <span aria-hidden className="h-2 w-2 rounded-full" style={{ background: r.color }} />
          )}
          <span>{r.name}</span>
          <span className="ml-auto pl-3 font-600 tabular-nums text-slate-900 dark:text-slate-100">
            {r.value}
          </span>
        </p>
      ))}
    </div>
  );
}

/** Spend per day. One series, so no legend — the card title says what it is. */
export function CostTrend({ data }: { data: DayPoint[] }) {
  if (!data.length) return <EmptyState title="No spend yet" hint="Reviews will show up here." />;

  return (
    <ResponsiveContainer width="100%" height={220}>
      <AreaChart data={data} margin={{ top: 8, right: 8, left: -12, bottom: 0 }}>
        <defs>
          <linearGradient id="costFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--series-1)" stopOpacity={0.28} />
            <stop offset="100%" stopColor="var(--series-1)" stopOpacity={0.02} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke="var(--grid)" vertical={false} />
        <XAxis
          dataKey="date"
          tick={AXIS}
          tickLine={false}
          axisLine={false}
          tickFormatter={(d: string) => d.slice(5)}
          minTickGap={24}
        />
        <YAxis tick={AXIS} tickLine={false} axisLine={false} width={52} tickFormatter={fmtAxisUSD} />
        <RTooltip
          cursor={{ stroke: "var(--axis)", strokeDasharray: "3 3" }}
          content={({ active, payload, label }) =>
            active && payload?.length ? (
              <TooltipBox
                label={String(label)}
                rows={[
                  { name: "Spend", value: fmtUSD(Number(payload[0].value)), color: "var(--series-1)" },
                  { name: "Reviews", value: fmtInt(payload[0].payload.runs) },
                  { name: "Comments", value: fmtInt(payload[0].payload.posted) },
                ]}
              />
            ) : null
          }
        />
        <Area
          type="monotone"
          dataKey="cost"
          stroke="var(--series-1)"
          strokeWidth={2}
          fill="url(#costFill)"
          activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--surface)" }}
          isAnimationActive={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

/** Reviews per day — its own chart, not a second axis on the cost chart. */
export function RunsTrend({ data }: { data: DayPoint[] }) {
  if (!data.length) return <EmptyState title="No reviews yet" />;

  return (
    <ResponsiveContainer width="100%" height={220}>
      <BarChart data={data} margin={{ top: 8, right: 8, left: -18, bottom: 0 }} barCategoryGap="28%">
        <CartesianGrid stroke="var(--grid)" vertical={false} />
        <XAxis
          dataKey="date"
          tick={AXIS}
          tickLine={false}
          axisLine={false}
          tickFormatter={(d: string) => d.slice(5)}
          minTickGap={24}
        />
        <YAxis tick={AXIS} tickLine={false} axisLine={false} width={40} allowDecimals={false} />
        <RTooltip
          cursor={{ fill: "var(--grid)", fillOpacity: 0.5 }}
          content={({ active, payload, label }) =>
            active && payload?.length ? (
              <TooltipBox
                label={String(label)}
                rows={[
                  { name: "Reviews", value: fmtInt(Number(payload[0].value)), color: "var(--series-1)" },
                  { name: "Comments", value: fmtInt(payload[0].payload.posted) },
                ]}
              />
            ) : null
          }
        />
        <Bar dataKey="runs" fill="var(--series-1)" radius={[4, 4, 0, 0]} isAnimationActive={false} />
      </BarChart>
    </ResponsiveContainer>
  );
}

/**
 * Findings by severity. Horizontal bars in fixed critical→low order on the
 * single-hue ordinal ramp, with the count direct-labelled — the ramp's lighter
 * steps sit below 3:1 on white, and the label is the required relief.
 */
export function SeverityBars({ counts }: { counts: Record<string, number> }) {
  const data = SEVERITY_ORDER.map((s) => ({ severity: s, count: counts[s] || 0 })).filter(
    (d) => d.count > 0,
  );
  if (!data.length) return <EmptyState title="No findings posted yet" />;

  const max = Math.max(...data.map((d) => d.count));

  return (
    <div className="space-y-2.5">
      {data.map((d) => (
        <div key={d.severity} className="flex items-center gap-3">
          <span className="w-16 shrink-0 text-[12px] capitalize text-slate-600 dark:text-slate-300">
            {d.severity}
          </span>
          <div className="h-5 flex-1 overflow-hidden rounded-r-[4px] bg-[var(--plane)]">
            <div
              className="h-full rounded-r-[4px]"
              style={{ width: `${Math.max(3, (d.count / max) * 100)}%`, background: SEVERITY_VAR[d.severity] }}
            />
          </div>
          <span className="w-10 shrink-0 text-right text-[12px] font-600 tabular-nums text-slate-900 dark:text-slate-100">
            {d.count}
          </span>
        </div>
      ))}
    </div>
  );
}

/**
 * Spend split by a dimension (model, source). Categorical slots in fixed
 * order, direct-labelled with the amount so the two low-contrast slots never
 * have to carry the value on colour alone.
 */
export function SpendBars({ data }: { data: Record<string, number> }) {
  const rows = Object.entries(data)
    .filter(([, v]) => v > 0)
    .sort((a, b) => b[1] - a[1]);
  if (!rows.length) return <EmptyState title="No spend recorded" />;

  // A 7th entry is folded into "Other" rather than getting an invented hue.
  const head = rows.slice(0, SERIES_VARS.length - 1);
  const tail = rows.slice(SERIES_VARS.length - 1);
  const shown = tail.length
    ? [...head, ["Other", tail.reduce((a, [, v]) => a + v, 0)] as [string, number]]
    : rows;

  const max = Math.max(...shown.map(([, v]) => v));

  return (
    <div className="space-y-2.5">
      {shown.map(([name, value], i) => (
        <div key={name} className="flex items-center gap-3">
          <span className="w-28 shrink-0 truncate text-[12px] text-slate-600 dark:text-slate-300" title={name}>
            {name}
          </span>
          <div className="h-5 flex-1 overflow-hidden rounded-r-[4px] bg-[var(--plane)]">
            <div
              className="h-full rounded-r-[4px]"
              style={{ width: `${Math.max(3, (value / max) * 100)}%`, background: SERIES_VARS[i] }}
            />
          </div>
          <span className="w-16 shrink-0 text-right text-[12px] font-600 tabular-nums text-slate-900 dark:text-slate-100">
            {fmtUSD(value)}
          </span>
        </div>
      ))}
    </div>
  );
}

/** A tiny inline bar used inside table cells. */
export function MiniBars({ data, color = "var(--series-1)" }: { data: number[]; color?: string }) {
  const max = Math.max(1, ...data);
  return (
    <span className="inline-flex h-5 items-end gap-[2px]" aria-hidden>
      {data.map((v, i) => (
        <span
          key={i}
          className="w-[3px] rounded-[1px]"
          style={{ height: `${Math.max(8, (v / max) * 100)}%`, background: color }}
        />
      ))}
    </span>
  );
}

export { Cell };

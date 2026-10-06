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
  cn,
  fmtAxisUSD,
  fmtInt,
  fmtUSD,
} from "../../lib/utils";
import { EmptyState } from "../ui";

const AXIS = { fontSize: 11, fill: "var(--axis)" };

function TooltipBox({ rows, label }: { rows: { name: string; value: string; color?: string }[]; label?: string }) {
  return (
    <div className="animate-pop-in rounded-xl border border-line bg-surface px-3 py-2 text-[12px] shadow-lg">
      {label && <p className="mb-1.5 font-semibold text-fg">{label}</p>}
      {rows.map((r) => (
        <p key={r.name} className="flex items-center gap-1.5 py-px text-fg-muted">
          {r.color && (
            <span aria-hidden className="h-2 w-2 rounded-full" style={{ background: r.color }} />
          )}
          <span>{r.name}</span>
          <span className="tabular ml-auto pl-4 font-semibold text-fg">
            {r.value}
          </span>
        </p>
      ))}
    </div>
  );
}

const SPEND_SERIES = [
  { key: "managed", name: "PullAgent credits", color: "var(--series-1)" },
  { key: "byok", name: "Your API keys", color: "var(--series-2)" },
] as const;

/**
 * Spend per day. One series until any review has run on a customer's own
 * keys; from then on two - CR credits and your API keys - in fixed hue order,
 * with a legend and the name at each line's end, never colour alone. Both are
 * dollars, so they share the one axis; they are not stacked, so each line
 * reads as its own spend.
 */
export function CostTrend({ data }: { data: DayPoint[] }) {
  if (!data.length) return <EmptyState title="No spend yet" hint="Reviews will show up here." />;

  const split = data.some((d) => (d.byok || 0) > 0);
  const series = split ? SPEND_SERIES : ([{ key: "cost", name: "Spend", color: "var(--series-1)" }] as const);
  const last = data.length - 1;

  return (
    <div>
      {split && (
        <ul className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-[12px] text-fg-muted" aria-label="Legend">
          {SPEND_SERIES.map((s) => (
            <li key={s.key} className="inline-flex items-center gap-1.5">
              <span aria-hidden className="h-0.5 w-3.5 rounded-full" style={{ background: s.color }} />
              {s.name}
            </li>
          ))}
        </ul>
      )}
      <ResponsiveContainer width="100%" height={220}>
        <AreaChart data={data} margin={{ top: 8, right: split ? 100 : 8, left: -12, bottom: 0 }}>
          <defs>
            {series.map((s) => (
              <linearGradient key={s.key} id={`fill-${s.key}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={s.color} stopOpacity={split ? 0.16 : 0.28} />
                <stop offset="100%" stopColor={s.color} stopOpacity={0.02} />
              </linearGradient>
            ))}
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
            content={({ active, payload, label }) => {
              if (!active || !payload?.length) return null;
              const p = payload[0].payload as DayPoint;
              return (
                <TooltipBox
                  label={String(label)}
                  rows={[
                    ...(split
                      ? [
                          { name: "PullAgent credits", value: fmtUSD(p.managed || 0), color: "var(--series-1)" },
                          { name: "Your API keys", value: fmtUSD(p.byok || 0), color: "var(--series-2)" },
                          { name: "Total", value: fmtUSD(p.cost) },
                        ]
                      : [{ name: "Spend", value: fmtUSD(p.cost), color: "var(--series-1)" }]),
                    { name: "Reviews", value: fmtInt(p.runs) },
                    { name: "Comments", value: fmtInt(p.posted) },
                  ]}
                />
              );
            }}
          />
          {series.map((s, i) => (
            <Area
              key={s.key}
              type="monotone"
              dataKey={s.key}
              name={s.name}
              stroke={s.color}
              strokeWidth={2}
              fill={`url(#fill-${s.key})`}
              activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--surface)" }}
              isAnimationActive={false}
              label={
                split
                  ? (props: { index?: number; x?: number | string; y?: number | string }) =>
                      props.index === last ? (
                        <text
                          key={`end-${s.key}`}
                          x={Number(props.x || 0) + 8}
                          y={Number(props.y || 0) + (i === 0 ? -6 : 12)}
                          fontSize={11}
                          fill="var(--fg-muted)"
                        >
                          {s.name}
                        </text>
                      ) : (
                        <g key={`none-${s.key}-${props.index}`} />
                      )
                  : undefined
              }
            />
          ))}
        </AreaChart>
      </ResponsiveContainer>
    </div>
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
          <span className="w-16 shrink-0 text-[12px] capitalize text-fg-muted">
            {d.severity}
          </span>
          <div className="h-5 flex-1 overflow-hidden rounded-r-md bg-surface-2">
            <div
              className="h-full rounded-r-md transition-[width] duration-500 ease-out"
              style={{ width: `${Math.max(3, (d.count / max) * 100)}%`, background: SEVERITY_VAR[d.severity] }}
            />
          </div>
          <span className="tabular w-10 shrink-0 text-right text-[12px] font-semibold text-fg">
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
export function SpendBars({
  data,
  color,
  labelWidth = "w-28",
}: {
  data: Record<string, number>;
  /** One hue for every bar: the bars are one measure, not separate identities. */
  color?: string;
  labelWidth?: string;
}) {
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
          <span className={cn(labelWidth, "shrink-0 truncate text-[12px] text-fg-muted")} title={name}>
            {name}
          </span>
          <div className="h-5 flex-1 overflow-hidden rounded-r-md bg-surface-2">
            <div
              className="h-full rounded-r-md transition-[width] duration-500 ease-out"
              style={{ width: `${Math.max(3, (value / max) * 100)}%`, background: color || SERIES_VARS[i] }}
            />
          </div>
          <span className="tabular w-16 shrink-0 text-right text-[12px] font-semibold text-fg">
            {fmtUSD(value)}
          </span>
        </div>
      ))}
    </div>
  );
}

/**
 * A tiny inline bar row for table cells and stat tiles. Decorative by
 * contract: no axis, no labels, aria-hidden - the number it sits beside is
 * the thing being reported, and this is only its recent shape.
 */
export function MiniBars({
  data,
  color = "var(--series-1)",
  className,
}: {
  data: number[];
  color?: string;
  className?: string;
}) {
  const max = Math.max(1, ...data);
  return (
    <span className={cn("inline-flex h-5 items-end gap-[2px]", className)} aria-hidden>
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

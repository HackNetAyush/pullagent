import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  CircleDollarSign,
  Database,
  ShieldX,
  Sparkles,
} from "lucide-react";
import * as React from "react";
import { Link } from "react-router-dom";

import { CostTrend, RunsTrend, SeverityBars, SpendBars } from "../components/charts";
import { PageHeader } from "../components/Layout";
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardDescription,
  CardHeader,
  CardTitle,
  Skeleton,
  StatusDot,
  Tooltip,
} from "../components/ui";
import { api, type Overview as OverviewData, type Run } from "../lib/api";
import { STATUS_VAR, cn, fmtInt, fmtPct, fmtUSD } from "../lib/utils";

const WINDOWS = [7, 30, 90];

/**
 * A stat tile, not a chart: a single number whose job is to be read, not
 * compared. The delta line is text, never a coloured bar.
 */
function StatTile({
  label,
  value,
  hint,
  icon: Icon,
  tone,
}: {
  label: string;
  value: string;
  hint?: React.ReactNode;
  icon: React.ComponentType<{ className?: string }>;
  tone?: "warning";
}) {
  return (
    <Card>
      <CardBody className="flex items-start gap-3">
        <div
          className={cn(
            "grid h-9 w-9 shrink-0 place-items-center rounded-lg",
            tone === "warning"
              ? "bg-[var(--warning)]/12 text-[var(--warning)]"
              : "bg-brand-50 text-brand-600 dark:bg-brand-500/15 dark:text-brand-200",
          )}
        >
          <Icon className="h-4 w-4" />
        </div>
        <div className="min-w-0">
          <p className="text-[12px] text-slate-500 dark:text-slate-400">{label}</p>
          <p className="font-display text-xl font-700 tabular-nums text-slate-900 dark:text-slate-50">
            {value}
          </p>
          {hint && <div className="mt-0.5 text-[12px] text-slate-500 dark:text-slate-400">{hint}</div>}
        </div>
      </CardBody>
    </Card>
  );
}

export function OverviewPage() {
  const [days, setDays] = React.useState(30);

  const { data, isLoading, error } = useQuery<OverviewData>({
    queryKey: ["overview", days],
    queryFn: () => api.overview(days),
    refetchInterval: 30_000,
  });

  // Active runs poll faster — this is the "is it working right now" signal.
  const { data: active } = useQuery<Run[]>({
    queryKey: ["active"],
    queryFn: api.activeRuns,
    refetchInterval: 5_000,
  });

  return (
    <>
      <PageHeader
        title="Overview"
        description={`Review activity and spend over the last ${days} days.`}
        actions={
          <div className="flex rounded-lg border border-slate-200 p-0.5 dark:border-slate-700">
            {WINDOWS.map((d) => (
              <button
                key={d}
                onClick={() => setDays(d)}
                className={cn(
                  "rounded-md px-2.5 py-1 text-[12px] font-medium transition-colors",
                  d === days
                    ? "bg-brand-600 text-white"
                    : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800",
                )}
              >
                {d}d
              </button>
            ))}
          </div>
        }
      />

      {error && (
        <Card className="mb-4 border-[var(--critical)]/40">
          <CardBody className="flex items-center gap-2 text-[13px] text-slate-700 dark:text-slate-200">
            <AlertTriangle className="h-4 w-4 text-[var(--critical)]" />
            {error instanceof Error ? error.message : "Could not load the overview."}
          </CardBody>
        </Card>
      )}

      {active && active.length > 0 && (
        <Card className="mb-4 border-brand-200 bg-brand-50/50 dark:border-brand-500/30 dark:bg-brand-500/5">
          <CardBody className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <StatusDot color={STATUS_VAR.running} label={`${active.length} review(s) in flight`} />
            {active.slice(0, 3).map((r) => (
              <Link
                key={r.id}
                to={`/runs/${r.id}`}
                className="text-[12px] text-slate-600 hover:underline dark:text-slate-300"
              >
                {r.repo}
                {r.pr ? `#${r.pr}` : ""} · <span className="tabular-nums">{r.stage}</span>
              </Link>
            ))}
          </CardBody>
        </Card>
      )}

      <div className="mb-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {isLoading || !data ? (
          Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-[86px]" />)
        ) : (
          <>
            <StatTile
              label="Spend"
              value={fmtUSD(data.cost)}
              hint={`${fmtUSD(data.cost_per_review)} per review`}
              icon={CircleDollarSign}
            />
            <StatTile
              label="Reviews"
              value={fmtInt(data.runs)}
              hint={`${fmtInt(data.posted)} comments posted`}
              icon={Sparkles}
            />
            <StatTile
              label="Verifier kill rate"
              value={data.kill_rate_reliable ? fmtPct(data.kill_rate) : "—"}
              tone={data.kill_rate_reliable ? undefined : "warning"}
              hint={
                data.kill_rate_reliable ? (
                  `${fmtInt(data.killed)} of ${fmtInt(data.kill_rate_sample)} judged`
                ) : (
                  <Tooltip label="A kill rate over a handful of findings is noise. This stays blank until at least 20 findings have been judged.">
                    <span className="underline decoration-dotted underline-offset-2">
                      {fmtInt(data.kill_rate_sample)} judged — too few to report
                    </span>
                  </Tooltip>
                )
              }
              icon={ShieldX}
            />
            <StatTile
              label="Prompt cache hit"
              value={fmtPct(data.cache_hit)}
              hint={`${fmtInt(data.suppression_hits)} suppressions fired`}
              icon={Database}
            />
          </>
        )}
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <div>
              <CardTitle>Spend per day</CardTitle>
              <CardDescription>Model cost, excluding cached replays.</CardDescription>
            </div>
          </CardHeader>
          <CardBody className="pt-1">
            {isLoading || !data ? <Skeleton className="h-[220px]" /> : <CostTrend data={data.series} />}
          </CardBody>
        </Card>

        <Card>
          <CardHeader>
            <div>
              <CardTitle>Reviews per day</CardTitle>
              <CardDescription>
                A separate chart from spend on purpose — different scales never share an axis.
              </CardDescription>
            </div>
          </CardHeader>
          <CardBody className="pt-1">
            {isLoading || !data ? <Skeleton className="h-[220px]" /> : <RunsTrend data={data.series} />}
          </CardBody>
        </Card>

        <Card>
          <CardHeader>
            <div>
              <CardTitle>Posted findings by severity</CardTitle>
              <CardDescription>One hue, darker is worse.</CardDescription>
            </div>
            <Link to="/findings">
              <Button size="sm" variant="ghost">
                View all
              </Button>
            </Link>
          </CardHeader>
          <CardBody>
            {isLoading || !data ? <Skeleton className="h-[120px]" /> : <SeverityBars counts={data.severity} />}
          </CardBody>
        </Card>

        <Card>
          <CardHeader>
            <div>
              <CardTitle>Where the money goes</CardTitle>
              <CardDescription>By model, then by what triggered the review.</CardDescription>
            </div>
          </CardHeader>
          <CardBody className="space-y-4">
            {isLoading || !data ? (
              <Skeleton className="h-[120px]" />
            ) : (
              <>
                <SpendBars data={data.cost_by_model} />
                <div className="border-t border-slate-100 pt-3 dark:border-slate-800">
                  <p className="mb-2 text-[11px] font-600 uppercase tracking-wide text-slate-500 dark:text-slate-400">
                    By source
                  </p>
                  <SpendBars data={data.cost_by_source || {}} />
                </div>
              </>
            )}
          </CardBody>
        </Card>
      </div>

      {data && !data.kill_rate_reliable && data.kill_rate_sample > 0 && (
        <p className="mt-4 flex items-start gap-1.5 text-[12px] text-slate-500 dark:text-slate-400">
          <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0 text-[var(--warning)]" />
          <span>
            Kill rate is hidden below 20 judged findings. A verifier that never refutes is not
            verifying — check this number once you have volume.
          </span>
        </p>
      )}

      {data && (
        <p className="mt-2 text-[12px] text-slate-400 dark:text-slate-500">
          Window: {data.window_days} days · {fmtInt(data.suppressions)} suppressions stored ·
          {" "}
          {fmtInt(data.active)} running now
        </p>
      )}
    </>
  );
}

export { Badge };

import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowRight,
  CircleDollarSign,
  Database,
  ShieldX,
  Sparkles,
} from "lucide-react";
import * as React from "react";
import { Link } from "react-router-dom";

import { CostTrend, MiniBars, RunsTrend, SeverityBars, SpendBars } from "../components/charts";
import { PageHeader } from "../components/Layout";
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardDescription,
  CardHeader,
  CardTitle,
  Segmented,
  Skeleton,
  Tooltip,
} from "../components/ui";
import { api, type Overview as OverviewData, type Run } from "../lib/api";
import { cn, fmtInt, fmtPct, fmtUSD } from "../lib/utils";
import { useWorkspace } from "../workspace";

const WINDOWS = [
  { value: 7, label: "7d" },
  { value: 30, label: "30d" },
  { value: 90, label: "90d" },
];

/**
 * A stat tile, not a chart: a single number whose job is to be read, not
 * compared. The sparkline behind it is context at a glance and carries no
 * axis - anyone who needs the values reads the chart below.
 */
function StatTile({
  label,
  value,
  hint,
  icon: Icon,
  tone,
  spark,
}: {
  label: string;
  value: string;
  hint?: React.ReactNode;
  icon: React.ComponentType<{ className?: string }>;
  tone?: "warning";
  spark?: number[];
}) {
  return (
    <Card className="animate-fade-up group overflow-hidden">
      <CardBody className="p-5">
        <div className="flex items-start justify-between gap-3">
          <p className="text-[12px] font-medium tracking-wide text-fg-muted">{label}</p>
          <div
            className={cn(
              "grid h-8 w-8 shrink-0 place-items-center rounded-lg ring-1 ring-inset transition-colors",
              tone === "warning"
                ? "bg-warning/12 text-warning ring-warning/20"
                : "bg-brand-500/10 text-brand-600 ring-brand-500/15 dark:text-brand-300",
            )}
          >
            <Icon className="h-4 w-4" />
          </div>
        </div>

        <p className="font-display mt-2 text-[28px] leading-none font-bold tracking-[-0.02em] tabular text-fg">
          {value}
        </p>

        <div className="mt-2.5 flex items-end justify-between gap-3">
          {hint && <div className="text-[12px] leading-snug text-fg-muted">{hint}</div>}
          {spark && spark.some((v) => v > 0) && (
            <MiniBars
              data={spark}
              className="h-6 shrink-0 opacity-45 transition-opacity group-hover:opacity-80"
            />
          )}
        </div>
      </CardBody>
    </Card>
  );
}

export function OverviewPage() {
  const [days, setDays] = React.useState(30);

  const { account } = useWorkspace();
  const { data, isLoading, error } = useQuery<OverviewData>({
    queryKey: ["overview", account, days],
    queryFn: () => api.overview(days, account),
    enabled: Boolean(account),
    refetchInterval: 30_000,
  });

  // Active runs poll faster - this is the "is it working right now" signal.
  const { data: active } = useQuery<Run[]>({
    queryKey: ["active", account],
    queryFn: () => api.activeRuns(account),
    enabled: Boolean(account),
    refetchInterval: 5_000,
  });

  // The last fourteen points, so a tile's sparkline reads as "recently"
  // regardless of which window the page is showing.
  const spark = (key: "cost" | "runs" | "posted") =>
    (data?.series || []).slice(-14).map((p) => p[key]);

  return (
    <>
      <PageHeader
        title="Overview"
        description={`Review activity and spend across the last ${days} days.`}
        actions={
          <Segmented
            value={days}
            onChange={setDays}
            options={WINDOWS}
            ariaLabel="Reporting window"
          />
        }
      />

      {error && (
        <Card className="mb-4 border-critical/35 bg-critical/4">
          <CardBody className="flex items-center gap-2.5 py-3 text-[13px] text-fg">
            <AlertTriangle className="h-4 w-4 shrink-0 text-critical" />
            {error instanceof Error ? error.message : "Could not load the overview."}
          </CardBody>
        </Card>
      )}

      {active && active.length > 0 && (
        <Card className="animate-fade-up mb-4 border-brand-500/25 bg-brand-500/6">
          <CardBody className="flex flex-wrap items-center gap-x-4 gap-y-2 py-3">
            <span className="inline-flex items-center gap-2 text-[13px] font-medium text-fg">
              <span
                aria-hidden
                className="animate-pulse-ring h-2 w-2 rounded-full"
                style={{ background: "var(--series-1)" }}
              />
              {active.length} review{active.length === 1 ? "" : "s"} in flight
            </span>
            <div className="flex flex-wrap items-center gap-2">
              {active.slice(0, 3).map((r) => (
                <Link
                  key={r.id}
                  to={`/runs/${r.id}`}
                  className="inline-flex items-center gap-1.5 rounded-md border border-line bg-surface px-2 py-1 text-[12px] text-fg-muted transition-colors hover:border-brand-500/40 hover:text-fg"
                >
                  <span className="font-medium text-fg">
                    {r.repo}
                    {r.pr ? `#${r.pr}` : ""}
                  </span>
                  <span className="tabular text-fg-faint">{r.stage}</span>
                </Link>
              ))}
              {active.length > 3 && (
                <Link to="/runs?status=running" className="text-[12px] text-fg-muted hover:text-fg">
                  +{active.length - 3} more
                </Link>
              )}
            </div>
          </CardBody>
        </Card>
      )}

      <div className="stagger mb-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {isLoading || !data ? (
          Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-[136px] rounded-2xl" />)
        ) : (
          <>
            <StatTile
              label="Spend"
              value={fmtUSD(data.cost)}
              hint={
                data.byok?.cost
                  ? `${fmtUSD(data.cost_per_review)} per review · ${fmtUSD(data.byok.cost)} on your API keys`
                  : `${fmtUSD(data.cost_per_review)} per review`
              }
              icon={CircleDollarSign}
              spark={spark("cost")}
            />
            <StatTile
              label="Reviews"
              value={fmtInt(data.runs)}
              hint={`${fmtInt(data.posted)} comments posted`}
              icon={Sparkles}
              spark={spark("runs")}
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

      <div className="stagger grid gap-3 lg:grid-cols-2">
        <Card className="animate-fade-up">
          <CardHeader>
            <div>
              <CardTitle>Spend per day</CardTitle>
              <CardDescription>
                {data?.byok?.cost
                  ? "PullAgent credits and your own API keys, excluding cached replays."
                  : "Model cost, excluding cached replays."}
              </CardDescription>
            </div>
            {data && (
              <Badge tone="brand" className="tabular">
                {fmtUSD(data.cost)} total
              </Badge>
            )}
          </CardHeader>
          <CardBody className="pt-2">
            {isLoading || !data ? (
              <Skeleton className="h-[220px]" />
            ) : (
              <CostTrend data={data.series} />
            )}
          </CardBody>
        </Card>

        <Card className="animate-fade-up">
          <CardHeader>
            <div>
              <CardTitle>Reviews per day</CardTitle>
              <CardDescription>
                A separate chart from spend on purpose — different scales never share an axis.
              </CardDescription>
            </div>
          </CardHeader>
          <CardBody className="pt-2">
            {isLoading || !data ? (
              <Skeleton className="h-[220px]" />
            ) : (
              <RunsTrend data={data.series} />
            )}
          </CardBody>
        </Card>

        <Card className="animate-fade-up">
          <CardHeader>
            <div>
              <CardTitle>Posted findings by severity</CardTitle>
              <CardDescription>One hue, darker is worse.</CardDescription>
            </div>
            <Link to="/findings">
              <Button size="xs" variant="ghost" className="text-fg-muted">
                View all
                <ArrowRight className="h-3 w-3" />
              </Button>
            </Link>
          </CardHeader>
          <CardBody className="pt-3">
            {isLoading || !data ? (
              <Skeleton className="h-[120px]" />
            ) : (
              <SeverityBars counts={data.severity} />
            )}
          </CardBody>
        </Card>

        <Card className="animate-fade-up">
          <CardHeader>
            <div>
              <CardTitle>Where the money goes</CardTitle>
              <CardDescription>By model, then by what triggered the review.</CardDescription>
            </div>
          </CardHeader>
          <CardBody className="space-y-4 pt-3">
            {isLoading || !data ? (
              <Skeleton className="h-[120px]" />
            ) : (
              <>
                <SpendBars data={data.cost_by_model} />
                <div className="border-t border-line pt-3.5">
                  <p className="mb-2.5 text-[10.5px] font-semibold tracking-[0.08em] text-fg-faint uppercase">
                    By source
                  </p>
                  <SpendBars data={data.cost_by_source || {}} />
                </div>
              </>
            )}
          </CardBody>
        </Card>
      </div>

      {data && data.byok?.runs > 0 && (
        <Card className="animate-fade-up mt-3">
          <CardHeader>
            <div>
              <CardTitle>Spend on your API keys</CardTitle>
              <CardDescription>
                {fmtInt(data.byok.runs)} review{data.byok.runs === 1 ? "" : "s"} ran on your own
                connections, billed by your providers. Prices come from what you set on each model.
              </CardDescription>
            </div>
            <Badge tone="brand" className="tabular">
              {fmtUSD(data.byok.cost)} total
            </Badge>
          </CardHeader>
          <CardBody className="space-y-3 pt-3">
            <SpendBars
              data={Object.fromEntries(data.byok.by_model.map((m) => [m.label, m.cost]))}
              color="var(--series-2)"
              labelWidth="w-60"
            />
            {data.byok.by_model.some((m) => !m.priced) && (
              <p className="flex items-start gap-1.5 text-[12px] text-fg-muted">
                <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0 text-warning" />
                <span>
                  Some models have no price, so their spend shows as less than it is:{" "}
                  {data.byok.by_model
                    .filter((m) => !m.priced)
                    .map((m) => `${m.label} (${fmtInt(m.input_tokens + m.output_tokens)} tokens)`)
                    .join(", ")}
                  . Set prices under{" "}
                  <Link to="/models" className="font-medium text-brand-600 hover:underline dark:text-brand-300">
                    Models
                  </Link>
                  .
                </span>
              </p>
            )}
          </CardBody>
        </Card>
      )}

      {data && !data.kill_rate_reliable && data.kill_rate_sample > 0 && (
        <p className="mt-4 flex items-start gap-1.5 text-[12px] text-fg-muted">
          <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0 text-warning" />
          <span>
            Kill rate is hidden below 20 judged findings. A verifier that never refutes is not
            verifying — check this number once you have volume.
          </span>
        </p>
      )}

      {data && (
        <p className="mt-3 text-[12px] text-fg-faint">
          Window: {data.window_days} days · {fmtInt(data.suppressions)} suppressions stored ·{" "}
          {fmtInt(data.active)} running now
        </p>
      )}
    </>
  );
}

export { Badge };

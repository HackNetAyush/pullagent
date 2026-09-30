import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ArrowLeft, ExternalLink } from "lucide-react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { SeverityBars } from "../components/charts";
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
} from "../components/ui";
import { api, type Finding, type RunDetail as RunDetailData } from "../lib/api";
import {
  SEVERITY_ORDER,
  SEVERITY_VAR,
  STATUS_VAR,
  cn,
  fmtInt,
  fmtPct,
  fmtSecs,
  fmtUSD,
  relTime,
} from "../lib/utils";

/** The nine pipeline stages, shown as a rail so a live review reads at a glance. */
function StageRail({ run }: { run: RunDetailData }) {
  return (
    <ol className="flex flex-wrap gap-1.5">
      {run.stages.map((stage, i) => {
        const done = i < run.stage_index || run.status === "done";
        const current = i === run.stage_index && run.status === "running";
        return (
          <li
            key={stage}
            className={cn(
              "rounded-md px-2 py-1 text-[11px] font-medium",
              current && "bg-[var(--series-1)] text-white",
              done && !current && "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300",
              !done && !current && "bg-slate-50 text-slate-400 dark:bg-slate-800/50 dark:text-slate-600",
            )}
          >
            {stage}
          </li>
        );
      })}
    </ol>
  );
}

function FindingCard({ f }: { f: Finding }) {
  return (
    <Card className={cn(!f.posted && "opacity-70")}>
      <CardBody className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="inline-flex items-center gap-1.5">
            <span
              aria-hidden
              className="h-2.5 w-2.5 rounded-[3px]"
              style={{ background: SEVERITY_VAR[f.severity] || "var(--axis)" }}
            />
            <span className="text-[12px] font-600 capitalize text-slate-700 dark:text-slate-200">
              {f.severity}
            </span>
          </span>
          <Badge>{f.category?.replace(/_/g, " ")}</Badge>
          <span className="text-[12px] text-slate-500 dark:text-slate-400">
            {fmtPct(f.confidence)} confidence · {f.found_by || "—"}
          </span>
          <span className="ml-auto text-[12px]">
            {f.posted ? (
              <span className="text-[var(--good)]">posted</span>
            ) : (
              <span className="text-slate-400">held back</span>
            )}
          </span>
        </div>

        <p className="font-medium text-slate-900 dark:text-slate-100">{f.claim}</p>
        <p className="text-[13px] leading-relaxed text-slate-600 dark:text-slate-300">
          {f.failure_scenario}
        </p>
        <p className="font-mono text-[11px] text-slate-400">
          {f.file}:{f.line} · {f.fingerprint}
        </p>
      </CardBody>
    </Card>
  );
}

export function RunDetailPage() {
  const { id } = useParams();
  const navigate = useNavigate();

  const { data, isLoading, error } = useQuery<RunDetailData>({
    queryKey: ["run", id],
    queryFn: () => api.run(Number(id)),
    // Follow a live review; stop polling once it settles.
    refetchInterval: (q) => (q.state.data?.status === "running" ? 4000 : false),
  });

  if (isLoading) return <Skeleton className="h-64" />;
  if (error || !data) {
    return (
      <Card>
        <CardBody className="flex items-center gap-2 text-[13px]">
          <AlertTriangle className="h-4 w-4 text-[var(--critical)]" />
          {error instanceof Error ? error.message : "Review not found."}
        </CardBody>
      </Card>
    );
  }

  const posted = data.findings.filter((f) => f.posted);
  const held = data.findings.filter((f) => !f.posted);

  const severity: Record<string, number> = {};
  for (const f of posted) severity[f.severity] = (severity[f.severity] || 0) + 1;

  return (
    <>
      <Button variant="ghost" size="sm" className="mb-2 -ml-1" onClick={() => navigate(-1)}>
        <ArrowLeft className="h-3.5 w-3.5" />
        Back
      </Button>

      <PageHeader
        title={`${data.repo}${data.pr ? ` #${data.pr}` : ""}`}
        description={`${data.tier} · ${data.model} · started ${relTime(data.started_at)}`}
        actions={
          data.pr ? (
            <a
              href={`https://github.com/${data.repo}/pull/${data.pr}`}
              target="_blank"
              rel="noreferrer"
            >
              <Button size="sm">
                <ExternalLink className="h-3.5 w-3.5" />
                Open on GitHub
              </Button>
            </a>
          ) : undefined
        }
      />

      {data.error && (
        <Card className="mb-3 border-[var(--critical)]/40">
          <CardBody>
            <p className="mb-1 flex items-center gap-1.5 text-[13px] font-600 text-slate-900 dark:text-slate-100">
              <AlertTriangle className="h-4 w-4 text-[var(--critical)]" />
              Review incomplete — nothing was posted
            </p>
            <p className="font-mono text-[12px] leading-relaxed text-slate-600 dark:text-slate-300">
              {data.error}
            </p>
          </CardBody>
        </Card>
      )}

      <div className="mb-3 grid gap-3 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader>
            <div>
              <CardTitle>Pipeline</CardTitle>
              <CardDescription>
                <StatusDot color={STATUS_VAR[data.status] || "var(--axis)"} label={data.status} />
              </CardDescription>
            </div>
          </CardHeader>
          <CardBody className="pt-1">
            <StageRail run={data} />
            <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-2 text-[13px] sm:grid-cols-4">
              {[
                ["Cost", data.cache_hit ? "$0 (cached)" : fmtUSD(data.cost)],
                ["Elapsed", fmtSecs(data.elapsed)],
                ["Tokens in", fmtInt(data.input_tokens)],
                ["Tokens out", fmtInt(data.output_tokens)],
                ["Cache read", fmtInt(data.cache_read)],
                ["Cache write", fmtInt(data.cache_write)],
                ["Comments", fmtInt(data.posted)],
                ["Killed by verifier", fmtInt(data.killed)],
              ].map(([k, v]) => (
                <div key={k}>
                  <dt className="text-[12px] text-slate-500 dark:text-slate-400">{k}</dt>
                  <dd className="font-600 tabular-nums text-slate-900 dark:text-slate-100">{v}</dd>
                </div>
              ))}
            </dl>
          </CardBody>
        </Card>

        <Card>
          <CardHeader>
            <div>
              <CardTitle>Posted by severity</CardTitle>
              <CardDescription>{posted.length} comment(s) on the pull request.</CardDescription>
            </div>
          </CardHeader>
          <CardBody>
            <SeverityBars counts={severity} />
          </CardBody>
        </Card>
      </div>

      <h2 className="mb-2 mt-5 font-display text-base font-700 text-slate-900 dark:text-slate-50">
        Posted findings ({posted.length})
      </h2>
      <div className="grid gap-2">
        {posted.length === 0 ? (
          <Card>
            <CardBody className="text-[13px] text-slate-500 dark:text-slate-400">
              Nothing survived verification — a clean review.
            </CardBody>
          </Card>
        ) : (
          posted
            .slice()
            .sort(
              (a, b) =>
                SEVERITY_ORDER.indexOf(a.severity as never) -
                SEVERITY_ORDER.indexOf(b.severity as never),
            )
            .map((f) => <FindingCard key={f.id} f={f} />)
        )}
      </div>

      {held.length > 0 && (
        <>
          <h2 className="mb-2 mt-6 font-display text-base font-700 text-slate-900 dark:text-slate-50">
            Held back ({held.length})
          </h2>
          <p className="mb-2 text-[13px] text-slate-500 dark:text-slate-400">
            Refuted by verification, suppressed by earlier feedback, or trimmed by the comment
            budget. Shown so the filtering is auditable rather than invisible.
          </p>
          <div className="grid gap-2">
            {held.map((f) => (
              <FindingCard key={f.id} f={f} />
            ))}
          </div>
        </>
      )}

      <p className="mt-6 text-[12px] text-slate-400">
        <Link to="/runs" className="hover:underline">
          ← All reviews
        </Link>
      </p>
    </>
  );
}

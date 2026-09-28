import { useCallback, useEffect, useState, type ReactNode } from "react";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Brain,
  Check,
  ChevronRight,
  FileCode2,
  Filter,
  GitPullRequest,
  Loader2,
  MessageSquare,
  Monitor,
  Moon,
  Receipt,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  Sun,
  Wallet,
  WifiOff,
  X,
  Zap,
  type LucideIcon,
} from "lucide-react";
import {
  api,
  fmtInt,
  fmtPct,
  fmtUSD,
  relTime,
  SEVERITY_ORDER,
  type Overview,
  type Run,
  type RunDetail,
  type Suppression,
} from "./api";
import { CostChart } from "./CostChart";

const WINDOWS = [7, 30, 90];
const POLL_MS = 4000;
const CARD = "min-w-0 overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-[0_1px_2px_rgba(15,23,42,.02),0_8px_28px_rgba(15,23,42,.04)] dark:border-slate-800 dark:bg-slate-900";
const MUTED = "text-slate-500 dark:text-slate-400";
type Theme = "light" | "dark" | "system";

const statusClass: Record<Run["status"], string> = {
  done: "bg-emerald-50 text-emerald-700 dark:bg-emerald-400/10 dark:text-emerald-300",
  running: "bg-brand-50 text-brand-700 dark:bg-brand-500/15 dark:text-brand-100",
  failed: "bg-rose-50 text-rose-700 dark:bg-rose-400/10 dark:text-rose-300",
  stalled: "bg-amber-50 text-amber-700 dark:bg-amber-400/10 dark:text-amber-300",
};

const severityColor: Record<string, string> = {
  critical: "#d5485a",
  high: "#ee7b3c",
  medium: "#dda32f",
  low: "#7695ba",
};

export default function App() {
  const [theme, setTheme] = useState<Theme>(() => {
    const saved = localStorage.getItem("cr-theme");
    return saved === "light" || saved === "dark" ? saved : "system";
  });
  const [days, setDays] = useState(30);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [active, setActive] = useState<Run[]>([]);
  const [suppressions, setSuppressions] = useState<Suppression[]>([]);
  const [selected, setSelected] = useState<RunDetail | null>(null);
  const [offline, setOffline] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      const resolved = theme === "system" ? (media.matches ? "dark" : "light") : theme;
      document.documentElement.dataset.theme = resolved;
      localStorage.setItem("cr-theme", theme);
    };
    apply();
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [theme]);

  const load = useCallback(async (opts: { silent?: boolean } = {}) => {
    if (!opts.silent) setRefreshing(true);
    try {
      const [nextOverview, nextRuns, nextActive, nextSuppressions] = await Promise.all([
        api.overview(days),
        api.runs(50),
        api.active(),
        api.suppressions(),
      ]);
      setOverview(nextOverview);
      setRuns(nextRuns);
      setActive(nextActive);
      setSuppressions(nextSuppressions);
      setOffline(null);
      setLastUpdated(new Date());
    } catch (error) {
      setOffline(error instanceof Error ? error.message : "The service did not respond");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [days]);

  // `loading` only ever gates the very first paint — it starts true and `load`
  // clears it in `finally`, then nothing sets it again, so switching the date
  // range or polling in the background never unmounts real content back to
  // skeletons. Those cases show the `refreshing` affordance instead, which
  // fades the existing data rather than replacing it.
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load({ silent: true }), POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const openRun = async (id: number) => {
    try {
      setSelected(await api.run(id));
    } catch {
      setOffline("Could not load that review. Please try again.");
    }
  };

  return (
    <div className="min-h-screen bg-[radial-gradient(circle_at_4%_-12%,rgba(100,100,220,.09),transparent_28rem)]">
      <header className="sticky top-0 z-30 h-[68px] border-b border-slate-200/80 bg-white/85 backdrop-blur-xl dark:border-slate-800 dark:bg-slate-950/85">
        <div className="mx-auto flex h-full max-w-[1440px] items-center justify-between px-4 sm:px-6">
          <a className="flex items-center gap-3 text-slate-900 no-underline dark:text-white" href="#top">
            <span className="grid size-9 place-items-center rounded-xl bg-gradient-to-br from-[#7777eb] to-[#4949bd] text-white shadow-[0_7px_16px_rgba(85,85,200,.24)]">
              <Sparkles size={19} strokeWidth={1.8} />
            </span>
            <span className="grid">
              <strong className="font-display text-base leading-none tracking-tight">CR</strong>
              <small className="mt-1 text-[11px] text-slate-400">Review intelligence</small>
            </span>
          </a>

          <div className="flex items-center gap-4">
            <span className={`hidden items-center gap-2 text-xs sm:flex ${offline ? "text-rose-600 dark:text-rose-300" : MUTED}`}>
              <span className={`size-2 rounded-full ring-4 ${offline ? "bg-rose-500 ring-rose-500/10" : "bg-emerald-500 ring-emerald-500/10"}`} />
              {offline ? "Disconnected" : active.length ? `${active.length} in progress` : "All systems ready"}
            </span>
            <div className="flex rounded-xl border border-slate-200 bg-slate-50 p-1 dark:border-slate-800 dark:bg-slate-900" role="group" aria-label="Color theme">
              {([
                ["light", Sun],
                ["system", Monitor],
                ["dark", Moon],
              ] as const).map(([item, ThemeIcon]) => (
                <button
                  key={item}
                  className={`grid size-7 place-items-center rounded-lg transition ${theme === item ? "bg-white text-slate-900 shadow-sm dark:bg-slate-800 dark:text-white" : "text-slate-400 hover:text-slate-700 dark:hover:text-slate-200"}`}
                  aria-label={`Use ${item} theme`}
                  aria-pressed={theme === item}
                  onClick={() => setTheme(item)}
                  title={`${capitalize(item)} theme`}
                >
                  <ThemeIcon size={14} />
                </button>
              ))}
            </div>
          </div>
        </div>
        {refreshing && (
          <div className="pointer-events-none absolute inset-x-0 bottom-0 h-[2px] overflow-hidden">
            <div className="h-full w-1/3 bg-gradient-to-r from-transparent via-brand-500 to-transparent [animation:loading-bar_1.1s_ease-in-out_infinite]" />
          </div>
        )}
      </header>

      <main id="top" className="mx-auto max-w-[1440px] px-3 py-8 sm:px-6 sm:py-10">
        <section className="mb-7 flex flex-col items-start justify-between gap-6 lg:flex-row lg:items-end">
          <div>
            <p className="mb-2 text-[11px] font-bold uppercase tracking-[.13em] text-brand-600 dark:text-brand-500">Review operations</p>
            <h1 className="font-display text-3xl font-bold tracking-[-.045em] text-slate-900 sm:text-[38px] dark:text-white">
              Your code reviews, at a glance.
            </h1>
            <p className="mt-2 text-sm text-slate-500 sm:text-[15px] dark:text-slate-400">
              Track review quality, spend, and verifier performance without digging through logs.
            </p>
          </div>
          <div className="flex w-full items-center gap-2 lg:w-auto">
            <div className="flex flex-1 rounded-xl border border-slate-200 bg-slate-100/70 p-1 dark:border-slate-800 dark:bg-slate-900" role="group" aria-label="Reporting window">
              {WINDOWS.map((windowDays) => (
                <button
                  key={windowDays}
                  className={`flex-1 rounded-lg px-3 py-2 text-xs font-semibold whitespace-nowrap transition lg:flex-none ${days === windowDays ? "bg-white text-slate-900 shadow-sm dark:bg-slate-800 dark:text-white" : "text-slate-400 hover:text-slate-700 dark:hover:text-slate-200"}`}
                  aria-pressed={days === windowDays}
                  onClick={() => setDays(windowDays)}
                >
                  {windowDays} days
                </button>
              ))}
            </div>
            <button
              className="flex size-[38px] items-center justify-center gap-2 rounded-xl border border-slate-200 bg-white text-xs font-semibold text-slate-500 shadow-sm transition hover:border-brand-500/40 hover:text-slate-800 disabled:opacity-60 sm:w-auto sm:px-3 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-300 dark:hover:text-white"
              onClick={() => void load()}
              disabled={refreshing}
              title={lastUpdated ? `Updated ${lastUpdated.toLocaleTimeString()}` : "Refresh data"}
            >
              <RefreshCw size={15} className={refreshing ? "animate-spin" : ""} />
              <span className="hidden sm:inline">{refreshing ? "Refreshing" : "Refresh"}</span>
            </button>
          </div>
        </section>

        {offline && (
          <div className="mb-5 flex items-center gap-3 rounded-xl border border-rose-200 bg-rose-50 p-3.5 text-rose-900 dark:border-rose-400/20 dark:bg-rose-400/10 dark:text-rose-100" role="alert">
            <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-rose-500/10 text-rose-600 dark:text-rose-300"><WifiOff size={18} /></span>
            <div>
              <strong className="block text-xs">We can’t reach the review service</strong>
              <p className="mt-0.5 text-[11px] text-rose-700/80 dark:text-rose-200/70">{offline}. Start it with <code className="rounded bg-white/60 px-1.5 py-0.5 dark:bg-black/20">uv run cr serve</code>.</p>
            </div>
            <button className="ml-auto shrink-0 text-xs font-bold text-rose-700 dark:text-rose-200" onClick={() => void load()}>Try again</button>
          </div>
        )}

        <div className={`transition-opacity duration-300 ease-out ${refreshing ? "opacity-70" : "opacity-100"}`}>
          <Kpis overview={overview} loading={loading} days={days} />

          <div className="mb-4 grid gap-4 lg:grid-cols-[minmax(0,1.8fr)_minmax(320px,.8fr)]">
            <section className={CARD}>
              <CardHeader icon={BarChart3} title="Daily spend" description={`Usage cost over the last ${days} days`} action={overview ? `${fmtUSD(overview.cost)} total` : undefined} />
              <div className="px-1 pb-2 pt-3 sm:px-3">
                {loading ? <Skeleton className="h-[236px]" /> : <CostChart data={overview?.series ?? []} height={236} />}
              </div>
            </section>

            <section className={CARD}>
              <CardHeader
                icon={Activity}
                title="In flight"
                description="Live review pipeline"
                node={active.length ? <span className="flex items-center gap-2 rounded-full bg-emerald-50 px-2.5 py-1 text-[10px] font-bold text-emerald-700 dark:bg-emerald-400/10 dark:text-emerald-300"><span className="size-1.5 animate-pulse rounded-full bg-emerald-500" />Live</span> : undefined}
              />
              <div className="p-4 sm:p-[18px]">{loading ? <SkeletonLines count={4} /> : <LivePanel runs={active} />}</div>
            </section>
          </div>

          <section className={CARD}>
            <CardHeader icon={GitPullRequest} title="Recent reviews" description="Select a review to inspect its findings" action={runs.length ? `${runs.length} recent` : undefined} />
            <div className="overflow-x-auto">
              {loading ? <div className="p-5"><SkeletonLines count={6} /></div> : <RunsTable runs={runs} onOpen={openRun} />}
            </div>
          </section>

          <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(0,.9fr)_minmax(0,1.5fr)]">
            <section className={CARD}>
              <CardHeader icon={ShieldCheck} title="Finding quality" description="Posted comments by severity" action={overview?.posted ? `${fmtInt(overview.posted)} posted` : undefined} />
              <div className="p-4 sm:p-[18px]">{loading ? <SkeletonLines count={4} /> : <SeverityBars counts={overview?.severity ?? {}} />}</div>
            </section>

            <section className={CARD}>
              <CardHeader icon={Brain} title="Suppression memory" description="Patterns CR learned to skip" action={`${suppressions.length} stored`} />
              <div className="px-4 py-2 sm:px-[18px]">{loading ? <div className="py-3"><SkeletonLines count={4} /></div> : <SuppressionList items={suppressions} />}</div>
            </section>
          </div>
        </div>

        <footer className="flex flex-col gap-2 px-1 pt-5 text-[10px] text-slate-400 sm:flex-row sm:justify-between dark:text-slate-500">
          <span className="flex items-center gap-2"><span className="size-1.5 rounded-full bg-emerald-500" />Auto-refreshes every 4 seconds</span>
          {lastUpdated && <span>Last updated {lastUpdated.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>}
        </footer>
      </main>

      {selected && <RunDrawer run={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}

function CardHeader({ icon: Icon, title, description, action, node }: { icon: LucideIcon; title: string; description: string; action?: string; node?: ReactNode }) {
  return (
    <div className="flex min-h-[76px] items-center gap-3 border-b border-slate-100 px-4 py-3.5 sm:px-[18px] dark:border-slate-800">
      <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-brand-50 text-brand-600 dark:bg-brand-500/15 dark:text-brand-100"><Icon size={17} /></span>
      <div>
        <h2 className="font-display text-sm font-bold tracking-tight text-slate-900 dark:text-white">{title}</h2>
        <p className="mt-0.5 text-[11px] text-slate-400">{description}</p>
      </div>
      <div className="ml-auto">
        {node ?? (action && <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-[10px] font-semibold text-slate-500 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">{action}</span>)}
      </div>
    </div>
  );
}

function Kpis({ overview, loading, days }: { overview: Overview | null; loading: boolean; days: number }) {
  const killHealthy = overview ? overview.kill_rate >= 0.4 && overview.kill_rate <= 0.6 : true;
  const items: Array<{ icon: LucideIcon; label: string; value: string; note: string; tone?: "good" | "warn" }> = [
    { icon: Wallet, label: "Total spend", value: overview ? fmtUSD(overview.cost) : "—", note: overview ? `${overview.runs} reviews in ${days} days` : "Waiting for review data" },
    { icon: Receipt, label: "Cost per review", value: overview ? fmtUSD(overview.cost_per_review) : "—", note: overview && overview.cost_per_review > .6 ? "Above the $0.60 target" : "Within the $0.60 target", tone: overview && overview.cost_per_review > .6 ? "warn" : "good" },
    { icon: MessageSquare, label: "Comments posted", value: overview ? fmtInt(overview.posted) : "—", note: overview ? `${fmtInt(overview.killed)} low-confidence findings filtered` : "Waiting for review data" },
    { icon: Filter, label: "Verifier kill rate", value: overview ? fmtPct(overview.kill_rate) : "—", note: killHealthy ? "Healthy · target is 40–60%" : "Outside the healthy range", tone: killHealthy ? "good" : "warn" },
    { icon: Zap, label: "Cache hit rate", value: overview ? fmtPct(overview.cache_hit) : "—", note: overview && overview.cache_hit < .2 ? "Prefix reuse needs attention" : "Prefix reuse is healthy", tone: overview && overview.cache_hit < .2 ? "warn" : "good" },
    { icon: Brain, label: "Suppressions", value: overview ? fmtInt(overview.suppressions) : "—", note: overview ? `Prevented ${fmtInt(overview.suppression_hits)} repeat findings` : "Waiting for review data" },
  ];
  return (
    <section className="mb-4 grid grid-cols-2 gap-2.5 md:grid-cols-3 xl:grid-cols-6" aria-label="Review summary">
      {items.map(({ icon: Icon, ...item }) => {
        const accent = item.tone === "good" ? "bg-emerald-500" : item.tone === "warn" ? "bg-rose-500" : "bg-brand-500/70";
        const chip = item.tone === "good"
          ? "bg-emerald-50 text-emerald-600 dark:bg-emerald-400/10 dark:text-emerald-300"
          : item.tone === "warn"
            ? "bg-rose-50 text-rose-600 dark:bg-rose-400/10 dark:text-rose-300"
            : "bg-brand-50 text-brand-600 dark:bg-brand-500/15 dark:text-brand-100";
        return (
          <div key={item.label} className="group relative flex min-h-[128px] gap-2.5 overflow-hidden rounded-2xl border border-slate-200/80 bg-gradient-to-b from-white to-slate-50/60 p-3.5 shadow-[0_5px_18px_rgba(15,23,42,.035)] transition-[transform,box-shadow,border-color] duration-200 hover:-translate-y-0.5 hover:border-brand-500/25 hover:shadow-[0_10px_28px_rgba(15,23,42,.07)] dark:border-slate-800 dark:from-slate-900 dark:to-slate-900/60">
            <span className={`absolute inset-x-0 top-0 h-[2.5px] scale-x-0 transition-transform duration-300 group-hover:scale-x-100 ${accent}`} />
            <span className={`grid size-8 shrink-0 place-items-center rounded-[10px] ${chip}`}><Icon size={16} /></span>
            <div className="min-w-0">
              <p className="truncate text-[11px] font-semibold text-slate-500 dark:text-slate-400">{item.label}</p>
              {loading ? <Skeleton className="mt-2 h-7 w-20" /> : <p className="mt-2 font-display text-2xl font-bold leading-none tracking-[-.04em] text-slate-900 dark:text-white">{item.value}</p>}
              <p className={`mt-2 flex items-center gap-1 text-[10px] leading-snug ${item.tone === "good" ? "text-emerald-600 dark:text-emerald-400" : item.tone === "warn" ? "text-rose-600 dark:text-rose-400" : "text-slate-400"}`}>
                {item.tone && <span className={`size-1 rounded-full ${item.tone === "good" ? "bg-emerald-500" : "bg-rose-500"}`} />}
                {item.note}
              </p>
            </div>
          </div>
        );
      })}
    </section>
  );
}

function LivePanel({ runs }: { runs: Run[] }) {
  if (!runs.length) return <EmptyState icon={Check} title="You’re all caught up" copy="New reviews will appear here as soon as they start." />;
  return (
    <div className="grid gap-4">
      {runs.map((run, runIndex) => {
        const pct = Math.min(100, Math.round(((run.stage_index + 1) / Math.max(1, run.stages.length)) * 100));
        return (
          <article key={run.id} className={runIndex ? "border-t border-slate-100 pt-4 dark:border-slate-800" : ""}>
            <div className="flex items-start justify-between gap-3">
              <div><strong className="block text-xs text-slate-800 dark:text-slate-100">{friendlyRepo(run.repo)}</strong><span className="mt-0.5 block text-[10px] text-slate-400">{run.pr ? `PR #${run.pr}` : "Local review"} · {relTime(run.started_at)}</span></div>
              <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-1 text-[10px] font-semibold text-slate-500 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">{run.tier}</span>
            </div>
            <div className="mb-2 mt-3 flex items-center justify-between text-[10px] font-semibold text-slate-500 dark:text-slate-400">
              <span className="flex items-center gap-1.5"><Loader2 size={11} className="animate-spin text-brand-500" strokeWidth={2.5} />{humanize(run.stage)}</span>
              <span className="tabular-nums">{pct}%</span>
            </div>
            <div
              className="relative h-1.5 overflow-hidden rounded-full bg-slate-100 dark:bg-slate-800"
              role="progressbar"
              aria-label={`Current stage: ${run.stage}`}
              aria-valuenow={pct}
              aria-valuemin={0}
              aria-valuemax={100}
            >
              <div className="h-full rounded-full bg-gradient-to-r from-brand-500/70 to-brand-500 transition-[width] duration-700 ease-out" style={{ width: `${pct}%` }} />
              <div className="absolute inset-y-0 left-0 w-1/4 bg-gradient-to-r from-transparent via-white/50 to-transparent [animation:loading-bar_1.6s_ease-in-out_infinite]" />
            </div>
          </article>
        );
      })}
    </div>
  );
}

function RunsTable({ runs, onOpen }: { runs: Run[]; onOpen: (id: number) => void }) {
  if (!runs.length) return <EmptyState icon={GitPullRequest} title="No reviews yet" copy="Run your first review and its results will show up here." />;
  return (
    <table className="w-full min-w-[780px] border-collapse text-xs">
      <thead>
        <tr className="border-b border-slate-200 bg-slate-50/80 text-left text-[10px] font-bold uppercase tracking-[.05em] text-slate-400 dark:border-slate-800 dark:bg-slate-950/30">
          <th className="px-4 py-3">Repository</th><th className="px-4 py-3">Pull request</th><th className="px-4 py-3">Status</th><th className="hidden px-4 py-3 text-right lg:table-cell">Posted</th><th className="px-4 py-3 text-right">Cost</th><th className="hidden px-4 py-3 text-right md:table-cell">Duration</th><th className="w-10 px-3"><span className="sr-only">Open</span></th>
        </tr>
      </thead>
      <tbody>
        {runs.map((run) => (
          <tr key={run.id} tabIndex={0} onClick={() => onOpen(run.id)} onKeyDown={(event) => (event.key === "Enter" || event.key === " ") && onOpen(run.id)} className="cursor-pointer border-b border-slate-100 text-slate-500 transition last:border-0 hover:bg-brand-50/40 focus:bg-brand-50/40 focus:outline-none dark:border-slate-800 dark:text-slate-400 dark:hover:bg-brand-500/5 dark:focus:bg-brand-500/5">
            <td className="px-4 py-3"><span className="flex items-center gap-3"><span className="grid size-8 shrink-0 place-items-center rounded-[10px] bg-brand-50 font-display text-[10px] font-bold text-brand-600 dark:bg-brand-500/15 dark:text-brand-100">{repoInitials(run.repo)}</span><span><strong className="block text-xs text-slate-800 dark:text-slate-100">{friendlyRepo(run.repo)}</strong><small className="mt-0.5 block text-[9px] text-slate-400">{run.model}</small></span></span></td>
            <td className="px-4 py-3 font-bold text-brand-600 dark:text-brand-100">{run.pr ? `#${run.pr}` : <span className="font-normal text-slate-400">Local</span>}</td>
            <td className="px-4 py-3"><span className={`inline-flex items-center gap-1.5 rounded-full px-2 py-1 text-[10px] font-bold ${statusClass[run.status]}`}><StatusDot status={run.status} />{run.status === "running" ? humanize(run.stage) : capitalize(run.status)}</span></td>
            <td className="hidden px-4 py-3 text-right font-bold text-slate-700 lg:table-cell dark:text-slate-200">{run.posted}</td>
            <td className="px-4 py-3 text-right tabular-nums">{fmtUSD(run.cost)}</td>
            <td className="hidden px-4 py-3 text-right tabular-nums md:table-cell">{run.elapsed ? formatDuration(run.elapsed) : "—"}</td>
            <td className="px-3 text-slate-300"><ChevronRight size={16} /></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function SeverityBars({ counts }: { counts: Record<string, number> }) {
  const total = Object.values(counts).reduce((sum, value) => sum + value, 0);
  if (!total) return <EmptyState icon={ShieldCheck} title="No findings posted" copy="Severity distribution will appear after a review posts comments." />;
  const max = Math.max(...Object.values(counts));
  return (
    <div className="grid gap-4">
      {SEVERITY_ORDER.map((severity) => {
        const count = counts[severity] ?? 0;
        return (
          <div className="grid grid-cols-[72px_1fr_48px] items-center gap-3" key={severity}>
            <span className="flex items-center gap-2 text-[11px] font-semibold text-slate-500 dark:text-slate-400"><span className="size-2 rounded-full" style={{ background: severityColor[severity] }} />{capitalize(severity)}</span>
            <span className="h-2 overflow-hidden rounded-full bg-slate-100 dark:bg-slate-800"><span className="block h-full rounded-full transition-[width] duration-500" style={{ width: `${max ? (count / max) * 100 : 0}%`, background: severityColor[severity] }} /></span>
            <span className="flex items-baseline justify-end gap-1 tabular-nums"><strong className="text-xs">{count}</strong><small className="text-[9px] text-slate-400">{Math.round((count / total) * 100)}%</small></span>
          </div>
        );
      })}
    </div>
  );
}

function SuppressionList({ items }: { items: Suppression[] }) {
  if (!items.length) return <EmptyState icon={Brain} title="Nothing learned yet" copy="Resolved or downvoted comments become helpful suppression rules here." />;
  return (
    <div>
      {items.slice(0, 6).map((item) => (
        <article key={item.id} className="flex gap-3 border-b border-slate-100 py-3 last:border-0 dark:border-slate-800">
          <span className="grid size-7 shrink-0 place-items-center rounded-lg bg-brand-50 text-brand-600 dark:bg-brand-500/15 dark:text-brand-100"><Sparkles size={14} /></span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2"><strong className="text-[10px] text-brand-600 dark:text-brand-100">{humanize(item.reason)}</strong><span className="truncate font-mono text-[9px] text-slate-400" title={item.file || item.repo}>{item.file || item.repo}</span>{item.hits > 0 && <span className="ml-auto shrink-0 text-[9px] font-bold text-emerald-600 dark:text-emerald-400">Used {item.hits}×</span>}</div>
            <p className="mt-1 truncate text-[11px] text-slate-500 dark:text-slate-400">{stripComment(item.claim).slice(0, 140) || "Suppression rule"}</p>
          </div>
        </article>
      ))}
    </div>
  );
}

function RunDrawer({ run, onClose }: { run: RunDetail; onClose: () => void }) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => { window.removeEventListener("keydown", onKey); document.body.style.overflow = ""; };
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-slate-950/50 backdrop-blur-[3px]" onMouseDown={onClose}>
      <aside role="dialog" aria-modal="true" aria-labelledby="drawer-title" onMouseDown={(event) => event.stopPropagation()} className="h-full w-full max-w-[620px] overflow-y-auto bg-white shadow-2xl [animation:drawer-in_.22s_ease] dark:bg-slate-900">
        <div className="sticky top-0 z-10 flex min-h-[90px] items-center justify-between border-b border-slate-200 bg-white/90 px-5 py-4 backdrop-blur-xl dark:border-slate-800 dark:bg-slate-900/90">
          <div><p className="mb-1 text-[10px] font-bold uppercase tracking-[.13em] text-brand-600 dark:text-brand-100">Review details</p><h2 id="drawer-title" className="font-display text-xl font-bold tracking-tight">{friendlyRepo(run.repo)} {run.pr && <span className="text-brand-600 dark:text-brand-100">#{run.pr}</span>}</h2></div>
          <button className="grid size-9 place-items-center rounded-xl border border-slate-200 bg-slate-50 text-slate-500 hover:text-slate-900 dark:border-slate-700 dark:bg-slate-800 dark:hover:text-white" onClick={onClose} aria-label="Close review details"><X size={18} /></button>
        </div>
        <div className="p-5 sm:p-6">
          <div className="flex flex-wrap gap-2"><span className={`inline-flex items-center gap-1.5 rounded-full px-2 py-1 text-[10px] font-bold ${statusClass[run.status]}`}><StatusDot status={run.status} />{capitalize(run.status)}</span><Pill>{capitalize(run.tier)}</Pill><Pill>{run.model}</Pill></div>
          <div className="mt-5 grid grid-cols-2 overflow-hidden rounded-xl border border-slate-200 sm:grid-cols-4 dark:border-slate-800">
            {([["Spend", fmtUSD(run.cost)], ["Duration", formatDuration(run.elapsed)], ["Posted", run.posted], ["Filtered", run.killed]] as const).map(([label, value], index) => <div key={label} className={`bg-slate-50 p-3 dark:bg-slate-950/30 ${index % 2 ? "border-l border-slate-200 dark:border-slate-800" : ""} ${index > 1 ? "border-t border-slate-200 sm:border-t-0 dark:border-slate-800" : ""} ${index === 2 ? "sm:border-l" : ""}`}><small className="block text-[9px] font-semibold uppercase tracking-wider text-slate-400">{label}</small><strong className="mt-1 block font-display text-sm">{value}</strong></div>)}
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2 rounded-xl bg-brand-50 px-3 py-2.5 text-[10px] text-slate-500 dark:bg-brand-500/10 dark:text-slate-300"><Zap size={15} className="text-brand-600 dark:text-brand-100" /><span><strong>{fmtInt(run.cache_read)}</strong> cached tokens read</span><span className="h-3 w-px bg-brand-500/20" /><span><strong>{fmtInt(run.cache_write)}</strong> written</span></div>
          {run.error && <div className="mt-4 flex items-center gap-2 rounded-xl bg-rose-50 p-3 text-[11px] text-rose-700 dark:bg-rose-400/10 dark:text-rose-200"><AlertTriangle size={16} />{run.error}</div>}
          <div className="mb-3 mt-7 flex items-end justify-between"><div><h3 className="font-display text-sm font-bold">Findings</h3><p className="mt-1 text-[10px] text-slate-400">What CR found and whether it passed verification.</p></div><Pill>{run.findings.length}</Pill></div>
          {!run.findings.length ? <EmptyState icon={Check} title="No findings" copy="This review completed without any reportable issues." /> : <div className="grid gap-3">{run.findings.map((finding) => (
            <article key={finding.id} className={`rounded-xl border border-slate-200 bg-slate-50 p-4 dark:border-slate-800 dark:bg-slate-950/30 ${finding.posted ? "" : "opacity-60"}`}>
              <div className="flex flex-wrap items-center gap-2"><span className="flex items-center gap-1.5 text-[10px] font-bold"><span className="size-2 rounded-full" style={{ background: severityColor[finding.severity] }} />{capitalize(finding.severity)}</span><Pill>{humanize(finding.category)}</Pill><span className="ml-auto text-[9px] text-slate-400">{Math.round(finding.confidence * 100)}% confidence</span></div>
              <h4 className="mt-3 font-display text-xs font-bold leading-relaxed">{finding.claim}</h4><p className="mt-1 text-[11px] leading-relaxed text-slate-500 dark:text-slate-400">{finding.failure_scenario}</p>
              <div className="mt-3 flex items-center justify-between gap-3 border-t border-slate-200 pt-3 dark:border-slate-800"><span className="flex min-w-0 items-center gap-1 truncate font-mono text-[9px] text-slate-400"><FileCode2 size={13} />{finding.file}:{finding.line}</span><span className={`flex items-center gap-1 text-[9px] font-bold ${finding.posted ? "text-emerald-600 dark:text-emerald-400" : "text-slate-400"}`}>{finding.posted ? <Check size={13} /> : <Filter size={13} />}{finding.posted ? "Posted" : "Filtered"}</span></div>
            </article>
          ))}</div>}
        </div>
      </aside>
    </div>
  );
}

function StatusDot({ status }: { status: Run["status"] }) {
  return status === "running"
    ? <Loader2 size={10} className="animate-spin" strokeWidth={3} />
    : <span className="size-1.5 rounded-full bg-current" />;
}

function Pill({ children }: { children: ReactNode }) {
  return <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-[10px] font-semibold text-slate-500 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">{children}</span>;
}

function EmptyState({ icon: Icon, title, copy }: { icon: LucideIcon; title: string; copy: string }) {
  return <div className="flex min-h-40 flex-col items-center justify-center p-5 text-center"><span className="mb-2.5 grid size-10 place-items-center rounded-xl bg-brand-50 text-brand-600 dark:bg-brand-500/15 dark:text-brand-100"><Icon size={19} /></span><strong className="font-display text-xs">{title}</strong><p className="mt-1 max-w-[280px] text-[10px] leading-relaxed text-slate-400">{copy}</p></div>;
}

function Skeleton({ className }: { className: string }) {
  return <div className={`rounded-lg bg-[linear-gradient(90deg,#f1f3f7_20%,#e5e8ef_50%,#f1f3f7_80%)] bg-[length:220%_100%] [animation:shimmer_1.5s_linear_infinite] dark:bg-[linear-gradient(90deg,#1e293b_20%,#293548_50%,#1e293b_80%)] ${className}`} />;
}

function SkeletonLines({ count }: { count: number }) {
  return <div className="grid gap-3">{Array.from({ length: count }, (_, index) => <Skeleton key={index} className={`h-4 ${index % 2 ? "w-4/5" : "w-full"}`} />)}</div>;
}

function friendlyRepo(repo: string) { return repo.split("/").pop() || repo; }
function repoInitials(repo: string) { return friendlyRepo(repo).replace(/[-_]/g, " ").split(" ").slice(0, 2).map((part) => part[0]?.toUpperCase()).join("") || "CR"; }
function humanize(value: string) { return value.replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase()); }
function capitalize(value: string) { return value ? value[0].toUpperCase() + value.slice(1) : value; }
function stripComment(value: string) { return value.replace(/<!--[\s\S]*?-->/g, "").trim(); }
function formatDuration(seconds: number) { return seconds < 60 ? `${Math.round(seconds)}s` : `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`; }

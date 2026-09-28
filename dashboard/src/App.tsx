import { useCallback, useEffect, useState } from "react";
import {
  api,
  fmtInt,
  fmtPct,
  fmtUSD,
  relTime,
  SEVERITY_ORDER,
  SEVERITY_VAR,
  STATUS_VAR,
  type Overview,
  type Run,
  type RunDetail,
  type Suppression,
} from "./api";
import { CostChart } from "./CostChart";

const WINDOWS = [7, 30, 90];
const POLL_MS = 4000;

export default function App() {
  const [theme, setTheme] = useState<"light" | "dark" | "system">("system");
  const [days, setDays] = useState(30);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [active, setActive] = useState<Run[]>([]);
  const [sups, setSups] = useState<Suppression[]>([]);
  const [selected, setSelected] = useState<RunDetail | null>(null);
  const [offline, setOffline] = useState<string | null>(null);

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
  }, [theme]);

  const load = useCallback(async () => {
    try {
      const [o, r, a, s] = await Promise.all([
        api.overview(days),
        api.runs(50),
        api.active(),
        api.suppressions(),
      ]);
      setOverview(o);
      setRuns(r);
      setActive(a);
      setSups(s);
      setOffline(null);
    } catch (e) {
      setOffline(e instanceof Error ? e.message : "unreachable");
    }
  }, [days]);

  useEffect(() => {
    load();
    const t = setInterval(load, POLL_MS);
    return () => clearInterval(t);
  }, [load]);

  const openRun = async (id: number) => {
    try {
      setSelected(await api.run(id));
    } catch {
      /* ignore — the drawer just will not open */
    }
  };

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <h1>CR</h1>
          <span className="sub">code review</span>
        </div>
        <div className="spacer" />
        <div className="controls">
          <div className="seg" role="group" aria-label="Time range">
            {WINDOWS.map((d) => (
              <button
                key={d}
                className="btn"
                aria-pressed={days === d}
                onClick={() => setDays(d)}
              >
                {d}d
              </button>
            ))}
          </div>
          <div className="seg" role="group" aria-label="Theme">
            {(["light", "system", "dark"] as const).map((t) => (
              <button
                key={t}
                className="btn"
                aria-pressed={theme === t}
                onClick={() => setTheme(t)}
              >
                {t === "light" ? "Light" : t === "dark" ? "Dark" : "Auto"}
              </button>
            ))}
          </div>
        </div>
      </header>

      {offline && (
        <div className="offline">
          <strong>API unreachable</strong> — {offline}. Start it with{" "}
          <span className="mono">uv run cr serve</span>.
        </div>
      )}

      <Kpis o={overview} />

      <div className="grid split" style={{ marginBottom: 16 }}>
        <section className="card">
          <div className="card-head">
            <h2>Daily spend</h2>
            <div className="spacer" />
            <span className="chip">last {days} days</span>
          </div>
          <div className="card-body">
            <CostChart data={overview?.series ?? []} />
          </div>
        </section>

        <section className="card">
          <div className="card-head">
            <h2>In flight</h2>
            <div className="spacer" />
            {active.length > 0 && (
              <span className="chip">
                <span className="dot" style={{ background: "var(--series-1)" }} />
                {active.length} running
              </span>
            )}
          </div>
          <div className="card-body">
            <LivePanel runs={active} />
          </div>
        </section>
      </div>

      <div className="grid split">
        <section className="card">
          <div className="card-head">
            <h2>Recent reviews</h2>
          </div>
          <div className="card-body" style={{ paddingTop: 4 }}>
            <RunsTable runs={runs} onOpen={openRun} />
          </div>
        </section>

        <div className="grid">
          <section className="card">
            <div className="card-head">
              <h2>Posted findings by severity</h2>
            </div>
            <div className="card-body">
              <SeverityBars counts={overview?.severity ?? {}} />
            </div>
          </section>

          <section className="card">
            <div className="card-head">
              <h2>Suppression memory</h2>
              <div className="spacer" />
              <span className="chip">{sups.length} stored</span>
            </div>
            <div className="card-body">
              <SuppressionList items={sups} />
            </div>
          </section>
        </div>
      </div>

      {selected && <RunDrawer run={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}

/* --- KPI row: single current values, so stat tiles, not a bar chart -------- */

function Kpis({ o }: { o: Overview | null }) {
  const killHealthy = o ? o.kill_rate >= 0.4 && o.kill_rate <= 0.6 : true;
  return (
    <div className="kpis">
      <Kpi label="Spend" value={o ? fmtUSD(o.cost) : "—"} note={o ? `${o.runs} reviews` : ""} />
      <Kpi
        label="Cost / review"
        value={o ? fmtUSD(o.cost_per_review) : "—"}
        note={o && o.cost_per_review > 0.6 ? "above target" : "within target"}
        tone={o && o.cost_per_review > 0.6 ? "warn" : "good"}
      />
      <Kpi
        label="Comments posted"
        value={o ? fmtInt(o.posted) : "—"}
        note={o ? `${fmtInt(o.killed)} killed by verifier` : ""}
      />
      <Kpi
        label="Verifier kill rate"
        value={o ? fmtPct(o.kill_rate) : "—"}
        note={killHealthy ? "healthy band 40–60%" : "outside 40–60% band"}
        tone={killHealthy ? "good" : "warn"}
      />
      <Kpi
        label="Cache hit"
        value={o ? fmtPct(o.cache_hit) : "—"}
        note={o && o.cache_hit < 0.2 ? "check for invalidator" : "prefix reuse healthy"}
        tone={o && o.cache_hit < 0.2 ? "warn" : "good"}
      />
      <Kpi
        label="Suppressions"
        value={o ? fmtInt(o.suppressions) : "—"}
        note={o ? `fired ${fmtInt(o.suppression_hits)}×` : ""}
      />
    </div>
  );
}

function Kpi({
  label,
  value,
  note,
  tone,
}: {
  label: string;
  value: string;
  note?: string;
  tone?: "good" | "warn";
}) {
  return (
    <div className="kpi">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {note && <div className={`note ${tone ?? ""}`}>{note}</div>}
    </div>
  );
}

/* --- live progress -------------------------------------------------------- */

function LivePanel({ runs }: { runs: Run[] }) {
  if (!runs.length) {
    return <div className="empty">Nothing running. Reviews appear here as they start.</div>;
  }
  return (
    <>
      {runs.map((r) => (
        <div className="live-item" key={r.id}>
          <div className="live-head">
            <span className="repo">{r.repo}</span>
            {r.pr && <span className="chip">#{r.pr}</span>}
            <span className="chip">{r.tier}</span>
            <span className="chip">{r.model}</span>
            <div className="spacer" />
            <span className="chip">{relTime(r.started_at)}</span>
          </div>
          <div className="stages">
            {r.stages.map((s, i) => (
              <div
                key={s}
                className={`stage ${
                  i < r.stage_index ? "done" : i === r.stage_index ? "current" : ""
                }`}
              >
                <div className="bar" />
                <div className="name">{s}</div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </>
  );
}

/* --- runs table ----------------------------------------------------------- */

function RunsTable({ runs, onOpen }: { runs: Run[]; onOpen: (id: number) => void }) {
  if (!runs.length) return <div className="empty">No reviews yet.</div>;
  return (
    <table>
      <thead>
        <tr>
          <th>Repo</th>
          <th>PR</th>
          <th>Status</th>
          <th>Tier</th>
          <th className="num">Posted</th>
          <th className="num">Killed</th>
          <th className="num">Cost</th>
          <th className="num">Time</th>
          <th>When</th>
        </tr>
      </thead>
      <tbody>
        {runs.map((r) => (
          <tr key={r.id} onClick={() => onOpen(r.id)}>
            <td className="primary">{r.repo}</td>
            <td>{r.pr ? `#${r.pr}` : "—"}</td>
            <td>
              {/* Status colour never travels alone — the label carries it. */}
              <span className="chip">
                <span className="dot" style={{ background: STATUS_VAR[r.status] }} />
                {r.status === "running" ? r.stage : r.status}
              </span>
            </td>
            <td>{r.tier}</td>
            <td className="num">{r.posted}</td>
            <td className="num">{r.killed}</td>
            <td className="num">{fmtUSD(r.cost)}</td>
            <td className="num">{r.elapsed ? `${r.elapsed.toFixed(0)}s` : "—"}</td>
            <td>{relTime(r.started_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/* --- severity: ordered scale, so an ordinal ramp, not categorical hues ----- */

function SeverityBars({ counts }: { counts: Record<string, number> }) {
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  if (!total) return <div className="empty">No findings posted yet.</div>;
  const max = Math.max(...Object.values(counts));
  return (
    <div className="bars">
      {SEVERITY_ORDER.map((sev) => {
        const n = counts[sev] ?? 0;
        return (
          <div className="barrow" key={sev}>
            <span className="sev-label">
              <span className="dot" style={{ background: SEVERITY_VAR[sev] }} />
              {sev}
            </span>
            <div className="track">
              <div
                className="fill"
                style={{
                  width: `${max ? (n / max) * 100 : 0}%`,
                  background: SEVERITY_VAR[sev],
                }}
              />
            </div>
            <span className="n">{n}</span>
          </div>
        );
      })}
    </div>
  );
}

/* --- suppression memory --------------------------------------------------- */

function SuppressionList({ items }: { items: Suppression[] }) {
  if (!items.length) {
    return (
      <div className="empty">
        Nothing suppressed. Run <span className="mono">cr learn --pr …</span> after a human
        resolves or 👎 a comment.
      </div>
    );
  }
  return (
    <div className="bars">
      {items.slice(0, 8).map((s) => (
        <div key={s.id} style={{ fontSize: 12.5 }}>
          <div style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
            <span className="chip">{s.reason}</span>
            <span className="mono" style={{ color: "var(--text-muted)" }}>
              {s.file || s.repo}
            </span>
            <div className="spacer" />
            {s.hits > 0 && <span className="chip">fired {s.hits}×</span>}
          </div>
          <div
            style={{
              color: "var(--text-secondary)",
              marginTop: 4,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {s.claim.replace(/<!--[\s\S]*?-->/g, "").slice(0, 110) || "—"}
          </div>
        </div>
      ))}
    </div>
  );
}

/* --- run detail ----------------------------------------------------------- */

function RunDrawer({ run, onClose }: { run: RunDetail; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.38)",
        display: "flex",
        justifyContent: "flex-end",
        zIndex: 50,
      }}
    >
      <div
        className="card"
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "min(620px, 100%)",
          height: "100%",
          borderRadius: 0,
          overflowY: "auto",
        }}
      >
        <div className="card-head" style={{ paddingTop: 18 }}>
          <h2>
            {run.repo}
            {run.pr ? ` #${run.pr}` : ""}
          </h2>
          <div className="spacer" />
          <button className="btn" onClick={onClose}>
            Close
          </button>
        </div>
        <div className="card-body">
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 16 }}>
            <span className="chip">{run.tier}</span>
            <span className="chip">{run.model}</span>
            <span className="chip">
              <span className="dot" style={{ background: STATUS_VAR[run.status] }} />
              {run.status}
            </span>
            <span className="chip">{fmtUSD(run.cost)}</span>
            <span className="chip">{run.elapsed.toFixed(0)}s</span>
            <span className="chip">
              cache {fmtInt(run.cache_read)} read / {fmtInt(run.cache_write)} write
            </span>
          </div>

          {run.error && (
            <div className="offline" style={{ marginBottom: 16 }}>
              {run.error}
            </div>
          )}

          {run.findings.length === 0 && <div className="empty">No findings recorded.</div>}

          {run.findings.map((f) => (
            <div
              key={f.id}
              style={{
                borderTop: "1px solid var(--border)",
                paddingTop: 12,
                marginTop: 12,
                opacity: f.posted ? 1 : 0.62,
              }}
            >
              <div style={{ display: "flex", gap: 6, marginBottom: 6, flexWrap: "wrap" }}>
                <span className="chip">
                  <span className="dot" style={{ background: SEVERITY_VAR[f.severity] }} />
                  {f.severity}
                </span>
                <span className="chip">{f.category}</span>
                <span className="chip">{Math.round(f.confidence * 100)}% confident</span>
                <span className="chip">{f.posted ? "posted" : "killed"}</span>
              </div>
              <div style={{ fontWeight: 600, marginBottom: 4 }}>{f.claim}</div>
              <div style={{ color: "var(--text-secondary)", fontSize: 13 }}>
                {f.failure_scenario}
              </div>
              <div className="mono" style={{ color: "var(--text-muted)", marginTop: 6 }}>
                {f.file}:{f.line} · found by {f.found_by}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

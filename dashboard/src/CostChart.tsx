import { useMemo, useRef, useState } from "react";
import type { DayPoint } from "./api";
import { fmtUSD } from "./api";

/* Trend over time, one series. Per the form heuristic that is an area chart with
   a single sequential hue — and a single series needs no legend, because the
   card title names it. Direct-labelled at the last point only; a number on every
   point is noise. Crosshair + tooltip ship by default. */

const PAD = { top: 14, right: 46, bottom: 26, left: 46 };

interface Props {
  data: DayPoint[];
  height?: number;
}

export function CostChart({ data, height = 220 }: Props) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  const [width, setWidth] = useState(720);

  useMemo(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setWidth(e.contentRect.width));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const geom = useMemo(() => {
    const w = Math.max(320, width);
    const innerW = w - PAD.left - PAD.right;
    const innerH = height - PAD.top - PAD.bottom;
    const max = Math.max(0.0001, ...data.map((d) => d.cost));
    // Round the axis top up so the tick labels are readable numbers.
    const top = niceCeil(max);
    const x = (i: number) =>
      PAD.left + (data.length <= 1 ? innerW / 2 : (i / (data.length - 1)) * innerW);
    const y = (v: number) => PAD.top + innerH - (v / top) * innerH;
    return { w, innerW, innerH, top, x, y };
  }, [data, width, height]);

  if (!data.length) {
    return (
      <div className="empty">
        No spend recorded yet. Run <span className="mono">cr review-pr</span> and it lands here.
      </div>
    );
  }

  const { w, top, x, y } = geom;
  const line = data.map((d, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(d.cost)}`).join(" ");
  const area = `${line} L${x(data.length - 1)},${height - PAD.bottom} L${x(0)},${
    height - PAD.bottom
  } Z`;
  const ticks = [0, top / 2, top];
  const last = data[data.length - 1];
  const hoveredPoint = hover !== null ? data[hover] : null;

  return (
    <div className="chart-wrap" ref={wrapRef}>
      <svg
        width="100%"
        height={height}
        viewBox={`0 0 ${w} ${height}`}
        role="img"
        aria-label={`Daily review spend, ${data.length} days, peak ${fmtUSD(top)}`}
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const box = e.currentTarget.getBoundingClientRect();
          const px = ((e.clientX - box.left) / box.width) * w;
          const rel = (px - PAD.left) / Math.max(1, w - PAD.left - PAD.right);
          const i = Math.round(rel * (data.length - 1));
          setHover(Math.min(data.length - 1, Math.max(0, i)));
        }}
      >
        <defs>
          <linearGradient id="costFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--series-1)" stopOpacity="0.22" />
            <stop offset="100%" stopColor="var(--series-1)" stopOpacity="0.02" />
          </linearGradient>
        </defs>

        {ticks.map((t) => (
          <g key={t}>
            <line
              x1={PAD.left}
              x2={w - PAD.right}
              y1={y(t)}
              y2={y(t)}
              stroke="var(--grid)"
              strokeWidth="1"
            />
            <text
              x={PAD.left - 8}
              y={y(t) + 4}
              textAnchor="end"
              fill="var(--text-muted)"
              fontSize="11"
              style={{ fontVariantNumeric: "tabular-nums" }}
            >
              {fmtUSD(t)}
            </text>
          </g>
        ))}

        <path d={area} fill="url(#costFill)" />
        <path
          d={line}
          fill="none"
          stroke="var(--series-1)"
          strokeWidth="2"
          strokeLinejoin="round"
          strokeLinecap="round"
        />

        {hover !== null && (
          <line
            x1={x(hover)}
            x2={x(hover)}
            y1={PAD.top}
            y2={height - PAD.bottom}
            stroke="var(--axis)"
            strokeWidth="1"
            strokeDasharray="3 3"
          />
        )}

        {/* Surface ring keeps the marker legible where it overlaps the line. */}
        {hover !== null && (
          <circle
            cx={x(hover)}
            cy={y(data[hover].cost)}
            r="5"
            fill="var(--series-1)"
            stroke="var(--surface)"
            strokeWidth="2"
          />
        )}

        <circle
          cx={x(data.length - 1)}
          cy={y(last.cost)}
          r="4"
          fill="var(--series-1)"
          stroke="var(--surface)"
          strokeWidth="2"
        />
        <text
          x={x(data.length - 1) + 9}
          y={y(last.cost) + 4}
          fill="var(--text-secondary)"
          fontSize="11.5"
          fontWeight="600"
        >
          {fmtUSD(last.cost)}
        </text>

        <text x={PAD.left} y={height - 7} fill="var(--text-muted)" fontSize="11">
          {shortDate(data[0].date)}
        </text>
        {data.length > 1 && (
          <text
            x={w - PAD.right}
            y={height - 7}
            textAnchor="end"
            fill="var(--text-muted)"
            fontSize="11"
          >
            {shortDate(last.date)}
          </text>
        )}
      </svg>

      {hoveredPoint && (
        <div
          className="tooltip"
          style={{
            left: Math.min(Math.max(0, (x(hover!) / w) * 100), 88) + "%",
            top: 6,
          }}
        >
          <div className="t-date">{longDate(hoveredPoint.date)}</div>
          <div className="t-val">{fmtUSD(hoveredPoint.cost)}</div>
          <div style={{ color: "var(--text-secondary)", marginTop: 2 }}>
            {hoveredPoint.runs} review{hoveredPoint.runs === 1 ? "" : "s"} ·{" "}
            {hoveredPoint.posted} comment{hoveredPoint.posted === 1 ? "" : "s"}
          </div>
        </div>
      )}
    </div>
  );
}

function niceCeil(v: number): number {
  if (v <= 0) return 1;
  const mag = 10 ** Math.floor(Math.log10(v));
  return Math.ceil(v / mag) * mag;
}

function shortDate(iso: string): string {
  return new Date(iso + "T00:00:00").toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}

function longDate(iso: string): string {
  return new Date(iso + "T00:00:00").toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}

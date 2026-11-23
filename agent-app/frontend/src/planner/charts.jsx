import { useState } from "react";
import { N } from "./ui.jsx";
import { Icon, K } from "./widgets.jsx";
import { useTween } from "./motion.js";

// Charts for the dashboard. Presentation only: values come from the API; colours are theme tokens; text sizes 12/13/14/16.

// Trend chip: "+4.8%" coloured by whether the change is good for this measure.
export function Delta({ pct, better = "up" }) {
  if (pct == null) return <span className="sp-delta">–</span>;
  const good = better === "up" ? pct >= 0 : pct <= 0;
  return <span className={"sp-delta " + (good ? "good" : "bad")}><Icon n={pct >= 0 ? "trend" : "down"} size={12} />{pct > 0 ? "+" : ""}{pct}%</span>;
}

const Y_ = v => (Math.abs(v) >= 100000 ? new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(v) : N(Math.round(v)));

// Area chart: current period (filled line) vs previous period (dashed), with a hover tooltip.
export function AreaChart({ points, labelV = "this period", labelP = "previous period" }) {
  const [hi, setHi] = useState(null);
  const n = points.length;
  const vals = points.flatMap(x => [x.v, x.p ?? x.v]);
  // the line, the previous-period line and the axis range all morph to the new data (filters, period)
  const tv = useTween(points.map(x => x.v));
  const tp = useTween(points.map(x => x.p ?? null));
  const [max, min] = useTween(n ? [Math.max(...vals) * 1.02, Math.min(...vals) * 0.98] : [1, 0]);
  if (n < 2) return <p className="muted">Not enough data for a trend.</p>;
  const span = max - min || 1;
  const X = i => (i / (n - 1)) * 100, Y = v => 100 - ((v - min) / span) * 100;
  const series = { v: tv, p: tp };
  const line = key => points.map((_, i) => `${i ? "L" : "M"}${X(i)},${Y(series[key][i] ?? tv[i] ?? 0)}`).join(" ");
  const ticks = [0, Math.round((n - 1) / 3), Math.round((2 * (n - 1)) / 3), n - 1];
  return (
    <div className="sp-area">
      <div className="sp-ylab"><span>{Y_(max)}</span><span>{Y_((max + min) / 2)}</span><span>{Y_(min)}</span></div>
      <div className="sp-plot" onMouseLeave={() => setHi(null)}
           onMouseMove={e => { const r = e.currentTarget.getBoundingClientRect(); setHi(Math.max(0, Math.min(n - 1, Math.round(((e.clientX - r.left) / r.width) * (n - 1))))); }}>
        <svg viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="Daily trend, this period against the previous period">
          <path d={line("v") + " L100,100 L0,100 Z"} style={{ fill: "color-mix(in srgb, var(--brand-blue) 14%, transparent)" }} />
          {points.some(x => x.p != null) && <path d={line("p")} fill="none" strokeWidth="1.5" strokeDasharray="4 4" vectorEffect="non-scaling-stroke" style={{ stroke: "var(--text-muted)" }} />}
          <path d={line("v")} fill="none" strokeWidth="2.5" vectorEffect="non-scaling-stroke" style={{ stroke: "var(--brand-blue)" }} />
          {hi != null && <line x1={X(hi)} x2={X(hi)} y1="0" y2="100" strokeDasharray="3 3" vectorEffect="non-scaling-stroke" style={{ stroke: "var(--text-muted)" }} />}
        </svg>
        {hi != null && <i className="sp-dot" style={{ left: `${X(hi)}%`, top: `${Y(tv[hi] ?? points[hi].v)}%` }} />}
        {hi != null && (
          <div className="sp-tip" style={{ left: `${Math.min(80, Math.max(20, X(hi)))}%` }}>
            <b>{points[hi].label}</b>
            <span><i className="c1" />{N(points[hi].v)} {labelV}</span>
            {points[hi].p != null && <span><i className="c0" />{N(points[hi].p)} {labelP}</span>}
          </div>
        )}
        <div className="sp-xlab">{ticks.map(i => <span key={i} style={{ left: `${X(i)}%` }}>{points[i].label}</span>)}</div>
      </div>
    </div>
  );
}

// Semicircle gauge made of ticks (reference "repeat customer rate").
export function TickGauge({ pct, label }) {
  const t = useTween(pct ?? 0);
  const T = 36, on = Math.round((t / 100) * T), R1 = 70, R2 = 88;
  return (
    <div className="sp-gauge">
      <svg viewBox="0 0 200 104" width="200" height="104" role="img" aria-label={`${label} ${pct ?? 0} percent`}>
        {Array.from({ length: T + 1 }, (_, i) => {
          const a = Math.PI - (i / T) * Math.PI, c = Math.cos(a), s = Math.sin(a);
          return <line key={i} x1={100 + R1 * c} y1={100 - R1 * s} x2={100 + R2 * c} y2={100 - R2 * s} strokeWidth="4" strokeLinecap="round"
                       style={{ stroke: i <= on ? "var(--chart-c4)" : "var(--muted-fill)" }} />;
        })}
      </svg>
      <div className="sp-gauge-v"><b>{pct == null ? "–" : (Math.round(t * 10) / 10)}%</b><small>{label}</small></div>
    </div>
  );
}

// Vertical bars per day of week (bars start at zero). Hover or keyboard focus shows that day's details; click opens the detailed view.
export function WeekBars({ rows, onOpen }) {
  const [hi, setHi] = useState(null);
  const max = Math.max(...rows.map(r => r.value), 1), top = rows.findIndex(r => r.value === max);
  const n = rows.length, cur = hi, r = hi == null ? null : rows[hi]?.d;          // details only while a bar is hovered or focused
  return (
    <div className="sp-weekwrap">
      {r && (
        <div className="sp-wtip" style={cur < 2 ? { left: 0, transform: "none" } : cur > n - 3 ? { right: 0, left: "auto", transform: "none" } : { left: `${((cur + 0.5) / n) * 100}%` }} aria-live="polite">
          <b>{r.day}{cur === top ? " · busiest" : ""}</b>
          <span>Avg good units <em>{N(r.avg_good_units)}</em></span>
          <span>Avg target <em>{N(r.avg_target)}</em></span>
          <span>Attainment <em>{r.attainment_pct ?? "–"}%</em></span>
          <span>Downtime <em>{N(r.avg_downtime_min)} min/day</em></span>
          <span>Rejects <em>{r.reject_pct ?? "–"}%</em></span>
          <span>Best line <em>{r.best_line ?? "–"}</em></span>
          <small>{r.days} days in this period</small>
        </div>
      )}
      <div className="sp-week" role="list" aria-label="Average good units by day of the week" onMouseLeave={() => setHi(null)}>
        {rows.map((x, i) => (
          <div key={x.label} role="listitem" tabIndex={0} className={"sp-wk" + (i === top ? " on" : "") + (i === hi ? " hov" : "")}
               aria-label={`${x.label}: ${N(x.value)} good units on average${x.d ? `, attainment ${x.d.attainment_pct}%` : ""}`}
               onMouseEnter={() => setHi(i)} onFocus={() => setHi(i)} onBlur={() => setHi(null)}
               onClick={onOpen} onKeyDown={e => (e.key === "Enter" || e.key === " ") && onOpen?.()}>
            <em className="sp-wk-v" aria-hidden="true">{K(x.value)}</em>
            <span style={{ height: `${Math.max(18, (x.value / max) * 100)}%` }} />
            <small>{x.label}</small>
          </div>
        ))}
      </div>
    </div>
  );
}

export const Orb = () => <div className="sp-orb" aria-hidden="true"><i /></div>;

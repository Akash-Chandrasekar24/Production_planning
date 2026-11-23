import { N } from "./ui.jsx";

// Dashboard building blocks. Pure presentation: every number is passed in from the API; colours are theme tokens only.

const P = {
  plus: "M12 5v14M5 12h14", menu: "M4 7h16M4 12h10M4 17h16", search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20 20l-4.5-4.5",
  share: "M4 13v6h16v-6M12 3v12M7 8l5-5 5 5", calendar: "M7 3v4M17 3v4M4 9h16M5 5h14v15H5z", refresh: "M20 12a8 8 0 1 1-2.3-5.6M20 4v5h-5",
  send: "M4 12l16-8-6 16-2-7-8-1z", arrow: "M5 12h14M13 6l6 6-6 6", lock: "M6 11h12v9H6zM8 11V8a4 4 0 0 1 8 0v3",
  clock: "M12 7v5l3 2M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z", chart: "M5 19V10M10 19V5M15 19v-7M20 19V8", trend: "M3 17l6-6 4 4 8-8M15 7h6v6",
  filter: "M4 6h16M7 12h10M10 18h4", more: "M12 6h.01M12 12h.01M12 18h.01", close: "M6 6l12 12M18 6L6 18",
  sun: "M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z",
  moon: "M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z", check: "M5 12l5 5 9-9", left: "M15 6l-6 6 6 6", right: "M9 6l6 6-6 6", chevron: "M6 9l6 6 6-6", truck: "M3 6h11v10H3zM14 9h4l3 3v4h-7M7 19a1.5 1.5 0 1 0 0-.1M17 19a1.5 1.5 0 1 0 0-.1", play: "M8 5v14l11-7z", pause: "M9 5v14M15 5v14", skip: "M6 5v14M10 5l9 7-9 7z",
  layers: "M12 3l9 5-9 5-9-5 9-5zM3 13l9 5 9-5", alert: "M12 8v5M12 16h.01M10.3 3.9L2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
  burst: "M12 2v5M12 17v5M2 12h5M17 12h5M5 5l3.5 3.5M15.5 15.5L19 19M5 19l3.5-3.5M15.5 8.5L19 5",
  home: "M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z", box: "M3 7l9-4 9 4v10l-9 4-9-4zM3 7l9 4 9-4M12 11v10",
  file: "M6 3h9l4 4v14H6zM14 3v5h5", bell: "M18 16v-5a6 6 0 0 0-12 0v5l-2 2h16zM10 20a2 2 0 0 0 4 0", grid: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
  download: "M12 4v11M7 10l5 5 5-5M5 20h14", info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v6M12 7.5h.01", help: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.7M12 17h.01",
  cart: "M3 4h2l2.4 11h11.2L21 7H6.2M9 20h.01M18 20h.01", users: "M16 20v-1a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v1M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM22 20v-1a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8",
  gear: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9L7 7M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1",
  upload: "M12 16V5M7 10l5-5 5 5M5 20h14", sparkle: "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8z",
  down: "M3 7l6 6 4-4 8 8M15 17h6v-6", factory: "M3 21V10l6 4V10l6 4V6l6 3v12zM7 17h2M12 17h2M17 17h2", expand: "M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7",
  db: "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  cloud: "M7 18h10a4 4 0 0 0 .5-8 6 6 0 0 0-11.5 1.5A3.3 3.3 0 0 0 7 18z", target: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM12 12h.01",
  sheet: "M4 4h16v16H4zM4 10h16M4 15h16M10 4v16", nodes: "M12 3v4M6 17l3-3M18 17l-3-3M12 11a3 3 0 1 0 0 .01M6 20a2 2 0 1 0 0-.01M18 20a2 2 0 1 0 0-.01M12 5a2 2 0 1 0 0-.01",
};
export const Icon = ({ n, size = 18 }) => (
  <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth={n === "more" ? 3 : 1.8}
       strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={P[n]} /></svg>
);

export const K = v => (v == null ? "–" : new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(v));

export const Panel = ({ title, sub, span = "s6", right, children }) => (
  <section className={"bx-card " + span}>
    <header className="bx-ch"><div><h3>{title}</h3>{sub && <small>{sub}</small>}</div>{right}</header>
    {children}
  </section>
);

// Dark ring gauge (reference "growth rate" dial). value is a 0..1 share reported by the API.
export function Ring({ value, sub }) {
  const R = 38, C = 2 * Math.PI * R, v = Math.max(0, Math.min(1, value || 0));
  return (
    <svg viewBox="0 0 100 100" width="100" height="100" className="bx-ring" role="img" aria-label={`${sub} ${(v * 100).toFixed(1)} percent`}>
      <circle cx="50" cy="50" r="49" style={{ fill: "var(--text-primary)" }} />
      <circle cx="50" cy="50" r={R} fill="none" strokeWidth="7" style={{ stroke: "color-mix(in srgb, var(--page-bg) 22%, transparent)" }} />
      <circle cx="50" cy="50" r={R} fill="none" strokeWidth="7" strokeLinecap="round" strokeDasharray={`${Math.max(0.5, v * C)} ${C}`}
              transform="rotate(-90 50 50)" style={{ stroke: "var(--brand-orange)" }} />
      <text x="50" y="52" textAnchor="middle" fontSize="14" fontWeight="700" style={{ fill: "var(--page-bg)" }}>{(v * 100).toFixed(1)}%</text>
      <text x="50" y="66" textAnchor="middle" fontSize="12" style={{ fill: "var(--page-bg)", opacity: 0.75 }}>{sub}</text>
    </svg>
  );
}

// Nested circles sized by value, all resting on the bottom edge (reference "annual profits").
export function Concentric({ parts }) {
  const items = parts.filter(p => p.value > 0).sort((a, b) => b.value - a.value), max = items[0]?.value || 1;
  if (!items.length) return <p className="muted">No demand in this schedule.</p>;
  const rs = items.map(p => Math.max(26, 112 * Math.sqrt(p.value / max)));
  return (
    <svg viewBox="0 0 240 232" width="240" height="232" className="bx-conc" role="img" aria-label={items.map(p => `${p.label} ${N(p.value)}`).join(", ")}>
      {items.map((p, i) => {
        const r = rs[i], last = i === items.length - 1, cy = 230 - r;
        return (
          <g key={p.label}>
            <circle cx="120" cy={cy} r={r} style={{ fill: last ? "var(--brand-purple)" : `color-mix(in srgb, var(--brand-purple) ${10 + i * 9}%, var(--surface-hi))` }} />
            <text x="120" y={last ? cy + 5 : cy - r + 20} textAnchor="middle" fontSize="13" fontWeight="700"
                  style={{ fill: last ? "var(--on-accent)" : "var(--brand-purple)" }}>{K(p.value)}</text>
          </g>
        );
      })}
    </svg>
  );
}

export function Sparkline({ values }) {
  if (values.length < 2) return <p className="muted">Not enough points.</p>;
  const w = 300, h = 64, max = Math.max(...values), min = Math.min(...values), span = max - min || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * w},${h - 6 - ((v - min) / span) * (h - 12)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="bx-spark" role="img" aria-label="trend">
      <polyline points={pts} fill="none" strokeWidth="2" vectorEffect="non-scaling-stroke" style={{ stroke: "var(--brand-orange)" }} />
    </svg>
  );
}

export const DotMatrix = ({ filled, total = 24 }) => (
  <div className="bx-dots" role="img" aria-label={`${filled} of ${total}`}>
    {Array.from({ length: total }, (_, i) => <i key={i} className={i < filled ? "on" : ""} />)}
  </div>
);

// bars: [{value, hot?, title}] ; heights relative to the largest
export function MiniBars({ bars, label }) {
  const max = Math.max(...bars.map(b => b.value), 1e-9);
  return (
    <div className="bx-bars" role="img" aria-label={label}>
      {bars.map((b, i) => <span key={i} title={b.title} className={b.hot ? "hot" : ""} style={{ height: `${Math.max(6, (b.value / max) * 100)}%` }} />)}
    </div>
  );
}

// rows: [{label, value, display?, color?}] ; bar length relative to the largest value
export function BarList({ rows, empty = "Nothing to show.", scale }) {     // scale: fixed maximum (e.g. 1 for shares); default = largest row
  if (!rows.length) return <p className="muted">{empty}</p>;
  const max = scale || Math.max(...rows.map(r => r.value), 1e-9);
  return (
    <ul className="barlist">
      {rows.map(r => (
        <li key={r.label}>
          <span className="bl-l" title={r.label}>{r.label}</span>
          <span className="bl-t" role="img" aria-label={`${r.label} ${r.display ?? N(r.value)}`}><i style={{ width: `${Math.min(100, (r.value / max) * 100)}%`, background: r.color || "var(--chart-c1)" }} /></span>
          <b className="bl-v">{r.display ?? N(r.value)}</b>
        </li>
      ))}
    </ul>
  );
}

// Capacity heat map: line x week, colour intensity = utilisation reported by the API
export function Heat({ gaps }) {
  const rows = gaps.by_line_week, weeks = [...new Set(rows.map(r => r.week))], lines = [...new Set(rows.map(r => r.line_id))];
  return (
    <div className="heat" style={{ gridTemplateColumns: `90px repeat(${lines.length}, 1fr)` }} role="table" aria-label="Utilisation by line and week">
      <span />{lines.map(l => <b key={l} className="heat-h" role="columnheader">{l}</b>)}
      {weeks.map(w => [
        <b key={w} className="heat-w" role="rowheader">{w}</b>,
        ...lines.map(l => {
          const r = rows.find(x => x.week === w && x.line_id === l), u = r?.utilisation ?? 0, p = Math.round(Math.min(1, u) * 100);
          return (
            <span key={w + l} role="cell" className="heat-c" aria-label={`${l} ${w} ${p} percent`} title={`${N(r?.used_min)} / ${N(r?.available_min)} min`}
              style={{ background: `color-mix(in srgb, ${u >= 0.95 ? "var(--depth-0)" : "var(--brand-blue)"} ${p}%, transparent)` }}>{p}%</span>
          );
        }),
      ])}
    </div>
  );
}

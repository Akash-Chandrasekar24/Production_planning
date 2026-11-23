import { useState } from "react";
import { N } from "./ui.jsx";
import { K } from "./widgets.jsx";

// Dashboard charts. Pure presentation: every number comes from the API; colours are theme tokens only. Each chart has a text
// alternative (aria-label) and its values are readable without hovering.

// columns per day for this period, with a tick for the same day of the previous period
export function Columns({ points, fmt = N }) {
  const [hi, setHi] = useState(null);
  const vals = points.flatMap(p => [p.v, p.p ?? 0]);
  const top = Math.max(...vals, 1), low = Math.min(...points.map(p => p.v)) * 0.92;
  const h = v => `${Math.max(4, ((v - low) / (top - low || 1)) * 100)}%`;
  const cur = hi == null ? null : points[hi];
  return (
    <div className="dc-cols" role="img" aria-label={`Daily values, ${points.length} days, from ${fmt(Math.min(...points.map(p => p.v)))} to ${fmt(Math.max(...points.map(p => p.v)))}`}>
      <div className="dc-cols-tip" aria-hidden="true">{cur ? <><b>{cur.label}</b> {fmt(cur.v)} this period{cur.p != null && <> · {fmt(cur.p)} previous</>}</> : <span className="muted">Point at a day to see it</span>}</div>
      <div className="dc-cols-plot" onMouseLeave={() => setHi(null)}>
        {points.map((p, i) => (
          <span key={i} className={"dc-col" + (hi === i ? " on" : "")} onMouseEnter={() => setHi(i)}>
            <i className="v" style={{ height: h(p.v) }} />
            {p.p != null && <i className="p" style={{ bottom: h(p.p) }} />}
          </span>
        ))}
      </div>
      <div className="dc-cols-x" aria-hidden="true"><span>{points[0]?.label}</span><span>{points[Math.floor(points.length / 2)]?.label}</span><span>{points[points.length - 1]?.label}</span></div>
    </div>
  );
}

// a dot per row on a shared scale (min..max of the rows), so small differences show; rows can be picked (selected / onSelect)
export function Lollipop({ rows, fmt = K, best, selected, onSelect, domain, note }) {
  const v = rows.map(r => r.value), hi = Math.max(...v);
  const [lo, up] = domain || [Math.min(...v), hi], span = up - lo || 1;
  const top = best ?? rows[v.indexOf(hi)]?.label;
  return (
    <ul className={"dc-lolli" + (onSelect ? " pick" : "")} aria-label={rows.map(r => `${r.label} ${fmt(r.value)}`).join(", ")}>
      {rows.map(r => {
        const x = 8 + ((r.value - lo) / span) * 84;
        const body = (<>
          <span className="l">{r.label}</span>
          <span className="t"><i className="stick" style={{ width: `${x}%` }} /><i className="dot" style={{ left: `${x}%` }} /></span>
          <b>{fmt(r.value)}{note && <small className="muted"> {note(r.value)}</small>}</b>
        </>);
        return (
          <li key={r.label} className={(r.label === top ? "top" : "") + (r.label === selected ? " sel" : "")}>
            {onSelect ? <button onClick={() => onSelect(r.label)} aria-pressed={r.label === selected}>{body}</button> : body}
          </li>
        );
      })}
    </ul>
  );
}

// donut with a legend; parts: [{label, value, color}]
export function Donut({ parts, center, sub, fmt = N, size = 132, pie = false }) {
  const total = parts.reduce((t, p) => t + p.value, 0) || 1, r = pie ? 24 : 42, C = 2 * Math.PI * r, sw = pie ? 48 : 12;   // a pie is a ring as thick as its radius
  let at = 0;
  return (
    <div className="dc-donut">
      <svg viewBox="0 0 100 100" width={size} height={size} role="img" aria-label={parts.map(p => `${p.label} ${Math.round((100 * p.value) / total)} percent`).join(", ")}>
        {!pie && <circle cx="50" cy="50" r={r} className="dc-track" />}
        {parts.map(p => {
          const len = (p.value / total) * C, el = <circle key={p.label} cx="50" cy="50" r={r} fill="none" strokeWidth={sw} stroke={p.color}
            strokeDasharray={`${Math.max(0, len - (pie ? 0.3 : 1.2))} ${C}`} strokeDashoffset={-at} transform="rotate(-90 50 50)" />;
          at += len;
          return el;
        })}
        <text x="50" y="49" textAnchor="middle" className="dc-c-v">{center}</text>
        {sub && <text x="50" y="62" textAnchor="middle" className="dc-c-s">{sub}</text>}
      </svg>
      <ul className="dc-legend">{parts.map(p => (
        <li key={p.label}><i style={{ background: p.color }} /><span>{p.label}</span><b>{fmt(p.value)}</b><small className="muted">{Math.round((100 * p.value) / total)}%</small></li>))}</ul>
    </div>
  );
}

// one ring per item (0..100 %), tone by threshold
export function Ring({ pct, label, sub, size = 92, prev }) {
  const v = Math.max(0, Math.min(100, pct ?? 0)), r = 40, C = 2 * Math.PI * r;
  const tone = v >= 95 ? "ok" : v >= 80 ? "warn" : "bad";
  const pa = prev == null ? null : (Math.max(0, Math.min(100, prev)) / 100) * 360 - 90;
  return (
    <figure className={"dc-ring t-" + tone}>
      <svg viewBox="0 0 100 100" width={size} height={size} role="img" aria-label={`${label}: ${pct ?? "no"} percent`}>
        <circle cx="50" cy="50" r={r} className="dc-track" strokeWidth="9" />
        <circle cx="50" cy="50" r={r} fill="none" strokeWidth="9" strokeLinecap="round" className="dc-ring-v" strokeDasharray={`${(v / 100) * C} ${C}`} transform="rotate(-90 50 50)" />
        {pa != null && <line x1={50 + 31 * Math.cos((pa * Math.PI) / 180)} y1={50 + 31 * Math.sin((pa * Math.PI) / 180)} x2={50 + 49 * Math.cos((pa * Math.PI) / 180)}
          y2={50 + 49 * Math.sin((pa * Math.PI) / 180)} className="dc-ring-prev" />}
        <text x="50" y="55" textAnchor="middle" className="dc-c-v">{pct == null ? "–" : `${pct}%`}</text>
      </svg>
      <figcaption><b>{label}</b>{sub && <small className="muted">{sub}</small>}</figcaption>
    </figure>
  );
}

// 100 squares, coloured by each part's share
export function Waffle({ parts }) {
  const total = parts.reduce((t, p) => t + p.value, 0) || 1;
  let n = parts.map(p => Math.round((100 * p.value) / total));
  const diff = 100 - n.reduce((a, b) => a + b, 0);
  n[n.indexOf(Math.max(...n))] += diff;                            // the squares always add up to 100
  const cells = parts.flatMap((p, i) => Array.from({ length: Math.max(0, n[i]) }, () => p.color));
  return (
    <div className="dc-waffle" role="img" aria-label={parts.map((p, i) => `${p.label} ${n[i]} percent`).join(", ")}>
      {cells.map((c, i) => <i key={i} style={{ background: c, "--i": i }} />)}
    </div>
  );
}

// two values per row on a shared scale, joined by a line
export function Dumbbell({ rows, aLabel, bLabel, fmt = K }) {
  const top = Math.max(...rows.flatMap(r => [r.a, r.b]), 1);
  return (
    <div className="dc-dumb">
      <ul className="dc-dumb-key" aria-hidden="true"><li><i className="a" />{aLabel}</li><li><i className="b" />{bLabel}</li></ul>
      <ul className="dc-dumb-rows">
        {rows.map(r => {
          const xa = (r.a / top) * 100, xb = (r.b / top) * 100;
          return (
            <li key={r.label} aria-label={`${r.label}: ${aLabel} ${fmt(r.a)}, ${bLabel} ${fmt(r.b)}`}>
              <span className="l"><b>{r.label}</b>{r.tag && <span className="dc-tag">{r.tag}</span>}</span>
              <span className="t">
                <i className="seg" style={{ left: `${Math.min(xa, xb)}%`, width: `${Math.abs(xb - xa)}%` }} />
                <i className="dot a" style={{ left: `${xa}%` }} title={`${aLabel} ${fmt(r.a)}`} />
                <i className="dot b" style={{ left: `${xb}%` }} title={`${bLabel} ${fmt(r.b)}`} />
              </span>
              <small className="muted">{fmt(r.a)} · {fmt(r.b)}</small>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// a bar for what there is, a marker for what there should be at least
export function Bullet({ rows, fmt = K }) {
  const top = Math.max(...rows.flatMap(r => [r.value, r.mark]), 1);
  return (
    <ul className="dc-bullet">
      {rows.map(r => (
        <li key={r.label} className={r.value < r.mark ? "under" : ""} aria-label={`${r.label}: ${fmt(r.value)} usable, safety stock ${fmt(r.mark)}`}>
          <span className="l">{r.label}</span>
          <span className="t"><i className="v" style={{ width: `${(r.value / top) * 100}%` }} /><i className="m" style={{ left: `${(r.mark / top) * 100}%` }} /></span>
          <small>{fmt(r.value)} <span className="muted">/ {fmt(r.mark)}</span></small>
        </li>
      ))}
    </ul>
  );
}

import { N } from "./ui.jsx";
import { Icon } from "./widgets.jsx";

// The final plan as pictures first: one small chart per part, built from the same figures as its table (the table is a click away).
const inr = v => (v == null ? "–" : v >= 1e7 ? `₹${(v / 1e7).toFixed(2)} Cr` : v >= 1e5 ? `₹${(v / 1e5).toFixed(1)} L` : `₹${N(v)}`);
const dm = d => new Date(String(d).slice(0, 10) + "T00:00:00").toLocaleDateString("en", { day: "numeric", month: "short" });
const hm = t => new Date(t).toLocaleTimeString("en", { hour: "2-digit", minute: "2-digit", hour12: false });
const pct = (a, b) => (b ? Math.max(0, Math.min(100, (100 * a) / b)) : 0);
const K = v => (v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${Math.round(v / 1e3)}K` : N(v));

// which colour is which product (shared by the charts)
export function Legend({ products, color, title, extra }) {
  return (
    <div className="pv-legend" aria-label={title || "Legend"}>
      {title && <small>{title}</small>}
      {products.map(p => <span key={p}><i style={{ background: color(p) }} />{p}</span>)}
      {extra}
    </div>
  );
}

// 1. what each product is planned at, against what customers ordered; the mix across products
export function StrategyViz({ rows, color }) {
  const max = Math.max(1, ...rows.flatMap(r => [r.demand, r.planned]));
  return (
    <div className="pv">
      <div className="pv-mix" role="img" aria-label="Product mix">
        {rows.map(r => <span key={r.product_id} style={{ width: `${r.mix_pct}%`, background: color(r.product_id) }} title={`${r.product_id}: ${r.mix_pct}% of the plan`}>{r.mix_pct >= 12 ? `${Math.round(r.mix_pct)}%` : ""}</span>)}
      </div>
      <Legend title="Bars:" products={[]} color={color}
        extra={<span className="pv-leg-key left"><i className="ghost" />Ordered<i className="solid" />Planned</span>} />
      {rows.map(r => (
        <div key={r.product_id} className="pv-pair">
          <div className="pv-lab"><i style={{ background: color(r.product_id) }} /><b>{r.product_id}</b><em className={`pv-prio ${String(r.priority).toLowerCase()}`}>{r.priority}</em></div>
          <div className="pv-bars">
            <span className="pv-barrow"><span className="pv-bar ghost" style={{ width: `${pct(r.demand, max)}%` }} /><small>Ordered {K(r.demand)}</small></span>
            <span className="pv-barrow"><span className="pv-bar" style={{ width: `${pct(r.planned, max)}%`, background: color(r.product_id) }} /><small>Planned {K(r.planned)}</small></span>
          </div>
        </div>))}
    </div>
  );
}

// 2. the weekly plan as stacked columns (one colour per product)
export function MpsViz({ mps, color }) {
  const cols = [...mps.weeks.map((w, i) => ({ key: w, label: `W${i + 1}`, sub: dm(w) })), { key: "later", label: "Later", sub: "" }];
  const tot = c => mps.rows.reduce((a, r) => a + (r[c.key] || 0), 0);
  const max = Math.max(1, ...cols.map(tot));
  return (<>
    <div className="pv-cols" role="img" aria-label="Units planned each week, by product">
      {cols.map(c => (
        <div key={c.key} className="pv-col">
          <small className="pv-top">{tot(c) ? K(tot(c)) : ""}</small>
          <div className="pv-stack" style={{ height: `${pct(tot(c), max)}%` }}>
            {mps.rows.map(r => r[c.key] ? <span key={r.product_id} style={{ flex: r[c.key], background: color(r.product_id) }} title={`${r.product_id}: ${N(r[c.key])} units`} /> : null)}
          </div>
          <b>{c.label}</b><small>{c.sub}</small>
        </div>))}
    </div>
    <Legend title="Units by product:" products={mps.rows.map(r => r.product_id)} color={color} />
  </>);
}

// 3. one day on each line, 00:00 to 24:00
export function DayViz({ daily, color }) {
  const lines = [...new Set(daily.rows.map(r => r.line_id))].sort((a, b) => (a.slice(-2) + a).localeCompare(b.slice(-2) + b));
  // the day itself, 00:00 to 24:00: a job reaching into the night before or after counts only its minutes on this day
  const d0 = new Date(String(daily.day).slice(0, 10) + "T00:00:00").getTime(), d1 = d0 + 86400000;
  const clip = r => [Math.max(d0, new Date(r.start).getTime()), Math.min(d1, new Date(r.finish).getTime())];
  const at = t => (100 * (t - d0)) / 86400000;
  const mins = r => { const [a, z] = clip(r); return Math.max(0, Math.round((z - a) / 60000)); };
  const products = [...new Set(daily.rows.map(r => r.product_id))];
  return (<>
    <div className="pv-day">
      <div className="pv-hours">{[0, 6, 12, 18, 24].map(h => <span key={h} className={h === 24 ? "end" : h === 0 ? "start" : ""} style={{ left: `${h / 0.24}%` }}>{String(h).padStart(2, "0")}:00</span>)}</div>
      {lines.map(l => {
        const jobs = daily.rows.filter(r => r.line_id === l && mins(r) > 0);
        const busy = Math.min(1440, jobs.reduce((t, r) => t + mins(r), 0));
        return (
          <div key={l} className="pv-dayrow"><span className="pv-dayname"><b>{l}</b><small>{N(busy)} min busy</small></span>
            <div className="pv-daytrack">{jobs.map((r, i) => {
              const [t0, t1] = clip(r), a = at(t0), w = Math.max(0.6, at(t1) - a), m = mins(r);
              return <i key={i} style={{ left: `${a}%`, width: `${w}%`, background: color(r.product_id) }}
                title={`${r.order_id} · ${r.product_id} · ${N(r.qty)} units · ${hm(r.start)}–${hm(r.finish)} · ${m} min on this day`}>
                {m >= 240 && <span>{m} min</span>}</i>;
            })}</div>
          </div>);
      })}
    </div>
    <Legend title="Jobs by product:" products={products} color={color} extra={<span className="pv-leg-key"><i className="free" />Line free</span>} />
  </>);
}

// 4. each material: what stock covers and what must be bought (and when it arrives)
export function MrpViz({ rows }) {
  return (
    <div className="pv">
      {rows.slice(0, 6).map(r => (
        <div key={r.material_id} className="pv-mat">
          <div className="pv-lab"><b>{r.material_id}</b>{r.shortage ? <em className="pv-bad">Buy {K(r.shortage)} · arrives {dm(r.arrives)}</em> : <em className="pv-ok">Covered by stock</em>}</div>
          <div className="pv-split" title={`Needed ${N(r.required)} · in stock ${N(r.available)} · short ${N(r.shortage)}`}>
            <span className="ok" style={{ width: `${pct(r.available, r.required)}%` }} /><span className="bad" style={{ width: `${pct(r.shortage, r.required)}%` }} />
          </div>
        </div>))}
      <div className="pv-key"><span><i className="ok" />In stock</span><span><i className="bad" />To buy</span></div>
    </div>
  );
}

// 5. how full each line is
export function MachinesViz({ rows }) {
  return (
    <div className="pv">
      {rows.map(r => {
        const u = r.utilisation ?? 0, tone = u >= 95 ? "bad" : u >= 85 ? "warn" : "ok";
        return (
          <div key={r.line_id} className="pv-gauge">
            <div className="pv-lab"><b>{r.line_id}</b><small>{r.plant}</small><strong className={"pv-" + tone}>{u}%</strong></div>
            <div className="pv-meter" title={`${N(r.required_h)} of ${N(r.available_h)} hours booked`}><span className={tone} style={{ width: `${Math.min(100, u)}%` }} /><i style={{ left: "85%" }} /></div>
            <small className="muted">{N(r.required_h)} of {N(r.available_h)} h booked</small>
          </div>);
      })}
      <div className="pv-key"><span><i className="ok" />Room to spare (under 85%)</span><span><i className="warn" />Nearly full</span><span><i className="bad" />Full (95%+)</span></div>
    </div>
  );
}

// 6. people needed against people available, each shift
export function WorkforceViz({ rows }) {
  const max = Math.max(1, ...rows.flatMap(w => [w.needed, w.available]));
  return (
    <div className="pv">
      {rows.map(w => (
        <div key={w.shift} className="pv-pair">
          <div className="pv-lab"><b>{w.shift}</b>{w.short_days ? <em className="pv-bad">{w.short_days} days short</em> : <em className="pv-ok">Fully staffed</em>}</div>
          <div className="pv-bars">
            <span className="pv-barrow"><span className="pv-bar ghost" style={{ width: `${pct(w.available, max)}%` }} /><small>Available {w.available}</small></span>
            <span className="pv-barrow"><span className={"pv-bar " + (w.needed > w.available ? "bad" : "brand")} style={{ width: `${pct(w.needed, max)}%` }} /><small>Needed {w.needed}</small></span>
          </div>
        </div>))}
    </div>
  );
}

// 8. planned services: how many, how long, and the next one on each line
export function MaintViz({ m }) {
  const next = {};
  for (const x of m.items) if (!next[x.line_id]) next[x.line_id] = x;
  return (
    <div className="pv">
      <div className="pv-nums"><div><strong>{m.count}</strong><small>planned services</small></div><div><strong>{m.hours} h</strong><small>line time blocked</small></div></div>
      <ul className="pv-chips">{Object.values(next).map(x => <li key={x.line_id}><Icon n="gear" size={12} /><b>{x.line_id}</b> next {dm(x.start)}, {x.hours} h</li>)}</ul>
    </div>
  );
}

// 9. quality: final-assembly yield as a ring, rejects and rework
export function QualityViz({ q }) {
  const y = q.fal_yield_pct ?? 0, c = 2 * Math.PI * 34;
  return (
    <div className="pv pv-q">
      <svg viewBox="0 0 84 84" className="pv-ring" role="img" aria-label={`Final-assembly yield ${y}%`}>
        <circle cx="42" cy="42" r="34" className="bg" /><circle cx="42" cy="42" r="34" className="fg" strokeDasharray={`${(c * y) / 100} ${c}`} transform="rotate(-90 42 42)" />
        <text x="42" y="40" textAnchor="middle">{y}%</text><text x="42" y="54" textAnchor="middle" className="s">yield</text>
      </svg>
      <div className="pv-qstats">
        <div><small>Expected rejects</small><b>{q.reject_pct}%</b><em>about {K(q.expected_rejects)} units</em></div>
        {q.rework_pct != null && <div><small>Rework</small><b>{q.rework_pct}%</b><em>of units made</em></div>}
        <div><small>Good output target</small><b>{K(q.good_target)}</b><em>units</em></div>
      </div>
    </div>
  );
}

// 10. where the money goes
export function CostViz({ cost }) {
  if (!cost?.available) return <p className="muted">No cost rates on record.</p>;
  const parts = Object.entries(cost.parts).filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]);
  const label = k => k.replace(/_/g, " ").replace(/^./, c => c.toUpperCase());
  return (
    <div className="pv">
      <div className="pv-nums one"><div><strong>{inr(cost.total_inr)}</strong><small>total estimated cost</small></div></div>
      {parts.map(([k, v]) => (
        <div key={k} className="pv-cost"><div className="pv-lab"><span>{label(k)}</span><b>{inr(v)}</b><small>{Math.round(pct(v, cost.total_inr))}%</small></div>
          <div className="pv-meter"><span className="brand" style={{ width: `${Math.max(1, pct(v, cost.total_inr))}%` }} /></div></div>))}
    </div>
  );
}

// 11. the risks as cards (the mitigation is in the details)
export function RiskViz({ rows }) {
  const tone = r => (/late|shortage|short/i.test(r.risk) ? "bad" : /capacity|labour/i.test(r.risk) ? "warn" : "info");
  return (
    <div className="pv-risks">
      {rows.map((r, i) => <div key={i} className={"pv-risk t-" + tone(r)}><Icon n="alert" size={14} /><div><b>{r.risk}</b><small>{r.impact}</small></div></div>)}
    </div>
  );
}

// Business impact: the plan in money and customers
const cr = v => (v == null ? "–" : Math.abs(v) >= 1e7 ? `₹${(v / 1e7).toFixed(1)} Cr` : Math.abs(v) >= 1e5 ? `₹${(v / 1e5).toFixed(1)} L` : `₹${N(v)}`);
export function BusinessViz({ b }) {
  if (!b) return <p className="muted">Order values aren't available for this plan.</p>;
  const wmax = Math.max(1, ...b.weeks.map(w => w.on_time + w.late));
  const cmax = Math.max(1, ...b.customers.map(c => c.value));
  const tiles = [
    ["Order value in this plan", cr(b.order_value), "customer orders", "brand"],
    ["Delivered on time", cr(b.on_time_value), `${b.on_time_value_pct ?? "–"}% of order value`, "ok"],
    ["Value at risk", cr(b.at_risk_value), "orders that will be late", "bad"],
    ["Late-penalty exposure", cr(b.penalty), "if those orders stay late", "warn"],
    ["Estimated gross margin", cr(b.margin), b.margin_pct != null ? `${b.margin_pct}% of order value` : "", b.margin != null && b.margin < 0 ? "bad" : "ok"],
  ];
  return (
    <div className="bz">
      <div className="bz-tiles">{tiles.map(([l, v, s2, tone]) => (
        <div key={l} className={"bz-tile t-" + tone}><small>{l}</small><strong>{v}</strong><em>{s2}</em></div>))}</div>
      <div className="bz-charts">
        <div className="bz-chart">
          <h4>Order value by delivery week</h4>
          <div className="bz-cols" role="img" aria-label="Order value due each week: on time and late">
            {b.weeks.map(w => (
              <div key={w.label} className="bz-col" title={`${w.label}${w.sub ? ` (${w.sub})` : ""}: ${cr(w.on_time)} on time, ${cr(w.late)} late`}>
                <small className="bz-top">{cr(w.on_time + w.late)}</small>
                <div className="bz-stack" style={{ height: `${pct(w.on_time + w.late, wmax)}%` }}>
                  {w.late > 0 && <span className="late" style={{ flex: w.late }} />}{w.on_time > 0 && <span className="ok" style={{ flex: w.on_time }} />}
                </div>
                <b>{w.label}</b><small>{w.sub || " "}</small>
              </div>))}
          </div>
          <div className="pv-key"><span><i className="ok" />Delivered on time</span><span><i className="bad" />Late (value at risk)</span></div>
        </div>
        <div className="bz-chart">
          <h4>Customers at risk</h4>
          {b.customers.map(c => (
            <div key={c.customer} className="bz-cust">
              <div className="pv-lab"><b>{c.customer}</b><small>{c.late_orders} of {c.orders} orders late</small><em className="pv-bad">{cr(c.penalty)} penalty</em></div>
              <div className="pv-split" title={`${cr(c.value)} ordered · ${cr(c.late_value)} late`} style={{ width: `${Math.max(8, pct(c.value, cmax))}%` }}>
                <span className="ok" style={{ width: `${pct(c.value - c.late_value, c.value)}%` }} /><span className="bad" style={{ width: `${pct(c.late_value, c.value)}%` }} />
              </div>
              <small className="muted">{cr(c.late_value)} of {cr(c.value)} at risk</small>
            </div>))}
          <div className="pv-key"><span><i className="ok" />On time</span><span><i className="bad" />At risk</span><span className="muted">bar length = order value</span></div>
        </div>
      </div>
    </div>
  );
}

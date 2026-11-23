import { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import { N } from "./ui.jsx";
import { Icon } from "./widgets.jsx";

// Processing order: which order is made first, which next, and when the new orders run. One row per customer order, in the order
// it starts, drawn on the same kind of timeline as the line timeline: circuit boards first, then final assembly.
const PLANT = { "IN-TN-01": "Hosur", "IN-KA-01": "Bengaluru" };
const STAGE = { SMT: "Circuit boards", FAL: "Final assembly" };
const DAY = 864e5;
const dayLabel = t => new Date(t).toLocaleDateString("en", { day: "numeric", month: "short" });
const time = t => new Date(t).toLocaleString("en", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const FIRST = 12;

export default function RunOrder({ rows, color }) {
  const [fresh, setFresh] = useState(new Set());           // orders received through uploads: the "new" orders
  const [pick, setPick] = useState(null);
  const [all, setAll] = useState(false);
  useEffect(() => { api("/api/orders/intake?limit=500").then(l => setFresh(new Set(l.map(x => x.customer_order_id)))).catch(() => {}); }, []);

  const jobs = useMemo(() => {
    const by = new Map();
    for (const r of rows) {
      const j = by.get(r.customer_order_id) || { id: r.customer_order_id, product: r.product_id, plant: r.plant_id, priority: r.priority, urgent: r.urgent,
        late: false, qty: 0, start: r.start, finish: r.finish, steps: [], waits: false, forecast: r.customer_order_id.startsWith("FC-") };
      if (r.start < j.start) j.start = r.start;
      if (r.finish > j.finish) j.finish = r.finish;
      if (r.stage === "FAL") j.qty += r.qty;
      j.late = j.late || r.status === "LATE_RISK";
      j.waits = j.waits || (r.reason_codes || []).some(c => c.startsWith("WAITS_FOR_PURCHASE"));
      j.steps.push({ stage: r.stage, line: r.line_id, start: r.start, finish: r.finish });
      by.set(r.customer_order_id, j);
    }
    return [...by.values()].sort((a, b) => (a.start < b.start ? -1 : a.start > b.start ? 1 : 0))
      .map((j, i) => ({ ...j, n: i + 1, isNew: fresh.has(j.id), steps: j.steps.sort((a, b) => (a.start < b.start ? -1 : 1)) }));
  }, [rows, fresh]);
  if (!jobs.length) return null;

  const shown = all ? jobs : jobs.slice(0, FIRST);         // the axis spans the rows on show, so the first few aren't squeezed
  const t0 = new Date(new Date(shown[0].start).toDateString()).getTime();
  const t1 = Math.max(...shown.map(j => +new Date(j.finish))), span = Math.max(1, t1 - t0);
  const X = t => ((+new Date(t) - t0) / span) * 100;
  const days = [];
  for (let d = t0; d < t1; d += DAY) days.push(d);
  const every = Math.max(1, Math.ceil(days.length / 8));                // at most ~8 date labels on the axis
  const news = jobs.filter(j => j.isNew);
  const sel = jobs.find(j => j.id === pick) || null;
  const route = j => j.steps.map(s => s.line).join(" → ");
  const tags = j => (
    <span className="ro-tags">
      {j.isNew && <span className="ro-tag new">New order</span>}
      {j.forecast && <span className="ro-tag hi">Forecast (stock)</span>}
      {j.waits && <span className="ro-tag late">Waits for parts</span>}
      {(j.urgent || j.priority === "HIGH") && <span className="ro-tag hi">{j.urgent ? "Urgent" : "High priority"}</span>}
      {j.late && <span className="ro-tag late">May be late</span>}
    </span>);

  return (
    <section className="sp-card ro" aria-label="Processing order">
      <header className="ltl-h">
        <div><h3>Processing order</h3>
          <small className="sp-sub-t">{jobs.length} orders, top to bottom in the order they are made · each bar is circuit boards, then final assembly</small></div>
        <ul className="sp-legend ltl-legend">
          {[...new Set(jobs.map(j => j.product))].sort().map(p => <li key={p}><i className="purple" style={{ background: color(p) }} />{p}</li>)}
          <li><i className="ro-key-new" />New order</li><li><i className="ltl-late-key" />Late</li></ul>
      </header>

      <p className="ro-say"><Icon n="info" size={14} />
        {news.length
          ? <> {news.length === 1 ? "The new order is" : `The ${news.length} new orders are`} number{news.length > 1 ? "s" : ""}{" "}
            {news.slice(0, 6).map((j, i) => <span key={j.id}>{i ? (i === Math.min(news.length, 6) - 1 ? " and " : ", ") : ""}<b>{j.n}</b></span>)}{news.length > 6 ? "…" : ""} of {jobs.length}
            , starting {time(news[0].start)}.</>
          : <> First up: <b>{jobs[0].id}</b>, {time(jobs[0].start)}.</>}
      </p>

      <div className={"ltl-plot ro-plot" + (all ? " all" : "")}>
        <div className="ltl-axis"><div /><div className="ltl-axis-t">
          {days.filter((_, i) => i % every === 0).map(d => <span key={d} style={{ left: `${X(d)}%`, transform: `translateX(${X(d) < 4 ? 0 : X(d) > 94 ? -100 : -50}%)` }}>{dayLabel(d)}</span>)}
        </div></div>
        {shown.map(j => (
          <div key={j.id} className={"ltl-lane ro-lane" + (j.isNew ? " is-new" : "")}>
            <div className="ltl-lab" title={`${j.n}. ${j.id} · ${j.product} · ${N(j.qty)} units`}>
              <span className="ltl-lab-t"><em className="ro-num">{j.n}</em><b>{j.id}</b></span>
              <small>{j.product} · {N(j.qty)} units</small></div>
            <div className="ltl-track ro-track">
              {days.map((d, i) => i % 2 === 1 && <i key={d} className="ltl-day" style={{ left: `${X(d)}%`, width: `${X(d + DAY) - X(d)}%` }} />)}
              {j.steps.map((s, i) => (
                <button key={i} className={"ltl-bar" + (j.late ? " late" : "") + (j.isNew ? " ro-new" : "") + (pick === j.id ? " on" : "") + (s.stage === "SMT" ? " ro-smt" : "")}
                  style={{ left: `${X(s.start)}%`, width: `${Math.max(0.5, X(s.finish) - X(s.start))}%`, "--c": color(j.product) }}
                  aria-label={`${j.n}. ${j.id}, ${STAGE[s.stage] || s.stage} on ${s.line}, ${time(s.start)} to ${time(s.finish)}`}
                  aria-pressed={pick === j.id} onClick={() => setPick(p => (p === j.id ? null : j.id))} />))}
            </div>
          </div>))}
      </div>

      <div className="ro-pick" aria-live="polite">
        {sel ? <><em className="ro-num">{sel.n}</em>
          <span><b>{sel.id}</b> · {sel.product} · {N(sel.qty)} units · {PLANT[sel.plant] || sel.plant} · {route(sel)} · starts {time(sel.start)}, done {time(sel.finish)}</span>{tags(sel)}</>
          : <small className="muted">Select a bar to see that order.</small>}
      </div>
      {jobs.length > FIRST && <button className="lnk ro-more" aria-expanded={all} onClick={() => setAll(a => !a)}>{all ? `Show the first ${FIRST}` : `See all ${jobs.length} orders`}</button>}
    </section>
  );
}

import { useEffect, useState } from "react";
import { api } from "../api.js";
import { N } from "./ui.jsx";
import { Icon } from "./widgets.jsx";
import { BusinessViz, CostViz, DayViz, MachinesViz, MaintViz, MpsViz, MrpViz, QualityViz, RiskViz, StrategyViz, WorkforceViz } from "./PlanVisuals.jsx";

// The final production plan as one page, in the standard layout of a plant's production plan: header and approvals, executive summary,
// then the twelve parts and the final outcome. Every figure comes from the plan (GET /api/final-plan/:id).
const inr = v => (v == null ? "–" : v >= 1e7 ? `₹${(v / 1e7).toFixed(2)} Cr` : v >= 1e5 ? `₹${(v / 1e5).toFixed(1)} L` : `₹${N(v)}`);
const dmy = d => (d ? new Date(String(d).slice(0, 10) + "T00:00:00").toLocaleDateString("en", { day: "2-digit", month: "short", year: "numeric" }) : "–");
const dm = d => new Date(String(d).slice(0, 10) + "T00:00:00").toLocaleDateString("en", { day: "numeric", month: "short" });
const hm = t => new Date(t).toLocaleTimeString("en", { hour: "2-digit", minute: "2-digit", hour12: false });
const BADGE = { fully_approved: ["Fully approved production plan", "ok", "check"], approved: ["Approved by the production manager", "info", "check"],
  pending: ["Waiting for approval", "warn", "clock"], rejected: ["Rejected", "bad", "close"] };
const PRIO = { HIGH: ["High", "bad"], MEDIUM: ["Medium", "warn"], LOW: ["Low", "info"] };
const STATUS = { Overloaded: "bad", "High load": "warn", Available: "ok" };
const COLOR = ["var(--chart-c1)", "var(--brand-purple)", "var(--chart-c4)", "var(--brand-orange)", "var(--chart-c2)"];

function Sec({ n, title, icon, children, className = "", details }) {
  const [open, setOpen] = useState(false);
  return (
    <section className={"fp-sec " + className + (open ? " show-details" : "")}>
      <header><Icon n={icon} size={15} /><b>{n ? `${n}. ` : ""}{title}</b>
        {details && <button type="button" className="fp-more no-print" aria-expanded={open} onClick={() => setOpen(o => !o)}>{open ? "Chart" : "Details"}</button>}</header>
      <div className="fp-body">{details && open ? details : children}</div>
      {details && !open && <div className="fp-print-only">{details}</div>}
    </section>);
}
const Pill = ({ tone, children }) => <span className={"fp-pill t-" + tone}>{children}</span>;
const Tbl = ({ head, children, foot }) => (
  <div className="fp-tw"><table className="fp-t"><thead><tr>{head.map((h, i) => <th key={i} className={h[1] || ""}>{h[0]}{h[2] && <small>{h[2]}</small>}</th>)}</tr></thead>
    <tbody>{children}</tbody>{foot && <tfoot>{foot}</tfoot>}</table></div>);

// where the latest upload's orders are in this plan, in one sentence
function newSummary(w) {
  if (!w) return "";
  const parts = [];
  if (w.on_lines) parts.push(`${w.on_lines} on the lines (outlined in orange)`);
  if (w.covered) parts.push(`${w.covered} covered by production already under way, so no new line time is needed`);
  if (w.outsourced) parts.push(`${w.outsourced} outsourced to a contract manufacturer (the contract manufacturer rows)`);
  if (w.not_scheduled) parts.push(`${w.not_scheduled} waiting for parts, not yet scheduled`);
  if (w.not_in_plan) parts.push(`${w.not_in_plan} arrived after this plan was made: run the agent again`);
  return `${w.total} new order${w.total === 1 ? "" : "s"} from the latest upload: ${parts.join("; ")}.`;
}

// 7. Gantt (machine view): one row per line, its runs back to back coloured by product; idle stretches hatched with their reason;
// filters by plant and line; runs with new purchase orders outlined in the accent colour.
function Gantt({ g, color }) {
  const [plant, setPlant] = useState("all");
  const [line, setLine] = useState("all");
  const [onlyNew, setOnlyNew] = useState(false);
  const [view, setView] = useState("day");
  const plants = [...new Set(g.rows.map(r => r.plant))];
  const t0 = new Date(String(g.weeks[0].from).slice(0, 10) + "T00:00:00").getTime(), nDays = g.weeks.length * 7;
  const days = r => {                                   // the line's work grouped by day: units per product, new orders that day
    if (r.external) return r.runs;
    const by = {};
    for (const x of r.runs) {
      const d = Math.floor((new Date(x.start).getTime() - t0) / 86400000);
      if (d < 0 || d >= nDays) continue;
      const b = by[d] || (by[d] = { units: {}, new: false, orders: 0 });
      b.units[x.product_id] = (b.units[x.product_id] || 0) + x.units;
      b.new = b.new || x.new; b.orders += x.orders;
    }
    return Object.entries(by).map(([d, b]) => {
      const main = Object.entries(b.units).sort((p, q) => q[1] - p[1])[0][0];
      const start = new Date(t0 + d * 86400000);
      return { product_id: main, units: Object.values(b.units).reduce((a, v) => a + v, 0), orders: b.orders, new: b.new, day: true,
        mix: Object.entries(b.units).map(([p, u]) => `${p} ${N(u)}`).join(", "), start, finish: start,
        left: (100 * d) / nDays + 0.08, width: 100 / nDays - 0.16 };
    });
  };
  const lines = [...new Set(g.rows.filter(r => plant === "all" || r.plant === plant).map(r => r.line_id))];
  const rows = g.rows.filter(r => (plant === "all" || r.plant === plant) && (line === "all" || r.line_id === line));
  const chip = (on, label, onClick, key) => <button key={key} type="button" className={"gt-chip" + (on ? " on" : "")} aria-pressed={on} onClick={onClick}>{label}</button>;
  const products = [...new Set(g.rows.flatMap(r => r.runs.map(x => x.product_id)))];
  const when = t => `${dm(t)} ${hm(t)}`;
  const tip = (r, x) => x.day
    ? `${r.line_id} on ${dm(x.start)}: ${N(x.units)} units (${x.mix})${x.new ? " · includes new purchase orders" : ""}`
    : r.external
    ? `${x.product_id} made by the contract manufacturer, delivered week of ${dm(x.start)}: ${N(x.units)} units, ${x.orders} order${x.orders === 1 ? "" : "s"}${x.new_orders ? ` (${x.new_orders} new, ${N(x.new_units)} units)` : ""}`
    : `${x.product_id} on ${r.line_id}: ${N(x.units)} units, ${x.orders} order${x.orders === 1 ? "" : "s"} · ${when(x.start)} to ${when(x.finish)}${x.new ? " · includes new purchase orders" : ""}`;
  return (
    <div className={"gt" + (onlyNew ? " only-new" : "")}>
      <div className="gt-filters no-print">
        <div role="group" aria-label="Plant"><small>Plant</small>{chip(plant === "all", "All", () => { setPlant("all"); setLine("all"); }, "all")}
          {plants.map(p => chip(plant === p, p, () => { setPlant(p); setLine("all"); }, p))}</div>
        <div role="group" aria-label="Line"><small>Line</small>{chip(line === "all", "All", () => setLine("all"), "all")}
          {lines.map(l => chip(line === l, l, () => setLine(l), l))}</div>
        <div role="group" aria-label="View"><small>View</small>{chip(view === "day", "Day by day", () => setView("day"), "day")}{chip(view === "run", "Every run", () => setView("run"), "run")}</div>
        {(g.new_where?.on_lines || 0) + (g.new_where?.outsourced || 0) > 0 && <div>{chip(onlyNew, `Highlight new orders (${(g.new_where.on_lines || 0) + (g.new_where.outsourced || 0)})`, () => setOnlyNew(v => !v), "new")}</div>}
      </div>
      {g.new_orders > 0 && <p className="gt-newsum"><span className="gt-tag">New</span><span>{newSummary(g.new_where)}</span></p>}
      <div className="gt-scroll"><div className="gt-grid" role="table" aria-label="Production runs on each line, week by week" style={{ "--n": g.weeks.length }}>
        <div className="gt-row gt-head" role="row">
          <span role="columnheader">Line</span><span role="columnheader">Operation</span>
          <div className="gt-weeks">{g.weeks.map(w => <span key={w.label} role="columnheader"><b>{w.label}</b><small>{dm(w.from)}</small></span>)}</div>
        </div>
        {rows.length ? rows.map(r => (
          <div key={r.plant_id + r.line_id + r.stage} className={"gt-row" + (r.new ? " is-new" : "") + (r.external ? " is-ext" : "")} role="row">
            <span role="cell" className="gt-line"><b>{r.line_id}</b><small>{r.plant}</small></span>
            <span role="cell" className="gt-op">{r.stage}{r.new && <em className="gt-tag">New</em>}</span>
            <div role="cell" className="gt-track">
              {r.idle.map((x, i) => <s key={"i" + i} style={{ left: `${x.left}%`, width: `${x.width}%` }}
                title={`Maintenance window: line free ${x.days} days (${x.why}).${x.next_service ? ` Bring the ${dm(x.next_service.start)} service (${x.next_service.hours} h) forward into it.` : " Use it for planned maintenance."}`}>
                {x.width > 4 && <span>Maintenance</span>}</s>)}
              {(r.services || []).map((x, i) => <b key={"m" + i} className="gt-svc" style={{ left: `${x.left}%` }} title={`Planned service ${dm(x.start)} ${hm(x.start)}, ${x.hours} h`} />)}
              {(view === "day" ? days(r) : r.runs).map((x, i) => <i key={i} className={(x.new ? "new" : "") + (x.day ? " d" : "")} style={{ left: `${x.left}%`, width: `${x.width}%`, "--c": color(x.product_id) }} title={tip(r, x)} />)}
            </div>
          </div>)) : <p className="muted gt-empty">No runs for this choice.</p>}
      </div></div>
      <div className="gt-legend">{products.map(p => <span key={p}><i style={{ background: color(p) }} />{p}</span>)}
        {g.new_orders > 0 && <span className="new"><i />New purchase orders</span>}
        <span className="idle"><i />Maintenance window (line free)</span><span className="svc"><i />Planned service</span></div>
      {g.idle_parts?.length > 0 && <p className="gt-note"><Icon n="gear" size={14} /> The lines are free while they wait for {g.idle_parts.join(", ")}: the work left needs
        these parts. That time is a maintenance window: bring planned services forward into it. Plan B (express parts) or a faster supplier shortens it.</p>}
    </div>
  );
}

export default function FinalPlan({ sid }) {
  const [f, setF] = useState(null);
  useEffect(() => { let live = true; setF(null); api(`/api/final-plan/${sid}`).then(j => live && setF(j)).catch(() => live && setF(false)); return () => { live = false; }; }, [sid]);
  if (f === null) return <section className="sp-card" role="status"><p className="muted">Putting the production plan together…</p></section>;
  if (f === false) return <section className="sp-card"><p className="muted">The production plan isn't available right now.</p></section>;
  const k = f.kpis, b = BADGE[f.badge] || BADGE.pending, color = p => COLOR[f.strategy.findIndex(x => x.product_id === p) % COLOR.length];
  const total = key => f.strategy.reduce((a, r) => a + (r[key] || 0), 0);
  const busiest = [...(f.machines || [])].sort((p, q) => (q.utilisation ?? 0) - (p.utilisation ?? 0))[0];
  return (
    <div className="fp">
      <header className="fp-head">
        <span className="fp-logo"><Icon n="factory" size={34} /></span>
        <div className="fp-title"><h2>Final production plan – {f.name}</h2>
          <small>Production plan · Period: {dmy(f.period.from)} – {dmy(f.period.to)}</small></div>
        <div className="fp-appr">
          <span className={"fp-badge t-" + b[1]}><Icon n={b[2]} size={16} /> {b[0]}</span>
          <small>Prepared by: <b>{f.prepared_by}</b> · Approved by: <b>{f.approved_by.length ? f.approved_by.join("; ") : "not yet"}</b></small>
          <button className="sp-btn no-print" onClick={() => print()}><Icon n="download" size={14} /> Print or save as PDF</button>
        </div>
      </header>

      <section className="fp-sec fp-kpis"><header><Icon n="chart" size={15} /><b>Plan highlights (executive summary)</b></header>
        <div className="fp-kgrid">
          {[["target", "Units ordered", `${N(k.ordered_units ?? k.target)}`, "open customer orders"],
            ["check", "Units planned", `${N(k.planned)}`, "incl. work already in production"],
            ["calendar", "Units ready by their due date", k.ready_units != null ? N(k.ready_units) : "–", k.ready_pct != null ? `${k.ready_pct}% on time` : ""],
            ["gear", "Line time used", `${k.utilisation_pct}%`, busiest ? `busiest ${busiest.line_id} at ${Math.round(busiest.utilisation)}%` : "of available time"],
            ["users", "People per shift", k.manpower_per_shift != null ? `${N(k.manpower_per_shift)} needed` : "–", k.manpower_available != null ? `${N(k.manpower_available)} on the roster` : `${k.shifts} shifts a day`],
            ["box", "Estimated production cost", inr(k.cost_inr), "estimate"]].map(([ic, l, v, sub]) => (
            <div key={l} className="fp-k"><span className="fp-kic"><Icon n={ic} size={22} /></span><div><small>{l}</small><b>{v}</b>{sub && <em>{sub}</em>}</div></div>))}
        </div>
      </section>

      <div className="fp-row r1">
        <Sec title="Business impact" icon="trend" className="wide fp-biz" details={<>
          <Tbl head={[["Order"], ["Customer"], ["Product"], ["Due"], ["Order value", "num"], ["Days late", "num"], ["Penalty", "num"]]}>
            {(f.business?.late_orders || []).map(r => <tr key={r.order_id}><td>{r.order_id}</td><td>{r.customer}</td><td>{r.product_id}</td><td>{dm(r.due)}</td>
              <td className="num">{inr(r.value)}</td><td className="num bad">{r.days_late}</td><td className="num bad">{inr(r.penalty)}</td></tr>)}
          </Tbl>
          <p className="fp-note">The {(f.business?.late_orders || []).length} late orders with the largest penalties. Call these customers first.</p>
        </>}>
          <BusinessViz b={f.business} />
        </Sec>
      </div>

      <div className="fp-row r3 first">
        <Sec n={1} title="Production strategy & targets" icon="target" details={<>
          <Tbl head={[["Product"], ["Demand (orders)", "num"], ["Planned", "num"], ["Mix", "num"], ["Priority"]]}
            foot={<tr><th>Total</th><th className="num">{N(total("demand"))}</th><th className="num">{N(total("planned"))}</th><th className="num">100%</th><th>–</th></tr>}>
            {f.strategy.map(r => <tr key={r.product_id}><td><i className="fp-dot" style={{ background: color(r.product_id) }} />{r.product_id}<small>{r.strategy}</small></td>
              <td className="num">{N(r.demand)}</td><td className="num">{N(r.planned)}</td><td className="num">{r.mix_pct}%</td>
              <td>{PRIO[r.priority] ? <Pill tone={PRIO[r.priority][1]}>{PRIO[r.priority][0]}</Pill> : "–"}</td></tr>)}
          </Tbl>
          <p className="fp-note">Planned also covers the work open production orders still have to finish.</p>
        </>}>
          <StrategyViz rows={f.strategy} color={color} />
        </Sec>
        <Sec n={2} title="Master production schedule (weekly plan)" icon="calendar" details={<>
          <Tbl head={[["Product"], ...f.mps.weeks.map((w, i) => [`W${i + 1}`, "num", dm(w)]), ["Later", "num"], ["Total", "num"]]}
            foot={<tr><th>Total</th>{f.mps.weeks.map(w => <th key={w} className="num">{N(f.mps.rows.reduce((a, r) => a + r[w], 0))}</th>)}
              <th className="num">{N(f.mps.rows.reduce((a, r) => a + r.later, 0))}</th><th className="num">{N(f.mps.rows.reduce((a, r) => a + r.total, 0))}</th></tr>}>
            {f.mps.rows.map(r => <tr key={r.product_id}><td>{r.product_id}</td>{f.mps.weeks.map(w => <td key={w} className="num">{r[w] ? N(r[w]) : "–"}</td>)}
              <td className="num">{r.later ? N(r.later) : "–"}</td><td className="num"><b>{N(r.total)}</b></td></tr>)}
          </Tbl>
        </>}>
          <MpsViz mps={f.mps} color={color} />
        </Sec>
        <Sec n={3} title={`One day on the lines (${dm(f.daily.day)})`} icon="clock" details={<>
          <Tbl head={[["Job"], ["Product"], ["Line"], ["Start"], ["End"], ["Qty", "num"], ["Priority"]]}>
            {f.daily.rows.map((r, i) => <tr key={i}><td>{r.order_id}</td><td>{r.product_id}</td><td>{r.line_id}<small>{r.plant}</small></td><td>{hm(r.start)}</td><td>{hm(r.finish)}</td>
              <td className="num">{N(r.qty)}</td><td>{PRIO[r.priority] ? <Pill tone={PRIO[r.priority][1]}>{PRIO[r.priority][0]}</Pill> : r.priority}</td></tr>)}
          </Tbl>
        </>}>
          <DayViz daily={f.daily} color={color} />
        </Sec>
      </div>

      <div className="fp-row r3">
        <Sec n={4} title="Material requirements plan (MRP)" icon="box" details={<>
          <Tbl head={[["Material"], ["Required", "num"], ["Available", "num"], ["Shortage", "num"], ["Action"]]}>
            {f.mrp.map(r => <tr key={r.material_id}><td>{r.material_id}</td><td className="num">{N(r.required)}</td><td className="num">{N(r.available)}</td>
              <td className={"num" + (r.shortage ? " bad" : "")}>{N(r.shortage)}</td>
              <td>{r.shortage ? <><Pill tone="bad">Procure</Pill><small>arrives {dm(r.arrives)}</small></> : <Pill tone="ok">No action</Pill>}</td></tr>)}
          </Tbl>
          {f.mrp_count > f.mrp.length && <p className="fp-note">Showing the {f.mrp.length} largest of {f.mrp_count} materials.</p>}
        </>}>
          <MrpViz rows={f.mrp} />
        </Sec>
        <Sec n={5} title="Machine & capacity plan" icon="gear" details={<>
          <Tbl head={[["Line"], ["Avail.", "num", "hours"], ["Req.", "num", "hours"], ["Use", "num"], ["Status"]]}>
            {f.machines.map(r => <tr key={r.line_id}><td>{r.line_id}<small>{r.plant}</small></td><td className="num">{N(r.available_h)}</td><td className="num">{N(r.required_h)}</td>
              <td className="num">{r.utilisation ?? "–"}%</td><td><Pill tone={STATUS[r.status]}>{r.status}</Pill></td></tr>)}
          </Tbl>
          <p className="fp-note">Over the weeks with planned work.</p>
        </>}>
          <MachinesViz rows={f.machines} />
        </Sec>
        <Sec n={6} title="Workforce & shift plan" icon="users" details={<>
          <Tbl head={[["Shift & crew per line"], ["Needed", "num"], ["Available", "num"]]}
            foot={<tr><th>Total a day</th><th className="num">{N(f.workforce.reduce((a, w) => a + w.needed, 0))}</th><th className="num">{N(f.workforce.reduce((a, w) => a + w.available, 0))}</th></tr>}>
            {f.workforce.map(w => <tr key={w.shift}><td><b>{w.shift}</b>{w.allocation.split(" · ").map(x => <small key={x} className="fp-alloc">{x}</small>)}
              {w.short_days ? <small className="bad">{w.short_days} days short · {N(w.overtime_h)} h overtime</small> : <small>fully staffed</small>}</td>
              <td className="num">{N(w.needed)}</td><td className="num">{N(w.available)}</td></tr>)}
          </Tbl>
          <p className="fp-note">Average a day over the next two weeks.</p>
        </>}>
          <WorkforceViz rows={f.workforce} />
        </Sec>
      </div>

      <div className="fp-row r1">
        <Sec n={7} title="Production Gantt chart (key operations)" icon="chart" className="wide">
          <Gantt g={f.gantt} color={color} />
        </Sec>
      </div>

      <div className="fp-row r4e">
        <Sec n={8} title="Maintenance plan" icon="gear" details={<>
          <ul className="fp-list">
            <li>{f.maintenance.count} planned services in the plan · {f.maintenance.hours} line hours blocked</li>
            {f.maintenance.items.map((m, i) => <li key={i}>{m.line_id}: {dm(m.start)} {hm(m.start)}, {m.hours} h{m.machines ? ` (${m.machines})` : ""}</li>)}
            <li>Services are placed in quieter shifts where possible</li>
          </ul>
        </>}>
          <MaintViz m={f.maintenance} />
        </Sec>
        <Sec n={9} title="Quality plan" icon="check" details={<>
          <ul className="fp-list">
            <li>Inspection at: {f.quality.inspection.map(([t, n]) => `${t} ×${n}`).join(", ")}</li>
            <li>Final-assembly yield: {f.quality.fal_yield_pct ?? "–"}% (recorded)</li>
            <li>Expected rejects: {f.quality.reject_pct}% (about {N(f.quality.expected_rejects)} units)</li>
            {f.quality.rework_pct != null && <li>Rework: {f.quality.rework_pct}% of units made</li>}
            <li>Good output target: {N(f.quality.good_target)} units</li>
          </ul>
        </>}>
          <QualityViz q={f.quality} />
        </Sec>
        <Sec n={10} title="Cost plan" icon="box" details={<>
          {f.cost?.available ? <table className="fp-t fp-cost"><tbody>
            {Object.entries(f.cost.parts).map(([k2, v]) => <tr key={k2}><td>{k2.replace(/_/g, " ").replace(/^./, c => c.toUpperCase())}</td><td className="num">{inr(v)}</td></tr>)}
            <tr className="fp-tot"><th>Total estimated cost</th><th className="num">{inr(f.cost.total_inr)}</th></tr></tbody></table>
            : <p className="muted">No cost rates on record.</p>}
        </>}>
          <CostViz cost={f.cost} />
        </Sec>
        <Sec n={11} title="Risk & contingency plan" icon="alert" details={<>
          <Tbl head={[["Risk"], ["Impact"], ["Mitigation / contingency"]]}>
            {f.risks.map((r, i) => <tr key={i}><td><b>{r.risk}</b></td><td>{r.impact}</td><td>{r.mitigation}</td></tr>)}
          </Tbl>
        </>}>
          <RiskViz rows={f.risks} />
        </Sec>
      </div>

      <div className="fp-row r2 end">
        <Sec n={12} title="Key recommendations" icon="sparkle" className="fp-recs">
          <ul className="fp-checks">{f.recommendations.map(x => <li key={x}><Icon n="check" size={14} />{x}</li>)}</ul>
        </Sec>
        <Sec title="Final outcome" icon="file" className="fp-out">
          <ul className="fp-checks">
            {[["What to produce", `${f.outcome.what} units`], ["When to produce", f.outcome.when], ["Where to produce", f.outcome.where],
              ["With what resources", f.outcome.resources], ["At what cost", `${inr(f.outcome.cost)} (estimated)`],
              ["Delivery commitment", f.outcome.delivery != null ? `${f.outcome.delivery}% of ordered units on time` : "–"]].map(([l, v]) => (
              <li key={l}><Icon n="check" size={14} /><span><b>{l}:</b> {v}</span></li>))}
          </ul>
        </Sec>
      </div>
    </div>
  );
}

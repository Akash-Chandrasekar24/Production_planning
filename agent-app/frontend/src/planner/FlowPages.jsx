import { Fragment, useEffect, useState } from "react";
import { api, can } from "../api.js";
import { N } from "./ui.jsx";
import { Icon } from "./widgets.jsx";
import { Segmented } from "./Filters.jsx";
import FinalPlan from "./FinalPlan.jsx";

// The planning stages around the plan: demand (forecast review, production budget), feasibility (rough-cut capacity), approval (cost),
// execution (board), control (comments and risk log) and reporting (plan document, scheduled reports).
const PLANT = { "IN-TN-01": "Hosur", "IN-KA-01": "Bengaluru" };
const inr = v => (v == null ? "–" : v >= 1e7 ? `₹${(v / 1e7).toFixed(2)} Cr` : v >= 1e5 ? `₹${(v / 1e5).toFixed(1)} L` : `₹${N(v)}`);
const day = d => new Date(String(d).slice(0, 10) + "T00:00:00").toLocaleDateString("en", { weekday: "short", day: "numeric", month: "short" });
const time = t => new Date(t).toLocaleTimeString("en", { hour: "2-digit", minute: "2-digit" });
const when = t => (t ? new Date(String(t).endsWith("Z") ? t : t + "Z").toLocaleString("en", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "");
const can2 = (a, role) => can(a, role) || ({ forecast: ["production_manager", "engineer", "admin", "planner"], report: ["production_manager", "admin", "planner"],
  task: ["production_manager", "plant_head", "admin"] }[a] || []).includes(role);
const useApi = (path, deps) => {
  const [v, setV] = useState(null);
  const [n, setN] = useState(0);
  useEffect(() => { let live = true; setV(null); if (path) api(path).then(j => live && setV(j)).catch(() => live && setV(false)); return () => { live = false; }; }, [path, n, ...(deps || [])]);
  return [v, () => setN(x => x + 1)];
};
const Wait = ({ v, children }) => (v === null ? <p className="muted" role="status">Loading…</p> : v === false ? <p className="muted">Not available right now.</p> : children);
const Card = ({ title, sub, right, children, id }) => (
  <section className="sp-card fl-card" id={id}><header className="ov-h"><div><h3>{title}</h3>{sub && <small className="muted">{sub}</small>}</div>{right}</header>{children}</section>);

// ---------------------------------------------------------------- 1 Demand: forecast review + production budget
export function Demand({ s, role, onError }) {
  const [f, reloadF] = useApi(`/api/demand/forecast?schedule_id=${s.id}`);
  const [b] = useApi(`/api/demand/budget?schedule_id=${s.id}`);
  const [edit, setEdit] = useState({});
  const mayEdit = can2("forecast", role);
  const save = async (r, qty) => {
    try { await api("/api/demand/forecast", "PUT", { product_id: r.product_id, week: r.week, qty, note: null }); setEdit(e => ({ ...e, [r.product_id + r.week]: undefined })); reloadF(); }
    catch (e) { onError(e); }
  };
  return (
    <div className="fl">
      <VolumeGate role={role} onError={onError} />
      <Card title="Forecast review" sub={f ? `${f.method}. ${f.applies}.` : "The model's forecast, and the number the team agrees"}>
        <Wait v={f}>{f && <>
          <div className="fl-scroll"><table className="ov-tbl"><thead><tr><th>Product</th><th className="num">Model error</th>{f.weeks.map(w => <th key={w} className="num">Week of {day(w)}</th>)}</tr></thead>
            <tbody>{f.products.map(p => (
              <tr key={p}><td><b>{p}</b></td><td className="num">{f.accuracy_pct[p] != null ? `${f.accuracy_pct[p]}%` : "–"}</td>
                {f.weeks.map(w => {
                  const r = f.rows.find(x => x.product_id === p && x.week === w); const k = p + w;
                  if (!r) return <td key={w} />;
                  return (
                    <td key={w} className="num fl-fc">
                      {edit[k] !== undefined
                        ? <form onSubmit={e => { e.preventDefault(); save(r, edit[k] === "" ? null : +edit[k]); }}>
                            <input type="number" min="0" value={edit[k]} onChange={e => setEdit(x => ({ ...x, [k]: e.target.value }))} aria-label={`Agreed forecast for ${p}, week of ${w}`} autoFocus />
                            <button className="lnk">Save</button></form>
                        : <><b className={r.agreed != null ? "fl-agreed" : ""}>{N(r.agreed ?? r.model)}</b>
                            {mayEdit && <button className="lnk" onClick={() => setEdit(x => ({ ...x, [k]: r.agreed ?? r.model }))} aria-label={`Change the forecast for ${p}, week of ${w}`}><Icon n="gear" size={12} /></button>}</>}
                      <small className="muted">{r.agreed != null ? `agreed · model ${N(r.model)}` : "model"} · {N(r.confirmed)} ordered</small>
                      {r.agreed != null && mayEdit && <button className="lnk" onClick={() => save(r, null)}>Use model</button>}
                    </td>);
                })}</tr>))}</tbody></table></div>
          <small className="muted">Model error = average miss on the last 4 weeks when the model forecast them. Orange numbers were agreed by the team.</small>
        </>}</Wait>
      </Card>
      <Card title="Production budget" sub={b ? b.formula : "What to make"}>
        <Wait v={b}>{b && <>
          <div className="fl-scroll"><table className="ov-tbl"><thead><tr><th>Product</th><th className="num">Confirmed orders</th><th className="num">Forecast beyond orders</th>
            <th className="num">Expected sales</th><th className="num">+ Ending stock wanted</th><th className="num">− Stock on hand</th><th className="num">− Open production orders</th>
            <th className="num">= Required</th><th className="num">In this plan</th><th className="num">Gap</th></tr></thead>
            <tbody>{b.rows.map(r => <tr key={r.product_id}><td><b>{r.product_id}</b></td><td className="num">{N(r.confirmed_orders)}</td><td className="num">{N(r.forecast_beyond_orders)}</td>
              <td className="num">{N(r.expected_sales)}</td><td className="num">{N(r.desired_ending_stock)}</td><td className="num">{N(r.beginning_stock)}</td><td className="num">{N(r.open_production_orders)}</td>
              <td className="num"><b>{N(r.required)}</b></td><td className="num">{N(r.planned)}</td><td className={"num" + (r.gap > 0 ? " bad" : "")}>{N(r.gap)}</td></tr>)}</tbody></table></div>
          <small className="muted">Ending stock wanted = {b.fg_target_days} days of forecast demand. {b.fg_stock_known ? "" : "The data has no finished-goods stock, so stock on hand counts as 0."}</small>
        </>}</Wait>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------- the demand and capacity gate: decided before the agent schedules
export function VolumeGate({ role, onError, onDone, quiet }) {
  const [st, reload] = useApi("/api/volume");
  const [pick, setPick] = useState(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(null);
  if (!st) return null;
  const p = st.proposal, a = st.agreed, mayAgree = can("approve", role);
  if (!p && (quiet || !a)) return null;
  const agree = async () => {
    setBusy(true);
    try { const r = await api(`/api/volume/${p.id}/agree`, "POST", { option: pick || p.recommended }); setDone(r.plan); reload(); onDone?.(); }
    catch (e) { onError?.(e); } finally { setBusy(false); }
  };
  if (!p) return (
    <Card title="Volume plan in force" sub={`Agreed by ${a.agreed_by} · ${when(a.agreed_at)}`}>
      <p className="fl-p">{(a.chosen ? { commit: "Achievable dates committed", overtime: "Breaks worked in overloaded weeks, then dates committed", keep: "Requested dates kept" }[a.chosen] : "")}
        {` · ${N(st.committed_orders)} orders on committed dates · ${N(st.accepted_late)} accepted as late`}{a.extra_min ? ` · +${a.extra_min} min a shift in ${a.extra_weeks.length} weeks` : ""}.
        {done?.schedule_id ? " The agent has made the plan with it: see Review & approve." : ""}</p>
    </Card>);
  const sel = pick || p.recommended;
  return (
    <section className="sp-card fl-card fl-gate" aria-label="Demand and capacity decision">
      <header className="ov-h"><div><h3><Icon n="alert" size={16} /> Decide before the plan is made: demand doesn't fit capacity</h3>
        <small className="muted">{N(p.late_count)} orders can't be finished by their date at current capacity. The agent has not scheduled yet: choose how to go on and it plans with your decision.</small></div></header>
      <ul className="ov-hbars fl-rc">{p.load.slice(0, 6).map((w, i) => <li key={w.week}><span>{i === 0 ? "This week, with overdue orders" : `Week of ${day(w.week)}`}</span>
        <span className={"ov-bar " + (w.load_pct > 100 ? "bad" : "ok")}><i style={{ width: `${Math.min(100, w.load_pct || 0)}%` }} /></span><em>{w.load_pct}% of final assembly</em></li>)}</ul>
      <div className="fl-opts" role="radiogroup" aria-label="How to go on">
        {p.options.map(o => (
          <label key={o.key} className={"fl-opt" + (sel === o.key ? " on" : "")}>
            <input type="radio" name="vol" checked={sel === o.key} onChange={() => setPick(o.key)} disabled={!mayAgree} />
            <span><b>{o.title}</b>{o.best && <em className="ro-tag hi">Agent&apos;s pick</em>}
              <small>{o.key === "keep" ? `${N(o.orders_affected)} orders stay on their requested dates and will be late`
                : `${N(o.orders_affected)} orders get a committed date, on average ${o.avg_days_later} days later (at most ${o.max_days_later})`}
                {o.high_affected ? ` · ${o.high_affected} high priority` : ""}{o.overtime_h ? ` · ${N(o.overtime_h)} extra line hours, about ${inr(o.overtime_cost_inr)}` : ""}
                {o.beyond_horizon && o.key !== "keep" ? ` · ${o.beyond_horizon} don't fit the next ${60} days and stay late` : ""}</small></span>
          </label>))}
      </div>
      <details className="fl-det"><summary>Orders affected ({N(p.options.find(o => o.key === sel)?.orders_affected || 0)})</summary>
        <table className="ov-tbl"><thead><tr><th>Order</th><th>Product</th><th className="num">Units</th><th>Priority</th><th>Requested</th><th>Can finish</th></tr></thead>
          <tbody>{(p.options.find(o => o.key === sel)?.orders || []).map(r => <tr key={r.order_id}><td><b>{r.order_id}</b></td><td>{r.product_id}</td><td className="num">{N(r.qty)}</td>
            <td>{r.priority}</td><td>{day(r.due)}</td><td>{r.can_finish ? day(r.can_finish) : "not in the next 60 days"}</td></tr>)}</tbody></table></details>
      {mayAgree ? <div className="sp-row sp-left"><button className="sp-btn primary" disabled={busy} onClick={agree}><Icon n="check" size={16} /> {busy ? "Planning…" : "Agree and let the agent plan"}</button></div>
        : <p className="muted">The production manager decides. They have it in their notifications.</p>}
    </section>
  );
}

// ---------------------------------------------------------------- 2 Feasibility: rough-cut capacity
export function RoughCut({ sid }) {
  const [r] = useApi(`/api/demand/rough-cut?schedule_id=${sid}`);
  return (
    <Card title="Rough-cut capacity" sub="Each week's demand (orders due and forecast) against what final assembly can make that week">
      <Wait v={r}>{r && <>
        <ul className="ov-hbars fl-rc">{r.weeks.map((w, i) => <li key={w.week}><span>{i === 0 ? "This week, with overdue orders" : `Week of ${day(w.week)}`}</span>
          <span className={"ov-bar " + (w.load_pct > 100 ? "bad" : w.load_pct > 85 ? "warn" : "ok")}><i style={{ width: `${Math.min(100, w.load_pct || 0)}%` }} /></span>
          <em>{w.load_pct ?? "–"}% · {N(w.demand)} of {N(w.capacity)} units</em></li>)}</ul>
        {r.overloaded.length > 0 && <p className="fl-warn"><Icon n="alert" size={14} /> {r.overloaded.length} week{r.overloaded.length > 1 ? "s ask" : " asks"} for more than final assembly can make: expect late orders unless capacity is added or orders move.</p>}
      </>}</Wait>
    </Card>
  );
}

// ---------------------------------------------------------------- 4 Approve: cost estimate
export function PlanCost({ sid }) {
  const [c] = useApi(sid ? `/api/plan-cost/${sid}` : null);
  if (!c || !c.available) return null;
  const parts = [["material", "Material"], ["labour", "Labour"], ["line_time", "Line time"], ["changeovers", "Changeovers"], ["overtime", "Overtime"], ["late_penalty", "Late penalties"], ["carrying", "Stock carrying"]];
  return (
    <div className="fl-cost"><b>Plan cost estimate: {inr(c.total_inr)}</b>{c.per_unit_inr != null && <small className="muted"> · {inr(c.per_unit_inr)} a unit</small>}
      <span>{parts.filter(([k]) => c.parts[k]).map(([k, l]) => <em key={k}>{l} {inr(c.parts[k])}</em>)}</span>
      <small className="muted">Estimate from the plan and the cost rates on record.</small></div>
  );
}

// ---------------------------------------------------------------- control: comments and the risk log
export function PlanNotes({ sid }) {
  const [list, reload] = useApi(`/api/schedules/${sid}/notes`);
  const [f, setF] = useState({ kind: "comment", text: "", severity: "medium", owner: "" });
  const [err, setErr] = useState(null);
  const add = async e => {
    e.preventDefault(); setErr(null);
    try { await api(`/api/schedules/${sid}/notes`, "POST", { kind: f.kind, text: f.text, severity: f.kind === "risk" ? f.severity : null, owner: f.kind === "risk" ? f.owner || null : null });
      setF({ ...f, text: "", owner: "" }); reload(); }
    catch (x) { setErr(x.message); }
  };
  const close = async id => { try { await api(`/api/notes/${id}/close`, "POST"); reload(); } catch (x) { setErr(x.message); } };
  const risks = (list || []).filter(n => n.kind === "risk"), comments = (list || []).filter(n => n.kind === "comment");
  return (
    <Card title="Discussion and risks" sub="Comments on this plan, and a log of what could go wrong and who handles it">
      <form className="fl-noteform" onSubmit={add}>
        <Segmented label="Type" options={[{ value: "comment", label: "Comment" }, { value: "risk", label: "Risk" }]} value={f.kind} onChange={v => setF({ ...f, kind: v })} />
        <input value={f.text} onChange={e => setF({ ...f, text: e.target.value })} maxLength={2000} required placeholder={f.kind === "risk" ? "What could go wrong?" : "Write a comment"} aria-label="Text" />
        {f.kind === "risk" && <><select value={f.severity} onChange={e => setF({ ...f, severity: e.target.value })} aria-label="Severity">
          <option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option></select>
          <input value={f.owner} onChange={e => setF({ ...f, owner: e.target.value })} maxLength={100} placeholder="Who handles it" aria-label="Owner" /></>}
        <button className="sp-btn primary">Add</button>
      </form>
      {err && <p className="bad">{err}</p>}
      <Wait v={list}>{list && <div className="ov-two">
        <div><h4 className="pbf-term"><b>Risk log</b><small className="muted">{risks.filter(r => r.status === "OPEN").length} open</small></h4>
          {!risks.length ? <p className="muted">No risks logged.</p> : <ul className="fl-notes">{risks.map(r => (
            <li key={r.id} className={r.status === "CLOSED" ? "closed" : ""}><span className={"sp-pill t-" + (r.severity === "high" ? "bad" : r.severity === "medium" ? "warn" : "info")}>{r.severity}</span>
              <div><b>{r.text}</b><small className="muted">{r.owner ? `${r.owner} · ` : ""}logged by {r.created_by} · {when(r.created_at)}{r.status === "CLOSED" ? ` · closed by ${r.closed_by}` : ""}</small></div>
              {r.status === "OPEN" && <button className="lnk" onClick={() => close(r.id)}>Close</button>}</li>))}</ul>}</div>
        <div><h4 className="pbf-term"><b>Comments</b><small className="muted">{comments.length}</small></h4>
          {!comments.length ? <p className="muted">No comments yet.</p> : <ul className="fl-notes">{comments.map(c => (
            <li key={c.id}><Icon n="users" size={14} /><div><span>{c.text}</span><small className="muted">{c.created_by} · {when(c.created_at)}</small></div></li>))}</ul>}</div>
      </div>}</Wait>
    </Card>
  );
}

// ---------------------------------------------------------------- 6 Report: the production plan document and scheduled reports
export function PlanDocument({ s, role }) {
  const [view, setView] = useState("plan");
  return (
    <div className="fl">
      <div className="fp-switch no-print"><Segmented label="View" options={[{ value: "plan", label: "Final production plan" }, { value: "parts", label: "Plan document (7 parts)" }]}
        value={view} onChange={setView} /></div>
      {view === "plan" ? <FinalPlan sid={s.id} /> : <PlanParts s={s} />}
      {can2("report", role) && <Reports />}
    </div>
  );
}

function PlanParts({ s }) {
  const [d] = useApi(`/api/plan-document/${s.id}`);
  return (
    <div className="fl fl-doc">
      <div className="fl-doc-h"><div><h2>Production plan: {s.name}</h2>
        <small className="muted">{d ? `${d.fully_approved ? "Fully approved" : d.status === "APPROVED" ? "Approved by the production manager" : "Waiting for approval"} · prepared from data as of ${day(d.as_of)}` : ""}</small></div>
        <button className="sp-btn no-print" onClick={() => print()}><Icon n="download" size={14} /> Print or save as PDF</button></div>
      <Wait v={d}>{d && <>
        <nav className="ov-jump no-print" aria-label="Parts">{["Forecast", "Budget", "Inventory", "Resources", "Cost", "Schedule", "Control"].map((x, i) =>
          <a key={x} href={"#doc-" + i} onClick={e => { e.preventDefault(); document.getElementById("doc-" + i)?.scrollIntoView({ behavior: "smooth" }); }}>{i + 1}. {x}</a>)}</nav>

        <Card id="doc-0" title="1. Sales forecast" sub={d.forecast?.method}>
          {d.forecast ? <table className="ov-tbl"><thead><tr><th>Product</th>{d.forecast.weeks.map(w => <th key={w} className="num">{day(w)}</th>)}<th className="num">Model error</th></tr></thead>
            <tbody>{d.forecast.products.map(p => <tr key={p}><td><b>{p}</b></td>{d.forecast.weeks.map(w => { const r = d.forecast.rows.find(x => x.product_id === p && x.week === w);
              return <td key={w} className="num">{r ? N(r.agreed ?? r.model) : "–"}{r?.agreed != null && <small className="fl-agreed"> agreed</small>}</td>; })}
              <td className="num">{d.forecast.accuracy_pct[p] != null ? `${d.forecast.accuracy_pct[p]}%` : "–"}</td></tr>)}</tbody></table> : <p className="muted">Not available.</p>}
        </Card>
        <Card id="doc-1" title="2. Production budget" sub={d.budget?.formula}>
          {d.budget ? <table className="ov-tbl"><thead><tr><th>Product</th><th className="num">Expected sales</th><th className="num">Ending stock wanted</th><th className="num">Stock on hand</th>
            <th className="num">Required</th><th className="num">Planned</th></tr></thead>
            <tbody>{d.budget.rows.map(r => <tr key={r.product_id}><td><b>{r.product_id}</b></td><td className="num">{N(r.expected_sales)}</td><td className="num">{N(r.desired_ending_stock)}</td>
              <td className="num">{N(r.beginning_stock)}</td><td className="num"><b>{N(r.required)}</b></td><td className="num">{N(r.planned)}</td></tr>)}</tbody></table> : <p className="muted">Not available.</p>}
        </Card>
        <Card id="doc-2" title="3. Inventory plan" sub={`Materials the plan needs. ${d.inventory.short} short: bought parts arrive one supplier lead time after they are ordered. Safety stock is ${d.inventory.safety_stock_kept ? "kept" : "not kept"}.`}>
          <div className="fl-scroll"><table className="ov-tbl"><thead><tr><th>Material</th><th className="num">Needed</th><th className="num">In stock</th><th className="num">Safety stock</th><th className="num">Short</th>
            <th className="num">Lead time</th><th>Arrives if ordered today</th><th className="num">Jobs waiting</th></tr></thead>
            <tbody>{d.inventory.materials.map(m => <tr key={m.material_id}><td><b>{m.material_id}</b></td><td className="num">{N(m.required_qty)}</td><td className="num">{N(m.available_qty)}</td>
              <td className="num">{N(m.safety_stock)}</td><td className={"num" + (m.shortfall_qty ? " bad" : "")}>{N(m.shortfall_qty)}</td><td className="num">{m.lead_days} d</td>
              <td>{day(m.arrives_if_ordered_today)}</td><td className="num">{N(m.jobs_waiting)}</td></tr>)}</tbody></table></div>
        </Card>
        <Card id="doc-3" title="4. Resource plan" sub={`Lines, planned services and crews. ${d.resources.crew_hours_removed ? `${N(d.resources.crew_hours_removed)} line hours removed for shifts short of people.` : ""}`}>
          <table className="ov-tbl"><thead><tr><th>Line</th><th>Plant</th><th className="num">Hours available</th><th className="num">Utilisation</th><th className="num">Planned services</th></tr></thead>
            <tbody>{d.resources.lines.map(l => <tr key={l.line_id}><td><b>{l.line_id}</b> <small className="muted">{l.stage === "SMT" ? "Circuit boards" : "Final assembly"}</small></td><td>{PLANT[l.plant_id] || l.plant_id}</td>
              <td className="num">{N(l.available_h)}</td><td className="num">{l.utilisation_pct}%</td><td className="num">{l.services} · {l.service_h} h</td></tr>)}</tbody></table>
          <p className="muted fl-p">Crews, next 14 days: {N(d.resources.crews.short_shifts)} shifts short of people{d.resources.crews.overtime_h ? `, ${N(d.resources.crews.overtime_h)} overtime hours to cover them` : ""}.</p>
        </Card>
        <Card id="doc-4" title="5. Cost estimate" sub="Estimate from the plan and the cost rates on record">
          {d.cost?.available ? <div className="pbf-tiles">{Object.entries(d.cost.parts).map(([k, v]) => <div key={k} className="pbf-tile"><small>{k.replace(/_/g, " ").replace(/^./, c => c.toUpperCase())}</small><b>{inr(v)}</b></div>)}
            <div className="pbf-tile t-info"><small>Total</small><b>{inr(d.cost.total_inr)}</b><small className="muted">{inr(d.cost.per_unit_inr)} a unit</small></div></div> : <p className="muted">No cost rates on record.</p>}
        </Card>
        <Card id="doc-5" title="6. Production schedule" sub={`Frozen zone: first ${d.schedule.fences.frozen_days} days · changes in weeks ${d.schedule.fences.frozen_days / 7 + 1}–${d.schedule.fences.slushy_days / 7} are checked at approval`}>
          <div className="pbf-tiles">{[["Units planned", N(d.schedule.summary.scheduled_qty)], ["On time", `${d.schedule.summary.on_time_qty_pct ?? "–"}%`], ["Late jobs", N(d.schedule.summary.late_jobs)],
            ["Waiting for parts", N(d.schedule.summary.purchase_wait_jobs || 0)], ["Forecast for stock", N(d.schedule.summary.forecast_qty || 0)], ["Changeovers", N(d.schedule.summary.changeovers)],
            ["Bottleneck", d.schedule.summary.bottleneck_line || "–"], ["Not planned", N(d.schedule.summary.unscheduled_qty)]].map(([l, v]) => <div key={l} className="pbf-tile"><small>{l}</small><b>{v}</b></div>)}</div>
          {Object.entries(d.schedule.plants).map(([p, b]) => b && <Fragment key={p}><h4 className="pbf-term"><b>{PLANT[p]}: master production schedule</b>
            <small className="muted">{N(b.jobs)} jobs · {N(b.units)} units · on time {b.otd_pct}% · bottleneck {b.bottleneck}</small></h4>
            <table className="ov-tbl"><thead><tr><th>Week of</th>{b.mps.products.map(x => <th key={x} className="num">{x}</th>)}<th className="num">Total</th></tr></thead>
              <tbody>{b.mps.weeks.map(w => <tr key={w.week}><td>{day(w.week)}</td>{b.mps.products.map(x => <td key={x} className="num">{N(w[x])}</td>)}<td className="num"><b>{N(w.total)}</b></td></tr>)}</tbody></table></Fragment>)}
        </Card>
        <Card id="doc-6" title="7. Control plan" sub="The measures the plan is held to, how it is reviewed, and what could go wrong">
          <table className="ov-tbl"><thead><tr><th>Measure</th><th className="num">Now</th><th className="num">Target</th></tr></thead>
            <tbody>{d.control.kpis.map(k => <tr key={k.key}><td>{k.label}{k.estimate ? <small className="muted"> · estimate</small> : ""}</td>
              <td className="num"><b>{k.value == null ? "–" : k.unit === "%" ? `${k.value}%` : `${N(k.value)} ${k.unit}`}</b></td><td className="num">{k.target != null ? `${k.target}%` : "–"}</td></tr>)}</tbody></table>
          {d.control.reviews.length > 0 && <><h4 className="pbf-term"><b>How the plan is reviewed</b></h4><ul className="fl-list">{d.control.reviews.map(r => <li key={r}>{r}</li>)}</ul></>}
          <h4 className="pbf-term"><b>Risk log</b><small className="muted">{d.control.risks.filter(r => r.status === "OPEN").length} open</small></h4>
          {d.control.risks.length ? <ul className="fl-list">{d.control.risks.map(r => <li key={r.id}>{r.severity} · {r.text}{r.owner ? ` (${r.owner})` : ""}{r.status === "CLOSED" ? " · closed" : ""}</li>)}</ul> : <p className="muted">No risks logged. Add them on the Plan page.</p>}
          {d.stages?.length > 0 && <><h4 className="pbf-term"><b>Approvals</b></h4><ul className="fl-list">{d.stages.map(st => <li key={st.stage + (st.plant || "")}>{st.label}: {st.done ? `${st.by} · ${when(st.at)}` : "waiting"}</li>)}</ul></>}
        </Card>
      </>}</Wait>
    </div>
  );
}

function Reports() {
  const [log, reload] = useApi("/api/reports/log");
  const [to, setTo] = useState("");
  const [msg, setMsg] = useState(null);
  const send = async (kind, plant) => {
    setMsg(null);
    try { const r = await api("/api/reports/send", "POST", { kind, plant, to: to || null }); setMsg(to ? `Preview sent to ${to}${r.email ? "" : " (email isn't set up, so nothing was sent)"}.` : `Sent to ${r.to} ${r.to === 1 ? "person" : "people"}.`); reload(); }
    catch (e) { setMsg(e.message); }
  };
  return (
    <section className="sp-card fl-card no-print"><header className="ov-h"><div><h3>Scheduled reports</h3>
      <small className="muted">A daily brief goes to each plant's production head every morning, and a weekly report to the production managers every Monday</small></div></header>
      <div className="fl-noteform"><input type="email" value={to} onChange={e => setTo(e.target.value)} placeholder="Send a preview to (optional)" aria-label="Preview email address" />
        <button className="sp-btn" onClick={() => send("daily", "IN-TN-01")}>Hosur daily brief</button>
        <button className="sp-btn" onClick={() => send("daily", "IN-KA-01")}>Bengaluru daily brief</button>
        <button className="sp-btn" onClick={() => send("weekly")}>Weekly report</button></div>
      {msg && <p className="muted" role="status">{msg}</p>}
      <Wait v={log}>{log && (log.length ? <ul className="fl-list">{log.slice(0, 8).map(r => <li key={r.key}>{r.kind === "daily" ? `Daily brief · ${PLANT[r.key.split(":")[2]] || ""}` : "Weekly report"} · {when(r.sent_at)} · {r.detail?.sent ?? 0} emailed</li>)}</ul>
        : <p className="muted">Nothing sent yet: the first briefs go out at the next scheduled time.</p>)}</Wait>
    </section>
  );
}

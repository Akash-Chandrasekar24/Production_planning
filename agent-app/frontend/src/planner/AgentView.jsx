import { useEffect, useRef, useState } from "react";
import { api, auth } from "../api.js";
import { N } from "./ui.jsx";
import { Icon, K } from "./widgets.jsx";
import { FilterBar, nice } from "./Filters.jsx";
import { KpiStrip } from "./PlanningViews.jsx";
import Problems from "./Problems.jsx";
import AgentTeam from "./AgentTeam.jsx";

// Planning agent: watches incoming orders, re-plans when enough arrive, and hands the work to the production manager and each plant
// head. It never approves or releases; people decide. Everything shown comes from the agent's API.

export const ROLE = { production_manager: "Production manager", plant_head: "Plant head" };
const TONE = { OPEN: "info", WAITING_APPROVAL: "warn", ACKNOWLEDGED: "info", DONE: "ok", CANCELLED: "bad" };
const STATUS = { OPEN: "To do", WAITING_APPROVAL: "Waiting for approval", ACKNOWLEDGED: "In progress", DONE: "Done", CANCELLED: "Withdrawn" };
const when = v => (v ? new Date(String(v).endsWith("Z") ? v : v + "Z").toLocaleString("en", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "–");
const jobs = n => `${N(n)} job${n === 1 ? "" : "s"}`;
const fmtT = v => (v ? new Date(String(v).slice(0, 19) + "Z").toLocaleString("en", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "UTC" }) : "–");
const CSV_COLS = ["customer_order_id", "customer_id", "product_id", "order_qty", "requested_delivery_date", "priority", "channel", "unit_price_inr"];

export default function AgentView({ role, setTab, openSchedule, onMsg, onError }) {
  const [st, setSt] = useState(null);
  const [runs, setRuns] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [team, setTeam] = useState([]);
  const [intake, setIntake] = useState([]);
  const [f, setF] = useState({ status: "active" });
  const [busy, setBusy] = useState("");
  const [focus, setFocus] = useState(null);
  const [ver, setVer] = useState(0);
  const load = async () => {
    const [a, b, c, d, e] = await Promise.all(["/api/agent/status", "/api/agent/runs", "/api/agent/tasks", "/api/agent/team", "/api/orders/intake?limit=50"].map(p => api(p)));
    setSt(a); setRuns(b); setTasks(c); setTeam(d); setIntake(e);
  };
  useEffect(() => { load().catch(onError); const t = setInterval(() => load().catch(() => {}), 30000); return () => clearInterval(t); }, []);   // eslint-disable-line react-hooks/exhaustive-deps

  const act = (key, fn) => async () => { setBusy(key); try { await fn(); await load(); setVer(v => v + 1); } catch (e) { onError(e); } finally { setBusy(""); } };
  const can = a => ({ agent: ["planner", "production_manager", "admin"], intake: ["planner", "admin"], team: ["admin"], incident: ["planner", "production_manager", "plant_head"] }[a] || []).includes(role);
  if (!st) return <div className="page"><div className="sp-card"><p className="muted" role="status">Loading the planning agent…</p></div></div>;
  const P = st.pending, R = st.rules, last = st.last_run;
  const plants = [...new Set(tasks.filter(t => t.plant_id).map(t => t.plant_id).concat(team.filter(m => m.plant_id).map(m => m.plant_id)))].sort();
  const active = t => ["OPEN", "WAITING_APPROVAL", "ACKNOWLEDGED"].includes(t.status);
  const shown = tasks.filter(t => f.status !== "active" || active(t)).filter(t => !f.plant || t.plant_id === f.plant || !t.plant_id);
  const columns = [["production_manager", null], ...(plants.length ? plants : ["IN-KA-01", "IN-TN-01"]).map(p => ["plant_head", p])];

  return (
    <div className="page sp-enter">
      <div className="sp-head">
        <div><h1>Planning agent</h1><small className="muted">An orchestrator and its agents watch the floor, the inventory and the order book, plan production, and hand the work to the right people</small></div>
        <div className="sp-head-r">
          <span className={"sp-pill t-" + (last?.outcome === "PLANNED" ? "ok" : "info")}><i className="tw-dot" /> {last ? (last.outcome === "PLANNED" ? `Planned ${when(last.started_at)}` : `Watching · checked ${when(last.started_at)}`) : "Not started"}</span>
          <button className="sp-btn" disabled={!can("agent") || !!busy} onClick={act("check", async () => { const r = await api("/api/agent/check", "POST"); const n = r.events.length;
              onMsg(n ? `The agents found ${n} new thing${n === 1 ? "" : "s"}; the orchestrator sent ${n === 1 ? "it" : "each"} to the planning agent or the people who own it.`
                : `Nothing new. ${r.orders?.reason || ""}`); })}>
            <Icon n="refresh" size={16} /> {busy === "check" ? "Looking…" : "Run the agents now"}</button>
          <button className="sp-btn primary" disabled={!can("agent") || !!busy} onClick={act("run", async () => { await api("/api/agent/run", "POST"); onMsg("The agent made a new plan and assigned the work."); })}>
            <Icon n="burst" size={16} /> {busy === "run" ? "Planning…" : "Plan now"}</button>
        </div>
      </div>
      <div className="sp-callout t-info">
        <span className="sp-callout-ic"><Icon n="lock" size={18} /></span>
        <div className="sp-callout-t"><b>The agent proposes, people decide</b>
          <span>It never approves, rejects, releases, cancels orders, changes dates or bills of materials, or contacts customers. Its plans and recovery plans wait for the production manager on Review & approve; plant heads start preparing once a plan is approved.</span></div>
      </div>

      <KpiStrip items={[
        { label: "New orders since its last plan", num: P.new_orders, note: `${N(P.open_orders)} open orders in total` },
        { label: "New units", num: P.new_units, fmt: K },
        { label: "Urgent high-priority", num: P.urgent_count, tone: P.urgent_count ? "bad" : "" },
        { label: "Open tasks", num: st.open_tasks, tone: st.open_tasks ? "warn" : "ok" },
        { label: "Open problems", num: st.open_problems, tone: st.open_problems ? "bad" : "ok" },
      ]} />

      <AgentTeam refresh={ver} openSchedule={openSchedule} setTab={setTab} onError={onError}
        onProblem={id => { setFocus(id); document.getElementById("ag-problems")?.scrollIntoView({ behavior: "smooth", block: "start" }); }} />

      <Problems role={role} can={can} rules={st.problem_rules} focus={focus} setFocus={setFocus} openSchedule={openSchedule} setTab={setTab}
        onMsg={onMsg} onError={onError} onChange={() => { load().catch(() => {}); setVer(v => v + 1); }} />

      <div className="ag-grid">
        <section className="sp-card">
          <header className="sp-ch"><div><h3>How the agent decides</h3><small className="sp-sub-t">Checks the order book every {R.interval_min} minutes and right after orders arrive</small></div></header>
          <Rule label="New open orders" v={P.new_orders} max={R.min_new_orders} unit="orders" />
          <Rule label="New units" v={P.new_units} max={R.min_new_units} unit="units" />
          <Rule label={`High priority due within ${R.urgent_days} days`} v={P.urgent_count} max={1} unit="orders" />
          <div className={"ag-verdict t-" + (st.would_plan ? "warn" : "ok")}><Icon n={st.would_plan ? "alert" : "check"} size={16} />
            <span>{st.would_plan ? `It will plan at the next check: ${st.why}` : st.why}</span></div>
        </section>
        <Intake intake={intake} can={can("intake")} onDone={async r => { onMsg(`${r.received} orders received. ${r.agent.outcome === "PLANNED" ? "The agent re-planned and assigned the work." : "The agent is watching: " + r.agent.reason}`); await load(); }} onError={onError} />
      </div>

      <section className="sp-card">
        <header className="sp-ch"><div><h3>Work handed over</h3><small className="sp-sub-t">Production manager approves; each plant head prepares their plant</small></div></header>
        <FilterBar value={f} onChange={setF} className="sp-filterbar-in" fields={[
          { key: "status", label: "Show", all: "All tasks", options: [{ value: "active", label: "Open tasks" }] },
          { key: "plant", label: "Plant", all: "All plants", options: plants.map(p => ({ value: p, label: p })) },
        ]} />
        <div className="ag-board">
          {columns.filter(([, p]) => !f.plant || !p || p === f.plant).map(([r, p]) => {
            const list = shown.filter(t => t.role === r && (t.plant_id || null) === p);
            const who = team.find(m => m.role === r && (m.plant_id || null) === p);
            return (
              <div key={r + p} className="ag-col">
                <header><span className="sp-wh-ic"><Icon n={r === "production_manager" ? "check" : "factory"} size={16} /></span>
                  <div><b>{ROLE[r]}{p ? ` · ${p}` : ""}</b><small className="muted">{who ? who.name : "No one assigned yet"}</small></div>
                  <span className="sp-pill t-info">{list.filter(active).length}</span></header>
                {!list.length && <p className="muted ag-empty">Nothing here.</p>}
                {list.map(t => <Task key={t.id} t={t} mine={t.role === role && (t.role !== "plant_head" || t.plant_id === auth.plant)} busy={busy}
                  onAct={(kind, note) => act(t.id, async () => { await api(`/api/agent/tasks/${t.id}/${kind}`, "POST", { note }); onMsg(kind === "done" ? "Marked as done." : "Acknowledged."); })()}
                  onOpen={() => { openSchedule(t.schedule_id); setTab("Review & approve"); }}
                  onProblem={() => { setFocus(t.details.incident_id); document.getElementById("ag-problems")?.scrollIntoView({ behavior: "smooth", block: "start" }); }} />)}
              </div>
            );
          })}
        </div>
      </section>

      <div className="ag-grid">
        <section className="sp-card">
          <header className="sp-ch"><div><h3>Agent log</h3><small className="sp-sub-t">Every check and what it decided</small></div></header>
          {!runs.length ? <p className="muted">No checks yet.</p> : (
            <ol className="sp-timeline ag-log">{runs.map(r => (
              <li key={r.id} className={"t-" + (r.outcome === "PLANNED" ? "ok" : r.outcome === "FAILED" ? "bad" : r.outcome === "INCIDENT" || r.outcome === "ALERT" ? "warn" : "info")}><span className="sp-bullet" /><div>
                <b>{r.outcome === "PLANNED" ? `Planned · ${r.tasks} tasks assigned` : r.outcome === "INCIDENT" ? `Problem analysed · ${r.tasks} tasks assigned` : r.outcome === "ALERT" ? `Alert from the ${r.actor} · ${r.tasks} sent` : "Watching"}{r.trigger !== "auto" && r.outcome !== "ALERT" ? ` · ${nice(r.trigger)}` : ""}</b>
                <small className="muted">{when(r.started_at)} · {r.actor}</small>
                <small>{r.reason}</small>
                {r.schedule_id && <button className="lnk" onClick={() => { openSchedule(r.schedule_id); setTab("Review & approve"); }}>Open the plan ›</button>}
              </div></li>))}
            </ol>)}
        </section>
        <Team team={team} can={can("team")} plants={plants.length ? plants : ["IN-KA-01", "IN-TN-01"]} reload={load} onMsg={onMsg} onError={onError} />
      </div>
    </div>
  );
}

function Rule({ label, v, max, unit }) {
  const pct = Math.min(100, (100 * v) / (max || 1));
  return (
    <div className="ag-rule">
      <div className="sp-row"><small>{label}</small><b>{N(v)} <small className="muted">/ {N(max)} {unit}</small></b></div>
      <span><i className={pct >= 100 ? "full" : ""} style={{ width: `${pct}%` }} /></span>
    </div>
  );
}

export function Task({ t, mine, busy, onAct, onOpen, onProblem }) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const d = t.details || {};
  return (
    <article className={"ag-task t-" + TONE[t.status]}>
      <div className="sp-row"><span className={"sp-pill t-" + TONE[t.status]}>{STATUS[t.status]}</span><small className="muted">{when(t.created_at)}</small></div>
      <b>{t.title}</b>
      <small className="muted">{t.assignee ? `Assigned to ${t.assignee}` : "Unassigned · add someone in Team"}</small>
      {t.kind === "alert" ? (
        <div className="ag-facts">
          <span>From the {d.agent} · <span className={d.severity === "critical" ? "bad" : ""}>{d.severity === "critical" ? "critical" : d.severity === "warn" ? "warning" : "notice"}</span></span>
          {d.materials?.slice(0, 4).map(m => <span key={m.material_id}>{m.material_id}{m.cover_days != null ? `: ${m.cover_days} days of cover` : ""}{m.shortfall ? ` · short ${N(m.shortfall)}` : ""}</span>)}
          {d.machines?.slice(0, 6).map(m => <span key={m.machine_id}>{m.machine_id} · {m.type} · {m.line_id} · due {m.due}</span>)}
        </div>
      ) : d.incident_id && t.kind !== "prepare" ? (
        <div className="ag-facts">
          {d.option && t.kind !== "repair" && <span>Recovery: {d.option}</span>}
          {d.affected_orders != null && <span>{N(d.affected_orders)} orders affected{d.still_late ? ` · ${d.still_late} still miss their date` : ""}</span>}
          {d.findings?.slice(1, 3).map((x, k) => <span key={k}>{x}</span>)}
        </div>
      ) : t.kind === "approve" || t.kind === "review" ? (
        <div className="ag-facts">
          <span>{N(d.new_orders)} new orders · {K(d.new_units)} units</span>
          <span>{N(d.scheduled_qty)} units scheduled · <span className={d.late_jobs ? "bad" : ""}>{N(d.late_jobs)} late jobs</span></span>
          {d.plants?.map(p => <span key={p.plant_id}>{p.plant_id}: {jobs(p.jobs)} · {K(p.units)} units{p.short_materials?.length ? ` · ${p.short_materials.length} materials short` : ""}</span>)}
        </div>
      ) : (
        <div className="ag-facts">
          <span>{jobs(d.jobs)} · {K(d.units)} units{d.late_jobs ? <> · <span className="bad">{d.late_jobs} late</span></> : null}</span>
          {d.first_start && <span>From {fmtT(d.first_start)} to {fmtT(d.last_finish)}</span>}
          {d.products?.length > 0 && <span>Products: {d.products.join(", ")}</span>}
        </div>
      )}
      {open && t.kind === "prepare" && (
        <div className="ag-more">
          {d.lines?.length > 0 && <><small className="muted">Lines</small>{d.lines.map(l => <span key={l.line_id}>{l.line_id}: {jobs(l.jobs)} · {fmtT(l.start)} → {fmtT(l.finish)}</span>)}</>}
          {d.short_materials?.length > 0 && <><small className="muted">Materials short</small>{d.short_materials.map(m => <span key={m.material_id} className="bad">{m.material_id}: {N(m.shortfall_qty)} short</span>)}</>}
          {d.orders?.length > 0 && <><small className="muted">Orders ({d.orders.length})</small><span>{d.orders.slice(0, 12).join(", ")}{d.orders.length > 12 ? "…" : ""}</span></>}
        </div>
      )}
      {t.note && <small className="ag-note">“{t.note}”</small>}
      {t.updated_by && <small className="muted">Last updated by {t.updated_by} · {when(t.updated_at)}</small>}
      <div className="sp-row sp-left sp-wrap">
        {t.kind === "prepare" && <button className="lnk" onClick={() => setOpen(o => !o)}>{open ? "Less" : "Details"}</button>}
        {d.incident_id && <button className="lnk" onClick={onProblem}>{t.kind === "repair" ? "Record the fix ›" : "See the analysis ›"}</button>}
        {t.schedule_id && <button className="lnk" onClick={onOpen}>{t.kind === "approve" ? "Approve on Review & approve ›" : "Open the plan ›"}</button>}
      </div>
      {mine && ["OPEN", "ACKNOWLEDGED"].includes(t.status) && t.kind !== "approve" && (
        <div className="ag-actions">
          <input value={note} maxLength={500} onChange={e => setNote(e.target.value)} placeholder="Add a note (optional)" aria-label="Note" />
          {t.status === "OPEN" && <button className="sp-btn" disabled={busy === t.id} onClick={() => onAct("acknowledge", note || null)}>Acknowledge</button>}
          {t.kind !== "repair" && <button className="sp-btn primary" disabled={busy === t.id} onClick={() => onAct("done", note || null)}><Icon n="check" size={14} /> Done</button>}
        </div>
      )}
      {!mine && active(t) && t.kind !== "approve" && <small className="muted">Only the {ROLE[t.role].toLowerCase()}{t.plant_id ? ` of ${t.plant_id}` : ""} can update this.</small>}
    </article>
  );
}
const active = t => ["OPEN", "WAITING_APPROVAL", "ACKNOWLEDGED"].includes(t.status);

// order intake: a CSV of new orders (the same endpoint an ERP or web shop would call)
function Intake({ intake, can, onDone, onError }) {
  const [rows, setRows] = useState(null);
  const [errs, setErrs] = useState(null);
  const [busy, setBusy] = useState(false);
  const file = useRef();
  const parse = text => {
    const lines = text.trim().split(/\r?\n/).filter(Boolean);
    const head = lines.shift().split(",").map(h => h.trim().toLowerCase());
    return lines.map(l => { const v = l.split(","); const o = {}; head.forEach((h, i) => { if (CSV_COLS.includes(h) && v[i] !== undefined && v[i].trim() !== "") o[h] = v[i].trim(); });
      if (o.order_qty) o.order_qty = +o.order_qty; if (o.unit_price_inr) o.unit_price_inr = +o.unit_price_inr; return o; });
  };
  const template = () => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([CSV_COLS.join(",") + "\n,CUS-001,SPH-A15-128,500,2026-12-15,HIGH,B2B,\n"], { type: "text/csv" }));
    a.download = "order-intake-template.csv"; a.click(); URL.revokeObjectURL(a.href);
  };
  async function send() {
    setBusy(true); setErrs(null);
    try { const r = await api("/api/orders/intake", "POST", { source: "upload", orders: rows }); setRows(null); file.current.value = ""; onDone(r); }
    catch (e) { if (e.api?.code === "INVALID_ORDERS") setErrs(e.api.detail); else onError(e); }
    finally { setBusy(false); }
  }
  return (
    <section className="sp-card">
      <header className="sp-ch"><div><h3>Order intake</h3><small className="sp-sub-t">New orders join the order book and the agent checks right away. Your ERP can send them to the same place.</small></div>
        <button className="lnk" onClick={template}>CSV template</button></header>
      <div className="ag-upload">
        <label className="sp-btn"><Icon n="download" size={14} /> Choose a CSV
          <input ref={file} type="file" accept=".csv,text/csv" hidden disabled={!can}
            onChange={async e => { const fl = e.target.files?.[0]; if (!fl) return; setErrs(null); try { setRows(parse(await fl.text())); } catch { onError(new Error("That file could not be read as CSV")); } }} />
        </label>
        {rows && <span>{rows.length} orders ready · {K(rows.reduce((t, r) => t + (r.order_qty || 0), 0))} units</span>}
        {rows && <button className="sp-btn primary" disabled={busy || !rows.length} onClick={send}>{busy ? "Sending…" : "Send to intake"}</button>}
        {!can && <small className="muted">Planners and admins can add orders.</small>}
      </div>
      {errs && (
        <ul className="sp-checks">{errs.slice(0, 8).map(e => <li key={e.row} className="t-bad"><span className="sp-bullet" /><div><b>Row {e.row} · {e.customer_order_id}</b><small className="muted">{e.problems.join("; ")}</small></div></li>)}
          {errs.length > 8 && <li className="t-bad"><span className="sp-bullet" /><div><b>…and {errs.length - 8} more</b></div></li>}</ul>
      )}
      {!intake.length ? <p className="muted">No orders received through intake yet.</p> : (
        <div className="sp-table ag-intake"><table>
          <thead><tr><th>Order</th><th>Product</th><th className="num">Units</th><th>Due</th><th>Priority</th><th>Received</th></tr></thead>
          <tbody>{intake.slice(0, 8).map(o => (
            <tr key={o.customer_order_id}><td>{o.customer_order_id}</td><td>{o.product_id}</td><td className="num">{N(o.order_qty)}</td><td>{o.requested_delivery_date}</td>
              <td><span className={"sp-pill t-" + (o.priority === "HIGH" ? "bad" : o.priority === "MEDIUM" ? "warn" : "info")}>{nice(o.priority)}</span></td>
              <td><small className="muted">{when(o.received_at)} · {o.source}</small></td></tr>))}</tbody>
        </table></div>
      )}
    </section>
  );
}

export function Team({ team, can, plants, reload, onMsg, onError }) {
  const [v, setV] = useState({ name: "", role: "production_manager", plant_id: plants[0], email: "" });
  async function add(e) {
    e.preventDefault();
    try { await api("/api/agent/team", "POST", { ...v, plant_id: v.role === "plant_head" ? v.plant_id : null, email: v.email || null }); setV({ ...v, name: "", email: "" }); onMsg("Added. Their open tasks are now assigned to them."); await reload(); }
    catch (err) { onError(err); }
  }
  return (
    <section className="sp-card">
      <header className="sp-ch"><div><h3>Team</h3><small className="sp-sub-t">Who receives the agent's tasks and the approved plan emails</small></div></header>
      {!team.length ? <p className="muted">No one yet. Tasks show “unassigned” until the production manager and plant heads are added.</p> : (
        <ul className="ag-team">{team.map(m => (
          <li key={m.id}><span className="sp-avatar ag-av">{m.name.split(/\s+/).map(w => w[0]).join("").slice(0, 2).toUpperCase()}</span>
            <div><b>{m.name}</b><small className="muted">{m.role_label}{m.plant_id ? ` · ${m.plant_id}` : ""}{m.email ? ` · ${m.email}` : ""}</small></div>
            {can && m.email && <button className="lnk" onClick={async () => { try { await api(`/api/agent/team/${m.id}/test-email`, "POST"); onMsg(`Test email sent to ${m.name}.`); } catch (e) { onError(e); } }}>Test email</button>}
            {can && <button className="sp-icon-btn" aria-label={`Remove ${m.name}`} onClick={async () => { try { await api(`/api/agent/team/${m.id}`, "DELETE"); await reload(); } catch (e) { onError(e); } }}><Icon n="close" size={14} /></button>}
          </li>))}</ul>
      )}
      {can ? (
        <form className="ag-add" onSubmit={add}>
          <label className="sp-field"><small>Name</small><input required maxLength={100} value={v.name} onChange={e => setV({ ...v, name: e.target.value })} /></label>
          <label className="sp-field"><small>Role</small><select value={v.role} onChange={e => setV({ ...v, role: e.target.value })}><option value="production_manager">Production manager</option><option value="plant_head">Plant head</option></select></label>
          {v.role === "plant_head" && <label className="sp-field"><small>Plant</small><select value={v.plant_id} onChange={e => setV({ ...v, plant_id: e.target.value })}>{plants.map(p => <option key={p}>{p}</option>)}</select></label>}
          <label className="sp-field"><small>Email (optional)</small><input type="email" maxLength={200} value={v.email} onChange={e => setV({ ...v, email: e.target.value })} /></label>
          <button className="sp-btn primary"><Icon n="plus" size={14} /> Add</button>
        </form>
      ) : <small className="muted">An admin adds people here.</small>}
    </section>
  );
}

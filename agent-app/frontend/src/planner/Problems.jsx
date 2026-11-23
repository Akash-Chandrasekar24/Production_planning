import { useEffect, useRef, useState } from "react";
import { api, auth } from "../api.js";
import { N } from "./ui.jsx";
import { Icon, K } from "./widgets.jsx";
import { nice } from "./Filters.jsx";

// Problems on the floor: a machine stopped or production orders running late. The agent spots them in the production feed (or
// someone reports them), works out what they delay, shows how similar problems were fixed before, compares recovery options and
// proposes one for approval. People decide and record the fix; the fix is what the agent shows next time.

const fmtT = v => (v ? new Date(String(v).slice(0, 19) + "Z").toLocaleString("en", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "UTC" }) : "–");
const fmtD = v => (v ? new Date(String(v).slice(0, 10) + "T00:00:00Z").toLocaleDateString("en", { day: "numeric", month: "short", timeZone: "UTC" }) : "–");
const hrs = (a, b) => (a && b ? Math.round((new Date(String(b).slice(0, 19) + "Z") - new Date(String(a).slice(0, 19) + "Z")) / 36e5) : null);
const S = (n, w) => `${N(n)} ${w}${n === 1 ? "" : "s"}`;

export default function Problems({ role, can, rules, focus, setFocus, openSchedule, setTab, onMsg, onError, onChange }) {
  const [list, setList] = useState(null);
  const [meta, setMeta] = useState({ lines: [], machines: [], open_production_orders: [] });
  const [cur, setCur] = useState(null);
  const [report, setReport] = useState(false);
  const [busy, setBusy] = useState("");
  const shown = useRef(null);                          // the problem on screen (a ref, so a reload after an action keeps the newest choice)
  const show = i => { shown.current = i?.id || null; setCur(i); };

  const load = async () => {
    const r = await api("/api/agent/incidents");
    setList(r.incidents); setMeta(r);
    const id = focus || shown.current || r.incidents.find(i => i.status === "OPEN")?.id || r.incidents[0]?.id;
    show(id ? await api(`/api/agent/incidents/${id}`) : null);
  };
  useEffect(() => { if (focus) shown.current = focus; load().catch(onError); }, [focus]);   // eslint-disable-line react-hooks/exhaustive-deps

  const open = async id => { setFocus(null); try { show(await api(`/api/agent/incidents/${id}`)); } catch (e) { onError(e); } };
  const run = (key, fn) => async (...a) => { setBusy(key); try { await fn(...a); await load(); onChange(); } catch (e) { onError(e); } finally { setBusy(""); } };
  const detect = run("detect", async () => {
    const r = await api("/api/agent/detect", "POST");
    onMsg(r.raised.length ? `The agent found ${S(r.raised.length, "new problem")} and handed over the work.` : "No new problems in the production feed or production orders.");
    if (r.raised[0]) show(r.raised[0]);
  });

  return (
    <section className="sp-card" id="ag-problems">
      <header className="sp-ch">
        <div><h3>Problems on the floor</h3><small className="sp-sub-t">Machine stops and late production orders: what they delay, how they were fixed before, and how to recover</small></div>
        <div className="sp-row sp-wrap sp-ch-r">
          {can("agent") && <button className="sp-btn" disabled={!!busy} onClick={detect}><Icon n="refresh" size={14} /> {busy === "detect" ? "Looking…" : "Look now"}</button>}
          {can("incident") && <button className="sp-btn primary" onClick={() => setReport(r => !r)} aria-expanded={report}><Icon n="alert" size={14} /> Report a problem</button>}
        </div>
      </header>
      {report && <Report meta={meta} role={role} busy={busy === "report"} onCancel={() => setReport(false)}
        onSend={run("report", async body => { const r = await api("/api/agent/incidents", "POST", body); setReport(false); show(r);
          onMsg(r.proposal_id ? "The agent analysed it and sent a recovery plan for approval." : "The agent analysed it and told the people involved."); })} />}
      {!list ? <p className="muted" role="status">Loading…</p> : !list.length ? (
        <p className="muted">No problems so far. The agent checks the production feed for breakdowns and the production orders for late ones every few minutes.</p>
      ) : (
        <div className="ag-prob">
          <ul className="ag-plist" aria-label="Problems">
            {list.map(i => (
              <li key={i.id}><button className={"ag-pitem t-" + (i.status === "OPEN" ? "warn" : "ok") + (cur?.id === i.id ? " on" : "")} aria-current={cur?.id === i.id} onClick={() => open(i.id)}>
                <span className="sp-row"><Icon n={i.kind === "MACHINE_DOWN" ? "factory" : "calendar"} size={14} /><b>{i.headline}</b></span>
                <small className="muted">{i.source === "detected" ? "Spotted by the agent" : `Reported by ${i.reported_by}`} · {fmtT(i.created_at)}</small>
                <span className="sp-row sp-left sp-wrap"><span className={"sp-pill t-" + (i.status === "OPEN" ? "warn" : "ok")}>{i.status === "OPEN" ? "Being handled" : "Fixed"}</span>
                  {i.option_name && <small>{i.option_name}</small>}</span>
              </button></li>))}
          </ul>
          {cur && <Problem i={cur} role={role} can={can} rules={rules} busy={busy} openSchedule={openSchedule} setTab={setTab}
            onChoose={run("choose", async k => { await api(`/api/agent/incidents/${cur.id}/choose`, "POST", { option: k }); onMsg("Done. That option now waits for approval instead."); })}
            onFix={run("fix", async b => { const r = await api(`/api/agent/incidents/${cur.id}/resolve`, "POST", b);
              onMsg(r.replan ? "Recorded. The line is back, so the agent re-planned and sent the new plan for approval." : "Recorded. The agent will show this fix next time."); })} />}
        </div>
      )}
    </section>
  );
}

function Report({ meta, role, busy, onCancel, onSend }) {
  const mine = l => role !== "plant_head" || l.plant_id === auth.plant;
  const lines = meta.lines.filter(mine);
  const [v, setV] = useState({ kind: "MACHINE_DOWN", line_id: lines[0]?.line_id || "", machine_id: "", started_at: "", repair_hours: "", pos: "", note: "" });
  const set = k => e => setV({ ...v, [k]: e.target.value });
  const submit = e => {
    e.preventDefault();
    onSend(v.kind === "MACHINE_DOWN"
      ? { kind: v.kind, line_id: v.line_id, machine_id: v.machine_id || null, started_at: v.started_at ? new Date(v.started_at).toISOString() : null,
          repair_hours: v.repair_hours ? +v.repair_hours : null, note: v.note || null }
      : { kind: v.kind, production_orders: v.pos.split(/[\s,;]+/).filter(Boolean), note: v.note || null });
  };
  return (
    <form className="ag-report" onSubmit={submit}>
      <label className="sp-field"><small>What happened</small>
        <select value={v.kind} onChange={set("kind")}><option value="MACHINE_DOWN">A machine stopped</option><option value="PO_DELAY">Production orders are running late</option></select></label>
      {v.kind === "MACHINE_DOWN" ? <>
        <label className="sp-field"><small>Line</small><select required value={v.line_id} onChange={e => setV({ ...v, line_id: e.target.value, machine_id: "" })}>
          {lines.map(l => <option key={l.line_id} value={l.line_id}>{l.line_id} · {l.plant_id}</option>)}</select></label>
        <label className="sp-field"><small>Machine (optional)</small><select value={v.machine_id} onChange={set("machine_id")}><option value="">Not sure / whole line</option>
          {meta.machines.filter(m => m.line_id === v.line_id).map(m => <option key={m.machine_id} value={m.machine_id}>{m.machine_id} · {m.name}</option>)}</select></label>
        <label className="sp-field"><small>Stopped at (empty = now)</small><input type="datetime-local" value={v.started_at} onChange={set("started_at")} /></label>
        <label className="sp-field"><small>Expected repair time, hours</small><input type="number" min="0.5" max="720" step="0.5" value={v.repair_hours} onChange={set("repair_hours")} placeholder="Unknown" /></label>
      </> : (
        <label className="sp-field ag-wide"><small>Production orders (comma separated)</small>
          <input required list="ag-open-pos" value={v.pos} onChange={set("pos")} placeholder="PO-000123, PO-000124" />
          <datalist id="ag-open-pos">{meta.open_production_orders.slice(0, 300).map(p => <option key={p} value={p} />)}</datalist></label>
      )}
      <label className="sp-field ag-wide"><small>Note (optional)</small><input maxLength={500} value={v.note} onChange={set("note")} placeholder="What you see, e.g. feeder jam, waiting for a spare part" /></label>
      <div className="sp-row sp-left">
        <button className="sp-btn primary" disabled={busy}>{busy ? "Analysing…" : "Send to the agent"}</button>
        <button type="button" className="sp-btn" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

function Problem({ i, role, can, rules, busy, openSchedule, setTab, onChoose, onFix }) {
  const a = i.analysis || {}, im = a.impact || {}, h = a.history || {}, f = h.feed || {};
  const [fix, setFix] = useState("");
  const [hours, setHours] = useState("");
  const isOpen = i.status === "OPEN";
  const canFix = isOpen && (["planner", "production_manager"].includes(role) || (role === "plant_head" && auth.plant === i.plant_id));
  const plan = id => { openSchedule(id); setTab("Review & approve"); };
  return (
    <article className="ag-pdetail">
      <div className="sp-row sp-wrap">
        <div><h4>{i.headline}</h4><small className="muted">{i.description}</small></div>
        <span className={"sp-pill t-" + (isOpen ? "warn" : "ok")}>{isOpen ? "Being handled" : "Fixed"}</span>
      </div>

      <div className="ag-kv">
        <span><small>Started</small><b>{fmtT(i.started_at)}</b></span>
        {i.kind === "MACHINE_DOWN" && <span><small>{i.end_assumed ? "Back by (assumed)" : "Back by"}</small><b>{fmtT(i.expected_end)}</b><small>{hrs(i.started_at, i.expected_end)} h</small></span>}
        {i.kind === "MACHINE_DOWN" && <span><small>Line time lost in the plan</small><b>{N(Math.round(im.lost_minutes / 60))} h</b><small>about {K(im.lost_units)} units</small></span>}
        <span><small>Orders affected</small><b>{N(im.affected_orders)}</b><small>{S(im.customers, "customer")}</small></span>
        {i.kind === "MACHINE_DOWN" && <span><small>If we just wait</small><b>{S(im.orders_delayed, "order")} later</b><small>about {im.avg_delay_h} h each</small></span>}
      </div>

      <ul className="sp-checks">{(a.findings || []).map((t, k) => <li key={k} className="t-info"><span className="sp-bullet" /><div><small>{t}</small></div></li>)}</ul>

      <h5>How it was handled before</h5>
      {h.recorded?.length ? (
        <ul className="ag-past">{h.recorded.map((r, k) => (
          <li key={k}><b>“{r.fix}”</b><small className="muted">{r.by} · {fmtD(r.when)} · {r.where} ({r.match}){r.repair_hours != null ? ` · back after ${r.repair_hours} h` : ""}{r.option ? ` · recovered by: ${r.option.toLowerCase()}` : ""}</small></li>))}</ul>
      ) : <p className="muted ag-small">No fix has been recorded for a problem like this yet. When it is fixed, record what was done below and the agent shows it next time.</p>}
      {i.kind === "MACHINE_DOWN" && f.days > 0 && (
        <p className="ag-small">Production feed: {i.line_id} had {rules?.fault_min ?? 60}+ minutes of equipment faults on {S(f.breakdown_days, "day")} of the last {f.days}
          {f.breakdown_days ? <> (typically {f.typical_min} min, worst {f.worst_min} min, last on {fmtD(f.last)}).{f.next_day_output_pct != null && <> The day after, it made {f.next_day_output_pct}% of its target.</>}</> : "."}</p>)}
      {i.kind === "PO_DELAY" && f.finished_orders > 0 && (
        <p className="ag-small">Of {S(f.finished_orders, "finished production order")} at {i.plant_id}, {N(f.late_finished)} finished after their due date{f.typical_days_late ? `, typically ${f.typical_days_late} days late` : ""}.</p>)}

      <h5>Recovery options</h5>
      <div className="sp-table ag-opts"><table>
        <thead><tr><th>Option</th><th className="num">Orders later</th><th className="num">Affected orders late</th><th className="num">Moved</th><th>People need to</th><th /></tr></thead>
        <tbody>{(a.options || []).map(o => (
          <tr key={o.key} className={o.recommended ? "ag-rec" : ""}>
            <td><b>{o.name}</b>{o.recommended && <span className="sp-pill t-ok">Agent's pick</span>}{i.option === o.key && <span className="sp-pill t-info">{i.proposal_id ? "Sent for approval" : "Chosen"}</span>}<br /><small className="muted">{o.what}</small></td>
            <td className="num">{N(o.orders_delayed)}<br /><small className="muted">{o.orders_delayed ? `~${o.avg_delay_h} h` : "–"}</small></td>
            <td className="num">{N(o.affected_late)}<br /><small className="muted">{o.affected_late ? `${o.affected_days_late} d late` : "–"}</small></td>
            <td className="num">{N(o.moved_orders)}</td>
            <td><small>{o.needs.length ? o.needs.join("; ") : "Nothing extra"}</small></td>
            <td>{isOpen && can("agent") && i.option !== o.key && <button className="sp-btn" disabled={!!busy} onClick={() => onChoose(o.key)}>Use this</button>}</td>
          </tr>))}</tbody>
      </table></div>
      {i.proposal_id && <button className="lnk" onClick={() => plan(i.proposal_id)}>Open the recovery plan on Review & approve ›</button>}

      {a.still_late?.length > 0 && <>
        <h5>Orders that still miss their date</h5>
        <p className="muted ag-small">Ship what is ready on the due date and agree a new date for the rest. The agent never changes dates or contacts customers.</p>
        <div className="sp-table ag-late"><table>
          <thead><tr><th>Order</th><th>Customer</th><th>Priority</th><th className="num">Units</th><th>Due</th><th className="num">Ready by due</th><th>New date</th><th>What to do</th></tr></thead>
          <tbody>{a.still_late.map(r => (
            <tr key={r.order_id}><td>{r.order_id}{r.was_late && <><br /><small className="muted">already late</small></>}</td>
              <td>{r.production_order ? <small>for {r.customer_id}</small> : r.customer_id}</td>
              <td><span className={"sp-pill t-" + (r.priority === "HIGH" ? "bad" : r.priority === "MEDIUM" ? "warn" : "info")}>{nice(r.priority)}</span></td>
              <td className="num">{N(r.qty)}</td><td>{fmtD(r.due)}</td><td className="num">{N(r.ready_by_due)}</td><td>{fmtD(r.new_date)}</td><td><small>{r.action}</small></td></tr>))}</tbody>
        </table></div>
      </>}
      {a.customers?.length > 0 && <>
        <h5>By customer</h5>
        <div className="ag-cust">{a.customers.map(c => (
          <span key={c.customer_id} className={"t-" + (c.late_orders ? "warn" : "ok")}><b>{c.customer_id}</b>
            <small>{S(c.orders, "order")} · {K(c.units)} units · {c.late_orders ? `${c.late_orders} late` : "on time"}</small></span>))}</div>
      </>}

      {isOpen ? (canFix ? (
        <form className="ag-fix" onSubmit={e => { e.preventDefault(); onFix({ fix, repair_hours: hours ? +hours : null }); }}>
          <h5>Fixed it? Record what was done</h5>
          <label className="sp-field"><small>What fixed it</small><textarea required minLength={3} maxLength={1000} rows={2} value={fix} onChange={e => setFix(e.target.value)}
            placeholder="e.g. Replaced the nozzle head; spare kept at the line" /></label>
          {i.kind === "MACHINE_DOWN" && <label className="sp-field"><small>How long it was down, hours (optional)</small><input type="number" min="0.5" max="720" step="0.5" value={hours} onChange={e => setHours(e.target.value)} /></label>}
          <button className="sp-btn primary" disabled={busy === "fix"}><Icon n="check" size={14} /> {busy === "fix" ? "Saving…" : "Mark as fixed"}</button>
        </form>
      ) : <small className="muted">The plant head of {i.plant_id}, the production manager or a planner records the fix.</small>) : (
        <div className="sp-callout t-ok"><span className="sp-callout-ic"><Icon n="check" size={18} /></span>
          <div className="sp-callout-t"><b>Fixed by {i.resolved_by} · {fmtT(i.resolved_at)}</b><span>“{i.resolution}”</span></div></div>
      )}
    </article>
  );
}

import { useEffect, useState } from "react";
import { api } from "../api.js";
import { N } from "./ui.jsx";
import { Icon, K } from "./widgets.jsx";
import { DataTable, KpiStrip } from "./PlanningViews.jsx";
import { Segmented } from "./Filters.jsx";

// Three planning pages in the sidebar: Material readiness, What-if, Floor problems. Every figure comes from the API.

const PLANT = { "IN-TN-01": "Hosur", "IN-KA-01": "Bengaluru" };
const where = p => PLANT[p] || p;
const inr = v => (v == null ? "–" : v >= 1e7 ? `₹${(v / 1e7).toFixed(1)} Cr` : v >= 1e5 ? `₹${(v / 1e5).toFixed(1)} L` : `₹${N(Math.round(v))}`);
const day = v => (v ? new Date(String(v).slice(0, 10) + "T00:00").toLocaleDateString("en", { day: "numeric", month: "short" }) : "–");
const pill = (tone, text) => <span className={"sp-pill t-" + tone}>{text}</span>;

// ------------------------------------------------------------------ Material readiness
export function Readiness({ s, onError }) {
  const [r, setR] = useState(null);
  const [plant, setPlant] = useState("");
  useEffect(() => {
    let live = true;
    setR(null);
    api(`/api/plan-tools/readiness?schedule_id=${encodeURIComponent(s.id)}`).then(j => live && setR(j)).catch(e => live && (setR({ failed: true }), onError?.(e)));
    return () => { live = false; };
  }, [s.id]);                                                         // eslint-disable-line react-hooks/exhaustive-deps
  if (!r) return <div className="sp-card"><p className="muted" role="status">Checking the plan against the stock…</p></div>;
  if (r.failed) return <div className="sp-card"><p className="muted">Material readiness isn't available right now.</p></div>;
  const byPlant = x => !plant || x.plant_id === plant;
  const kits = r.kits.filter(byPlant), risk = r.risk.filter(byPlant);
  const first = r.runs_out[0];
  const more = Math.max(0, ...r.room.map(x => x.more_qty));
  const lim = [...new Set(r.room.filter(x => x.limiting).map(x => x.limiting))];
  const scope = r.pooled ? "Both plants share stock in this plan" : "Each plant uses its own stock in this plan";
  return (
    <>
      <div className="mr-head">
        <p className="muted"><Icon n="info" size={14} /> {scope}{r.safety_reserved ? ", and safety stock is kept back" : ""}. Figures follow the planner's own material rules.</p>
        <Segmented label="Plant" options={[{ value: "", label: "Both plants" }, { value: "IN-TN-01", label: "Hosur" }, { value: "IN-KA-01", label: "Bengaluru" }]} value={plant} onChange={setPlant} />
      </div>
      <KpiStrip items={[
        { label: "More phones the stock can build", num: more, tone: more ? "ok" : "bad", note: more ? "beyond this plan, best product" : `held back by ${lim.join(", ") || "material"}` },
        { label: "First material to run out", value: first ? first.material_id : "None", tone: first ? "bad" : "ok", note: first ? `used up ${day(first.date)}` : "stock covers the plan" },
        { label: "Orders held back by material", num: r.held.length, tone: r.held.length ? "warn" : "ok", note: `${K(r.held_units)} units` },
        { label: "Value held back", value: inr(r.held_value_inr), tone: r.held_value_inr ? "warn" : "ok", note: "orders not (fully) scheduled" },
        { label: "Jobs at risk from material", num: risk.length, tone: risk.length ? "warn" : "ok", note: plant ? where(plant) : "both plants" },
      ]} />

      <section className="sp-card">
        <header className="sp-ch"><div><h3>What holds the plan back</h3>
          <small className="sp-sub-t">Phones the stock left after this plan can still build, and the material that stops more</small></div></header>
        <div className="mr-room">
          {r.room.map(x => (
            <div key={x.scope + x.product_id} className={"mr-room-i" + (x.more_qty ? "" : " bad")}>
              <small className="muted">{x.product_id} · {x.scope === "Both plants" ? "both plants" : where(x.scope)}</small>
              <b>{x.more_qty ? `+${N(x.more_qty)} more` : "No more"}</b>
              <span>{N(x.planned_qty)} in this plan · limited by <b>{x.limiting || "–"}</b></span>
            </div>))}
        </div>
        {r.runs_out.length > 0 && (
          <ul className="mr-out">{r.runs_out.map(m => (
            <li key={m.material_id + m.scope}><Icon n="alert" size={14} /> <b>{m.material_id}</b> is used up on {day(m.date)} ({m.scope === "Both plants" ? "both plants" : where(m.scope)}): nothing that needs it can be added after that without new stock.</li>))}
          </ul>)}
      </section>

      <DataTable title="Orders held back by material" sub="Orders the plan could not schedule, or only partly, because a material ran out · most urgent first" pageSize={10}
        empty="No order is held back by material." rows={r.held} cols={[
          { key: "order_id", label: "Order" }, { key: "product_id", label: "Product" },
          { key: "qty", label: "Units held back", num: true }, { key: "priority", label: "Priority", render: x => x.priority.charAt(0) + x.priority.slice(1).toLowerCase() },
          { key: "due", label: "Due", render: x => day(x.due) },
          { key: "partly", label: "Scheduled", render: x => x.partly ? pill("warn", "Partly") : pill("bad", "Not at all") },
          { key: "limiting", label: "Waiting for" }, { key: "value_inr", label: "Value", num: true, render: x => inr(x.value_inr) }]} />

      <DataTable title="Material to each line, day by day" sub="What has to be issued to each circuit-board line so its jobs start on time" pageSize={14}
        empty="No material is issued in this plan." rows={kits} cols={[
          { key: "date", label: "Day", render: x => day(x.date) }, { key: "line_id", label: "Line", render: x => `${x.line_id} · ${where(x.plant_id)}` },
          { key: "jobs", label: "Jobs", num: true }, { key: "phones", label: "Phones", num: true }, { key: "parts", label: "Parts to issue", num: true },
          { key: "runs_out", label: "Note", render: x => x.runs_out.length ? pill("bad", `Uses the last of ${x.runs_out.join(", ")}`) : <span className="muted">Covered</span> }]} />

      <DataTable title="Jobs at risk from material" sub="Scheduled jobs the stock only partly covers, or that use the last of a material · earliest first" pageSize={10}
        empty="No scheduled job is at risk from material." rows={risk} cols={[
          { key: "start", label: "Starts", render: x => day(x.start) }, { key: "order_id", label: "Order" }, { key: "product_id", label: "Product" },
          { key: "qty", label: "Units", num: true }, { key: "line_id", label: "Line", render: x => `${x.line_id} · ${where(x.plant_id)}` },
          { key: "due", label: "Due", render: x => day(x.due) }, { key: "why", label: "Why", render: x => pill(x.late ? "bad" : "warn", x.why) }]} />
    </>
  );
}

// ------------------------------------------------------------------ What-if
const KINDS = [
  { value: "demand", label: "Demand change" }, { value: "line", label: "Line down" }, { value: "absent", label: "Crew absence" },
  { value: "material", label: "Material shortage" }, { value: "urgent", label: "Urgent order" }];
// better: 1 = higher is better, -1 = lower is better, 0 = neither
const METRICS = [
  ["on_time_qty_pct", "On-time delivery", v => `${v}%`, 1], ["late_jobs", "Late jobs", N, -1], ["scheduled_qty", "Units scheduled", N, 1],
  ["unscheduled_qty", "Units not scheduled", N, -1], ["avg_utilisation", "Capacity utilisation", v => `${(v * 100).toFixed(1)}%`, 0],
  ["changeovers", "Changeovers", N, -1], ["changeover_min", "Changeover time", v => `${N(Math.round(v / 6) / 10)} h`, -1],
  ["wip_unit_days_proxy", "Work in progress (unit-days, estimate)", N, -1], ["stability_pct", "Orders unchanged", v => `${v}%`, 1],
  ["material_cost_inr", "Material cost", inr, 0], ["late_order_value_inr", "Value of late orders", inr, -1]];
const local = d => new Date(d.getTime() - d.getTimezoneOffset() * 6e4).toISOString().slice(0, 16);

export function WhatIf({ s, refData, list, onSaved, onError, onAdopted }) {
  const start = s.as_of ? new Date(String(s.as_of).slice(0, 19)) : new Date();
  const [kind, setKind] = useState("demand");
  const [f, setF] = useState({ product: "", pct: 20, line: "", from: local(start), to: local(new Date(start.getTime() + 864e5)), absent: 15,
    material: "*", avail: 50, qty: 5000, due: local(new Date(start.getTime() + 7 * 864e5)).slice(0, 10) });
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState(null);                                  // {name, rows: [baseline, what-if]}
  const set = k => e => setF(x => ({ ...x, [k]: e.target.value }));
  const P = refData?.products || [], L = refData?.lines || [], M = refData?.materials || [];
  const earlier = (list || []).filter(x => x.base_id === s.id && !x.adopted);
  const [ask, setAsk] = useState(0);                                   // 0 closed, 1 "add it?", 2 "the plan will change"
  const [adopting, setAdopting] = useState(false);
  const adopt = async () => {
    setAdopting(true);
    try { const r = await api(`/api/schedules/${res.id}/adopt`, "POST"); setAsk(0); onSaved?.(); onAdopted?.(r.schedule_id); }
    catch (err) { setAsk(0); onError?.(err); } finally { setAdopting(false); }
  };
  useEffect(() => { setRes(null); }, [s.id]);

  const describe = () => ({
    demand: `Demand ${f.pct >= 0 ? "+" : ""}${f.pct}% · ${f.product || "all products"}`,
    line: `${f.line || L[0]} down ${day(f.from)} – ${day(f.to)}`,
    absent: `${f.absent}% of crews absent`,
    material: `${f.material === "*" ? "All materials" : f.material} at ${f.avail}% of stock`,
    urgent: `Urgent order: ${N(+f.qty)} × ${f.product || P[0]} by ${day(f.due)}`,
  }[kind]);
  const body = () => {
    const sc = { name: describe() };
    if (kind === "demand") sc.demand_spikes = [{ product_id: f.product || null, multiplier: 1 + +f.pct / 100 }];
    if (kind === "line") sc.machine_losses = [{ line_id: f.line || L[0], start: f.from, end: f.to }];
    if (kind === "absent") sc.absenteeism_pct = +f.absent;
    if (kind === "material") sc.material_factor = { [f.material]: +f.avail / 100 };
    if (kind === "urgent") sc.urgent_orders = [{ product_id: f.product || P[0], qty: +f.qty, due: f.due }];
    return sc;
  };
  const go = async e => {
    e.preventDefault();
    setBusy(true); setRes(null);
    try {
      const sc = body();
      const r = await api(`/api/schedules/${s.id}/scenarios`, "POST", sc);
      setRes({ id: r.id, name: sc.name, rows: r.comparison });
      onSaved?.();
    } catch (err) { onError?.(err); } finally { setBusy(false); }
  };
  const open = async x => {
    setBusy(true);
    try { setRes({ id: x.id, name: x.name, rows: await api(`/api/compare?base=${s.id}&ids=${x.id}`) }); } catch (err) { onError?.(err); } finally { setBusy(false); }
  };
  const [b, w] = res?.rows || [];
  const verdict = () => {
    const d = (w.on_time_qty_pct ?? 0) - (b.on_time_qty_pct ?? 0), late = (w.late_jobs ?? 0) - (b.late_jobs ?? 0);
    if (Math.abs(d) < 0.05 && !late) return ["ok", "The plan absorbs this: on-time delivery and late jobs don't change."];
    return [d < 0 || late > 0 ? "bad" : "ok", `On-time delivery ${d >= 0 ? "rises" : "drops"} by ${Math.abs(d).toFixed(1)} points and late jobs ${late > 0 ? `rise by ${late}` : late < 0 ? `fall by ${-late}` : "stay the same"}.`];
  };

  return (
    <>
      <form className="sp-card wi" onSubmit={go}>
        <header className="sp-ch"><div><h3>Test a change against this plan</h3>
          <small className="sp-sub-t">The planner re-plans every order with the change and compares the result. Nothing in the real plan changes.</small></div></header>
        <Segmented label="Change" options={KINDS} value={kind} onChange={v => { setKind(v); setRes(null); }} />
        <div className="wi-fields">
          {(kind === "demand" || kind === "urgent") && (
            <label className="wi-f"><span>Product</span><select value={f.product} onChange={set("product")}>
              {kind === "demand" && <option value="">All products</option>}{P.map(p => <option key={p}>{p}</option>)}</select></label>)}
          {kind === "demand" && <label className="wi-f"><span>Change in demand (%)</span><input type="number" min={-90} max={300} step={5} value={f.pct} onChange={set("pct")} /></label>}
          {kind === "line" && <>
            <label className="wi-f"><span>Line</span><select value={f.line || L[0] || ""} onChange={set("line")}>{L.map(l => <option key={l}>{l}</option>)}</select></label>
            <label className="wi-f"><span>Down from</span><input type="datetime-local" value={f.from} onChange={set("from")} /></label>
            <label className="wi-f"><span>Back on</span><input type="datetime-local" value={f.to} onChange={set("to")} /></label></>}
          {kind === "absent" && <label className="wi-f"><span>Crews absent (% of line time lost)</span><input type="number" min={1} max={90} value={f.absent} onChange={set("absent")} /></label>}
          {kind === "material" && <>
            <label className="wi-f"><span>Material</span><select value={f.material} onChange={set("material")}><option value="*">All materials</option>{M.map(m => <option key={m}>{m}</option>)}</select></label>
            <label className="wi-f"><span>Stock still usable (%)</span><input type="number" min={0} max={100} step={5} value={f.avail} onChange={set("avail")} /></label></>}
          {kind === "urgent" && <>
            <label className="wi-f"><span>Units</span><input type="number" min={1} max={1000000} value={f.qty} onChange={set("qty")} /></label>
            <label className="wi-f"><span>Needed by</span><input type="date" value={f.due} onChange={set("due")} /></label></>}
        </div>
        <div className="wi-go">
          <span className="muted"><b>{describe()}</b></span>
          <button className="sp-btn primary" disabled={busy}><Icon n="play" size={14} /> {busy ? "Re-planning…" : "Run what-if"}</button>
        </div>
      </form>

      {busy && !res && <div className="sp-card"><p className="muted" role="status">The planner is re-planning every order with this change…</p></div>}
      {res && b && w && (
        <section className="sp-card wi-res" aria-live="polite">
          <header className="sp-ch"><div><h3>{res.name}</h3><small className="sp-sub-t">Against {s.name} · saved as a what-if, not a plan to approve</small></div></header>
          <p className={"wi-verdict t-" + verdict()[0]}><Icon n={verdict()[0] === "ok" ? "check" : "alert"} size={16} /> {verdict()[1]}</p>
          {res.id && (
            <div className="wi-adopt">
              <div><b>Want this change in the real plan?</b><small className="muted">The planning agent re-plans every order with it. The production manager approves the new plan.</small></div>
              <button className="sp-btn primary" onClick={() => setAsk(1)}><Icon n="plus" size={14} /> Add this change to the plan</button>
            </div>)}
          <div className="sp-table"><table>
            <thead><tr><th>Measure</th><th className="num">This plan</th><th className="num">With the change</th><th className="num">Change</th></tr></thead>
            <tbody>{METRICS.filter(([k]) => b[k] != null || w[k] != null).map(([k, label, fmt, better]) => {
              const d = w[`d_${k}`] ?? (w[k] != null && b[k] != null ? w[k] - b[k] : null);
              const tone = !d || !better ? "" : d * better > 0 ? "up" : "down";
              return (
                <tr key={k}><td>{label}</td><td className="num">{b[k] == null ? "–" : fmt(b[k])}</td><td className="num"><b>{w[k] == null ? "–" : fmt(w[k])}</b></td>
                  <td className={"num wi-d " + tone}>{d == null || Math.abs(d) < 1e-9 ? "–" : `${d > 0 ? "+" : "−"}${fmt(Math.abs(d))}`}</td></tr>);
            })}</tbody>
          </table></div>
        </section>)}

      {ask > 0 && res && b && w && (
        <div className="wi-modal" role="presentation" onMouseDown={e => { if (e.target === e.currentTarget && !adopting) setAsk(0); }}>
          <div className="wi-dlg" role="alertdialog" aria-modal="true" aria-labelledby="wi-dlg-t" aria-describedby="wi-dlg-d">
            {ask === 1 ? (
              <>
                <span className="wi-dlg-ic"><Icon n="plus" size={20} /></span>
                <h3 id="wi-dlg-t">Add this change to the plan?</h3>
                <p id="wi-dlg-d"><b>{res.name}</b> becomes part of the plan, and the planning agent works out the plan again with it.</p>
                <div className="wi-dlg-a"><button className="sp-btn" onClick={() => setAsk(0)} autoFocus>No, keep it as a what-if</button>
                  <button className="sp-btn primary" onClick={() => setAsk(2)}>Yes, add it</button></div>
              </>
            ) : (
              <>
                <span className="wi-dlg-ic warn"><Icon n="alert" size={20} /></span>
                <h3 id="wi-dlg-t">The plan will change</h3>
                <p id="wi-dlg-d">The agent re-plans every order with <b>{res.name}</b>. The new plan replaces <b>{s.name}</b> as the plan waiting for approval:</p>
                <ul className="wi-dlg-ch">{[["on_time_qty_pct", "On-time delivery", v => `${v}%`], ["late_jobs", "Late jobs", N], ["unscheduled_qty", "Units not scheduled", N],
                  ["avg_utilisation", "Line use", v => `${(v * 100).toFixed(1)}%`]].filter(([k]) => b[k] != null && w[k] != null).map(([k, l, fm]) => (
                  <li key={k}><span>{l}</span><b>{fm(b[k])}</b><Icon n="arrow" size={12} /><b>{fm(w[k])}</b></li>))}</ul>
                <p className="muted">Nothing is released until the production manager approves it.</p>
                <div className="wi-dlg-a"><button className="sp-btn" onClick={() => setAsk(0)} disabled={adopting}>Cancel</button>
                  <button className="sp-btn primary" onClick={adopt} disabled={adopting} autoFocus>{adopting ? "The agent is re-planning…" : "Yes, change the plan"}</button></div>
              </>
            )}
          </div>
        </div>)}

      <section className="sp-card">
        <header className="sp-ch"><div><h3>Earlier what-ifs on this plan</h3><small className="sp-sub-t">Open one to see its comparison again</small></div></header>
        {!earlier.length ? <p className="muted">None yet. Run one above.</p> : (
          <ul className="wi-list">{earlier.map(x => (
            <li key={x.id}><button className="lnk" onClick={() => open(x)}>{x.name}</button>
              <small className="muted">{x.created_by ? `by ${x.created_by} · ` : ""}{x.created_at ? new Date(String(x.created_at).slice(0, 19) + "Z").toLocaleString("en", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : ""}</small></li>))}
          </ul>)}
      </section>
    </>
  );
}


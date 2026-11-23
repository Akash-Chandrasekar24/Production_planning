import { useEffect, useState } from "react";
import { api } from "../api.js";
import { N } from "./ui.jsx";
import { Icon } from "./widgets.jsx";
import FinalPlan from "./FinalPlan.jsx";

// The agent's five plans from the same data. The agent recommends one and says why; each plan opens as a full one-page production plan;
// the production manager chooses one, which is approved (then each plant's production head confirms it for their plant).
const inr = v => (v == null ? "–" : v >= 1e7 ? `₹${(v / 1e7).toFixed(2)} Cr` : v >= 1e5 ? `₹${(v / 1e5).toFixed(1)} L` : `₹${N(v)}`);
const day = d => (d ? new Date(String(d).slice(0, 10) + "T00:00:00").toLocaleDateString("en", { day: "numeric", month: "short" }) : "–");
const when = t => (t ? new Date(String(t).endsWith("Z") ? t : t + "Z").toLocaleString("en", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "");
const LETTER = ["A", "B", "C", "D", "E"];

// rows of the side-by-side comparison: label, metric, format, which way is better (1 higher, -1 lower, 0 not ranked)
const ROWS = [
  ["On time (requested dates)", "on_time_pct", v => (v == null ? "–" : `${v}%`), 1],
  ["Orders late", "late_orders", N, -1],
  ["Late penalties", "late_penalty_inr", inr, -1],
  ["Jobs with parts by express", "express_jobs", N, 0],
  ["Express premium", "express_premium_inr", inr, -1],
  ["Extra line hours (shifts)", "extra_shift_h", v => `${N(v)} h`, 0],
  ["Extra shift labour", "extra_shift_inr", inr, -1],
  ["Orders outsourced", "outsourced_orders", N, 0],
  ["Outsourcing cost", "outsource_inr", inr, -1],
  ["Total cost incl. late penalties", "total_cost_inr", inr, -1],
  ["Product changeovers", "changeovers", N, -1],
  ["Line utilisation", "utilisation_pct", v => `${v}%`, 0],
  ["Jobs waiting for bought parts", "purchase_jobs", N, -1],
  ["Last job finishes", "finish", day, 0],
];
// what a plan spends beyond running the lines: its lever
const spend = m => (m.outsource_inr ? ["Outsourcing", m.outsource_inr, `${N(m.outsourced_orders)} orders`] : m.extra_shift_inr ? ["Extra shifts", m.extra_shift_inr, `${N(m.extra_shift_h)} line hours`]
  : m.express_premium_inr ? ["Express parts", m.express_premium_inr, `${N(m.express_jobs)} jobs`] : ["Extra spend", 0, "none"]);
const bestOf = (plans, k, dir) => {
  const v = plans.map(p => p.metrics[k]).filter(x => x != null);
  return !dir || !v.length ? null : dir > 0 ? Math.max(...v) : Math.min(...v);
};

export default function PlanOptions({ role, onChosen, onError, onMsg }) {
  const [ps, setPs] = useState(null);
  const [sel, setSel] = useState(null);
  const [busy, setBusy] = useState("");
  const load = () => api("/api/plan-sets/latest").then(j => { setPs(j || false); setSel(s => s || j?.chosen || j?.recommended); }).catch(() => setPs(false));
  useEffect(() => { load(); }, []);                  // eslint-disable-line react-hooks/exhaustive-deps

  const mayChoose = role === "production_manager" && ps?.status === "OPEN";
  const mayRun = ["production_manager", "engineer"].includes(role);

  async function runAgent() {
    setBusy("run");
    try { await api("/api/agent/run", "POST"); onMsg?.("The agent analysed the latest data and made five new plans."); setSel(null); await load(); }
    catch (e) { onError(e); } finally { setBusy(""); }
  }
  async function choose(p) {
    setBusy(p.schedule_id);
    const post = ack => api(`/api/plan-sets/${ps.id}/choose`, "POST", { schedule_id: p.schedule_id, acknowledge_risks: ack });
    try {
      try { await post(false); }
      catch (e) {
        if (!/risk/i.test(e.message)) throw e;
        if (!window.confirm(`“${p.title}” has risks (late orders or jobs waiting for parts). Approve it anyway?`)) return;
        await post(true);
      }
      onMsg?.(`“${p.title}” is approved and emailed to the production heads. Each now confirms it for their plant.`);
      await load();
    } catch (e) { onError(e); } finally { setBusy(""); }
  }

  if (ps === null) return <div className="sp-card"><p className="muted" role="status">Loading the agent's plans…</p></div>;
  const runBtn = mayRun && <button className="sp-btn" disabled={!!busy} onClick={runAgent}><Icon n="refresh" size={14} /> {busy === "run" ? "The agent is planning…" : "Run the agent again"}</button>;
  if (!ps) return (
    <div className="sp-card po-empty"><h3>No plans yet</h3>
      <p className="muted">Upload purchase orders on Connections. The agent analyses orders, stock, machines and crews and makes five plans to choose from.</p>{runBtn}</div>);

  const plans = ps.plans;
  const cur = plans.find(p => p.schedule_id === sel) || plans[0];
  const best = plans.find(p => p.best);
  const chosen = plans.find(p => p.schedule_id === ps.chosen);
  const gap = plans[0]?.gap;
  const chooseBtn = p => mayChoose && (
    <button className="sp-btn primary" disabled={!!busy} onClick={() => choose(p)}>
      <Icon n="check" size={14} /> {busy === p.schedule_id ? "Approving…" : `Choose plan ${LETTER[plans.indexOf(p)]}`}</button>);

  return (
    <div className="po">
      <div className="po-top">
        <div><h2>{chosen ? "The chosen production plan" : "Choose a production plan"}</h2>
          <small className="muted">Five plans from the same orders, stock, machines and crews · every plan keeps the customers' dates · {when(ps.created_at)}</small></div>
        <div className="po-top-act">{chosen && <button className="sp-btn primary" onClick={() => onChosen(chosen.schedule_id)}><Icon n="file" size={14} /> Open the plan document</button>}{runBtn}</div>
      </div>

      {/* the five plans first: what each does, how it delivers, what it costs */}
      <div className="po-cards" role="tablist" aria-label="Plans">
        {plans.map((p, i) => {
          const m = p.metrics, [sl, sv, sd] = spend(m);
          return (
            <button key={p.key} role="tab" aria-selected={p === cur} className={"po-card" + (p === cur ? " on" : "") + (p.best ? " best" : "") + (p.schedule_id === ps.chosen ? " chosen" : "")}
              onClick={() => setSel(p.schedule_id)}>
              <span className="po-card-h"><span className="po-letter">{LETTER[i]}</span><b>{p.title}</b>
                {p.schedule_id === ps.chosen ? <span className="po-pick ok">Chosen</span> : p.best && <span className="po-pick">Agent&apos;s pick</span>}</span>
              <span className="po-card-t">{p.text}</span>
              <span className="po-card-big"><strong>{m.on_time_pct ?? "–"}%</strong><small>of units on time</small></span>
              <span className="po-card-k">
                <span><small>Late orders</small><b>{N(m.late_orders)}</b></span>
                <span><small>Late penalties</small><b>{inr(m.late_penalty_inr)}</b></span>
                <span><small>{sl}</small><b>{sv ? inr(sv) : "–"}</b><em>{sd}</em></span>
                <span><small>Total cost</small><b>{inr(m.total_cost_inr)}</b></span>
              </span>
            </button>);
        })}
      </div>

      {/* the agent's recommendation: one line, the reasons a click away */}
      {!chosen && (
        <details className="sp-card po-rec2">
          <summary><Icon n="sparkle" size={16} /><span><b>The agent recommends plan {LETTER[plans.indexOf(best)]} · {best.title}.</b> {ps.why?.[0]}</span>
            <em>Why</em></summary>
          <ul>{(ps.why || []).map(x => <li key={x}><Icon n="check" size={14} /><span>{x}</span></li>)}</ul>
        </details>)}

      {/* the newest purchase orders, checked before their dates are confirmed to the customer */}
      <Promise pr={cur.promise} title={cur.title} />

      {/* what no plan can deliver */}
      {gap?.units > 0 && (
        <details className="sp-card po-gap2">
          <summary><Icon n="alert" size={16} /><span><b>Capacity gap:</b> {N(gap.units)} units can't be made by their dates by our own lines</span><em>Details</em></summary>
          <p>By the week of {day(gap.week)}, customers want more than final assembly can make (busiest week {gap.peak_pct}% loaded). Delivering every order on
            its date needs about <b>{N(gap.hours)} more line hours</b> (≈{N(gap.shifts)} 8-hour line shifts), outsourcing, or partial deliveries agreed with the customers.</p>
        </details>)}

      <details className="sp-card po-cmp">
        <summary>Compare all five plans, line by line</summary>
        <div className="po-tbl-wrap"><table className="ov-tbl po-tbl">
          <thead><tr><th>Measured against the dates customers asked for</th>{plans.map((p, i) => <th key={p.key} className={`num${p === cur ? " on" : ""}`}>{LETTER[i]} · {p.title}</th>)}</tr></thead>
          <tbody>{ROWS.map(([label, k, fmt, dir]) => {
            const b = bestOf(plans, k, dir);
            return <tr key={k}><td>{label}</td>{plans.map(p => <td key={p.key} className={`num${p === cur ? " on" : ""}${b != null && p.metrics[k] === b && plans.some(q => q.metrics[k] !== b) ? " po-best" : ""}`}>{fmt(p.metrics[k])}</td>)}</tr>;
          })}</tbody>
        </table></div>
      </details>

      <div className="po-plan-bar">
        <div><b>Plan {LETTER[plans.indexOf(cur)]} · {cur.title}</b><small className="muted">{cur.text}</small></div>
        {!chosen && chooseBtn(cur)}
      </div>
      <FinalPlan key={cur.schedule_id} sid={cur.schedule_id} />
    </div>
  );
}

// Before confirming a new order's date to the customer: which the selected plan makes by the date asked, and the earliest date for the rest
function Promise({ pr, title }) {
  if (!pr?.new) return null;
  const late = pr.late_count || 0;
  return (
    <details className={"sp-card po-gap2 po-prom" + (late ? " warn" : " ok")} open={late > 0}>
      <summary><Icon n={late ? "alert" : "check"} size={16} /><span><b>New orders: {N(pr.on_time)} of {N(pr.new)} can be delivered on the date asked</b>
        {late ? ` · confirm the earliest dates below for the other ${N(late)} before accepting them, so no penalty is agreed` : " · safe to confirm"} <small className="muted">(“{title}”)</small></span>
        <em>{late ? "Dates to promise" : "Details"}</em></summary>
      {late > 0 ? (<>
        <div className="po-tbl-wrap"><table className="ov-tbl po-tbl">
          <thead><tr><th>Order</th><th>Customer</th><th>Product</th><th className="num">Units</th><th className="num">Order value</th><th>Asked for</th><th>Earliest we can deliver</th><th className="num">Days later</th></tr></thead>
          <tbody>{pr.late.map(r => <tr key={r.id}><td>{r.id}</td><td>{r.customer || "–"}</td><td>{r.product_id}</td><td className="num">{N(r.qty)}</td><td className="num">{inr(r.value)}</td>
            <td>{day(r.asked)}</td><td><b>{r.promise ? day(r.promise) : `after ${day(pr.until)}`}</b></td><td className="num bad">{r.days_late ?? "–"}</td></tr>)}</tbody>
        </table></div>
        <p className="fp-note">Agree these dates (or a partial delivery) with the customer now: a date we can keep carries no late penalty. {late > pr.late.length ? `First ${pr.late.length} of ${N(late)} shown.` : ""}
          Plan “Zero penalty” shows what it would cost to make every date instead.</p>
      </>) : <p>Every new order is made by the date the customer asked for in this plan.</p>}
    </details>);
}

// Every planning run, newest first: why it ran, the agent's pick, the plan chosen and where it stands. Open shows that plan in detail.
const STAND = (r) => r.status === "OPEN" ? ["Waiting for the production manager", "warn"]
  : r.status === "SUPERSEDED" ? ["Replaced by newer plans", "muted"]
  : r.fully_approved ? ["Approved by every plant", "ok"]
  : r.plan_status === "APPROVED" ? ["Approved · waiting for production heads", "info"]
  : r.plan_status === "REJECTED" ? ["Withdrawn", "muted"] : [r.plan_status || r.status, "muted"];
export function PlanHistory({ title = "Plan history", sub, onOpen, limit = 20 }) {
  const [rows, setRows] = useState(null);
  useEffect(() => { api(`/api/plan-sets?limit=${limit}`).then(setRows).catch(() => setRows(false)); }, [limit]);
  return (
    <section className="sp-card ph">
      <header className="ov-h"><h3><Icon n="calendar" size={16} /> {title}</h3>{rows && <span className="ao-chip">{rows.length} planning runs</span>}</header>
      {sub && <small className="muted">{sub}</small>}
      {rows === null ? <p className="muted" role="status">Loading…</p> : !rows ? <p className="muted">Not available right now.</p> : !rows.length ? <p className="muted">No plans yet.</p> : (
        <div className="po-tbl-wrap"><table className="ov-tbl ph-tbl">
          <thead><tr><th>When</th><th>Why the agent planned</th><th>Agent&apos;s pick</th><th>Chosen plan</th><th>Status</th><th /></tr></thead>
          <tbody>{rows.map(r => {
            const [label, tone] = STAND(r);
            return (
              <tr key={r.id}>
                <td className="ph-when">{when(r.created_at)}</td>
                <td className="ph-why" title={r.reason}>{r.reason || "–"}</td>
                <td>{r.recommended || "–"}</td>
                <td>{r.chosen ? <b>{r.chosen}</b> : <span className="muted">Not chosen</span>}{r.chosen_by ? <small className="muted"> · {r.chosen_by}</small> : null}</td>
                <td><span className={`ph-st t-${tone}`}>{label}</span></td>
                <td>{r.schedule_id && onOpen && <button className="sp-btn" onClick={() => onOpen(r.schedule_id)}>Open</button>}</td>
              </tr>);
          })}</tbody>
        </table></div>)}
    </section>
  );
}

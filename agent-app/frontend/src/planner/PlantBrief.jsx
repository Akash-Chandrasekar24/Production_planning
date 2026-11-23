import { useEffect, useState } from "react";
import { api, auth } from "../api.js";
import { N } from "./ui.jsx";
import { Icon } from "./widgets.jsx";
import { Segmented } from "./Filters.jsx";

// The plan for one plant in standard manufacturing terms, for its production head. A production head sees their own plant only.
const PLANT = { "IN-TN-01": "Hosur", "IN-KA-01": "Bengaluru" };
const STAGE = { SMT: "Circuit boards", FAL: "Final assembly" };
const wk = d => new Date(d + "T00:00:00").toLocaleDateString("en", { day: "numeric", month: "short" });
const day = d => new Date(d + "T00:00:00").toLocaleDateString("en", { day: "numeric", month: "short" });
const inr = v => (v == null ? "–" : v >= 1e7 ? `₹${(v / 1e7).toFixed(2)} Cr` : v >= 1e5 ? `₹${(v / 1e5).toFixed(1)} L` : `₹${N(v)}`);
const Bar = ({ v, max = 100, tone = "" }) => <span className={"ov-bar " + tone}><i style={{ width: `${Math.min(100, Math.max(0, (100 * (v || 0)) / (max || 1)))}%` }} /></span>;
const Term = ({ t, children }) => <h4 className="pbf-term"><b>{t}</b><small className="muted">{children}</small></h4>;

export default function PlantBrief({ sid }) {
  const head = auth.role === "plant_head" && auth.plant;
  const [plant, setPlant] = useState(head || "IN-TN-01");
  const [b, setB] = useState(null);
  const [err, setErr] = useState(false);
  useEffect(() => {
    let live = true; setB(null); setErr(false);
    api(`/api/plan-tools/plant-brief?schedule_id=${encodeURIComponent(sid)}&plant=${plant}`).then(j => live && setB(j)).catch(() => live && setErr(true));
    return () => { live = false; };
  }, [sid, plant]);

  const oee = b?.oee?.length ? Math.round(10 * b.oee.reduce((a, l) => a + (l.oee || 0), 0) / b.oee.length) / 10 : null;
  const fal = b?.takt?.find(t => t.stage === "FAL");
  const bott = b?.utilisation?.find(u => u.line_id === b.bottleneck);
  const tiles = b?.available ? [
    ["On-time delivery (OTD)", b.otd_pct != null ? `${b.otd_pct}%` : "–", `${b.late_jobs} job${b.late_jobs === 1 ? "" : "s"} at risk of being late`, b.otd_pct >= 95 ? "ok" : b.otd_pct >= 80 ? "warn" : "bad"],
    ["Capacity utilisation", bott ? `${bott.utilisation_pct}%` : "–", `Bottleneck: ${b.bottleneck} (${STAGE[bott?.stage] || ""})`, bott?.utilisation_pct > 95 ? "bad" : "ok"],
    ["OEE", oee != null ? `${oee}%` : "–", "Availability × performance × quality, today", oee >= 85 ? "ok" : "warn"],
    ["Takt time vs cycle time", fal ? `${fal.takt_s}s / ${fal.cycle_s}s` : "–", fal ? (fal.keeps_up ? "Final assembly keeps up with demand" : "Final assembly is slower than demand") : "", fal?.keeps_up ? "ok" : "bad"],
    ["Plan adherence", b.adherence?.on_plan_pct != null ? `${b.adherence.on_plan_pct}%` : "–", b.adherence ? `of ${b.adherence.orders} finished orders on plan (±1 day) · estimate` : "Not available", b.adherence?.on_plan_pct >= 90 ? "ok" : "warn"],
    ["Inventory turnover", b.inventory_turns != null ? `${b.inventory_turns}×` : "–", `a year · stock worth ${inr(b.stock_value_inr)} · estimate`, ""],
    ["Manufacturing lead time", `${b.lead_time_h} h`, "Average from first circuit board to final assembly done", ""],
    ["Changeovers", N(b.changeovers), `${b.setup_h} h of setup in this plan`, ""],
    ["Waiting for parts", N(b.purchase_jobs || 0), "jobs that start once bought parts arrive (MRP)", b.purchase_jobs ? "warn" : "ok"],
    ["Forecast for stock", `${N(b.forecast_units || 0)} units`, "made from the demand forecast after customer orders", ""],
  ] : [];

  return (
    <section className="sp-card pbf" aria-label="Plant plan in manufacturing terms">
      <header className="ov-h">
        <div><h3>Plant plan: {PLANT[plant]}</h3>
          <small className="muted">{b?.available ? `${N(b.jobs)} jobs · ${N(b.units)} units over ${b.plan_days} days · the standard production-planning measures, worked out from this plan` : "The standard production-planning measures for one plant"}</small></div>
        {head ? <span className="sp-pill t-info"><Icon n="factory" size={12} /> Your plant</span>
          : <Segmented label="Plant" options={Object.entries(PLANT).map(([value, label]) => ({ value, label }))} value={plant} onChange={setPlant} />}
      </header>
      {err ? <p className="muted">These figures aren't available right now.</p> : !b ? <p className="muted" role="status">Working out the figures…</p>
        : !b.available ? <p className="muted">This plan has no work for {PLANT[plant]}.</p> : (<>
        <div className="pbf-tiles">{tiles.map(([l, v, n, tone]) => (
          <div key={l} className={"pbf-tile t-" + (tone || "info")}><small>{l}</small><b>{v}</b><small className="muted">{n}</small></div>))}</div>

        <div className="ov-two">
          <div>
            <Term t="Master production schedule (MPS)">Finished units to build each week</Term>
            <table className="ov-tbl"><thead><tr><th>Week of</th>{b.mps.products.map(p => <th key={p} className="num">{p}</th>)}<th className="num">Total</th></tr></thead>
              <tbody>{b.mps.weeks.map(w => <tr key={w.week}><td>{wk(w.week)}</td>{b.mps.products.map(p => <td key={p} className="num">{N(w[p])}</td>)}<td className="num"><b>{N(w.total)}</b></td></tr>)}</tbody></table>

            <Term t="Time fences">How firm each part of the plan is</Term>
            <ul className="pbf-fence">{[["frozen", `Frozen · 0–${b.time_fences.frozen_weeks} weeks`, "Locked: changes need management approval"],
              ["slushy", `Slushy · ${b.time_fences.frozen_weeks + 1}–${b.time_fences.slushy_weeks} weeks`, "Changes allowed after a material and capacity check"],
              ["flexible", `Flexible · ${b.time_fences.slushy_weeks + 1}+ weeks`, "Open to change as demand moves"]].map(([k, l, n]) => (
              <li key={k} className={k}><b>{l}</b><span>{N(b.time_fences[k].jobs)} jobs · {N(b.time_fences[k].units)} units</span><small className="muted">{n}</small></li>))}</ul>
            <small className="muted">On a re-plan the agent keeps every job in the frozen zone exactly where it was; changes in weeks {b.time_fences.frozen_weeks + 1}–{b.time_fences.slushy_weeks} need sign-off at approval.</small>

            <Term t="Every product every (EPE)">How often each product is made on final assembly</Term>
            <ul className="ov-hbars">{b.epe.map(e => <li key={e.product_id}><span>{e.product_id}</span><Bar v={1 / (e.every_days || 1)} max={1} /><em>every {e.every_days} days · {e.runs} runs</em></li>)}</ul>
          </div>
          <div>
            <Term t="Takt time vs cycle time">Takt = time available ÷ units needed. The line keeps up when its cycle time is below takt.</Term>
            <ul className="ov-hbars">{b.takt.map(t => <li key={t.stage}><span>{STAGE[t.stage]}</span><Bar v={t.cycle_s} max={t.takt_s} tone={t.keeps_up ? "ok" : "bad"} />
              <em>{t.cycle_s}s cycle · {t.takt_s}s takt</em></li>)}</ul>

            <Term t="Capacity utilisation">Planned run and setup time against the hours each line has</Term>
            <ul className="ov-hbars">{b.utilisation.map(u => <li key={u.line_id}><span>{u.line_id}{u.line_id === b.bottleneck && <em className="pbf-bn">Bottleneck</em>}</span>
              <Bar v={u.utilisation_pct} tone={u.utilisation_pct > 95 ? "bad" : u.utilisation_pct > 85 ? "warn" : "ok"} /><em>{u.utilisation_pct}% · {N(u.busy_h)} of {N(u.available_h)} h</em></li>)}</ul>

            <Term t="OEE (overall equipment effectiveness)">Availability × performance × quality, today</Term>
            <table className="ov-tbl"><thead><tr><th>Line</th><th className="num">Availability</th><th className="num">Performance</th><th className="num">Quality</th><th className="num">OEE</th></tr></thead>
              <tbody>{b.oee.map(l => <tr key={l.line_id}><td><b>{l.line_id}</b></td><td className="num">{l.availability}%</td><td className="num">{l.performance}%</td><td className="num">{l.quality}%</td><td className="num"><b>{l.oee}%</b></td></tr>)}</tbody></table>
          </div>
        </div>

        <Term t="Material requirements planning (MRP)">Net requirements after stock and safety stock, ordered one supplier lead time ahead, in pack sizes and minimum order quantities</Term>
        {!b.mrp.count ? <p className="muted">Nothing to buy: stock covers this plant's plan.</p> : <>
          <small className="muted">{b.mrp.count} materials to order{b.mrp.late ? `, ${b.mrp.late} already past their order-by date` : ""} · {inr(b.mrp.buy_value_inr)} in total</small>
          <div className="pbf-scroll"><table className="ov-tbl"><thead><tr><th>Material</th><th className="num">Net requirement</th><th className="num">In stock</th><th>Runs short</th><th className="num">Lead time</th><th>Order by</th><th className="num">Order qty</th><th>Status</th></tr></thead>
            <tbody>{b.mrp.rows.map(r => <tr key={r.material_id}><td><b>{r.material_id}</b></td><td className="num">{N(r.need_qty)}</td><td className="num">{N(r.stock_qty)}</td><td>{day(r.short_on)}</td>
              <td className="num">{r.lead_days} d</td><td>{day(r.order_by)}</td><td className="num">{N(r.buy_qty)}</td>
              <td><span className={"sp-pill t-" + (r.status === "late" ? "bad" : r.status === "order now" ? "warn" : "ok")}>{r.status === "late" ? `Late by ${r.late_by_days} d` : r.status === "order now" ? "Order now" : "Planned"}</span></td></tr>)}</tbody></table></div></>}

        <Atp key={plant} products={b.mps.products} />
      </>)}
    </section>
  );
}

// Available to promise: can we deliver this quantity by this date? Runs the real plan with the order added.
function Atp({ products }) {
  const [f, setF] = useState({ product: products[0], qty: 1000, due: new Date(Date.now() + 14 * 864e5).toISOString().slice(0, 10) });
  const [r, setR] = useState(null);
  const [busy, setBusy] = useState(false);
  const check = async e => {
    e.preventDefault(); setBusy(true); setR(null);
    try { setR(await api(`/api/plan-tools/promise?product_id=${encodeURIComponent(f.product)}&qty=${f.qty}&due=${f.due}`)); }
    catch (x) { setR({ error: x.message }); } finally { setBusy(false); }
  };
  return (
    <div className="pbf-atp">
      <Term t="Available to promise (ATP)">Can a new order be delivered by a date? Checked against the plan's real line loading and stock</Term>
      <form className="pbf-atp-f" onSubmit={check}>
        <label>Product<select value={f.product} onChange={e => setF({ ...f, product: e.target.value })}>{products.map(p => <option key={p}>{p}</option>)}</select></label>
        <label>Units<input type="number" min="1" max="1000000" value={f.qty} onChange={e => setF({ ...f, qty: +e.target.value })} required /></label>
        <label>Needed by<input type="date" value={f.due} onChange={e => setF({ ...f, due: e.target.value })} required /></label>
        <button className="sp-btn primary" disabled={busy}>{busy ? "Checking…" : "Check"}</button>
      </form>
      {r && (r.error ? <p className="bad">{r.error}</p> : (
        <p className={"pbf-atp-r t-" + (r.answer === "yes" ? "ok" : r.answer === "with_purchase" ? "warn" : "bad")} role="status">
          <b>{r.answer === "yes" ? "Yes, it can be promised" : r.answer === "with_purchase" ? "Yes, if the parts are ordered today" : "Not by that date"}</b>
          {r.promise_date && <> · earliest {day(r.promise_date)}</>}
          {r.pushes_back?.orders > 0 && <> · moves {r.pushes_back.orders} other order{r.pushes_back.orders > 1 ? "s" : ""}{r.pushes_back.newly_late ? `, ${r.pushes_back.newly_late} would become late` : ""}</>}
        </p>))}
    </div>
  );
}

import { useEffect, useState } from "react";
import { api } from "../api.js";
import { N } from "./ui.jsx";
import { Heat, Icon, K } from "./widgets.jsx";
import { FilterBar } from "./Filters.jsx";

// ------------------------------------------------------------------ Materials & capacity, by category
// Five categories, each a tile with its headline: inventory, purchasing, machinery, line capacity and workforce. Clicking a tile opens
// its details below. Every figure comes from the API (the plan's call-off and capacity, the plan tools and the recorded floor data).
const CITY = { "IN-TN-01": "Hosur", "IN-KA-01": "Bengaluru" };
const CATS = [["inventory", "Inventory", "box"], ["purchasing", "Purchasing", "cart"], ["machinery", "Machinery", "factory"],
  ["capacity", "Line capacity", "chart"], ["workforce", "Workforce", "users"]];
const dd = v => (v ? new Date(String(v).slice(0, 10) + "T00:00").toLocaleDateString("en", { day: "numeric", month: "short" }) : "–");
const inr = v => (v == null ? "–" : v >= 1e7 ? `₹${(v / 1e7).toFixed(1)} Cr` : v >= 1e5 ? `₹${(v / 1e5).toFixed(1)} L` : `₹${N(Math.round(v))}`);
const keep = (k, v) => { try { localStorage.setItem(k, v); } catch { /* storage may be blocked */ } };
const recall = (k, d) => { try { return localStorage.getItem(k) || d; } catch { return d; } };

function More({ rows, n = 5, children }) {                      // first n rows, the rest on request
  const [all, setAll] = useState(false);
  return (
    <>
      {children(all ? rows : rows.slice(0, n))}
      {rows.length > n && <button className="lnk mc-more" onClick={() => setAll(v => !v)}>{all ? "Show fewer" : `Show all ${N(rows.length)}`}</button>}
    </>
  );
}
const Stat = ({ label, value, sub, tone = "" }) => <div className={"mc-stat " + tone}><small>{label}</small><b>{value}</b>{sub && <span>{sub}</span>}</div>;

export function Materials({ s, calloff, gaps }) {
  const [cat, setCat] = useState(() => recall("mc.cat", "inventory"));
  const [x, setX] = useState({});
  useEffect(() => {
    let live = true;
    const get = (k, url) => api(url).then(v => live && setX(o => ({ ...o, [k]: v }))).catch(() => live && setX(o => ({ ...o, [k]: null })));
    get("mrp", "/api/plan-tools/materials"); get("twin", "/api/twin"); get("maint", "/api/plan-tools/maintenance");
    get("yields", "/api/plan-tools/yields"); get("crews", "/api/plan-tools/crews?days=14"); get("chg", "/api/plan-tools/changeovers");
    return () => { live = false; };
  }, []);
  const choose = k => { setCat(k); keep("mc.cat", k); };
  const [fs, setFs] = useState({});                     // filters, kept per category
  const f = fs[cat] || {};
  const setF = v => setFs(o => ({ ...o, [cat]: v }));

  const mats = calloff.filter(r => r.required_qty || r.available_qty);
  const short = mats.filter(r => r.shortfall_qty > 0);
  const buys = x.mrp?.rows || [];
  const late = buys.filter(r => r.status === "late");
  const lines = (x.twin?.lines || []).filter(l => !l.generated);
  const plantOf = l => lines.find(z => z.line_id === l)?.plant_id || (/L1$/.test(l) ? "IN-TN-01" : /L2$/.test(l) ? "IN-KA-01" : null);
  const oee = lines.length ? lines.reduce((t, l) => t + (l.today?.oee || 0), 0) / lines.length : null;
  const services = x.maint?.services || [];
  const busy = gaps.overloaded_weeks || [];
  const peak = gaps.by_line_week.reduce((m, r) => ((r.utilisation ?? 0) > (m?.utilisation ?? -1) ? r : m), null);
  const shifts = x.crews?.short_shifts || [];
  const tiles = {
    inventory: { tone: short.length ? "bad" : "ok", big: `${short.length} of ${mats.length}`, line: "materials short for open demand",
      sub: `${inr(short.reduce((t, r) => t + (r.shortfall_cost_inr || 0), 0))} to cover` },
    purchasing: { tone: late.length ? "bad" : buys.length ? "warn" : "ok", big: x.mrp === undefined ? "…" : N(buys.length), line: "purchase orders to place",
      sub: x.mrp ? `${late.length} already late · ${inr(x.mrp.buy_value_inr)}` : "" },
    machinery: { tone: lines.some(l => l.state === "down") ? "bad" : lines.some(l => l.state === "slowed") ? "warn" : "ok",
      big: x.twin === undefined ? "…" : oee != null ? `${oee.toFixed(1)}%` : "–", line: "average OEE today",
      sub: `${lines.filter(l => l.state === "running").length} of ${lines.length} lines running · ${services.length} services planned` },
    capacity: { tone: busy.length ? "warn" : "ok", big: N(busy.length), line: "busy weeks (95% or more)",
      sub: peak ? `peak ${Math.round((peak.utilisation || 0) * 100)}% on ${peak.line_id}, ${peak.week}` : "" },
    workforce: { tone: shifts.length ? "warn" : "ok", big: x.crews === undefined ? "…" : N(shifts.length), line: "shifts short of people (14 days)",
      sub: x.crews ? `${N(x.crews.overtime_h)} h overtime to cover` : "" },
  };

  return (
    <div className="mc">
      <div className="mc-tiles" role="tablist" aria-label="Categories">{CATS.map(([k, label, ic]) => (
        <button key={k} role="tab" aria-selected={cat === k} className={"mc-tile t-" + tiles[k].tone + (cat === k ? " on" : "")} onClick={() => choose(k)}>
          <span className="mc-tile-h"><span className="mc-tile-ic"><Icon n={ic} size={18} /></span><b>{label}</b><Icon n="right" size={14} /></span>
          <span className="mc-tile-big">{tiles[k].big}</span>
          <small>{tiles[k].line}</small>
          <span className="mc-tile-sub">{tiles[k].sub}</span>
        </button>))}
      </div>
      <FilterBar value={f} onChange={setF} fields={fieldsFor(cat, { buys, lines, gaps, crews: x.crews, plantOf })} />
      <div className="mc-body sp-enter" role="tabpanel" key={cat}>
        {cat === "inventory" && (() => {
          const m = mats.filter(r => (!f.q || r.material_id.toLowerCase().includes(f.q.toLowerCase()))
            && (!f.show || (f.show === "short" ? r.shortfall_qty > 0 : f.show === "safety" ? r.shortfall_incl_safety_qty > 0 : r.required_qty > 0)));
          const st = x.twin?.stock && { ...x.twin.stock, warehouses: x.twin.stock.warehouses.filter(w => !f.plant || w.plant_id === f.plant) };
          return <Inventory mats={m} short={m.filter(r => r.shortfall_qty > 0)} stock={st} plantSet={!!f.plant} />;
        })()}
        {cat === "purchasing" && (() => {
          if (!x.mrp) return <Purchasing mrp={x.mrp} />;
          const rows = x.mrp.rows.filter(r => (!f.plant || r.plant_id === f.plant) && (!f.status || (f.status === "late") === (r.status === "late"))
            && (!f.supplier || r.supplier === f.supplier) && (!f.q || r.material_id.toLowerCase().includes(f.q.toLowerCase())));
          return <Purchasing mrp={{ ...x.mrp, rows, buy_value_inr: rows.reduce((t, r) => t + (r.value_inr || 0), 0) }} />;
        })()}
        {cat === "machinery" && (() => {
          const q = (f.q || "").toLowerCase();
          const hit = m => !q || `${m.name} ${m.machine_id} ${m.type}`.toLowerCase().includes(q);
          const ls = lines.filter(l => (!f.plant || l.plant_id === f.plant) && (!f.kind || l.kind === f.kind) && (!f.state || l.state === f.state)
            && (!q || l.line_id.toLowerCase().includes(q) || (l.machines || []).some(hit)))
            .map(l => (q && !l.line_id.toLowerCase().includes(q) ? { ...l, machines: l.machines.filter(hit) } : l));
          const sv = services.filter(v => ls.some(l => l.line_id === v.line_id));
          return <Machinery lines={ls} at={x.twin?.at} services={sv} yields={x.yields} loading={x.twin === undefined} />;
        })()}
        {cat === "capacity" && (() => {
          const okLine = l => (!f.line || l === f.line) && (!f.plant || plantOf(l) === f.plant);
          let rows = gaps.by_line_week.filter(r => okLine(r.line_id));
          if (f.busy) { const wk = new Set(rows.filter(r => (r.utilisation ?? 0) >= +f.busy).map(r => r.week)); rows = rows.filter(r => wk.has(r.week)); }
          const chg = x.chg && { ...x.chg, history: x.chg.history.filter(h => okLine(h.line_id)) };
          return <Capacity s={s} gaps={{ ...gaps, by_line_week: rows, overloaded_weeks: [...new Set(rows.filter(r => (r.utilisation ?? 0) >= 0.95).map(r => r.week))] }} chg={chg} />;
        })()}
        {cat === "workforce" && (() => {
          if (!x.crews?.available) return <Workforce crews={x.crews} />;
          const ok = r => (!f.plant || r.plant_id === f.plant) && (!f.shift || r.shift === f.shift);
          const short = x.crews.short_shifts.filter(r => ok(r) && (!f.gap || r.gap >= +f.gap));
          return <Workforce crews={{ ...x.crews, rows: x.crews.rows.filter(ok), short_shifts: short, overtime_h: short.reduce((t, r) => t + r.overtime_h, 0) }} />;
        })()}
      </div>
    </div>
  );
}

// The filters each category offers (options come from the data on the page).
function fieldsFor(cat, { buys, lines, gaps, crews, plantOf }) {
  const plant = { key: "plant", label: "Plant", all: "Both plants", options: [{ value: "IN-TN-01", label: "Hosur" }, { value: "IN-KA-01", label: "Bengaluru" }] };
  if (cat === "inventory") return [
    { key: "show", label: "Show", all: "All materials", options: [{ value: "short", label: "Short" }, { value: "safety", label: "Below safety stock" }, { value: "needed", label: "Needed by orders" }] },
    { ...plant, label: "Warehouses in" }, { key: "q", label: "Find a material…", type: "search" }];
  if (cat === "purchasing") return [plant,
    { key: "status", label: "Status", all: "Any", options: [{ value: "late", label: "Late to order" }, { value: "soon", label: "Order soon" }] },
    { key: "supplier", label: "Supplier", all: "All suppliers", options: [...new Set(buys.map(r => r.supplier))].sort().map(v => ({ value: v, label: v })) },
    { key: "q", label: "Find a material…", type: "search" }];
  if (cat === "machinery") return [plant,
    { key: "kind", label: "Stage", all: "All stages", options: [{ value: "SMT", label: "Circuit boards (SMT)" }, { value: "FAL", label: "Final assembly" }] },
    { key: "state", label: "Line state", all: "Any state", options: [...new Set(lines.map(l => l.state))].map(v => ({ value: v, label: v.charAt(0).toUpperCase() + v.slice(1) })) },
    { key: "q", label: "Find a line or machine…", type: "search" }];
  if (cat === "capacity") return [plant,
    { key: "line", label: "Line", all: "All lines", options: [...new Set(gaps.by_line_week.map(r => r.line_id))].sort().map(v => ({ value: v, label: `${v} · ${plantOf(v) === "IN-TN-01" ? "Hosur" : "Bengaluru"}` })) },
    { key: "busy", label: "Weeks", all: "All weeks with work", options: [{ value: "0.8", label: "80% or more used" }, { value: "0.95", label: "95% or more used" }] }];
  return [plant,
    { key: "shift", label: "Shift", all: "All shifts", options: [...new Set((crews?.rows || []).map(r => r.shift))].sort().map(v => ({ value: v, label: `Shift ${v}` })) },
    { key: "gap", label: "Short by", all: "Any number", options: [{ value: "2", label: "2 or more people" }, { value: "4", label: "4 or more people" }] }];
}

function Inventory({ mats, short, stock, plantSet }) {
  const top = [...short].sort((a, b) => b.shortfall_qty - a.shortfall_qty);
  const byPlant = (stock?.warehouses || []).reduce((g, w) => ((g[w.plant_id] = g[w.plant_id] || []).push(w), g), {});
  return (
    <>
      <div className="mc-stats">
        <Stat label="Materials tracked" value={N(mats.length)} />
        <Stat label="Short for open demand" value={N(short.length)} tone={short.length ? "bad" : "ok"} />
        <Stat label="Below safety stock" value={N(mats.filter(r => r.shortfall_incl_safety_qty > 0).length)} tone="warn" />
        <Stat label="Cost to cover the shortfall" value={inr(short.reduce((t, r) => t + (r.shortfall_cost_inr || 0), 0))} />
      </div>
      <section className="mc-card">
        <header><h3>Biggest shortages</h3><small className="muted">Available stock against what all open orders need · both plants together{plantSet ? " (the plant filter applies to the warehouses below)" : ""}</small></header>
        {!top.length ? <p className="muted">No material matches these filters.</p> : (
          <More rows={top}>{rows => (
            <ul className="mc-bars">{rows.map(r => {
              const pct = Math.min(100, (100 * r.available_qty) / Math.max(1, r.required_qty));
              return (
                <li key={r.material_id}>
                  <b>{r.material_id}</b>
                  <span className="mc-bar" title={`${N(r.available_qty)} of ${N(r.required_qty)}`}><i style={{ width: `${pct}%` }} /></span>
                  <span className="mc-bar-t"><b className="bad">{N(r.shortfall_qty)} short</b><small className="muted">{N(r.available_qty)} of {N(r.required_qty)} · {inr(r.shortfall_cost_inr)}</small></span>
                </li>);
            })}</ul>)}
          </More>)}
      </section>
      <section className="mc-card">
        <header><h3>Warehouses</h3><small className="muted">Stock on hand and usable (not blocked or on quality hold){stock?.day ? ` · counted ${dd(stock.day)}` : ""}</small></header>
        {!stock ? <p className="muted" role="status">Loading…</p> : (
          <div className="mc-wh">{Object.entries(byPlant).map(([p, ws]) => (
            <div key={p}><h4>{CITY[p] || p}</h4>
              <ul>{ws.map(w => (
                <li key={w.warehouse_id}><span><b>{w.warehouse_id}</b><small className="muted">{w.materials} materials · {w.lots} lots</small></span>
                  <span className="mc-bar"><i style={{ width: `${(100 * w.usable) / Math.max(1, w.on_hand)}%` }} /></span>
                  <span className="mc-num"><b>{K(w.usable)}</b><small className="muted">of {K(w.on_hand)}</small></span></li>))}
              </ul></div>))}
          </div>)}
      </section>
    </>
  );
}

function Purchasing({ mrp }) {
  if (mrp === undefined) return <p className="muted" role="status">Loading…</p>;
  if (!mrp) return <p className="muted">Purchasing data isn't available right now.</p>;
  const rows = [...mrp.rows].sort((a, b) => (a.status === "late" ? 0 : 1) - (b.status === "late" ? 0 : 1) || String(a.order_by).localeCompare(String(b.order_by)));
  const late = rows.filter(r => r.status === "late");
  return (
    <>
      <div className="mc-stats">
        <Stat label="Purchase orders to place" value={N(rows.length)} />
        <Stat label="Already late to order" value={N(late.length)} tone={late.length ? "bad" : "ok"} sub={late.length ? "order today and ask to expedite" : ""} />
        <Stat label="Value to buy" value={inr(mrp.buy_value_inr)} />
        <Stat label="Suppliers" value={N(new Set(rows.map(r => r.supplier)).size)} />
      </div>
      <section className="mc-card">
        <header><h3>What to order, and by when</h3><small className="muted">Each plant against its own stock · most urgent first</small></header>
        <More rows={rows}>{list => (
          <div className="sp-table"><table className="mc-t">
            <thead><tr><th>Material</th><th>Plant</th><th className="num">Buy</th><th>Supplier</th><th>Order by</th><th>Status</th></tr></thead>
            <tbody>{list.map((r, i) => (
              <tr key={i}><td><b>{r.material_id}</b></td><td>{CITY[r.plant_id] || r.plant_id}</td><td className="num">{N(r.buy_qty)}</td>
                <td>{r.supplier}<small className="muted"> · {r.lead_days} d</small></td><td>{dd(r.order_by)}</td>
                <td><span className={"sp-pill t-" + (r.status === "late" ? "bad" : "warn")}>{r.status === "late" ? `Late ${r.late_by_days} d` : "Order soon"}</span></td></tr>))}
            </tbody></table></div>)}
        </More>
      </section>
    </>
  );
}

const STATE = { running: "ok", slowed: "warn", down: "bad", idle: "info" };
function Machinery({ lines, at, services, yields, loading }) {
  if (loading) return <p className="muted" role="status">Loading…</p>;
  const yl = (yields?.rows || []).reduce((g, r) => ((g[r.line_id] = r.line_avg_pct), g), {});
  return (
    <>
      <p className="muted mc-note"><Icon n="clock" size={12} /> Machine states from the latest recorded hour{at ? `: ${new Date(at).toLocaleString("en", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}` : ""}.</p>
      <div className="mc-lines">{lines.map(l => (
        <article key={l.line_id} className={"mc-line t-" + (STATE[l.state] || "info")}>
          <header><div><b>{l.line_id}</b><small className="muted">{CITY[l.plant_id] || l.plant_id} · {l.kind === "SMT" ? "Circuit boards" : "Final assembly"}</small></div>
            <span className={"sp-pill t-" + (STATE[l.state] || "info")}>{l.state}</span></header>
          <div className="mc-line-k">
            <div><small>OEE today</small><b>{l.today?.oee != null ? `${l.today.oee}%` : "–"}</b></div>
            <div><small>Good units today</small><b>{K(l.today?.good)}</b><span className="muted">of {K(l.today?.target)}</span></div>
            <div><small>Yield (30 days)</small><b>{yl[l.line_id] != null ? `${yl[l.line_id]}%` : "–"}</b></div>
          </div>
          <ul className="mc-mach">{(l.machines || []).map(m => (
            <li key={m.machine_id} className={"t-" + (STATE[m.state] || "info")}>
              <i aria-hidden="true" /><span><b>{m.name}</b><small className="muted">{m.type} · {m.machine_id}</small></span>
              <small className="mc-due">{m.maintenance_due_days != null ? `service in ${m.maintenance_due_days} d` : ""}</small></li>))}
          </ul>
        </article>))}
      </div>
      <section className="mc-card">
        <header><h3>Planned services</h3><small className="muted">Line time held out of the plan for maintenance</small></header>
        {!services.length ? <p className="muted">No services planned in the horizon.</p> : (
          <More rows={services}>{list => (
            <div className="sp-table"><table className="mc-t">
              <thead><tr><th>Date</th><th>Line</th><th>Machines</th><th className="num">Minutes</th></tr></thead>
              <tbody>{list.map((v, i) => <tr key={i}><td>{dd(v.start)}</td><td><b>{v.line_id}</b></td><td className="muted">{v.machines.join(", ")}</td><td className="num">{N(v.minutes)}</td></tr>)}</tbody>
            </table></div>)}
          </More>)}
      </section>
    </>
  );
}

function Capacity({ s, gaps, chg }) {
  const busy = gaps.overloaded_weeks || [];
  const loaded = [...new Set(gaps.by_line_week.filter(r => (r.utilisation ?? 0) > 0).map(r => r.week))];
  return (
    <>
      <div className="mc-stats">
        <Stat label="Busy weeks (95% or more)" value={N(busy.length)} tone={busy.length ? "warn" : "ok"} />
        <Stat label="Units held up by materials" value={K(gaps.material_limited_qty)} tone={gaps.material_limited_qty ? "bad" : "ok"} />
        <Stat label="Orders that don't fit the horizon" value={N(gaps.unfit_orders.length)} />
        <Stat label="Product changes on the lines" value={chg ? N(chg.history.reduce((t, h) => t + h.switches, 0)) : "–"} sub="last 90 days" />
      </div>
      <section className="mc-card">
        <header><h3>Line load by week</h3><small className="muted">Share of shift time the plan uses · red = 95% or more</small></header>
        {loaded.length ? <Heat gaps={{ ...gaps, by_line_week: gaps.by_line_week.filter(r => loaded.includes(r.week)) }} />
          : <p className="muted">No load in this plan{s.assignments.length ? "" : " (it schedules no work)"}.</p>}
        {loaded.length > 0 && loaded.length < new Set(gaps.by_line_week.map(r => r.week)).size && <small className="muted">Weeks with no planned work are left out.</small>}
      </section>
      {chg && (
        <section className="mc-card">
          <header><h3>Product changeovers</h3><small className="muted">Time lost switching products, per line</small></header>
          <ul className="mc-bars">{chg.history.map(h => {
            const max = Math.max(...chg.history.map(z => z.lost_units), 1);
            return <li key={h.line_id}><b>{h.line_id}</b><span className="mc-bar"><i style={{ width: `${(100 * h.lost_units) / max}%` }} /></span>
              <span className="mc-bar-t"><b>{N(h.lost_units)} units lost</b><small className="muted">{N(h.switches)} changes · {N(h.changeover_min)} min</small></span></li>;
          })}</ul>
        </section>)}
      {gaps.unfit_orders.length > 0 && (
        <section className="mc-card">
          <header><h3>Orders that don't fit the horizon</h3></header>
          <More rows={gaps.unfit_orders}>{list => (
            <div className="sp-table"><table className="mc-t"><thead><tr><th>Order</th><th>Product</th><th className="num">Units</th><th>Priority</th></tr></thead>
              <tbody>{list.map(r => <tr key={r.customer_order_id}><td><b>{r.customer_order_id}</b></td><td>{r.product_id}</td><td className="num">{N(r.qty)}</td><td>{r.priority}</td></tr>)}</tbody></table></div>)}
          </More>
        </section>)}
    </>
  );
}

function Workforce({ crews }) {
  if (crews === undefined) return <p className="muted" role="status">Loading…</p>;
  if (!crews?.available) return <p className="muted">Crew rosters aren't available.</p>;
  const short = crews.short_shifts;
  return (
    <>
      <div className="mc-stats">
        <Stat label="Shifts checked (14 days)" value={N(crews.rows.length)} />
        <Stat label="Shifts short of people" value={N(short.length)} tone={short.length ? "warn" : "ok"} />
        <Stat label="Overtime to cover" value={`${N(crews.overtime_h)} h`} />
        <Stat label="People missing at worst" value={N(Math.max(0, ...short.map(r => r.gap)))} />
      </div>
      <section className="mc-card">
        <header><h3>Shifts short of people</h3><small className="muted">People the active lines need against the roster, minus leave</small></header>
        {!short.length ? <p className="muted">Every shift has the people it needs.</p> : (
          <More rows={short}>{list => (
            <div className="sp-table"><table className="mc-t">
              <thead><tr><th>Date</th><th>Shift</th><th>Plant</th><th>Team</th><th className="num">Needed</th><th className="num">Available</th><th className="num">Short</th><th className="num">Overtime</th></tr></thead>
              <tbody>{list.map((r, i) => (
                <tr key={i}><td>{dd(r.date)}</td><td>{r.shift}</td><td>{CITY[r.plant_id] || r.plant_id}</td><td>{r.team}</td><td className="num">{r.needed}</td>
                  <td className="num">{r.available}</td><td className="num"><b className="bad">{r.gap}</b></td><td className="num">{r.overtime_h} h</td></tr>))}
              </tbody></table></div>)}
          </More>)}
      </section>
    </>
  );
}


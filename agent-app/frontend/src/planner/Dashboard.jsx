import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, auth, can } from "../api.js";
import { FilterBar, NotFiltered, Segmented, nice, opts } from "./Filters.jsx";
import { Num } from "./motion.js";
import DrillSections, { rangeText } from "./Drill.jsx";
import { Delta } from "./charts.jsx";
import { Bullet, Columns, Donut, Dumbbell, Ring, Waffle } from "./dzcharts.jsx";
import { FEAS, N, dt, plainDq } from "./ui.jsx";
import { BarList, Heat, Icon, K } from "./widgets.jsx";
import { UseCaseKpis } from "./PlanningViews.jsx";

// Widget dashboard. Every number comes from the API (data insights or the selected plan); nothing here decides feasibility.
// No technical details are shown. The chosen widgets are a per-browser preference.

export const CATALOG = [
  { id: "kpis", title: "Key figures", desc: "Orders, units ordered, production output and downtime, compared with the previous period.", tag: "Overview", icon: "grid", span: 12 },
  { id: "output", title: "Production output", desc: "Daily good units this period against the previous period, plus orders by sales channel.", tag: "Performance", icon: "trend", span: 8 },
  { id: "weekday", title: "Busiest production day", desc: "Average good units for each day of the week.", tag: "Performance", icon: "chart", span: 4 },
  { id: "products", title: "Products", desc: "Orders, units, revenue and open units for each product.", tag: "Demand", icon: "cart", span: 8 },
  { id: "promise", title: "Delivery promise kept", desc: "Share of orders confirmed on or before the date the customer asked for.", tag: "Service", icon: "check", span: 4 },
  { id: "plan", title: "Plan status", desc: "The selected plan, its status and where it is in the approval path.", tag: "Plan", icon: "file", span: 4 },
  { id: "due", title: "Due dates", desc: "Open demand by due date, including orders that are already overdue.", tag: "Demand", icon: "clock", span: 4 },
  { id: "supply", title: "Demand vs production orders", desc: "Open demand next to open production orders for each product.", tag: "Supply", icon: "layers", span: 12 },
  { id: "stock", title: "Stock vs safety stock", desc: "Materials with the lowest cover on the last stock count.", tag: "Materials", icon: "box", span: 6 },
  { id: "downtime", title: "Downtime by reason", desc: "Minutes lost on all lines, by reason.", tag: "Performance", icon: "alert", span: 6 },
  { id: "lines", title: "Line performance", desc: "Attainment, rejects and downtime for each production line.", tag: "Performance", icon: "factory", span: 6 },
  { id: "late", title: "Late orders in the plan", desc: "Orders in the selected plan that finish after their due date.", tag: "Plan", icon: "alert", span: 6 },
  { id: "shortages", title: "Material shortages", desc: "Materials needed for open demand that are short.", tag: "Materials", icon: "box", span: 6 },
  { id: "heat", title: "Line load by week", desc: "Share of shift time the selected plan uses, per line and week.", tag: "Plan", icon: "grid", span: 12 },
  { id: "notes", title: "About the data", desc: "Data notes and what the plan does not take into account.", tag: "Data", icon: "help", span: 4 },
];
const SUB = {
  output: "Good units per day, this period vs. the previous one", weekday: "Average good units by day of the week", products: "Orders and revenue this period",
  promise: "Confirmed on or before the requested date", plan: "Selected plan and approval path", due: "Open demand by due date",
  supply: "Open demand vs. open production orders, by product", stock: "Lowest cover on the last stock count", downtime: "Time lost on all lines",
  lines: "Attainment, rejects and downtime", late: "Finishing after the due date", shortages: "Needed vs. in stock", heat: "Shift time used per line and week",
  notes: "Read before approving",
};
const money = v => "₹" + new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 2 }).format(v);
const DUE_COLOR = { Overdue: "var(--depth-0)", "Due in 7 days": "var(--brand-orange)", "Due in 8-30 days": "var(--brand-blue)", Later: "var(--chart-c4)" };
// which detailed view (or full page) each card opens
const DRILL = { output: "output", weekday: "weekday", products: "products", promise: "promise", due: "due", supply: "supply", stock: "stock", downtime: "downtime", lines: "lines" };
const PAGE = { plan: "Review & approve", late: "Plan", shortages: "Materials & capacity", heat: "Materials & capacity" };
const DEFAULT = ["kpis", "output", "weekday", "lines", "downtime", "products", "promise", "due", "supply", "stock", "plan", "late", "shortages"];
// cards are grouped into sections by their tag; within a section they keep the user's order
const SECTIONS = [
  { key: "production", title: "Production", desc: "How the lines are running", icon: "factory", tags: ["Performance"], acc: "var(--brand-blue)" },
  { key: "demand", title: "Demand and delivery", desc: "What customers ordered and when it is due", icon: "cart", tags: ["Demand", "Service"], acc: "var(--brand-purple)" },
  { key: "supply", title: "Supply and plan", desc: "Production orders, materials and the plan", icon: "layers", tags: ["Supply", "Materials", "Plan", "Data"], acc: "var(--chart-c4)" },
];
// the cards each position starts with (each can still add or remove cards; the layout is kept per position)
const BY_ROLE = {
  production_manager: ["kpis", "promise", "due", "late", "plan", "shortages", "output", "lines", "supply"],
  plant_head: ["kpis", "output", "lines", "downtime", "weekday", "late", "shortages", "stock"],
};
const KEY = () => "dash.widgets.v3." + auth.role;
const def = () => BY_ROLE[auth.role] || DEFAULT;
const loadIds = () => {
  try {
    const v = JSON.parse(localStorage.getItem(KEY()));
    if (!Array.isArray(v) || !v.length) return def();
    const ids = v.filter(id => CATALOG.some(c => c.id === id));
    return ids;
  } catch { return def(); }
};
const saveIds = ids => { try { localStorage.setItem(KEY(), JSON.stringify(ids)); } catch { /* storage may be blocked */ } };
const CH = { B2B: ["Business (B2B)", "var(--chart-c1)", "users"], DISTRIBUTOR: ["Distributors", "var(--chart-c4)", "box"], E_COMMERCE: ["E-commerce", "var(--brand-orange)", "cart"] };
const KPI_ICON = { orders: "file", units: "layers", output: "factory", downtime: "clock", revenue: "trend", attainment: "check" };
const kfmt = (k, v) => (v == null ? "–" : k.unit === "inr" ? money(v) : k.unit === "pct" ? v + "%" : k.unit === "days" ? `${v} day${v === 1 ? "" : "s"}` : N(v));
const hrs = m => (m >= 1440 ? `${(m / 1440).toFixed(1)} days` : `${(m / 60).toFixed(1)} h`);
const PRIO = [["HIGH", "High", "var(--depth-0)"], ["MEDIUM", "Medium", "var(--brand-orange)"], ["LOW", "Low", "var(--brand-blue)"]];
const STEPS = [["Proposal created", () => true], ["Risks reviewed", s => s !== "PENDING_APPROVAL"], ["Approved by planner", s => s === "APPROVED" || s === "PUBLISHED"], ["Released to production", s => s === "PUBLISHED"]];
const pct = v => (v == null ? "–" : v + "%");
const fmtDay = iso => new Date(iso + "T00:00:00").toLocaleDateString("en", { day: "numeric", month: "short" });

// card -> which data it shows, so a card can say when an active filter cannot narrow it ("plan" cards follow the selected plan)
const AREA = { output: "production", weekday: "production", downtime: "production", lines: "production", products: "orders", promise: "orders",
  due: "orders", supply: "supply", stock: "stock", plan: "plan", late: "plan", shortages: "plan", heat: "plan" };
const KPI_AREA = { orders: "orders", units: "orders", revenue: "orders", output: "production", downtime: "production", attainment: "production" };
const FKEY = () => "dash.filters.v1." + auth.role;
const lockF = f => (auth.role === "plant_head" ? { ...f, plant: auth.plant } : f);           // a production head sees their own plant only
const loadF = () => { try { return lockF(JSON.parse(localStorage.getItem(FKEY())) || {}); } catch { return lockF({}); } };
const qs = (days, f) => `?days=${days}` + Object.entries(f).filter(([, v]) => v).map(([k, v]) => `&${k}=${encodeURIComponent(v)}`).join("");
const PERIODS = [7, 14, 30, 60].map(x => ({ value: x, label: `${x} days` }));

function DashFilters({ f, setF, options, busy, days, setDays, withPeriod, only }) {
  const lines = (options?.line || []).filter(l => !f.plant || l.plant === f.plant);
  const fields = [
    { key: "plant", label: "Plant", all: "All plants", options: opts(options?.plant || [], x => x) },
    { key: "line", label: "Line", all: "All lines", options: opts(lines.map(l => l.value), x => x) },
    { key: "product", label: "Product", all: "All products", options: opts(options?.product || [], x => x) },
    { key: "priority", label: "Priority", all: "All priorities", options: opts(options?.priority || []) },
    { key: "channel", label: "Channel", all: "All channels", options: opts(options?.channel || [], c => (CH[c] || [c])[0]) },
  ].filter(x => (!only || only.includes(x.key)) && !(x.key === "plant" && auth.role === "plant_head"));
  // a line belongs to one plant: changing the plant clears a line from the other plant
  const change = next => setF(!next.line || !next.plant || (options?.line || []).some(l => l.value === next.line && l.plant === next.plant) ? next : { ...next, line: undefined });
  return <FilterBar fields={fields} value={f} onChange={change} busy={busy}
    right={withPeriod && <Segmented label="Period" options={PERIODS} value={days} onChange={setDays} />} />;
}

export default function Dashboard(p) {
  const { d, list, info, role, openSchedule, setTab, exportCsv, ran } = p;
  const [days, setDays] = useState(30);
  const [f, setF] = useState(loadF);
  const [ins, setIns] = useState(p.ins || null);
  const [busy, setBusy] = useState(false);
  const [ids, setIds] = useState(loadIds);
  const [drawer, setDrawer] = useState(false);
  const req = useRef(0);
  useEffect(() => {
    try { localStorage.setItem(FKEY(), JSON.stringify(f)); } catch { /* storage may be blocked */ }
    const n = ++req.current;                                       // only the latest answer is shown, whatever order they arrive in
    setBusy(true);
    const t = setTimeout(() => {
      api(`/api/insights${qs(days, f)}`)
        .then(j => { if (n === req.current) setIns(j); })
        .catch(e => { if (n === req.current && e.api?.code === "UNKNOWN_FILTER_VALUE") setF(lockF({})); })
        .finally(() => { if (n === req.current) setBusy(false); });
    }, 120);                                                       // short pause so quick successive changes make one request
    return () => clearTimeout(t);
  }, [days, f]);
  useEffect(() => saveIds(ids), [ids]);
  const add = id => setIds(x => (x.includes(id) ? x : [...x, id]));
  const remove = id => setIds(x => x.filter(i => i !== id));
  const P = ins?.period;

  function exportSummary() {
    if (!P) return;
    const rows = [["Section", "Item", "Value", "Previous period", "Change %"]];
    P.kpis.forEach(k => rows.push(["Key figures", k.label, k.value, k.prev, k.delta_pct ?? ""]));
    P.channels.forEach(c => rows.push(["Orders by channel", (CH[c.channel] || [c.channel])[0], c.orders, "", c.delta_pct ?? ""]));
    P.products.forEach(r => rows.push(["Products", r.product_id, `${r.orders} orders; ${r.units} units; revenue ${r.revenue_inr}; open ${r.open_units}`, "", r.revenue_delta_pct ?? ""]));
    P.weekday.forEach(w => rows.push(["Average good units by day", w.day, w.avg_good_units, "", ""]));
    rows.push(["Service", "Delivery promise kept (%)", P.promise.pct, P.promise.prev_pct, ""]);
    const csv = rows.map(r => r.map(v => `"${String(v).replace(/"/g, '""')}"`).join(",")).join("\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" })); a.download = `dashboard-summary-last-${days}-days.csv`; a.click(); URL.revokeObjectURL(a.href);
  }

  const [drill, setDrill] = useState(null);           // {key, title, icon, area}: the floating detail window behind a figure
  const openCard = w => (DRILL[w.id] ? setDrill({ key: DRILL[w.id], title: w.title, icon: w.icon, area: AREA[w.id] }) : PAGE[w.id] ? setTab(PAGE[w.id]) : null);
  const applied = ins?.filters?.applies || {};
  const active = Object.keys(f).filter(k => f[k]);
  const na = area => (area === "plan" ? active : active.filter(k => !(applied[area] || []).includes(k)));   // filters that don't narrow this area
  const ctx = { P, ins, d, list, info, role, openSchedule, setTab, exportCsv, openCard, na, planLoading: p.planLoading,
    drillKpi: k => setDrill({ key: k.key, title: k.label, icon: KPI_ICON[k.key], area: KPI_AREA[k.key] }) };
  const filters = <DashFilters f={f} setF={setF} options={ins?.filters?.options} busy={busy} days={days} setDays={setDays} />;
  const kpiOn = ids.includes("kpis");
  const groups = SECTIONS.map(s => ({ ...s, ws: ids.map(id => CATALOG.find(c => c.id === id)).filter(w => w && w.id !== "kpis" && s.tags.includes(w.tag)) }))
    .filter(g => g.ws.length);
  let n = 0;                                                     // running index for the entrance stagger
  return (
    <div className="sp sp-dash dz">
      <header className="dz-hero">
        <div className="dz-hero-t">
          <small className="dz-eyebrow">Production overview · last {days} days</small>
          <h1>Dashboard</h1>
          {!ran ? <p className="muted">The plan and what needs attention appear once the planning agent has run.</p> : P ? <p className="muted">Orders {fmtDay(P.orders_range[0])} – {fmtDay(P.orders_range[1])} · Production {fmtDay(P.production_range[0])} – {fmtDay(P.production_range[1])}</p>
            : <p className="muted">Loading the latest figures…</p>}
        </div>
        <div className="dz-hero-r">
          <Segmented label="Period" options={PERIODS} value={days} onChange={setDays} />
          <button className="sp-btn" onClick={() => setDrawer(true)}><Icon n="grid" size={16} /> Customize</button>
          <button className="sp-btn primary" onClick={exportSummary} disabled={!P}><Icon n="download" size={16} /> Export</button>
        </div>
        {ran && <StatusStrip ins={ins} d={d} list={list} setTab={setTab} />}
      </header>
      {filters}

      {ran && <UseCaseKpis />}
      {kpiOn && <Kpis P={P} onOpen={ctx.drillKpi} na={na} />}
      {!groups.length && !kpiOn && <div className="dz-empty"><p className="muted">No cards on the dashboard. Use <b>Customize</b> to choose some.</p></div>}
      {groups.map(g => (
        <section key={g.key} className="dz-sec" style={{ "--acc": g.acc }} aria-labelledby={`sec-${g.key}`}>
          <header className="dz-sec-h">
            <span className="dz-sec-ic" aria-hidden="true"><Icon n={g.icon} size={16} /></span>
            <div><h2 id={`sec-${g.key}`}>{g.title}</h2><small className="muted">{g.desc}</small></div>
          </header>
          <div className={"sp-grid dz-grid" + (busy ? " busy" : "")} aria-busy={busy}>
            {fit(g.ws).map(([w, span]) => <Widget key={w.id} w={w} span={span} i={n++} onRemove={() => remove(w.id)} ctx={ctx} />)}
          </div>
        </section>
      ))}

      {drawer && <AddWidget ids={ids} add={add} remove={remove} reset={() => setIds(def())} close={() => setDrawer(false)} />}
      {drill && <DrillWindow drill={drill} f0={f} days0={days} options={ins?.filters?.options} only={ins?.filters?.supports?.[drill.area] || []} onClose={() => setDrill(null)} />}
    </div>
  );
}

// ------------------------------------------------------------------ the status strip: where things stand, at a glance
function StatusStrip({ ins, d, list, setTab }) {
  const s = d?.s;
  const pending = list.filter(x => x.status === "PENDING_APPROVAL" && (!x.base_id || x.adopted)).length;
  const lines = ins?.lines?.per_line || [];
  const onTarget = lines.filter(l => (l.attainment_pct ?? 0) >= 95).length;
  const overdue = ins?.demand?.due_buckets?.find(b => b.bucket === "Overdue");
  const chips = [
    s && { tone: (FEAS[s.feasibility] || ["info"])[0], text: `Plan: ${s.feasibility.replace(/_/g, " ").toLowerCase()}`, go: "Review & approve" },
    { tone: pending ? "warn" : "ok", text: pending ? `${pending} plan${pending === 1 ? "" : "s"} waiting for approval` : "No plan waiting for approval", go: pending ? "Review & approve" : null },
    lines.length > 0 && { tone: onTarget === lines.length ? "ok" : onTarget ? "warn" : "bad", text: `${onTarget} of ${lines.length} lines at 95% of target or better`, go: "Digital twin" },
    overdue && { tone: overdue.qty ? "bad" : "ok", text: overdue.qty ? `${K(overdue.qty)} units overdue` : "Nothing overdue", go: "Plan" },
    ins?.stock && { tone: ins.stock.below_safety ? "warn" : "ok", text: `${ins.stock.below_safety} of ${ins.stock.materials} materials below safety stock`, go: "Materials & capacity" },
  ].filter(Boolean);
  if (!ins) return <div className="dz-status" aria-hidden="true">{[0, 1, 2, 3].map(k => <span key={k} className="dz-chip skel" />)}</div>;
  return (
    <ul className="dz-status" aria-label="Where things stand">
      {chips.map(c => (
        <li key={c.text}>
          {c.go ? <button className={"dz-chip t-" + c.tone} onClick={() => setTab(c.go)}><i aria-hidden="true" />{c.text}<Icon n="right" size={12} /></button>
            : <span className={"dz-chip t-" + c.tone}><i aria-hidden="true" />{c.text}</span>}
        </li>))}
    </ul>
  );
}

// ------------------------------------------------------------------ layout: no holes in the grid
// Cards keep the user's order; when a row would end short, its last card widens to fill the row (spans stay 4/6/8/10/12).
function fit(ws) {
  const out = [];
  let row = [], used = 0;
  const close = () => { if (row.length && used < 12) row[row.length - 1][1] += 12 - used; out.push(...row); row = []; used = 0; };
  ws.filter(Boolean).forEach(w => {
    if (used + w.span > 12) close();
    row.push([w, w.span]); used += w.span;
    if (used === 12) close();
  });
  close();
  return out;
}

// ------------------------------------------------------------------ card frame: title, details, the card, and one line on what it shows
function Widget({ w, span, i, onRemove, ctx }) {
  const body = BODIES[w.id];
  const can = DRILL[w.id] || PAGE[w.id];
  let note = null;
  try { note = INSIGHT[w.id]?.(ctx) || null; } catch { note = null; }
  return (
    <section className={`sp-card sp-widget dz-card span${span}`} style={{ "--i": i }} aria-labelledby={`w-${w.id}`}>
      <header className="dz-card-h">
        <span className="dz-card-ic" aria-hidden="true"><Icon n={w.icon} size={16} /></span>
        <div className="dz-card-t">
          {can ? <button className="dz-title" onClick={() => ctx.openCard(w)}><h3 id={`w-${w.id}`}>{w.title}</h3></button> : <h3 id={`w-${w.id}`}>{w.title}</h3>}
          {SUB[w.id] && <small className="muted">{SUB[w.id]}</small>}
        </div>
        <div className="dz-card-a">
          {AREA[w.id] && <NotFiltered keys={ctx.na(AREA[w.id])} />}
          {can && <button className="dz-icon" onClick={() => ctx.openCard(w)} aria-label={`Open details: ${w.title}`} title={DRILL[w.id] ? "Open details" : `Go to ${PAGE[w.id]}`}>
            <Icon n={DRILL[w.id] ? "expand" : "right"} size={14} /></button>}
          <button className="dz-icon dz-rm" aria-label={`Remove ${w.title} from the dashboard`} title="Remove card" onClick={onRemove}><Icon n="close" size={14} /></button>
        </div>
      </header>
      <div className="dz-card-b sp-wb">{body(ctx)}</div>
      {note && <footer className="dz-card-f"><Icon n="sparkle" size={14} /><span>{note}</span></footer>}
    </section>
  );
}

// placeholders shaped like the content they stand in for, so nothing jumps when data arrives
export const Skeleton = ({ lines = 3, chart = false }) => (
  <div className="sp-skel" role="status" aria-label="Loading">
    {chart && <i className="sp-skel-chart" />}
    {Array.from({ length: lines }, (_, k) => <i key={k} style={{ width: `${92 - k * 14}%` }} />)}
  </div>
);
const Loading = () => <Skeleton />;
const Empty = ({ icon = "check", text }) => (
  <div className="sp-empty-in sp-empty-sm"><span className="sp-empty-ic"><Icon n={icon} size={20} /></span><p className="muted">{text}</p></div>
);
const NoPlan = ({ loading }) => (loading ? <Skeleton lines={4} /> : <Empty icon="file" text="No plan yet. Upload purchase orders and the agent makes one." />);

function Spark({ values }) {
  const v = (values || []).map(x => (x == null ? null : +x));
  const pts = v.map((x, i) => [i, x]).filter(([, x]) => x != null);
  if (pts.length < 2) return <span className="dz-spark empty" />;
  const lo = Math.min(...pts.map(p => p[1])), hi = Math.max(...pts.map(p => p[1])), span = hi - lo || 1, last = v.length - 1 || 1;
  const xy = pts.map(([i, x]) => [(i / last) * 100, 30 - ((x - lo) / span) * 26]);
  const line = xy.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)}`).join(" ");
  return (
    <svg className="dz-spark" viewBox="0 0 100 32" preserveAspectRatio="none" aria-hidden="true">
      <path className="a" d={`${line} L${xy[xy.length - 1][0]},32 L${xy[0][0]},32 Z`} />
      <path className="l" d={line} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function Kpis({ P, onOpen, na }) {
  if (!P) return <section className="dz-kpis" aria-label="Key figures">{Array.from({ length: 6 }, (_, k) => <div key={k} className="dz-kpi"><Skeleton lines={3} /></div>)}</section>;
  return (
    <section className="dz-kpis" aria-label="Key figures">
      {P.kpis.map((k, i) => {
        const good = k.delta_pct == null ? null : k.better === "down" ? k.delta_pct <= 0 : k.delta_pct >= 0;
        const nf = na(KPI_AREA[k.key]);
        return (
          <button key={k.key} className={"dz-kpi t-" + (good === null ? "info" : good ? "ok" : "bad")} style={{ "--i": i }} onClick={() => onOpen(k)}
            aria-label={`${k.label}: ${kfmt(k, k.value)}. Open details`}>
            <span className="dz-kpi-h"><span className="dz-kpi-l">{k.label}</span><span className="dz-kpi-ic"><Icon n={KPI_ICON[k.key]} size={16} /></span></span>
            <span className="dz-kpi-v"><Num value={k.value} fmt={v => kfmt(k, v)} /></span>
            <span className="dz-kpi-d"><Delta pct={k.delta_pct} better={k.better} />
              {nf.length ? <small className="muted" title="This figure's data has no such field, so the filter does not change it">all {nf.map(x => x + "s").join(", ")}</small>
                : <small className="muted">vs {k.unit ? kfmt(k, k.prev) : K(k.prev)}</small>}</span>
            <Spark values={k.spark} />
          </button>
        );
      })}
    </section>
  );
}

// busiest production day: pick what "busy" means, click a day for its figures
const WK = [
  { value: "avg_good_units", label: "Good units", fmt: K, best: "max", what: "busiest" },
  { value: "attainment_pct", label: "Attainment", fmt: v => `${v}%`, best: "max", what: "closest to target" },
  { value: "avg_downtime_min", label: "Downtime", fmt: v => `${(v / 60).toFixed(1)} h`, best: "min", what: "fewest stops" },
];
function WeekdayCard({ P }) {
  const [m, setM] = useState(WK[0].value);
  const [wk, setWk] = useState("");                               // "" = average of the period; else a week's Monday
  const M = WK.find(x => x.value === m);
  const week = (P.weekday_weeks || []).find(w => w.start === wk);
  const days = (week ? week.days : P.weekday).filter(w => w.days > 0);   // a part-recorded week shows only its recorded days
  const vals = days.map(w => w[m] ?? 0);
  const bestDay = days[vals.indexOf(M.best === "max" ? Math.max(...vals) : Math.min(...vals))]?.day;
  const [sel, setSel] = useState(null);
  const day = days.find(w => w.day === (sel || bestDay)) || days[0];
  // an honest scale: centred on the average day, at least +-5 %, so a 1 % difference does not look like a big one
  const avg = vals.reduce((t, x) => t + x, 0) / (vals.length || 1);
  const dev = Math.max(...vals.map(x => Math.abs(x - avg)), Math.abs(avg) * 0.05, 1e-9);
  const spread = avg ? (100 * (Math.max(...vals) - Math.min(...vals))) / avg : 0;
  const rel = x => (avg ? `${x >= avg ? "+" : "−"}${Math.abs((100 * (x - avg)) / avg).toFixed(1)}%` : "");
  return (
    <div className="dc-stack">
      <div className="dc-wk-bar">
        <Segmented label="Measure" options={WK.map(x => ({ value: x.value, label: x.label }))} value={m} onChange={v => { setM(v); setSel(null); }} />
        <label className="sp-select dc-wk-sel"><Icon n="calendar" size={14} />
          <select value={wk} onChange={e => { setWk(e.target.value); setSel(null); }} aria-label="Week">
            <option value="">Average of the period</option>
            {(P.weekday_weeks || []).map(w => <option key={w.start} value={w.start}>Week of {w.label}{w.days_recorded < 7 ? ` (${w.days_recorded} days)` : ""}</option>)}
          </select></label>
      </div>
      {spread < 3 && m !== "avg_downtime_min" && <p className="dc-days-note"><Icon n="info" size={12} /> Every day is within {spread.toFixed(1)}% of the others: output is steady across the week. Bars are drawn around the average so small differences show.</p>}
      <ul className="dc-days" aria-label={days.map(w => `${w.day} ${M.fmt(w[m] ?? 0)}`).join(", ")}>
        {days.map(w => {
          const v = w[m] ?? 0, lo = avg - dev * 1.15, span = dev * 2.3 || 1;
          const width = 10 + Math.max(0, Math.min(1, (v - lo) / span)) * 90, d = avg ? (100 * (v - avg)) / avg : 0;
          const good = M.best === "max" ? d >= 0 : d <= 0;
          return (
            <li key={w.day}>
              <button className={(w.day === day?.day ? "on" : "") + (w.day === bestDay ? " best" : "")} aria-pressed={w.day === day?.day} onClick={() => setSel(w.day)}>
                <span className="dc-days-d">{w.day}{w.day === bestDay && <small className="dc-days-best">{M.what === "busiest" ? "Busiest" : M.what === "fewest stops" ? "Fewest stops" : "Best"}</small>}</span>
                <span className="dc-days-t"><i style={{ width: `${width}%` }} /></span>
                <b>{M.fmt(v)}</b>
                <em className={Math.abs(d) < 0.05 ? "" : good ? "up" : "down"}>{rel(v)}</em>
              </button>
            </li>);
        })}
      </ul>
      {day && (
        <div className="dc-day" aria-live="polite">
          <b>{day.day}{day.day === bestDay ? ` · ${M.what}` : ""}</b>
          <div className="dc-day-k">
            <span><small className="muted">Good units{week ? "" : " / day"}</small>{K(day.avg_good_units)}</span>
            <span><small className="muted">Target{week ? "" : " / day"}</small>{K(day.avg_target)}</span>
            <span><small className="muted">Attainment</small>{pct(day.attainment_pct)}</span>
            <span><small className="muted">Downtime{week ? "" : " / day"}</small>{(day.avg_downtime_min / 60).toFixed(1)} h</span>
            {day.reject_pct != null && <span><small className="muted">Rejects</small>{day.reject_pct}%</span>}
            {day.best_line && <span><small className="muted">Best line</small>{day.best_line}</span>}
          </div>
        </div>
      )}
    </div>
  );
}

// one plain sentence under each card, from the figures on it
const INSIGHT = {
  output: ({ P }) => P && `Good units ${P.output.delta_pct >= 0 ? "up" : "down"} ${Math.abs(P.output.delta_pct ?? 0)}% on the previous period; attainment ${pct(P.output.attainment_pct)}.`,
  weekday: ({ P }) => {
    if (!P?.weekday?.length) return null;
    const v = P.weekday.map(w => w.avg_good_units), hi = P.weekday[v.indexOf(Math.max(...v))], lo = P.weekday[v.indexOf(Math.min(...v))];
    const gap = (100 * (hi.avg_good_units - lo.avg_good_units)) / lo.avg_good_units;
    return gap < 3 ? `Across the whole period no day stands out: ${hi.day} is highest, only ${gap.toFixed(1)}% above ${lo.day}. Pick a week to see each one.`
      : `Across the whole period ${hi.day} is the busiest day, ${gap.toFixed(1)}% above ${lo.day}.`;
  },
  products: ({ P }) => {
    const top = [...(P?.products || [])].sort((a, b) => b.revenue_inr - a.revenue_inr)[0];
    return top && `${top.product_id} brings the most revenue: ${money(top.revenue_inr)} from ${N(top.orders)} orders.`;
  },
  promise: ({ P }) => P && `${pct(P.promise.pct)} of orders confirmed on or before the date asked, ${P.promise.pct >= P.promise.prev_pct ? "up" : "down"} ${Math.abs(P.promise.pct - P.promise.prev_pct).toFixed(1)} points.`,
  due: ({ ins }) => {
    const o = ins?.demand?.due_buckets?.find(b => b.bucket === "Overdue");
    return o && (o.qty ? `${K(o.qty)} units on ${N(o.orders)} orders are already past their date.` : "No open order is past its date.");
  },
  supply: ({ ins }) => {
    const rows = Object.keys(ins?.demand?.by_product || {}).map(k => ({ k, c: (ins.supply.by_product[k] || 0) / (ins.demand.by_product[k] || 1) })).sort((a, b) => a.c - b.c);
    return rows[0] && `${rows[0].k} is the least covered: production orders cover ${rows[0].c.toFixed(1)}× its open demand.`;
  },
  stock: ({ ins }) => ins?.stock && `${ins.stock.below_safety} of ${ins.stock.materials} materials are below safety stock on the last count.`,
  downtime: ({ ins }) => {
    const r = ins?.lines?.downtime_by_reason || [], t = r.reduce((s, x) => s + x.minutes, 0);
    return t ? `${r[0].reason === "NONE" ? "Stops with no reason recorded" : nice(r[0].reason)} is the biggest cause of lost time: ${Math.round((100 * r[0].minutes) / t)}% of ${hrs(t)}.` : null;
  },
  lines: ({ ins }) => {
    const l = [...(ins?.lines?.per_line || [])].sort((a, b) => (a.attainment_pct ?? 0) - (b.attainment_pct ?? 0))[0];
    return l && `${l.line_id} is furthest from target at ${pct(l.attainment_pct)} attainment.`;
  },
  late: ({ d }) => d && (d.gaps.late_orders.length ? `${N(d.gaps.late_orders.length)} orders in this plan finish after their date.` : "Every order in this plan finishes on time."),
  shortages: ({ d }) => d && `${N(d.calloff.filter(r => r.shortfall_qty > 0).length)} materials are short for this plan.`,
};

// ------------------------------------------------------------------ the floating detail window behind a figure
// Opens over the dashboard with its own filters (starting from the dashboard's), limited to the ones this figure can be narrowed by.
function DrillWindow({ drill, f0, days0, options, only, onClose }) {
  const [f, setF] = useState(() => Object.fromEntries(Object.entries(f0).filter(([k]) => only.includes(k))));
  const [days, setDays] = useState(days0);
  const [meta, setMeta] = useState(null);
  const box = useRef(), prev = useRef(document.activeElement);
  useEffect(() => {
    box.current?.focus();
    const k = e => {
      if (e.key === "Escape") onClose();
      if (e.key === "Tab" && box.current) {                       // keep keyboard focus inside the window
        const els = [...box.current.querySelectorAll("button, select, input")].filter(x => !x.disabled);
        if (!els.length) return;
        if (e.shiftKey && document.activeElement === els[0]) { e.preventDefault(); els[els.length - 1].focus(); }
        else if (!e.shiftKey && document.activeElement === els[els.length - 1]) { e.preventDefault(); els[0].focus(); }
      }
    };
    window.addEventListener("keydown", k);
    const o = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const back = prev.current;
    return () => { window.removeEventListener("keydown", k); document.body.style.overflow = o; back?.focus?.(); };
  }, [onClose]);
  const query = qs(days, f).replace(`?days=${days}`, "");
  return createPortal(
    <div className="db-drill-bg" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}>
      <section className="db-drill" role="dialog" aria-modal="true" aria-labelledby="db-drill-t" tabIndex={-1} ref={box}>
        <header className="db-drill-h">
          <span className="dz-sec-ic"><Icon n={drill.icon || "chart"} size={18} /></span>
          <div><h2 id="db-drill-t">{meta?.title || drill.title}</h2><small className="muted">{rangeText(meta?.range)}{only.length ? "" : " · covers the whole company: no filters apply"}</small></div>
          <button className="sp-icon-btn" aria-label="Close details" onClick={onClose}><Icon n="close" size={16} /></button>
        </header>
        <div className="db-drill-f"><DashFilters f={f} setF={setF} options={options} days={days} setDays={setDays} withPeriod only={only} /></div>
        <div className="db-drill-b"><DrillSections drillKey={drill.key} days={days} query={query} onMeta={setMeta} /></div>
      </section>
    </div>,
    document.body,                                               // above the sidebar and top bar, outside the page's animated layer
  );
}

const Stat = ({ label, children }) => <div className="dc-stat"><small className="muted">{label}</small><span>{children}</span></div>;
const REASON_C = ["var(--depth-0)", "var(--brand-orange)", "var(--chart-c3)", "var(--chart-c1)", "var(--chart-c4)", "var(--text-muted)"];
const PROD_C = ["var(--chart-c1)", "var(--chart-c3)", "var(--chart-c4)", "var(--brand-orange)"];

// ------------------------------------------------------------------ forecast: recorded weeks in one colour, the weeks ahead in another
const FC_SECTIONS = [["demand", "Demand"], ["production", "Production"], ["inventory", "Inventory"], ["machinery", "Machinery"], ["workforce", "Workforce"]];
const fcFmt = (sec, v) => (v == null ? "–" : sec === "machinery" ? `${(+v).toFixed(1)}%` : sec === "workforce" ? `${(+v).toFixed(1)} people` : K(v));
const fcDay = v => new Date(v + "T00:00").toLocaleDateString("en", { day: "numeric", month: "short" });
// round axis steps (1, 2, 2.5, 5 x 10^n) so the scale reads at a glance
const niceTicks = (lo, hi, n = 4) => {
  const raw = (hi - lo) / n || 1, mag = 10 ** Math.floor(Math.log10(raw)), step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(m => m >= raw) || raw;
  const a = Math.floor(lo / step) * step, out = [];
  for (let v = a; out.length < 12; v += step) { out.push(+v.toFixed(6)); if (v >= hi - step * 0.001) break; }   // the top tick is at or above the highest value
  return out;
};
// a smooth line through the points that never overshoots (monotone cubic)
const smooth = p => {
  if (p.length < 3) return p.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const d = p.slice(1).map((q, i) => (q[1] - p[i][1]) / (q[0] - p[i][0]));
  const m = p.map((_, i) => (i === 0 ? d[0] : i === p.length - 1 ? d[i - 1] : d[i - 1] * d[i] <= 0 ? 0 : (d[i - 1] + d[i]) / 2));
  let s = `M${p[0][0].toFixed(1)},${p[0][1].toFixed(1)}`;
  for (let i = 0; i < p.length - 1; i++) {
    const h = (p[i + 1][0] - p[i][0]) / 3;
    s += ` C${(p[i][0] + h).toFixed(1)},${(p[i][1] + m[i] * h).toFixed(1)} ${(p[i + 1][0] - h).toFixed(1)},${(p[i + 1][1] - m[i + 1] * h).toFixed(1)} ${p[i + 1][0].toFixed(1)},${p[i + 1][1].toFixed(1)}`;
  }
  return s;
};
// The forecast agent's working: what it read, the methods it tested, what it chose, and what the forecast means.
function FcAgent({ a, onRerun }) {
  const [n, setN] = useState(0);
  const total = a.thoughts.length;
  useEffect(() => {
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) { setN(total); return; }
    setN(0);
    let i = 0;
    const t = setInterval(() => { i += 1; setN(i); if (i >= total) clearInterval(t); }, 600);
    return () => clearInterval(t);
  }, [a, total]);
  const done = n >= total;
  const worst = Math.max(1, ...a.methods.map(m => m.error_pct));
  return (
    <aside className={"fc-ag" + (done ? " done" : "")} aria-label="Forecast agent">
      <header>
        <span className="fc-ag-ic"><Icon n="sparkle" size={14} /></span>
        <div><b>Forecast agent</b><small>{done ? `Worked this out at ${new Date(a.ran_at).toLocaleTimeString("en", { hour: "numeric", minute: "2-digit" })}` : "Working it out…"}</small></div>
        <button className="lnk" onClick={onRerun} disabled={!done}><Icon n="refresh" size={12} /> Run again</button>
      </header>
      <ol className="fc-ag-steps" aria-live="polite">
        {a.thoughts.slice(0, n).map((t, i) => <li key={i}>{t}</li>)}
        {!done && <li className="typing" aria-hidden="true"><i /><i /><i /></li>}
      </ol>
      {done && a.methods.length > 0 && (
        <div className="fc-ag-m">
          <small>Methods tested on the last 4 recorded weeks · how far off each was</small>
          {a.methods.map((m, i) => (
            <div key={m.label} className={i === 0 ? "on" : ""}>
              <span title={m.label}>{i === 0 && <Icon n="check" size={12} />}{m.short}</span>
              <i><b style={{ width: `${Math.max(4, (100 * m.error_pct) / worst)}%` }} /></i>
              <strong>{m.error_pct}%</strong>
            </div>))}
        </div>)}
      {done && (
        <ul className="fc-ag-f">
          {a.findings.map((f, i) => <li key={i} className={f.tone}><Icon n={f.tone === "good" ? "check" : "alert"} size={14} /><span>{f.text}</span></li>)}
        </ul>)}
    </aside>
  );
}

// Shown on Review & approve once the agent's run is done (not on the dashboard).
export function ForecastCard() {
  const [sec, setSec] = useState("demand");
  const [plant, setPlant] = useState("");
  const [weeks, setWeeks] = useState(8);
  const [d, setD] = useState(null);
  const [err, setErr] = useState(false);
  const [hi, setHi] = useState(null);
  const [table, setTable] = useState(false);
  const [run, setRun] = useState(0);
  useEffect(() => {
    let live = true;
    setD(null); setErr(false); setHi(null);
    const q = new URLSearchParams({ section: sec, weeks: String(weeks), ...(plant && sec !== "demand" ? { plant } : {}) });
    api(`/api/plan-tools/forecast-agent?${q}`).then(j => live && setD(j)).catch(() => live && setErr(true));
    return () => { live = false; };
  }, [sec, plant, weeks, run]);
  const pts = d ? [...d.history.map(p => ({ ...p, f: false })), ...d.forecast.map(p => ({ ...p, f: true }))] : [];
  const nh = d?.history.length || 0;
  const all = pts.flatMap(p => [p.v, p.lo ?? p.v, p.hi ?? p.v]);
  const mn = Math.min(...all), mx = Math.max(...all), pad = (mx - mn) * 0.12 || Math.abs(mx) * 0.05 || 1;
  const ticks = pts.length ? niceTicks(sec === "inventory" ? 0 : Math.max(0, mn - pad), mx + pad) : [];
  const lo = ticks[0] ?? 0, top = ticks[ticks.length - 1] ?? 1;
  const W = 900, H = 260, L = 4, R = 14, T = 16, B = 6;
  const x = i => L + (pts.length > 1 ? (i * (W - L - R)) / (pts.length - 1) : (W - L - R) / 2);
  const y = v => T + (1 - (v - lo) / ((top - lo) || 1)) * (H - T - B);
  const hP = pts.slice(0, nh).map((p, i) => [x(i), y(p.v)]);
  const fP = [...(nh ? [[x(nh - 1), y(pts[nh - 1].v)]] : []), ...pts.slice(nh).map((p, i) => [x(nh + i), y(p.v)])];
  const band = d?.forecast.some(p => p.hi != null && p.hi !== p.v)
    ? smooth([...(nh ? [[x(nh - 1), y(pts[nh - 1].v)]] : []), ...pts.slice(nh).map((p, i) => [x(nh + i), y(p.hi ?? p.v)])])
      + " L" + smooth([...pts.slice(nh).map((p, i) => [x(nh + i), y(p.lo ?? p.v)]).reverse(), ...(nh ? [[x(nh - 1), y(pts[nh - 1].v)]] : [])]).slice(1)
      + " Z" : null;
  const area = (p, a) => p.length > 1 && `${smooth(p)} L${p[p.length - 1][0].toFixed(1)},${H - B} L${p[0][0].toFixed(1)},${H - B} Z${a}`;
  const every = Math.max(1, Math.ceil(pts.length / 7));
  const cur = hi != null ? pts[hi] : null;
  const S = d?.summary || {};
  const ch = S.change_pct;
  const key = e => {
    if (!pts.length) return;
    if (e.key === "ArrowRight") { e.preventDefault(); setHi(h => Math.min(pts.length - 1, h == null ? nh : h + 1)); }
    if (e.key === "ArrowLeft") { e.preventDefault(); setHi(h => Math.max(0, h == null ? nh - 1 : h - 1)); }
    if (e.key === "Escape") setHi(null);
  };
  return (
    <div className="fc">
      <div className="fc-bar">
        <Segmented label="Section" options={FC_SECTIONS.map(([v, l]) => ({ value: v, label: l }))} value={sec} onChange={v => setSec(v)} />
        <div className="fc-bar-r">
          <label className={"sp-select fc-sel" + (sec === "demand" ? " off" : "")} title={sec === "demand" ? "Customer orders are not recorded by plant" : ""}>
            <Icon n="factory" size={14} />
            <select value={sec === "demand" ? "" : plant} disabled={sec === "demand"} onChange={e => setPlant(e.target.value)} aria-label="Plant">
              <option value="">Both plants</option><option value="IN-TN-01">Hosur</option><option value="IN-KA-01">Bengaluru</option>
            </select></label>
          <Segmented label="Weeks ahead" options={[4, 8, 12].map(w => ({ value: w, label: `${w} wk` }))} value={weeks} onChange={setWeeks} />
        </div>
      </div>
      {err ? <p className="muted">The forecast isn't available right now.</p> : !d ? <Skeleton chart lines={2} /> : (
        <div className="fc-main">
        <div className="fc-left">
          <div className="fc-stats">
            <div className="h"><small><i />{d.history.length ? "Last recorded week" : "Recorded weeks"}</small><b>{d.history.length ? fcFmt(sec, S.last) : "None"}</b>
              <span>{d.history.length ? `week of ${fcDay(d.history[nh - 1].week)}` : "no past records for this section"}</span></div>
            <div className="f"><small><i />Next week</small><b>{fcFmt(sec, S.next)}</b>
              <span>{d.forecast[0]?.hi != null && d.forecast[0].hi !== d.forecast[0].v ? `likely ${fcFmt(sec, d.forecast[0].lo)} – ${fcFmt(sec, d.forecast[0].hi)}` : d.forecast[0] ? `week of ${fcDay(d.forecast[0].week)}` : ""}</span></div>
            <div className="f"><small><i />Average, next {weeks} weeks</small><b>{fcFmt(sec, S.forecast_avg)}</b>
              <span>{ch != null ? <em className={ch >= 0 ? "up" : "down"}><Icon n={ch >= 0 ? "trend" : "down"} size={12} /> {ch >= 0 ? "+" : ""}{ch}% vs last 4 weeks</em> : d.unit}</span></div>
            {d.runs_out
              ? <div className="bad"><small><Icon n="alert" size={12} /> {d.short_materials ? "Materials run short" : "Stock runs out"}</small><b>{fcDay(d.runs_out)}</b>
                <span>{d.short_materials ? `${d.short_materials} materials, unless purchases arrive` : "unless purchases arrive in time"}</span></div>
              : <div><small>Measured as</small><b className="fc-unit">{d.unit}</b><span>{d.plant ? (d.plant === "IN-TN-01" ? "Hosur" : "Bengaluru") : "both plants"}</span></div>}
          </div>

          <div className="fc-chart">
            <div className="fc-head">
              <ul className="fc-key">
                <li><i className="h" />Recorded</li><li><i className="f" />Forecast</li>
                {band && <li><i className="b" />Likely range (80%)</li>}
              </ul>
              <button className="lnk" onClick={() => setTable(t => !t)} aria-pressed={table}>{table ? "Show as chart" : "Show as table"}</button>
            </div>
            {table ? (
              <div className="sp-table fc-table"><table>
                <thead><tr><th>Week of</th><th>Kind</th><th className="num">Value</th><th className="num">Likely range</th></tr></thead>
                <tbody>{pts.map(p => (
                  <tr key={p.week}><td>{fcDay(p.week)}</td><td><span className={"fc-tag " + (p.f ? "f" : "h")}>{p.f ? "Forecast" : "Recorded"}</span></td>
                    <td className="num"><b>{fcFmt(sec, p.v)}</b></td><td className="num muted">{p.f && p.hi != null && p.hi !== p.v ? `${fcFmt(sec, p.lo)} – ${fcFmt(sec, p.hi)}` : "–"}</td></tr>))}
                </tbody></table></div>
            ) : (
              <>
                <div className="fc-plot">
                  <div className="fc-y" aria-hidden="true">{ticks.map(t => <span key={t} style={{ top: `${(100 * y(t)) / H}%` }}>{fcFmt(sec, t)}</span>)}</div>
                  <div className="fc-svgw" tabIndex={0} role="img" onKeyDown={key} onMouseLeave={() => setHi(null)} onBlur={() => setHi(null)}
                    aria-label={`${d.label}: ${nh} recorded weeks then ${d.forecast.length} forecast weeks. Use the arrow keys to read each week.`}>
                    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">
                      <defs>
                        <linearGradient id="fcH" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style={{ stopColor: "var(--brand-blue)", stopOpacity: 0.28 }} /><stop offset="1" style={{ stopColor: "var(--brand-blue)", stopOpacity: 0.02 }} /></linearGradient>
                        <linearGradient id="fcF" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style={{ stopColor: "var(--brand-purple)", stopOpacity: 0.2 }} /><stop offset="1" style={{ stopColor: "var(--brand-purple)", stopOpacity: 0.01 }} /></linearGradient>
                      </defs>
                      {ticks.map(t => <line key={t} x1={L} x2={W - R} y1={y(t)} y2={y(t)} className="fc-grid" vectorEffect="non-scaling-stroke" />)}
                      {nh > 0 && <rect x={x(nh - 1)} y={T - 10} width={W - R - x(nh - 1)} height={H - T + 10 - B} className="fc-zone" rx="8" />}
                      {hP.length > 1 && <path d={area(hP, "")} fill="url(#fcH)" />}
                      {fP.length > 1 && <path d={area(fP, "")} fill="url(#fcF)" />}
                      {band && <path d={band} className="fc-band" />}
                      {hP.length > 1 && <path d={smooth(hP)} className="fc-lh" vectorEffect="non-scaling-stroke" />}
                      {fP.length > 1 && <path d={smooth(fP)} className="fc-lf" vectorEffect="non-scaling-stroke" />}
                      {nh > 0 && <line x1={x(nh - 1)} x2={x(nh - 1)} y1={T - 10} y2={H - B} className="fc-now" vectorEffect="non-scaling-stroke" />}
                      {hi != null && <line x1={x(hi)} x2={x(hi)} y1={T - 10} y2={H - B} className="fc-cross" vectorEffect="non-scaling-stroke" />}
                      {pts.map((p, i) => <rect key={i} x={x(i) - (W / pts.length) / 2} y={0} width={W / pts.length} height={H} fill="transparent" onMouseEnter={() => setHi(i)} />)}
                    </svg>
                    <div className="fc-dots" aria-hidden="true">
                      {pts.map((p, i) => (
                        <i key={i} className={(p.f ? "f" : "h") + (hi === i ? " on" : "") + (i === nh - 1 ? " last" : "")} style={{ left: `${(100 * x(i)) / W}%`, top: `${(100 * y(p.v)) / H}%` }} />))}
                      {nh > 0 && <span className="fc-now-l" style={{ left: `${(100 * x(nh - 1)) / W}%` }}>Now</span>}
                      {cur && (
                        <div className={"fc-pop " + (cur.f ? "f" : "h") + ((100 * x(hi)) / W > 70 ? " left" : "")} style={{ left: `${(100 * x(hi)) / W}%`, top: `${(100 * y(cur.v)) / H}%` }}>
                          <small>{cur.f ? "Forecast" : "Recorded"} · week of {fcDay(cur.week)}</small>
                          <b>{fcFmt(sec, cur.v)}</b>
                          {cur.f && cur.hi != null && cur.hi !== cur.v && <span>likely {fcFmt(sec, cur.lo)} – {fcFmt(sec, cur.hi)}</span>}
                        </div>)}
                    </div>
                  </div>
                </div>
                <div className="fc-x" aria-hidden="true">{pts.map((p, i) => (i === 0 || i === nh - 1 || i === pts.length - 1
                  || (i % every === 0 && Math.abs(i - (nh - 1)) >= every * 0.7 && pts.length - 1 - i >= every * 0.7)) && (
                  <span key={p.week} className={i === nh - 1 ? "now" : p.f ? "f" : ""} style={{ left: `${(100 * x(i)) / W}%` }}>{fcDay(p.week)}</span>))}
                </div>
              </>
            )}
          </div>
          <p className="fc-note"><Icon n="info" size={12} /> {d.method}.{sec === "workforce" ? " Only the weeks ahead exist for crews, from the roster and planned leave." : ""} Point at the chart, or focus it and use the arrow keys, to read each week.</p>
        </div>
        {d.agent && <FcAgent a={d.agent} onRerun={() => setRun(r => r + 1)} />}
        </div>
      )}
    </div>
  );
}

const BODIES = {
  output: ({ P }) => !P ? <Loading /> : (
    <div className="dc-stack dc-fill">
      <div className="dc-stats">
        <Stat label="Good units"><Num value={P.output.total} fmt={N} /><Delta pct={P.output.delta_pct} /></Stat>
        <Stat label="Attainment"><Num value={P.output.attainment_pct} fmt={pct} /></Stat>
        <Stat label="Previous period"><Num value={P.output.prev_attainment_pct} fmt={pct} /></Stat>
        <ul className="dc-key" aria-label="Legend"><li><i className="bar" />This period</li><li><i className="tick" />Previous period</li></ul>
      </div>
      <div className="dc-well"><Columns points={P.output.series.map(x => ({ label: fmtDay(x.date), v: x.v, p: x.p }))} /></div>
      <div className="dc-chan-w">
        <div className="dc-chan-h"><b>Orders by sales channel</b><small className="muted">this period · change on the previous one</small></div>
        <div className="dc-chan">
          {P.channels.map(c => {
            const [name, color, icon] = CH[c.channel] || [c.channel, "var(--chart-c3)", "box"];
            const all = P.channels.reduce((t, x) => t + x.orders, 0) || 1;
            const share = (100 * c.orders) / all;
            return (
              <div key={c.channel} className="dc-chan-i" style={{ "--c": color }}>
                <span className="dc-chan-ic"><Icon n={icon} size={16} /></span>
                <div className="dc-chan-t"><small>{name}</small><span><b>{N(c.orders)}</b><small className="muted">orders</small></span></div>
                <Delta pct={c.delta_pct} />
                <span className="dc-chan-sh"><i className="dc-chan-bar" aria-hidden="true"><em style={{ width: `${share}%` }} /></i><small>{share.toFixed(0)}% of orders</small></span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  ),
  weekday: ({ P }) => !P ? <Loading /> : <WeekdayCard P={P} />,
  products: ({ P, ins }) => {
    if (!P) return <Loading />;
    const rows = [...P.products].sort((a, b) => b.revenue_inr - a.revenue_inr);
    return (
      <div className="dc-split">
        <div className="dc-well"><Donut parts={rows.map((r, i) => ({ label: r.product_id, value: r.revenue_inr, color: PROD_C[i % PROD_C.length] }))}
          center={money(P.revenue.value_inr)} sub="revenue" fmt={money} /></div>
        <ul className="dc-prods">
          {rows.map((r, i) => (
            <li key={r.product_id} style={{ "--c": PROD_C[i % PROD_C.length] }}>
              <div className="sp-row"><b><i />{r.product_id}</b><Delta pct={r.revenue_delta_pct} /></div>
              <div className="dc-prods-k"><span><small className="muted">Orders</small>{N(r.orders)}</span><span><small className="muted">Units</small>{K(r.units)}</span>
                <span><small className="muted">Open</small>{K(r.open_units)}</span></div>
            </li>))}
          {ins && <li className="dc-prods-sum"><small className="muted">{N(ins.demand.open_orders)} open orders · {K(ins.demand.open_qty)} units still to ship</small></li>}
        </ul>
      </div>
    );
  },
  promise: ({ P }) => !P ? <Loading /> : (
    <div className="dc-center">
      <Ring pct={P.promise.pct} prev={P.promise.prev_pct} label="Confirmed on time" sub={`Previous period ${pct(P.promise.prev_pct)} (marker)`} size={150} />
      <div className="dc-stats dc-stats-c">
        <Stat label="Change"><b className={P.promise.pct >= P.promise.prev_pct ? "ok" : "bad"}>{(P.promise.pct - P.promise.prev_pct > 0 ? "+" : "") + (P.promise.pct - P.promise.prev_pct).toFixed(1)} pts</b></Stat>
      </div>
    </div>
  ),
  plan: ({ d, list, role, openSchedule, setTab, exportCsv, planLoading }) => {
    const s = d?.s, f = s ? FEAS[s.feasibility] || ["", s.feasibility] : null;
    return (
      <div className="sp-plan">
        <div className="sp-row">
          <label className="sp-select"><select value={s?.id || ""} onChange={e => openSchedule(e.target.value)} aria-label="Choose a plan">
            {list.length ? list.map(x => <option key={x.id} value={x.id}>{x.name}</option>) : <option value="">No plans yet</option>}
          </select></label>
        </div>
        {s ? <p><b>{s.name}</b> <small className="muted">· starts {dt(s.as_of).slice(0, 10)} · by {s.created_by}</small><br /><b className={f[0]}>{s.feasibility.replace(/_/g, " ").toLowerCase()}</b></p> : <NoPlan loading={planLoading} />}
        <ol className="sp-steps">
          {STEPS.map(([t, done]) => <li key={t} className={s && done(s.status) ? "done" : ""}><span>{s && done(s.status) ? <Icon n="check" size={12} /> : null}</span>{t}</li>)}
        </ol>
        <div className="sp-row sp-left sp-wrap">
          <button className="sp-btn primary" onClick={() => setTab("Review & approve")}>Review &amp; approve</button>
          <button className="sp-btn" onClick={exportCsv} disabled={!s || !can("export", role)}><Icon n="download" size={14} /> Dispatch list</button>
        </div>
      </div>
    );
  },
  due: ({ ins }) => {
    if (!ins) return <Loading />;
    const b = ins.demand.due_buckets;
    return (
      <div className="dc-split">
        <div className="dc-well dc-well-c"><Waffle parts={b.map(x => ({ label: x.bucket, value: x.qty, color: DUE_COLOR[x.bucket] }))} /></div>
        <div className="dc-stack">
          <ul className="dc-legend dc-legend-lg">{b.map(x => (
            <li key={x.bucket}><i style={{ background: DUE_COLOR[x.bucket] }} /><span>{x.bucket}<small className="muted"> · {N(x.orders)} orders</small></span>
              <b className={x.bucket === "Overdue" && x.qty ? "bad" : ""}>{K(x.qty)}</b></li>))}</ul>
          <div className="dc-prio">
            <small className="muted">Open units by priority</small>
            <div className="dc-prio-bar" role="img" aria-label={PRIO.map(([k, l]) => `${l} ${K(ins.demand.by_priority[k] || 0)}`).join(", ")}>
              {PRIO.map(([k, l, c]) => <i key={k} style={{ flex: ins.demand.by_priority[k] || 0, background: c }} title={`${l}: ${N(ins.demand.by_priority[k] || 0)} units`} />)}
            </div>
            <ul className="dc-key">{PRIO.map(([k, l, c]) => <li key={k}><i style={{ background: c }} />{l} {K(ins.demand.by_priority[k] || 0)}</li>)}</ul>
          </div>
        </div>
      </div>
    );
  },
  supply: ({ ins }) => {
    if (!ins) return <Loading />;
    const rows = Object.keys(ins.demand.by_product).map(k => {
      const dem = ins.demand.by_product[k], po = ins.supply.by_product[k] || 0;
      return { label: k, a: dem, b: po, tag: dem ? `${(po / dem).toFixed(1)}× covered` : null };
    });
    return <div className="dc-well"><Dumbbell rows={rows} aLabel="Open demand" bLabel="Open production orders" /></div>;
  },
  stock: ({ ins }) => !ins ? <Loading /> : (
    <div className="dc-stack">
      <small className="muted">{ins.stock.below_safety} of {ins.stock.materials} materials below safety stock · bar = usable, line = safety stock</small>
      <div className="dc-well"><Bullet rows={ins.stock.lowest_cover.map(r => ({ label: r.material_id, value: r.usable, mark: r.safety }))} /></div>
    </div>
  ),
  downtime: ({ ins }) => {
    if (!ins) return <Loading />;
    const rows = ins.lines.downtime_by_reason, total = rows.reduce((t, r) => t + r.minutes, 0);
    if (!total) return <Empty text="No downtime recorded for this selection." />;
    return (
      <div className="dc-well"><Donut parts={rows.map((r, i) => ({ label: r.reason === "NONE" ? "Reason not recorded" : nice(r.reason), value: r.minutes, color: REASON_C[i % REASON_C.length] }))}
        center={hrs(total)} sub="lost, all lines" fmt={hrs} size={150} /></div>
    );
  },
  lines: ({ ins }) => !ins ? <Loading /> : !ins.lines.per_line.length ? <Empty icon="factory" text="No production on these lines for this selection." /> : (
    <div className="dc-rings">
      {ins.lines.per_line.map(r => <Ring key={r.line_id} pct={r.attainment_pct} label={r.line_id}
        sub={`${K(r.good_units)} good · ${r.reject_pct ?? "–"}% rejects · ${hrs(r.downtime_min)} down`} />)}
    </div>
  ),
  late: ({ d, planLoading }) => !d ? <NoPlan loading={planLoading} /> : !d.gaps.late_orders.length ? <Empty text="No late orders in this plan." /> : (
    <ul className="sp-list">
      {d.gaps.late_orders.slice(0, 8).map(r => (
        <li key={r.customer_order_id}><b>{r.customer_order_id}</b><small className="muted">{N(r.qty)} units</small><b className="bad">{r.late_days} days late</b></li>
      ))}
    </ul>
  ),
  shortages: ({ d, planLoading }) => !d ? <NoPlan loading={planLoading} /> : !d.calloff.some(r => r.shortfall_qty > 0) ? <Empty text="No material is short for this plan." /> : (
    <BarList empty="No material shortfall." rows={d.calloff.filter(r => r.shortfall_qty > 0).sort((x, y) => y.shortfall_qty - x.shortfall_qty).slice(0, 6)
      .map(r => ({ label: r.material_id, value: r.shortfall_qty, display: `${K(r.shortfall_qty)} short · ${K(r.available_qty)} in stock`, color: "var(--brand-orange)" }))} />
  ),
  heat: ({ d, planLoading }) => !d ? <NoPlan loading={planLoading} /> : (d.s.assignments.length ? <Heat gaps={d.gaps} /> : <Empty icon="calendar" text="The selected plan schedules no work, so no line time is used." />),
  notes: ({ d, info }) => <Notes s={d?.s} issues={info?.issues} />,
};

function Notes({ s, issues }) {
  const items = [...(issues || []).map(x => ({ k: "About the data", t: plainDq(x) })), ...(s?.constraints_not_enforced || []).map(t => ({ k: "Not considered in this plan", t }))];
  const [i, setI] = useState(0);
  const n = Math.max(items.length, 1), it = items[i % n];
  return (
    <div className="sp-notes">
      <small className="muted">{it ? `${it.k} · ${(i % n) + 1} of ${items.length}` : "Nothing to flag."}</small>
      <p>{it?.t}</p>
      <div className="sp-row sp-left">
        <button className="sp-icon-btn" aria-label="Previous note" onClick={() => setI(x => (x - 1 + n) % n)}><Icon n="left" size={16} /></button>
        <button className="sp-icon-btn" aria-label="Next note" onClick={() => setI(x => (x + 1) % n)}><Icon n="right" size={16} /></button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ Add widget drawer
function AddWidget({ ids, add, remove, reset, close }) {
  useEffect(() => { const k = e => e.key === "Escape" && close(); window.addEventListener("keydown", k); return () => window.removeEventListener("keydown", k); }, [close]);
  return (
    <div className="sp-overlay" onClick={close}>
      <aside className="sp-drawer" role="dialog" aria-modal="true" aria-labelledby="aw-title" onClick={e => e.stopPropagation()}>
        <header className="sp-row">
          <h2 id="aw-title">Customize the dashboard</h2>
          <button className="sp-icon-btn" aria-label="Close" onClick={close}><Icon n="close" size={16} /></button>
        </header>
        <div className="sp-drawer-list">
          {CATALOG.map(c => {
            const on = ids.includes(c.id);
            return (
              <div key={c.id} className={"sp-wopt" + (on ? " on" : "")}>
                <div className="sp-wprev"><Icon n={c.icon} size={28} /></div>
                <div className="sp-wtxt">
                  <b>{c.title}</b>
                  <small>{c.desc}</small>
                  <div className="sp-row">
                    <span className="sp-tag">#{c.tag}</span>
                    {on ? <button className="sp-btn" onClick={() => remove(c.id)}>Remove</button> : <button className="sp-btn primary" onClick={() => add(c.id)}>Select</button>}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
        <button className="lnk" onClick={reset}>Reset to the default layout</button>
      </aside>
    </div>
  );
}

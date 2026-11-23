import { useEffect, useMemo, useRef, useState } from "react";
import { api, auth, can } from "../api.js";
import { Delta } from "./charts.jsx";
import { FEAS, N, codeHelp, label as codeLabel, plainDq } from "./ui.jsx";
import { Icon, K } from "./widgets.jsx";
import { FilterBar, Segmented, opts } from "./Filters.jsx";
import { Num } from "./motion.js";
import RunOrder from "./RunOrder.jsx";

// Planning pages (Schedule, Scenarios, Review & approve, Details). Same look as the dashboard. Every number comes from the API.

const FEAS_TITLE = {
  NO_NEW_PRODUCTION_NEEDED: "No new production needed", COMPLETE_ON_TIME: "All demand scheduled on time", COMPLETE_WITH_LATE_RISK: "All demand scheduled, some jobs late",
  INCOMPLETE: "Not all demand can be scheduled", NOTHING_SCHEDULABLE: "Nothing can be scheduled",
};
const SUBS = {
  Plan: "What each line makes and when, and the weeks ahead", Scenarios: "Test what-ifs against the selected plan and compare the outcome",
  "Review & approve": "Check what you are approving, then approve or reject",
  "Materials & capacity": "Inventory, purchasing, machinery, line capacity and workforce: pick a category to see its details",
};
const TONE = { PENDING_APPROVAL: "warn", APPROVED: "ok", REJECTED: "bad", PUBLISHED: "ok" };
const nice = s => { const t = String(s ?? "").replace(/_/g, " ").toLowerCase(); return t.charAt(0).toUpperCase() + t.slice(1); };
const PALETTE = ["var(--chart-c1)", "var(--chart-c3)", "var(--chart-c4)", "var(--chart-c2)", "var(--cyan)", "var(--depth-5)"];
const fmtDT = v => (v ? new Date(v).toLocaleString("en", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "–");
const fmtD = v => (v ? new Date(String(v).length <= 10 ? v + "T00:00:00" : v).toLocaleDateString("en", { day: "numeric", month: "short", year: "numeric" }) : "–");

// ------------------------------------------------------------------ shared pieces
export function PlanBar({ tab, list, sel, s, onSelect, onRefresh }) {
  const f = s ? FEAS[s.feasibility] || ["", ""] : null;
  const full = list.filter(x => x.fully_approved);                 // every approval stage done: shown in their own colour
  const isFull = full.some(x => x.id === sel);
  return (
    <>
      <div className="sp-head">
        <div><h1>{tab}</h1><small className="muted">{SUBS[tab]}</small></div>
        <div className="sp-head-r">
          <label className="sp-select"><Icon n="file" size={16} />
            <select value={sel} onChange={e => onSelect(e.target.value)} aria-label="Plan">
              {list.length ? list.map(x => <option key={x.id} value={x.id} className={x.fully_approved ? "pb-opt-ok" : undefined}>{x.fully_approved ? "✓ " : ""}{x.name}{x.base_id && !x.adopted ? " (what-if)" : ""} · {x.fully_approved ? "Fully approved" : nice(x.status)}</option>) : <option value="">No plans yet</option>}
            </select>
          </label>
          <button className="sp-icon-btn" onClick={onRefresh} aria-label="Refresh" title="Refresh"><Icon n="refresh" size={16} /></button>
        </div>
      </div>
      {tab === "Plan" && full.length > 0 && (
        <div className="pb-ok-row" role="group" aria-label="Approved plans">
          <small>Approved plans</small>
          {full.map(x => (
            <button key={x.id} className={"pb-ok" + (x.id === sel ? " on" : "")} aria-pressed={x.id === sel} onClick={() => onSelect(x.id)}>
              <Icon n="check" size={12} />{x.name}</button>))}
        </div>
      )}
      {s && (
        <div className={"sp-callout t-" + (f[0] || "info") + (isFull ? " pb-full" : "")}>
          <span className="sp-callout-ic"><Icon n={f[0] === "bad" ? "alert" : f[0] === "ok" ? "check" : "help"} size={18} /></span>
          <div className="sp-callout-t"><b>{FEAS_TITLE[s.feasibility] || nice(s.feasibility)}</b><span>{f[1].replace(FEAS_TITLE[s.feasibility] + ". ", "").replace("ASSUMPTION:", "Assumption:")}</span></div>
          <div className="sp-callout-r">
            <span className={"sp-pill t-" + (isFull ? "ok" : TONE[s.status] || "info")}>{isFull ? "Fully approved" : nice(s.status)}</span>
            <small className="muted">{s.name} · starts {fmtD(s.as_of)} · by {s.created_by}</small>
          </div>
        </div>
      )}
    </>
  );
}

// item.num (a number) rolls to its new value, formatted by item.fmt; item.value is shown as-is
export const KpiStrip = ({ items }) => (
  <div className="sp-kstrip">
    {items.map(it => (
      <div key={it.key ?? it.label} className="sp-kpi">
        <small className="muted">{it.label}</small>
        {typeof it.num === "number" ? <Num value={it.num} fmt={it.fmt || N} className={it.tone || ""} /> : <b className={it.tone || ""}>{it.value ?? "–"}</b>}
        {it.note && <small className="muted">{it.note}</small>}
      </div>
    ))}
  </div>
);

// The use case's seven success measures for one plan (default: the plan everyone works from). Dashboard and Plan page.
export function UseCaseKpis({ sid, title = "Success measures" }) {
  const [k, setK] = useState(null);
  const [err, setErr] = useState(false);
  useEffect(() => {
    let live = true;
    setK(null); setErr(false);
    api(`/api/plan-tools/kpis${sid ? `?schedule_id=${encodeURIComponent(sid)}` : ""}`).then(j => live && setK(j)).catch(() => live && setErr(true));
    return () => { live = false; };
  }, [sid]);
  const fmt = it => it.value == null ? "–" : it.unit === "%" ? `${it.value}%` : it.unit === "h" ? `${it.value} h` : N(it.value);
  const items = k?.items || Array.from({ length: 7 }, (_, i) => ({ key: i }));
  return (
    <section className="uk" aria-label={title}>
      <header className="uk-h">
        <div><h3>{title}</h3>
          <small className="muted">{k ? `${k.schedule_name} · ${k.status === "APPROVED" || k.status === "PUBLISHED" ? "approved plan" : "waiting for approval"}` : "The seven measures the planning use case is judged on"}</small></div>
        <small className="muted uk-key"><span className="uk-est">Estimate</span> worked out from limited records</small>
      </header>
      {err ? <p className="muted">These figures aren't available right now.</p> : (
        <div className="uk-grid">
          {items.map((it, i) => !k ? <div key={i} className="dz-kpi uk-card"><span className="uk-skel" /><span className="uk-skel w" /></div> : (
            <div key={it.key} className={"dz-kpi uk-card t-" + (UK_TONE[it.tone] || "info")} style={{ "--i": i }}
              aria-label={`${it.label}: ${fmt(it)}${it.estimate ? " (estimate)" : ""}. ${it.note}`}>
              <span className="dz-kpi-h"><span className="dz-kpi-l">{it.label}</span><span className="dz-kpi-ic"><Icon n={UK_ICON[it.key] || "chart"} size={16} /></span></span>
              <span className="dz-kpi-v"><b>{fmt(it)}</b></span>
              <span className="dz-kpi-d">
                {it.tone && <em className={"uk-pill t-" + UK_TONE[it.tone]}>{UK_WORD[it.tone]}</em>}
                {it.estimate && <em className="uk-est">Estimate</em>}
              </span>
              <small className="uk-note">{it.note}</small>
              <i className="uk-meter" aria-hidden="true"><b style={{ width: it.unit === "%" && it.value != null ? `${Math.min(100, Math.max(2, it.value))}%` : "100%" }} /></i>
            </div>))}
        </div>)}
    </section>
  );
}
const UK_ICON = { adherence: "calendar", attainment: "check", on_time: "truck", changeover: "refresh", wip: "layers", utilisation: "factory", stability: "grid" };
const UK_TONE = { ok: "ok", warn: "warn", bad: "bad" };
const UK_WORD = { ok: "On track", warn: "Watch", bad: "Below target" };

export const EmptyState = ({ icon = "help", title, text, action }) => (
  <div className="sp-card sp-empty">
    <span className="sp-empty-ic"><Icon n={icon} size={26} /></span>
    <b>{title}</b>
    <p className="muted">{text}</p>
    {action}
  </div>
);

// cols: [{key, label, num?, render?(row), sort?(row)}]
export function DataTable({ title, sub, cols, rows, pageSize = 20, right, empty = "Nothing to show." }) {
  const [sort, setSort] = useState(null);
  const [n, setN] = useState(pageSize);
  const sorted = useMemo(() => {
    if (!sort) return rows;
    const c = cols.find(x => x.key === sort.key), get = c.sort || (r => r[c.key]);
    const out = [...rows].sort((a, b) => (get(a) > get(b) ? 1 : get(a) < get(b) ? -1 : 0));
    return sort.dir < 0 ? out.reverse() : out;
  }, [rows, sort, cols]);
  return (
    <section className="sp-card">
      <header className="sp-ch"><div><h3>{title}</h3>{sub && <small className="sp-sub-t">{sub}</small>}</div>{right}</header>
      {!rows.length ? <p className="muted">{empty}</p> : (
        <div className="sp-table">
          <table>
            <thead><tr>{cols.map(c => (
              <th key={c.key} className={c.num ? "num" : ""} aria-sort={sort?.key === c.key ? (sort.dir > 0 ? "ascending" : "descending") : "none"}>
                <button className="sp-th" onClick={() => setSort(x => ({ key: c.key, dir: x?.key === c.key ? -x.dir : c.num ? -1 : 1 }))}>
                  {c.label}{sort?.key === c.key ? (sort.dir > 0 ? " ▲" : " ▼") : ""}
                </button>
              </th>))}</tr></thead>
            <tbody>{sorted.slice(0, n).map((r, i) => (
              <tr key={i}>{cols.map(c => <td key={c.key} className={c.num ? "num" : ""}>{c.render ? c.render(r) : c.num ? N(r[c.key]) : r[c.key]}</td>)}</tr>
            ))}</tbody>
          </table>
        </div>
      )}
      {rows.length > n && <button className="lnk" onClick={() => setN(rows.length)}>Show all {rows.length} rows</button>}
    </section>
  );
}

const Reason = ({ c }) => <span className="sp-reason" title={codeHelp(c)}>{codeLabel(c)}</span>;

// ------------------------------------------------------------------ Schedule
export function ScheduleView({ s, dispatch, filt, setFilt, setTab }) {
  const m = s.summary;
  const head = auth.role === "plant_head";
  if (head) dispatch = dispatch.filter(r => r.plant_id === auth.plant);          // a production head sees their own plant only
  const plants = [...new Set(dispatch.map(r => r.plant_id))].sort();
  const products = [...new Set(dispatch.map(r => r.product_id))].sort();
  const color = p => PALETTE[products.indexOf(p) % PALETTE.length];
  const shown = dispatch.filter(r => (!filt.fp || r.plant_id === filt.fp) && (!filt.fl || r.line_id === filt.fl)
    && (!filt.fpr || r.product_id === filt.fpr) && (!filt.fpi || r.priority === filt.fpi)
    && (!filt.fs || (filt.fs === "late") === (r.status === "LATE_RISK"))
    && (!filt.fo || r.customer_order_id.toLowerCase().includes(filt.fo.toLowerCase()))
    && (!filt.fd || (String(r.start).slice(0, 10) <= filt.fd && String(r.finish).slice(0, 10) >= filt.fd)));
  const lineOpts = [...new Set(dispatch.filter(r => !filt.fp || r.plant_id === filt.fp).map(r => r.line_id))].sort();
  const setScheduleFilt = next => setFilt(next.fl && !dispatch.some(r => r.line_id === next.fl && (!next.fp || r.plant_id === next.fp)) ? { ...next, fl: undefined } : next);
  // with filters on, the figures describe the filtered jobs (dispatch rows are one per stage; final assembly carries the units)
  const filtered = head || Object.values(filt).some(Boolean);
  const fal = shown.filter(r => r.stage === "FAL"), orders = new Set(shown.map(r => r.customer_order_id));
  const kpis = filtered ? [
    { label: "Jobs", num: orders.size, note: "matching the filters" }, { label: "Units scheduled", num: fal.reduce((t, r) => t + r.qty, 0) },
    { label: "Late jobs", num: new Set(shown.filter(r => r.status === "LATE_RISK").map(r => r.customer_order_id)).size, tone: shown.some(r => r.status === "LATE_RISK") ? "bad" : "ok" },
    { label: "Changeovers", num: shown.filter(r => r.setup_min > 0).length, note: `${N(shown.reduce((t, r) => t + (r.setup_min || 0), 0))} min` },
    { label: "Lines involved", num: new Set(shown.map(r => r.line_id)).size },
    { label: "Covered by production orders", value: K(m.po_covered_qty), note: "whole plan" },
  ] : [
    { label: "Jobs", num: s.assignments.length }, { label: "Units scheduled", num: m.scheduled_qty },
    { label: "Late jobs", num: m.late_jobs, tone: m.late_jobs ? "bad" : "ok" }, { label: "Changeovers", num: m.changeovers, note: `${N(m.changeover_min)} min` },
    { label: "Avg line use", num: m.avg_utilisation == null ? undefined : Math.round(m.avg_utilisation * 1000) / 10, fmt: v => v + "%", value: "–",
      note: s.assignments.length ? `busiest: ${m.bottleneck_line}` : null },
    { label: "Covered by production orders", num: m.po_covered_qty, fmt: K },
  ];
  if (!s.assignments.length) {
    return (
      <>
        <KpiStrip items={kpis} />
        <EmptyState icon="calendar" title="No production is scheduled in this plan"
          text={s.feasibility === "NO_NEW_PRODUCTION_NEEDED"
            ? "Open demand is fully covered by open production orders, so nothing new needs to be made."
            : "Nothing could be scheduled. See Review & approve for the reasons."}
          action={<button className="sp-btn primary" onClick={() => setTab("Review & approve")}><Icon n="check" size={16} /> Go to Review & approve</button>} />
      </>
    );
  }
  return (
    <>
      <FilterBar value={filt} onChange={setScheduleFilt} fields={[
        { key: "fpr", label: "Product", all: "All products", options: opts(products, x => x) },
        { key: "fpi", label: "Priority", all: "All priorities", options: opts(["HIGH", "MEDIUM", "LOW"].filter(p => dispatch.some(r => r.priority === p))) },
        { key: "fs", label: "Status", all: "On time and late", options: [{ value: "late", label: "Late only" }, { value: "on_time", label: "On time only" }] },
        { key: "fd", label: "Running on", type: "date" },
        { key: "fo", label: "Find an order…", type: "search" },
      ]} />
      <KpiStrip items={kpis} />
      <RunOrder key={"ro" + s.id} rows={shown} color={color} />
      <Gantt key={s.id} rows={shown} color={color} products={products} plants={plants} lineOpts={lineOpts} filt={filt} setFilt={setScheduleFilt} head={head} />
    </>
  );
}

const PLANT_NAME = { "IN-TN-01": "Hosur", "IN-KA-01": "Bengaluru" };
const STAGE = { SMT: "Circuit boards", FAL: "Final assembly" };
const DAY = 864e5;

// Line timeline: one lane per line, grouped by location; bars are jobs coloured by product. Late jobs are outlined and striped
// (not colour alone). Location and line choose what is shown; a week picker zooms in; bars open a tooltip on hover or focus.
function Gantt({ rows: all, color, products, plants, lineOpts, filt, setFilt, head }) {
  const [tip, setTip] = useState(null);
  const [week, setWeek] = useState(null);                                  // null = whole plan, else the week's start (ms)
  const box = useRef(null);
  const t0all = all.length ? Math.min(...all.map(r => +new Date(r.start))) : 0, t1all = all.length ? Math.max(...all.map(r => +new Date(r.finish))) : 0;
  const first = new Date(new Date(t0all).toDateString()).getTime();
  const weeks = [];
  for (let w = first; w < t1all; w += 7 * DAY) weeks.push(w);
  const t0 = week ?? t0all, t1 = week != null ? week + 7 * DAY : t1all, span = Math.max(1, t1 - t0);
  const rows = all.filter(r => +new Date(r.finish) > t0 && +new Date(r.start) < t1);
  const X = t => Math.min(100, Math.max(0, ((t - t0) / span) * 100));
  const hr = 36e5, step = [1, 2, 3, 6, 12, 24, 48, 72, 168, 336].map(h => h * hr).find(x => span / x <= 8) || 336 * hr;
  const mid = new Date(new Date(t0).toDateString()).getTime(), ticks = [];
  for (let t = mid + Math.ceil((t0 - mid) / step) * step; t <= t1; t += step) ticks.push(t);
  const days = [];
  if (span <= 21 * DAY) for (let d = mid; d < t1; d += DAY) days.push(d);
  const tickLabel = t => new Date(t).toLocaleString("en", step < 24 * hr ? { hour: "numeric", minute: "2-digit" } : { weekday: span <= 8 * DAY ? "short" : undefined, day: "numeric", month: "short" });
  const now = Date.now();
  const groups = [...new Set(rows.map(r => r.plant_id))].sort().map(p => ({
    plant: p, lanes: [...new Set(rows.filter(r => r.plant_id === p).map(r => r.line_id))].sort((a, b) => (a.startsWith("SMT") ? 0 : 1) - (b.startsWith("SMT") ? 0 : 1) || a.localeCompare(b)) }));
  const busy = l => {
    const ms = rows.filter(r => r.line_id === l).reduce((t, r) => t + Math.max(0, Math.min(t1, +new Date(r.finish)) - Math.max(t0, +new Date(r.start))), 0);
    return Math.round((100 * ms) / span);
  };
  const show = (r, e) => {
    const c = box.current?.getBoundingClientRect(), b = e.currentTarget.getBoundingClientRect();
    if (c) setTip({ r, x: b.left - c.left + b.width / 2, y: b.bottom - c.top + 6, right: b.left - c.left > c.width * 0.6 });
  };
  const locOpts = [...(head ? [] : [{ value: "", label: "All locations" }]), ...plants.map(p => ({ value: p, label: PLANT_NAME[p] || p }))];
  const late = rows.filter(r => r.status === "LATE_RISK").length;
  return (
    <section className="sp-card ltl">
      <header className="ltl-h">
        <div><h3>Line timeline</h3>
          <small className="sp-sub-t">{rows.length ? `${N(new Set(rows.map(r => r.customer_order_id)).size)} jobs on ${groups.reduce((t, g) => t + g.lanes.length, 0)} line${groups.reduce((t, g) => t + g.lanes.length, 0) === 1 ? "" : "s"}${late ? ` · ${late} late` : ""} · point at a bar, or tab to it, for details` : "No jobs in this view"}</small></div>
        <ul className="sp-legend ltl-legend">{products.map(p => <li key={p}><i className="purple" style={{ background: color(p) }} />{p}</li>)}<li><i className="ltl-late-key" />Late</li></ul>
      </header>
      <div className="ltl-ctrl">
        <div className="ltl-ctrl-g"><span>Location</span>
          {head ? <span className="ltl-fixed"><Icon n="factory" size={14} /> {PLANT_NAME[plants[0]] || plants[0]} <small className="muted">· your plant</small></span>
            : <Segmented label="Location" options={locOpts} value={filt.fp || ""} onChange={v => setFilt({ ...filt, fp: v || undefined, fl: undefined })} />}
        </div>
        <div className="ltl-ctrl-g"><span>Production line</span>
          <div className="ltl-chips" role="group" aria-label="Production line">
            {[{ v: "", l: "All lines" }, ...lineOpts.map(v => ({ v, l: v, s: STAGE[v.slice(0, 3)] }))].map(o => (
              <button key={o.v || "all"} className={"ltl-chip" + ((filt.fl || "") === o.v ? " on" : "")} aria-pressed={(filt.fl || "") === o.v}
                onClick={() => setFilt({ ...filt, fl: o.v || undefined })}>{o.l}{o.s && <small>{o.s}</small>}</button>))}
          </div>
        </div>
        <label className="sp-select ltl-week"><Icon n="calendar" size={14} />
          <select value={week ?? ""} onChange={e => { setWeek(e.target.value ? +e.target.value : null); setTip(null); }} aria-label="Time range">
            <option value="">Whole plan</option>
            {weeks.map(w => <option key={w} value={w}>Week of {new Date(w).toLocaleDateString("en", { day: "numeric", month: "short" })}</option>)}
          </select></label>
      </div>
      {!rows.length ? <p className="muted">No jobs match this location, line and filters{week != null ? " in this week" : ""}.</p> : (
        <div className="ltl-plot" ref={box} onMouseLeave={() => setTip(null)}>
          <div className="ltl-axis"><div />{<div className="ltl-axis-t">{ticks.map(t => <span key={t} style={{ left: `${X(t)}%`, transform: `translateX(${X(t) < 4 ? 0 : X(t) > 96 ? -100 : -50}%)` }}>{tickLabel(t)}</span>)}</div>}</div>
          {groups.map(g => (
            <div key={g.plant} className="ltl-group">
              <div className="ltl-gh"><Icon n="factory" size={14} /> {PLANT_NAME[g.plant] || g.plant}<small>{g.lanes.length} line{g.lanes.length > 1 ? "s" : ""}</small></div>
              {g.lanes.map(l => (
                <div key={l} className="ltl-lane">
                  <div className="ltl-lab" title={`${l} · ${STAGE[l.slice(0, 3)] || ""} · ${busy(l)}% busy`}>
                    <span className="ltl-lab-t"><b>{l}</b><em aria-label={`${busy(l)}% busy`}>{busy(l)}%</em></span><small>{STAGE[l.slice(0, 3)] || ""}</small></div>
                  <div className="ltl-track">
                    {days.map((d, i) => i % 2 === 1 && <i key={d} className="ltl-day" style={{ left: `${X(d)}%`, width: `${X(d + DAY) - X(d)}%` }} />)}
                    {ticks.map(t => <i key={t} className="sp-gline" style={{ left: `${X(t)}%` }} />)}
                    {now > t0 && now < t1 && <i className="ltl-now" style={{ left: `${X(now)}%` }} />}
                    {rows.filter(r => r.line_id === l).slice(0, 600).map((r, i) => {
                      const a = X(+new Date(r.start)), w = Math.max(0.4, X(+new Date(r.finish)) - a);
                      return (
                        <button key={i} className={"ltl-bar" + (r.status === "LATE_RISK" ? " late" : "") + (tip?.r === r ? " on" : "")}
                          aria-label={`${r.customer_order_id}, ${r.product_id}, ${N(r.qty)} units, ${fmtDT(r.start)} to ${fmtDT(r.finish)}${r.status === "LATE_RISK" ? ", late" : ""}`}
                          style={{ left: `${a}%`, width: `${w}%`, "--c": color(r.product_id) }}
                          onMouseEnter={e => show(r, e)} onFocus={e => show(r, e)} onBlur={() => setTip(null)}>
                          {w > 6 && <span>{r.customer_order_id}</span>}
                        </button>);
                    })}
                  </div>
                </div>))}
            </div>))}
          {now > t0 && now < t1 && <div className="ltl-now-l" style={{ left: `calc(150px + (100% - 150px) * ${X(now) / 100})` }}>Today</div>}
          {tip && (
            <div className={"ltl-tip" + (tip.right ? " r" : "")} style={{ left: tip.x, top: tip.y }} role="status">
              <small><i style={{ background: color(tip.r.product_id) }} />{tip.r.product_id} · {STAGE[tip.r.stage] || tip.r.stage}</small>
              <b>{tip.r.customer_order_id}</b>
              <span>{N(tip.r.qty)} units · {tip.r.priority.charAt(0) + tip.r.priority.slice(1).toLowerCase()} priority</span>
              <span>{fmtDT(tip.r.start)} → {fmtDT(tip.r.finish)}</span>
              <span>{tip.r.line_id} · {PLANT_NAME[tip.r.plant_id] || tip.r.plant_id}{tip.r.setup_min ? ` · ${N(tip.r.setup_min)} min changeover first` : ""}</span>
              <em className={tip.r.status === "LATE_RISK" ? "bad" : "ok"}>{tip.r.status === "LATE_RISK" ? "Finishes after its due date" : "On time"}</em>
            </div>)}
        </div>)}
    </section>
  );
}

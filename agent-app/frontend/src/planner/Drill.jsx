import { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import { AreaChart, Delta } from "./charts.jsx";
import { N } from "./ui.jsx";
import { BarList, Icon, K } from "./widgets.jsx";
import { Num } from "./motion.js";

// Drill-through: the detailed view behind a dashboard card. Sections come from the server; this file only lays them out.
const money = v => "₹" + new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 2 }).format(v);
const fmt = (v, unit) => (v == null ? "–" : unit === "inr" ? money(v) : unit === "pct" ? `${v}%` : typeof v === "number" ? N(v) : String(v));
const fmtDay = iso => (/^\d{4}-\d{2}-\d{2}$/.test(iso) ? new Date(iso + "T00:00:00").toLocaleDateString("en", { day: "numeric", month: "short" }) : iso);
const label = s => { const t = String(s ?? "").replace(/_/g, " "); return /^[A-Z ]+$/.test(t) && t.length >= 3 ? t.toLowerCase().replace(/(^|\s)\S/g, c => c.toUpperCase()) : t; };

// The sections behind a dashboard figure, for the floating detail window. Keeps the current figures while new filters load.
export default function DrillSections({ drillKey, days, query = "", onMeta }) {
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let live = true;
    setErr(null); setBusy(true);
    api(`/api/insights/detail/${drillKey}?days=${days}${query}`)
      .then(j => { if (live) { setD(j); onMeta?.({ title: j.title, range: j.range }); } })
      .catch(e => live && setErr(e.message)).finally(() => live && setBusy(false));
    return () => { live = false; };
  }, [drillKey, days, query]);                                  // eslint-disable-line react-hooks/exhaustive-deps
  if (err) return <p className="bad" role="alert">This view couldn't be loaded. {err}</p>;
  if (!d) return <div className="db-drill-skel" role="status" aria-label="Loading details">{Array.from({ length: 4 }, (_, k) => <i key={k} />)}</div>;
  return <div className={"sp-dgrid" + (busy ? " busy" : "")} aria-busy={busy}>{d.sections.map((s, i) => <Section key={i} s={s} />)}</div>;
}

export const rangeText = r => (!r ? "" : r[0] === r[1] ? `as of ${fmtDay(r[0])}` : `${fmtDay(r[0])} – ${fmtDay(r[1])}`);

function Section({ s }) {
  if (s.type === "stats") {
    return (
      <div className="sp-dstats span12">
        {s.items.map(it => (
          <div key={it.label} className="sp-kpi">
            <small className="muted">{it.label}</small>
            {it.unit === "delta" ? <Delta pct={it.value} /> : <Num value={it.value} fmt={v => fmt(v, it.unit)} />}
          </div>
        ))}
      </div>
    );
  }
  if (s.type === "series") {
    return (
      <section className="sp-card span12">
        <header className="sp-ch"><div><h3>{s.title}</h3><small className="sp-sub-t">Hover the chart for exact values</small></div>
          <ul className="sp-legend"><li><i className="solid" />This period</li><li><i className="dash" />Previous period</li></ul></header>
        <AreaChart points={s.points.map(p => ({ label: fmtDay(p.label), v: p.v, p: p.p }))} />
      </section>
    );
  }
  if (s.type === "bars") {
    return (
      <section className="sp-card span6">
        <header className="sp-ch"><div><h3>{s.title}</h3></div></header>
        <BarList scale={s.max} rows={s.rows.map(r => ({ label: label(r.label), value: r.value || 0, display: s.unit === "inr" ? money(r.value) : s.unit === "pct" ? `${r.value}%` : K(r.value), color: "var(--brand-blue)" }))} />
      </section>
    );
  }
  return <TableSection s={s} />;
}

function TableSection({ s }) {
  const [sort, setSort] = useState(null);           // {key, dir}
  const [all, setAll] = useState(false);
  const rows = useMemo(() => {
    if (!sort) return s.rows;
    const out = [...s.rows].sort((a, b) => (a[sort.key] > b[sort.key] ? 1 : a[sort.key] < b[sort.key] ? -1 : 0));
    return sort.dir < 0 ? out.reverse() : out;
  }, [s.rows, sort]);
  const shown = all ? rows : rows.slice(0, 12);
  const csv = () => {
    const lines = [s.columns.map(c => c.label), ...rows.map(r => s.columns.map(c => r[c.key] ?? ""))].map(r => r.map(v => `"${String(v).replace(/"/g, '""')}"`).join(","));
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" })); a.download = `${s.title.replace(/[^a-z0-9]+/gi, "-").toLowerCase()}.csv`; a.click(); URL.revokeObjectURL(a.href);
  };
  return (
    <section className="sp-card span12">
      <header className="sp-ch">
        <div><h3>{s.title}</h3><small className="sp-sub-t">{rows.length} rows · click a column to sort</small></div>
        <button className="sp-btn" onClick={csv}><Icon n="download" size={14} /> Download</button>
      </header>
      <div className="sp-table">
        <table>
          <thead><tr>{s.columns.map(c => (
            <th key={c.key} className={c.num ? "num" : ""} aria-sort={sort?.key === c.key ? (sort.dir > 0 ? "ascending" : "descending") : "none"}>
              <button className="sp-th" onClick={() => setSort(x => ({ key: c.key, dir: x?.key === c.key ? -x.dir : c.num ? -1 : 1 }))}>
                {c.label}{sort?.key === c.key ? (sort.dir > 0 ? " ▲" : " ▼") : ""}
              </button>
            </th>
          ))}</tr></thead>
          <tbody>{shown.map((r, i) => (
            <tr key={i}>{s.columns.map(c => <td key={c.key} className={c.num ? "num" : ""}>{c.num ? N(r[c.key]) : label(r[c.key])}</td>)}</tr>
          ))}</tbody>
        </table>
      </div>
      {rows.length > 12 && <button className="lnk" onClick={() => setAll(x => !x)}>{all ? "Show fewer" : `Show all ${rows.length} rows`}</button>}
    </section>
  );
}

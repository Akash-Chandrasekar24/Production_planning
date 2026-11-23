import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Icon, K } from "./widgets.jsx";
import { Segmented } from "./Filters.jsx";
import { Skeleton } from "./Dashboard.jsx";

const KIND = { buy: ["cart", "Buy"], capacity: ["factory", "Capacity"], crew: ["users", "Crew"], maintenance: ["gear", "Maintenance"] };
const day = v => (v ? new Date(String(v).slice(0, 10) + "T00:00").toLocaleDateString("en", { day: "numeric", month: "short" }) : "–");

// The weeks ahead, shown under the schedule on the Plan page: demand against capacity, and the most urgent things to do.
export function Outlook({ onError }) {
  const [weeks, setWeeks] = useState(12);
  const [d, setD] = useState(null);
  const [all, setAll] = useState(false);
  useEffect(() => {
    let live = true;
    setD(null);
    api(`/api/plan-tools/outlook?weeks=${weeks}`).then(j => live && setD(j)).catch(e => live && onError?.(e));
    return () => { live = false; };
  }, [weeks]);                                                     // eslint-disable-line react-hooks/exhaustive-deps
  const S = d?.summary;
  const acts = (d?.actions || []).slice(0, all ? undefined : 5);
  return (
    <section className="sp-card pa-out">
      <header className="sp-ch">
        <div><h3>Coming weeks</h3><small className="sp-sub-t">{S ? `Demand ${K(S.demand)} · lines can make ${K(S.capacity)} · ${S.weeks_over} week${S.weeks_over === 1 ? "" : "s"} over capacity · ${S.materials_to_buy} materials to order`
          : "Demand against what the lines can make"}</small></div>
        <div className="sp-ch-r"><Segmented label="Horizon" options={[8, 12, 16].map(w => ({ value: w, label: `${w} weeks` }))} value={weeks} onChange={setWeeks} /></div>
      </header>
      {!d ? <Skeleton chart lines={3} /> : (
        <div className="pa-out-body">
          <div>
            <ul className="pa-key" aria-hidden="true"><li><i className="firm" />Firm orders</li><li><i className="fc" />Forecast</li><li><i className="cap" />Capacity</li></ul>
            <WeekChart rows={d.weeks} />
            {d.reasoning?.text && (
              <section className="ai-r" aria-label="AI reasoning">
                <header><span className="ai-r-ic" aria-hidden="true"><Icon n="sparkle" size={14} /></span><b>AI reasoning</b>
                  <small className="muted">{d.reasoning.at ? `after the agent's run at ${new Date(d.reasoning.at).toLocaleTimeString("en", { hour: "numeric", minute: "2-digit" })}` : "on the weeks ahead"}</small></header>
                <p key={d.reasoning.at || "start"}>{d.reasoning.text}</p>
              </section>)}
          </div>
          <div>
            <h4>What to do next</h4>
            <ol className="pa-acts">
              {acts.map((a, i) => {
                const late = new Date(a.date) < new Date(d.as_of);
                return (
                  <li key={i} className={"t-" + a.severity}>
                    <span className="pa-when"><b>{late ? "Late" : day(a.date)}</b><small className="muted">{late ? `due ${day(a.date)}` : a.date === d.as_of ? "today" : ""}</small></span>
                    <span className="pa-ic"><Icon n={KIND[a.kind]?.[0] || "info"} size={16} /></span>
                    <div><b>{a.title}</b><small className="muted">{a.detail}</small></div>
                  </li>);
              })}
              {!acts.length && <li className="t-ok"><div><b>Nothing to do in these weeks.</b></div></li>}
            </ol>
            {d.actions.length > 5 && <button className="lnk" onClick={() => setAll(v => !v)}>{all ? "Show fewer" : `Show all ${d.actions.length}`}</button>}
          </div>
        </div>
      )}
    </section>
  );
}

function WeekChart({ rows }) {
  const [hi, setHi] = useState(null);
  const top = Math.max(...rows.map(r => Math.max(r.demand, r.capacity)), 1);
  const cur = hi == null ? null : rows[hi];
  return (
    <div className="pa-chart">
      <div className="dc-cols-tip" aria-live="polite">{cur ? <><b>Week of {cur.week}</b> · firm {K(cur.firm)} · forecast {K(cur.forecast)} · capacity {K(cur.capacity)} · load {cur.load_pct}%</>
        : <span className="muted">Point at a week to see it</span>}</div>
      <div className="pa-plot" onMouseLeave={() => setHi(null)} role="img" aria-label={rows.map(r => `${r.week}: demand ${K(r.demand)}, capacity ${K(r.capacity)}`).join("; ")}>
        {rows.map((r, i) => (
          <span key={r.start} className={"pa-wk" + (r.demand > r.capacity * 1.02 ? " over" : "") + (hi === i ? " on" : "")} onMouseEnter={() => setHi(i)}>
            <span className="pa-bar">
              <i className="fc" style={{ height: `${(100 * r.forecast) / top}%` }} />
              <i className="firm" style={{ height: `${(100 * r.firm) / top}%` }} />
            </span>
            <i className="cap" style={{ bottom: `${(100 * r.capacity) / top}%` }} />
            <small>{r.week}</small>
          </span>
        ))}
      </div>
    </div>
  );
}

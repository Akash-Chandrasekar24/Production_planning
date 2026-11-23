import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Icon } from "./widgets.jsx";
import { FilterBar } from "./Filters.jsx";

// The agent team: the orchestrator and its four agents. Shows what each one last saw and, as a feed, every signal they raised and
// where the orchestrator sent it (planning agent, or an alert to the people who own it). Everything comes from the agents' API.

const when = v => (v ? new Date(String(v).endsWith("Z") ? v : v + "Z").toLocaleString("en", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "–");
const ICON = { floor: "factory", inventory: "box", orders: "file", sourcing: "truck", planning: "calendar" };
const SEV = { critical: ["bad", "Critical"], warn: ["warn", "Warning"], info: ["info", "Notice"] };
const STATUS = { PLANNED: "New plan", RECOVERY: "Recovery plan", ALERTED: "Alert sent", FAILED: "Will retry", NEW: "Handling" };

export default function AgentTeam({ refresh, openSchedule, setTab, onProblem, onError }) {
  const [s, setS] = useState(null);
  const [f, setF] = useState({});
  useEffect(() => {
    const load = () => api("/api/agent/orchestrator?limit=60").then(setS).catch(onError);
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [refresh]);   // eslint-disable-line react-hooks/exhaustive-deps
  if (!s) return <section className="sp-card"><p className="muted" role="status">Loading the agent team…</p></section>;
  const evs = s.events.filter(e => (!f.agent || e.agent === f.agent) && (!f.severity || e.severity === f.severity));
  const agents = [...new Set(s.events.map(e => e.agent))];
  return (
    <section className="sp-card">
      <header className="sp-ch"><div><h3>Agent team</h3>
        <small className="sp-sub-t">The orchestrator asks each agent what it sees every {s.interval_min} minutes, then sends each finding to the planning agent or to the people who own it</small></div></header>
      <div className="at-team">
        <div className="at-orch"><span className="at-ic"><Icon n="burst" size={18} /></span>
          <div><b>Orchestrator</b><small className="muted">Runs the team, gathers what the agents find, decides who acts</small></div></div>
        <div className="at-agents">
          {s.agents.map(a => (
            <article key={a.key} className={"at-agent t-" + (a.last?.error ? "bad" : a.last ? "ok" : "info")}>
              <header><span className="at-ic"><Icon n={ICON[a.key]} size={16} /></span><b>{a.name}</b>
                <i className="tw-dot" aria-label={a.last?.error ? "Failed at its last look" : a.last ? "Working" : "Not run yet"} /></header>
              <small className="muted">{a.job}</small>
              <small>{a.last?.error ? "Its last look failed; the next round tries again." : a.last?.summary || "Waiting for the first round"}</small>
              {a.raised != null && <small className="muted">{a.last?.at ? `Last look ${when(a.last.at)} · ` : ""}{a.raised} signal{a.raised === 1 ? "" : "s"} raised</small>}
            </article>))}
        </div>
      </div>

      <h4 className="at-h">What the agents found</h4>
      <FilterBar value={f} onChange={setF} className="sp-filterbar-in" fields={[
        { key: "agent", label: "Agent", all: "All agents", options: agents.map(a => ({ value: a, label: a[0].toUpperCase() + a.slice(1) })) },
        { key: "severity", label: "Severity", all: "All", options: Object.entries(SEV).map(([k, [, l]]) => ({ value: k, label: l })) },
      ]} />
      {!evs.length ? <p className="muted">{s.events.length ? "Nothing matches these filters." : "Nothing yet. The first round runs when the service starts."}</p> : (
        <ol className="at-feed">{evs.map(e => (
          <li key={e.id} className={"t-" + SEV[e.severity][0]}>
            <span className="sp-bullet" />
            <div>
              <div className="sp-row sp-left sp-wrap"><span className={"sp-pill t-" + SEV[e.severity][0]}>{SEV[e.severity][1]}</span><small className="muted">{e.agent} · {when(e.created_at)}</small></div>
              <b>{e.title}</b>
              <small><Icon n="arrow" size={12} /> {STATUS[e.status] || e.status}{e.outcome ? `: ${e.outcome}` : ""}</small>
              <div className="sp-row sp-left sp-wrap">
                {e.refs?.incident_id && <button className="lnk" onClick={() => onProblem(e.refs.incident_id)}>See the analysis ›</button>}
                {e.refs?.schedule_id && <button className="lnk" onClick={() => { openSchedule(e.refs.schedule_id); setTab("Review & approve"); }}>Open the plan ›</button>}
              </div>
            </div>
          </li>))}
        </ol>)}
    </section>
  );
}

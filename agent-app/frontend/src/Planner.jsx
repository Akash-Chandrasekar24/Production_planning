import { useEffect, useState } from "react";
import { PAGES, api, refreshRan, useAgentRan } from "./api.js";
import { Icon } from "./planner/widgets.jsx";
import Dashboard, { Skeleton } from "./planner/Dashboard.jsx";
import { Materials } from "./planner/views.jsx";
import { PlanBar, ScheduleView, UseCaseKpis } from "./planner/PlanningViews.jsx";
import { ReviewFlow } from "./planner/OrderIntake.jsx";
import IntegrationsView from "./planner/IntegrationsView.jsx";
import TwinView from "./planner/TwinView.jsx";
import { Outlook } from "./planner/PlanAhead.jsx";
import UsersView from "./planner/UsersView.jsx";
import Overview from "./planner/Overview.jsx";
import Pipeline, { StageDetail } from "./planner/Pipeline.jsx";
import PlantBrief from "./planner/PlantBrief.jsx";
import PlanOptions, { PlanHistory } from "./planner/PlanOptions.jsx";
import { Demand, PlanDocument, PlanNotes, RoughCut, VolumeGate } from "./planner/FlowPages.jsx";
import { Segmented } from "./planner/Filters.jsx";
import { Readiness, WhatIf } from "./planner/PlanPages.jsx";


// Shows what the API returns. Feasibility, risks and approval rules are all decided by the backend.
export default function Planner({ role, tab, setTab, jump, onMeta }) {
  const [list, setList] = useState([]);
  const [sel, setSel] = useState("");
  const [d, setD] = useState(null);                 // {s, calloff, gaps, dispatch, audit}
  const [status, setStatus] = useState(null);
  const [refData, setRefData] = useState(null);
  const [dq, setDq] = useState(null);
  const [ins, setIns] = useState(null);
  const [filt, setFilt] = useState({});
  const [planView, setPlanView] = useState("options");     // Plan page: the agent's five plans, or the plan in detail
  const [msg, setMsg] = useState(null);
  const [loading, setLoading] = useState(false);
  const [fatal, setFatal] = useState(null);
  const ran = useAgentRan();                           // false until the planning agent has run since the app started

  const say = (text, err) => { setMsg({ text, err }); setTimeout(() => setMsg(null), err ? 9000 : 4000); };
  const fail = e => say(e.message, true);
  const run = fn => async (...a) => { try { await fn(...a); } catch (e) { fail(e); } };

  async function loadList(keep) {
    const l = await api("/api/schedules?limit=50");
    setList(l);
    const next = keep && l.some(x => x.id === keep) ? keep : l[0]?.id || "";
    setSel(next);
    return next;
  }
  async function loadSchedule(id) {
    if (!id) return setD(null);
    setLoading(true);
    try {
      const [s, calloff, gaps, dispatch, audit] = await Promise.all([api(`/api/schedules/${id}`), api(`/api/schedules/${id}/material-calloff`),
        api(`/api/schedules/${id}/capacity-gaps`), api(`/api/schedules/${id}/dispatch`), api(`/api/schedules/${id}/audit`)]);
      setD({ s, calloff, gaps, dispatch, audit });
    } finally { setLoading(false); }
  }
  async function loadStatus() { setStatus(await api("/api/status")); }

  async function boot() {
    setFatal(null);
    for (let attempt = 1; ; attempt++) {           // the service may still be starting: retry a few times before showing an error
      try {
        const soft = path => api(path).catch(() => null);            // everything at once: the page waits for the slowest, not the sum
        const [, r, q, n, first] = await Promise.all([loadStatus(), soft("/api/reference"), soft("/api/data-quality"), soft("/api/insights"), loadList()]);
        setRefData(r); setDq(q); setIns(n);
        await loadSchedule(first);
        return;
      } catch (e) {
        if (attempt >= 4) { setFatal(e.message); return; }
        await new Promise(r => setTimeout(r, 1500 * attempt));
      }
    }
  }
  useEffect(() => { boot(); }, []);                 // eslint-disable-line react-hooks/exhaustive-deps

  const reload = async id => { await loadStatus(); await loadSchedule(await loadList(id)); };



  const exportCsv = run(async () => {
    const r = await api(`/api/schedules/${sel}/export`, "GET", undefined, true);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await r.blob()); a.download = `schedule-${sel}.csv`; a.click(); URL.revokeObjectURL(a.href);
    say("Dispatch list downloaded (not released to production).");
  });
  const openSchedule = run(async id => { setSel(id); await loadList(id); await loadSchedule(id); });
  const openInDetail = async id => { await openSchedule(id); setPlanView("schedule"); setTab("Plan"); };
  useEffect(() => { if (jump?.order) { setFilt({ fo: jump.order }); setTab("Plan"); } }, [jump]);   // search: the order's jobs on the plan
  useEffect(() => { onMeta?.({ pending: ran ? list.filter(x => x.status === "PENDING_APPROVAL" && (!x.base_id || x.adopted)).length : 0, notes: (dq || []).length, ran }); }, [list, dq, ran]);
  useEffect(() => { if (ran) reload(sel).catch(() => {}); }, [ran]);   // the agent's new plan   // eslint-disable-line react-hooks/exhaustive-deps

  if (fatal) return <div className="panel bad" role="alert">The planning service is not reachable right now. <button className="sp-btn" onClick={boot}>Try again</button></div>;

  if (tab === "Review & approve") {
    return (
      <>
        <ReviewFlow role={role} setTab={setTab} onError={fail} onMsg={say} onDecided={() => reload(sel)} ran={ran} onRan={refreshRan}
          onOpenPlan={async id => { await openSchedule(id); setTab("Plan document"); }} />
        <div className="toast" aria-live="assertive">{msg && <div className={msg.err ? "err" : ""}>{msg.text}</div>}</div>
      </>
    );
  }
  if (tab === "Digital twin") return <TwinView dispatch={loading ? null : d?.dispatch} planName={d?.s?.name} planLoading={loading || (!d && !fatal)} setTab={setTab} />;
  if (tab === "Users") {
    return (
      <>
        <UsersView onMsg={say} onError={fail} />
        <div className="toast" aria-live="assertive">{msg && <div className={msg.err ? "err" : ""}>{msg.text}</div>}</div>
      </>
    );
  }
  if (tab === "Overview") return <Overview setTab={setTab} onError={fail} />;
  if (tab === "Previous plans") return <div className="page sp-enter"><PlanHistory title="Previous plans and where they stand" sub="Every planning run: what the agent recommended, which plan was chosen and its approval status" onOpen={openInDetail} /></div>;
  if (tab === "Plan history") return <div className="page sp-enter"><PlanHistory sub="Every planning run: what the agent recommended, which plan was chosen and its approval status" onOpen={openInDetail} /></div>;
  if (tab === "System architecture") return <ArchPage />;
  if (tab === "Connections") {
    return (
      <>
        <IntegrationsView role={role} onMsg={say} onError={fail} onStarted={() => setTab("Review & approve")} />
        <div className="toast" aria-live="assertive">{msg && <div className={msg.err ? "err" : ""}>{msg.text}</div>}</div>
      </>
    );
  }
  let body = null;
  const options = tab === "Plan" && planView === "options";       // the Plan page opens on the agent's five plans
  const waiting = ran !== true && tab !== "Dashboard" && !options;
  if (waiting) body = ran === null ? <div className="sp-card"><Skeleton lines={4} /></div>
    : <><VolumeGate role={role} onError={fail} onDone={() => { refreshRan(); reload(sel).catch(() => {}); }} quiet /><NotYet role={role} setTab={setTab} /></>;
  else if (tab === "Dashboard") body = <Dashboard ran={ran} d={ran ? d : null} ins={ins} list={ran ? list : []} refData={refData} info={{ issues: dq, inventory_snapshot: status?.data?.latest?.inventory_snapshot }}
    role={role} openSchedule={openSchedule} setTab={setTab} exportCsv={exportCsv}
    canPublish={(status?.integrations?.erp_mes_publish || "").startsWith("AVAILABLE")} planLoading={loading} />;
  else if (options) body = <PlanOptions role={role} onError={fail} onMsg={say} onChosen={async id => { await openSchedule(id); setTab("Plan document"); }} />;
  else if (loading) body = (
    <div className="sp-pageskel" role="status" aria-label="Loading plan">
      <div className="sp-kstrip">{Array.from({ length: 6 }, (_, k) => <div key={k} className="sp-kpi"><Skeleton lines={2} /></div>)}</div>
      <div className="sp-card"><Skeleton chart lines={2} /></div>
      <div className="sp-card"><Skeleton lines={6} /></div>
    </div>
  );
  else if (!d) body = <div className="sp-card"><p className="muted">No plan yet. Upload purchase orders in <b>Connections</b> and the agent makes one.</p></div>;
  else {
    const { s, calloff, gaps, dispatch, audit } = d;
    body = {
      Plan: <><UseCaseKpis sid={s.id} title="Success measures for this plan" /><PlantBrief sid={s.id} /><PlanNotes sid={s.id} /><ScheduleView s={s} dispatch={dispatch} filt={filt} setFilt={setFilt} setTab={setTab} /><Outlook onError={fail} /></>,
      "Materials & capacity": <><RoughCut sid={s.id} /><Materials s={s} calloff={calloff} gaps={gaps} /></>,
      Demand: <Demand s={s} role={role} onError={fail} />,
      "Plan document": <PlanDocument s={s} role={role} />,
      "Material readiness": <Readiness s={s} onError={fail} />,
      "What-if": <WhatIf s={s} refData={refData} list={list} onSaved={() => loadList(sel).catch(() => {})} onError={fail}
        onAdopted={id => { say("The agent re-planned with your change. The new plan is waiting for the production manager's approval."); openSchedule(id); }} />,
    }[tab];
  }

  return (
    <div className="page">
      {tab === "Plan" && (
        <div className="pv-switch"><Segmented label="Plan view" value={planView} onChange={setPlanView}
          options={[{ value: "options", label: "The agent's five plans" }, { value: "schedule", label: "Plan in detail" }]} /></div>)}
      {tab !== "Dashboard" && !waiting && !options && (
        <PlanBar tab={tab} list={list} sel={sel} s={loading ? null : d?.s}
          onSelect={run(async id => { setSel(id); await loadSchedule(id); })} onRefresh={run(() => reload(sel))} />)}
      <div role="tabpanel" tabIndex={-1} aria-live="polite" key={tab} className="sp-enter">{body}</div>
      <div className="toast" aria-live="assertive">{msg && <div className={msg.err ? "err" : ""}>{msg.text}</div>}</div>
    </div>
  );
}

// System architecture: the interactive map (built from graphify's map of the code) or the drawn diagram; both fit the screen.
function ArchPage() {
  const [view, setView] = useState("map");
  const [stage, setStage] = useState("core");
  return (
    <div className="page sp-enter arch-page">
      <div className="arch-top"><h1 className="sr-only">System architecture</h1>
        <Segmented label="View" options={[{ value: "map", label: "Pipeline" }, { value: "diagram", label: "Drawn diagram" }]} value={view} onChange={setView} /></div>
      {view === "map" ? <div className="arch-pl"><section className="sp-card"><Pipeline sel={stage} onSelect={setStage} /></section>
        <section className="sp-card"><StageDetail stage={stage} /></section></div>
        : <iframe className="arch-frame" src="/architecture.html" title="System architecture diagram" />}
    </div>
  );
}

// Before the planning agent has run (since the app started) the plan pages show this instead of a plan or warnings.
function NotYet({ role, setTab }) {
  return (
    <section className="sp-card rf-empty">
      <span className="uo-zone-ic" aria-hidden="true"><Icon n="sparkle" size={26} /></span>
      <b>No plan from the agent yet</b>
      <p className="muted">Upload purchase orders on the Connections page and run the agent. If demand doesn't fit capacity, the production manager decides how to go on first (shown above); the plan, and anything that needs attention, appear here once it is made.</p>
      {PAGES[role]?.includes("Connections") && <button className="sp-btn primary" onClick={() => setTab("Connections")}><Icon n="upload" size={14} /> Go to Connections</button>}
    </section>
  );
}

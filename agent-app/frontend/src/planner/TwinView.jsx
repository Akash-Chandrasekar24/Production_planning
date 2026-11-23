import { Suspense, lazy, useEffect, useMemo, useRef, useState } from "react";
import { api, auth } from "../api.js";
import { N } from "./ui.jsx";
import { Icon, K } from "./widgets.jsx";
import { FilterBar, Segmented, nice } from "./Filters.jsx";
import { Num, useTween } from "./motion.js";
import { KpiStrip } from "./PlanningViews.jsx";

// Digital twin. It opens on a satellite map of the plants; open a plant to see its site in 3D (or as line cards), and click any
// machine or vehicle for its details. Every figure is from the recorded hourly production data. There is no live feed yet, so the
// twin replays the record and says so; the replay controls sit in a floating window.

const Floor3D = lazy(() => import("./Floor3D.jsx"));      // three.js loads only when a plant is opened in 3D
const PlantMap = lazy(() => import("./PlantMap.jsx"));    // the map library loads only for the map
const STATE = { running: ["Running", "ok"], slowed: ["Slowed", "warn"], down: ["Down", "bad"], waiting: ["Waiting", "info"], idle: ["Idle", "info"], no_data: ["No data", "info"] };
const MICON = { SPI: "search", "Pick and Place": "grid", "Reflow Oven": "sun", AOI: "expand", "ICT Tester": "chart", "Assembly Cell": "gear",
  "RF Tester": "trend", "Camera Tester": "search", "Aging Chamber": "clock", "Leak Tester": "box" };
const SPEEDS = [{ value: 1200, label: "1×" }, { value: 500, label: "4×" }, { value: 220, label: "10×" }];
const HOUR = 36e5;
const iso = t => new Date(t).toISOString().slice(0, 19);
const fmtAt = s => new Date(s + "Z").toLocaleString("en", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "UTC" });
const fmtShort = s => new Date(s + "Z").toLocaleString("en", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "UTC" });
const fmtDay = s => (s ? new Date(String(s).slice(0, 10) + "T00:00:00Z").toLocaleDateString("en", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }) : "–");
const reason = r => (r ? nice(r) : "Reason not recorded");
const pct = v => (v == null ? "–" : `${v}%`);

export default function TwinView({ dispatch, planName, planLoading, setTab }) {
  const [snap, setSnap] = useState(null);
  const [sites, setSites] = useState(null);
  const head = auth.role === "plant_head" ? auth.plant : null;   // a production head opens straight into their own plant
  const [plant, setPlant] = useState(head);       // null = the map
  const [idx, setIdx] = useState(null);           // hours since the first record; null = latest
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1200);
  const [replay, setReplay] = useState(false);    // the floating replay window
  const [f, setF] = useState({});
  const [sel, setSel] = useState(null);           // {kind: "line"|"machine"|"vehicle", id, ...}
  const [busy, setBusy] = useState(false);
  const [view, setViewState] = useState(() => { try { return localStorage.getItem("twin.view") || "3d"; } catch { return "3d"; } });
  const setView = v => { setViewState(v); try { localStorage.setItem("twin.view", v); } catch { /* storage may be blocked */ } };
  const req = useRef(0);

  const start = snap ? Date.parse(snap.range[0] + "Z") : 0;
  const total = snap ? Math.round((Date.parse(snap.range[1] + "Z") - start) / HOUR) : 0;
  const cur = snap ? Math.round((Date.parse(snap.at + "Z") - start) / HOUR) : 0;

  useEffect(() => {                               // one request per hour shown; only the latest answer is used
    const n = ++req.current;
    setBusy(true);
    const t = setTimeout(() => api(`/api/twin${idx == null ? "" : `?at=${iso(start + idx * HOUR)}`}`)
      .then(j => n === req.current && setSnap(j)).catch(() => {}).finally(() => n === req.current && setBusy(false)), idx == null ? 0 : 90);
    return () => clearTimeout(t);
  }, [idx]);                                      // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {                               // the map's plant cards follow the hour shown
    if (plant || !snap) return;
    api(`/api/twin/sites?at=${snap.at}`).then(setSites).catch(() => {});
  }, [plant, snap?.at]);                          // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {                               // replay: advance one hour after each hour has arrived, so the view never runs ahead
    if (!playing || busy || !snap) return undefined;
    if (cur >= total) { setPlaying(false); return undefined; }
    const t = setTimeout(() => setIdx(cur + 1), speed);
    return () => clearTimeout(t);
  }, [playing, busy, snap, speed]);               // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {                               // Esc closes the details, then the replay window
    const k = e => { if (e.key === "Escape") { if (sel) setSel(null); else setReplay(false); } };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [sel]);

  const nextJobs = useMemo(() => {
    const by = {};
    (dispatch || []).slice().sort((a, b) => String(a.start).localeCompare(String(b.start))).forEach(r => { (by[r.line_id] ||= []).push(r); });
    return by;
  }, [dispatch]);

  if (!snap) return <div className="sp-card"><p className="muted" role="status">Loading the production floor…</p></div>;
  const open = id => { setSel(null); setPlant(id); };
  const site = id => (sites || []).find(s => s.plant_id === id);
  const plantLines = snap.lines.filter(l => !plant || l.plant_id === plant);
  const shown = plantLines.filter(l => !f.state || l.state === f.state);
  const P = plant && snap.plants.find(p => p.plant_id === plant);
  const lineOf = id => snap.lines.find(l => l.line_id === id);
  const F = snap.floor;
  const kpis = P ? [
    { label: `OEE this hour · ${plant}`, num: P.oee, fmt: pct, tone: P.oee == null ? "" : P.oee >= 75 ? "ok" : P.oee >= 60 ? "warn" : "bad" },
    { label: "Good units this hour", num: P.hour.good, note: `target ${N(P.hour.target)}` },
    { label: "Lines running", num: plantLines.filter(l => ["running", "slowed"].includes(l.state)).length, fmt: v => `${v} of ${plantLines.length}`,
      tone: plantLines.some(l => !["running", "slowed"].includes(l.state)) ? "warn" : "ok" },
    { label: "Downtime this hour", num: plantLines.reduce((t, l) => t + (l.hour?.downtime_min || 0), 0), fmt: v => `${N(v)} min` },
    { label: "Good units today", num: plantLines.reduce((t, l) => t + l.today.good, 0), fmt: K },
  ] : [
    { label: "Floor OEE this hour", num: F.oee, fmt: pct, tone: F.oee == null ? "" : F.oee >= 75 ? "ok" : F.oee >= 60 ? "warn" : "bad" },
    { label: "Good units this hour", num: F.good, note: `target ${N(F.target)}` },
    { label: "Lines running", num: F.running, fmt: v => `${v} of ${F.lines}`, tone: F.running < F.lines ? "warn" : "ok" },
    { label: "Downtime this hour", num: F.downtime_min, fmt: v => `${N(v)} min`, tone: F.downtime_min ? "warn" : "ok" },
    { label: "Good units today", num: F.today_good, fmt: K },
  ];
  const other = snap.plants.find(p => p.plant_id !== plant);
  const name = id => { const s = site(id); return s?.city ? `${s.city} · ${id}` : id; };

  return (
    <div className="page sp-enter tw-page">
      <div className="sp-head">
        <div><h1>Digital twin</h1><small className="muted">{plant ? "The plant site: click any machine, forklift, AGV or lorry for its details" : "Your plants on the map: open one to walk its production floor"}</small></div>
        <div className="sp-head-r">
          <button className={"tw-replay-btn" + (replay ? " on" : "")} aria-expanded={replay} aria-controls="tw-replay" onClick={() => setReplay(r => !r)}
            title="Replay controls">
            <span className={"tw-replay-ic" + (playing ? " playing" : "")}><Icon n={playing ? "pause" : "play"} size={14} /></span>
            <span><small>{playing ? "Replaying" : busy ? "Updating…" : "Showing"}</small><b>{fmtAt(snap.at)}</b></span>
            {snap.lines[0]?.hour && <span className="sp-pill t-info">Shift {snap.lines[0].hour.shift}</span>}
            <Icon n="chevron" size={14} />
          </button>
        </div>
      </div>
      {replay && (
        <section className="tw-float tw-replay" id="tw-replay" role="dialog" aria-label="Replay controls">
          <header className="sp-row"><b>Replay recorded production</b><button className="sp-icon-btn" aria-label="Close replay controls" onClick={() => setReplay(false)}><Icon n="close" size={14} /></button></header>
          <small className="muted">Hour-by-hour records from {fmtShort(snap.range[0])} to {fmtShort(snap.range[1])}. A live view needs a live feed from the shop floor; connect one in <button className="lnk" onClick={() => setTab("Connections")}>Integrations</button>.</small>
          <div className="tw-ctl-row">
            <div className="tw-btns">
              <button className="sp-icon-btn" aria-label="Back one hour" title="Back one hour" disabled={cur <= 0} onClick={() => { setPlaying(false); setIdx(Math.max(0, cur - 1)); }}><Icon n="left" size={16} /></button>
              <button className="sp-btn primary tw-play" aria-label={playing ? "Pause replay" : "Play replay"} onClick={() => { if (!playing && cur >= total) setIdx(Math.max(0, total - 24)); setPlaying(p => !p); }}>
                <Icon n={playing ? "pause" : "play"} size={16} /> {playing ? "Pause" : "Play"}</button>
              <button className="sp-icon-btn" aria-label="Forward one hour" title="Forward one hour" disabled={cur >= total} onClick={() => { setPlaying(false); setIdx(Math.min(total, cur + 1)); }}><Icon n="right" size={16} /></button>
              <button className="sp-btn" disabled={cur >= total} onClick={() => { setPlaying(false); setIdx(null); }}><Icon n="skip" size={14} /> Latest</button>
            </div>
            <Segmented label="Replay speed" options={SPEEDS} value={speed} onChange={setSpeed} />
          </div>
          <div className="tw-now" aria-live="polite"><small className="muted">Showing</small><b>{fmtAt(snap.at)}</b></div>
          <input type="range" className="tw-slider" min={0} max={total} value={cur} aria-label="Hour to show" aria-valuetext={fmtAt(snap.at)}
            onChange={e => { setPlaying(false); setIdx(+e.target.value); }} style={{ "--p": `${(100 * cur) / (total || 1)}%` }} />
          <div className="tw-axis"><span>{fmtShort(snap.range[0])}</span><span>{fmtShort(snap.range[1])}</span></div>
        </section>
      )}

      <KpiStrip items={kpis} />
      {snap.records?.generated_lines?.length > 0 && <Gen>Hosur has 3 bays and Bengaluru 5. In each plant bays 1–2 are recorded and the rest are generated for the twin (planning uses the recorded lines only). The figures above include every bay.</Gen>}

      {!plant ? (
        <div className="tw-grid">
          <div className="tw-floor">
            <section className="sp-card tw-mapcard">
              <header className="sp-ch"><div><h3>Plants</h3>
                <small className="sp-sub-t">Satellite view · plant positions are approximate (city level) until the exact sites are set · click a plant to open it</small></div></header>
              <Suspense fallback={<div className="pm-map"><div className="sp-skel" role="status" aria-label="Loading the map"><i className="sp-skel-chart" /></div></div>}>
                <PlantMap sites={sites} onOpen={open} />
              </Suspense>
              <div className="pm-list">{(sites || []).map(s => (
                <button key={s.plant_id} className={"pm-row t-" + (s.down ? "bad" : s.running < s.lines ? "warn" : "ok")} onClick={() => open(s.plant_id)}>
                  <span className="tw-led" /><b>{s.city}</b><small className="muted">{s.plant_id} · {s.region}</small>
                  <span>{s.running} of {s.lines} lines running · OEE {pct(s.oee)}</span><span className="lnk">Open ›</span>
                </button>))}</div>
            </section>
          </div>
          <Aside snap={snap} plant={null} setSel={l => { open(lineOf(l.id).plant_id); setSel(l); }} nextJobs={nextJobs} planLoading={planLoading} planName={planName} dispatch={dispatch} />
        </div>
      ) : (
        <>
          <div className="tw-crumbs">
            {!head && <button className="sp-btn" onClick={() => { setPlant(null); setSel(null); }}><Icon n="left" size={14} /> All plants</button>}
            <span className="tw-crumb"><Icon n="factory" size={16} /><b>{name(plant)}</b>{site(plant)?.region && <small className="muted">{site(plant).region}</small>}</span>
            {other && <button className="sp-btn" onClick={() => open(other.plant_id)}>Go to {name(other.plant_id)} <Icon n="right" size={14} /></button>}
            <span className="tw-crumb-r"><Segmented label="Floor view" options={[{ value: "3d", label: "3D site" }, { value: "cards", label: "Line cards" }]} value={view} onChange={setView} /></span>
          </div>
          <FilterBar value={f} onChange={setF} busy={busy} fields={[
            { key: "state", label: "Lines", all: "All states", options: Object.entries(STATE).map(([k, [label]]) => ({ value: k, label })) },
          ]} />
          <div className="tw-grid is-3d">
            <div className="tw-floor">
              <FlowStrip flow={snap.flow} dispatch={snap.dispatch} plant={plant} />
              <div>
                {view === "3d" ? (
                  <>
                    <div className="tw-stage">
                      <Suspense fallback={<div className="tw3 tw3-loading"><div className="sp-skel" role="status" aria-label="Loading the 3D site"><i className="sp-skel-chart" /></div></div>}>
                        <Floor3D key={plant} snap={snap} plant={plant} sel={sel} onSelect={setSel} />
                      </Suspense>
                      {sel && <Details sel={sel} snap={snap} plant={plant} onClose={() => setSel(null)} onPick={setSel} />}
                    </div>
                    <div className="tw3-strip" role="list" aria-label="Lines">
                      {shown.map(l => (
                        <button key={l.line_id} role="listitem" className={`tw3-chip t-${STATE[l.state][1]}` + (sel?.kind === "line" && sel.id === l.line_id ? " on" : "")} onClick={() => setSel({ kind: "line", id: l.line_id })}>
                          <span className="tw-led" /><b>{l.line_id}</b><span>{STATE[l.state][0]}</span>
                          <small className="muted">{l.hour ? `${N(l.hour.good)} / ${N(l.hour.target)} good · OEE ${pct(l.hour_oee.oee)}` : "No record"}</small>
                        </button>))}
                    </div>
                    <div className="tw3-vehicles" role="list" aria-label="Vehicles">
                      <small className="muted">Vehicles</small>
                      {vehiclesOf(plant, plantLines).map(v => (
                        <button key={v.id} role="listitem" className={"tw3-vchip" + (sel?.kind === "vehicle" && sel.id === v.id ? " on" : "")} onClick={() => setSel({ kind: "vehicle", ...v })}>
                          <Icon n={v.type === "lorry" ? "truck" : "box"} size={14} /> {v.name}</button>))}
                    </div>
                    <div className="tw3-vehicles" role="list" aria-label="Places">
                      <small className="muted">Places</small>
                      {PLACES.map(([label, info, ic]) => {
                        const on = sel && sel.kind === info.kind && (sel.wh || sel.area) === (info.wh || info.area);
                        return <button key={label} role="listitem" className={"tw3-vchip" + (on ? " on" : "")} onClick={() => setSel({ ...info, plant })}><Icon n={ic} size={14} /> {label}</button>;
                      })}
                    </div>
                  </>
                ) : (
                  <div className="tw-stage">
                  <section className="sp-card tw-hall" aria-label={`Plant ${plant}`}>
                    <header className="tw-hall-h">
                      <span className="sp-wh-ic"><Icon n="factory" size={16} /></span>
                      <div><h3>{name(plant)}</h3><small className="sp-sub-t">{P.lines.join(" → ")} · material flows from SMT into final assembly</small></div>
                      <div className="tw-hall-k"><small className="muted">OEE</small><Num value={P.oee} fmt={pct} /><small className="muted">{N(P.hour.good)} / {N(P.hour.target)} good</small></div>
                    </header>
                    {!shown.length && <p className="muted">No lines match these filters.</p>}
                    {shown.map((l, i) => (
                      <div key={l.line_id}>
                        {i > 0 && <div className="tw-feed" aria-hidden="true"><Icon n="chevron" size={16} /> feeds</div>}
                        <Line l={l} sel={sel} onSelect={setSel} />
                      </div>))}
                  </section>
                  {sel && <Details sel={sel} snap={snap} plant={plant} onClose={() => setSel(null)} onPick={setSel} />}
                  </div>
                )}
              </div>
            </div>
            <Aside snap={snap} plant={plant} setSel={setSel} nextJobs={nextJobs} planLoading={planLoading} planName={planName} dispatch={dispatch} />
          </div>
        </>
      )}
    </div>
  );
}

const PLACES = [["Raw materials", { kind: "warehouse", wh: "RM-WH-01" }, "box"], ["SMT kits", { kind: "warehouse", wh: "SMT-KIT-01" }, "box"],
  ["Work in progress", { kind: "warehouse", wh: "WIP-WH-01" }, "box"], ["Finished goods", { kind: "warehouse", wh: "FG-WH-01" }, "box"],
  ["Receiving dock", { kind: "area", area: "receiving" }, "truck"], ["Shipping dock", { kind: "area", area: "shipping" }, "send"],
  ["Packing", { kind: "area", area: "packing" }, "cart"], ["Gate", { kind: "area", area: "gate" }, "lock"], ["Office", { kind: "area", area: "office" }, "factory"]];

// the same vehicles the 3D site draws (ids match), so they can be opened without pointing at them
function vehiclesOf(plant, lines) {
  const smt = lines.find(l => l.kind === "SMT"), fal = lines.find(l => l.kind === "FAL");
  return [{ id: `${plant}-FL-1`, type: "forklift", role: "receiving", plant, name: "Forklift 1 · receiving" },
    { id: `${plant}-FL-2`, type: "forklift", role: "shipping", plant, name: "Forklift 2 · shipping" },
    ...(smt ? [{ id: `${plant}-AGV-1`, type: "agv", plant, line: smt.line_id, name: "AGV 1 · kits to SMT" }] : []),
    ...(fal ? [{ id: `${plant}-AGV-2`, type: "agv", plant, line: fal.line_id, name: "AGV 2 · products to packing" }] : []),
    { id: `${plant}-LORRY-IN`, type: "lorry", role: "in", plant, name: "Inbound lorry" },
    { id: `${plant}-LORRY-OUT`, type: "lorry", role: "out", plant, name: "Dispatch lorry" }];
}

function Aside({ snap, plant, setSel, nextJobs, planLoading, planName, dispatch }) {
  const evs = snap.events.filter(e => !plant || e.plant_id === plant);
  return (
    <aside className="tw-side">
      <section className="sp-card">
        <header className="sp-ch"><div><h3>Stoppages</h3><small className="sp-sub-t">15 minutes or more, last 24 hours{plant ? ` · ${plant}` : " · both plants"}</small></div></header>
        {!evs.length ? <p className="muted">No stoppages in the last 24 hours.</p> : (
          <ol className="sp-timeline">{evs.map((e, i) => (
            <li key={i} className={"t-" + (e.minutes >= 45 ? "bad" : "warn")}><span className="sp-bullet" /><div>
              <b><button className="lnk" onClick={() => setSel({ kind: "line", id: e.line_id })}>{e.line_id}</button> · {e.minutes} min</b>
              <small className="muted">{reason(e.reason)} · {fmtShort(e.ts)} · shift {e.shift}</small></div></li>))}
          </ol>)}
      </section>
      <section className="sp-card">
        <header className="sp-ch"><div><h3>Next in the plan</h3><small className="sp-sub-t">{planLoading ? "Loading the selected plan…" : planName ? `From “${planName}”` : "No plan selected"}</small></div></header>
        {planLoading ? <div className="sp-skel" role="status" aria-label="Loading"><i /><i style={{ width: "70%" }} /><i style={{ width: "50%" }} /></div>
          : !dispatch?.length ? <p className="muted">{planName ? "The selected plan schedules no jobs on these lines." : "Pick or generate a plan to see what each line makes next."}</p> : (
          <ul className="tw-next">{snap.lines.filter(l => !plant || l.plant_id === plant).map(l => {
            const j = (nextJobs[l.line_id] || [])[0];
            return <li key={l.line_id}><b>{l.line_id}</b>{j ? <span>{j.customer_order_id} · {j.product_id} · {N(j.qty)} units<small className="muted"> from {fmtShort(String(j.start).slice(0, 19))}</small></span>
              : <span className="muted">Nothing scheduled</span>}</li>;
          })}</ul>)}
      </section>
    </aside>
  );
}

// the floating details window: anything on the site
function Details({ sel, snap, plant, onClose, onPick }) {
  const ref = useRef();
  useEffect(() => { ref.current?.focus(); }, [sel.id, sel.kind, sel.wh, sel.area, sel.line]);
  const at = snap.at;
  return (
    <section className="tw-float tw-details" role="dialog" aria-label="Details" tabIndex={-1} ref={ref}>
      <button className="sp-icon-btn tw-details-x" aria-label="Close details" onClick={onClose}><Icon n="close" size={14} /></button>
      {sel.kind === "machine" ? <MachineDetails id={sel.id} at={at} />
        : sel.kind === "vehicle" ? (sel.type === "lorry" ? <LorryDetails v={sel} snap={snap} plant={plant} /> : <VehicleDetails id={sel.id} at={at} />)
        : sel.kind === "warehouse" ? <WarehouseDetails plant={sel.plant || plant} wh={sel.wh} at={at} />
        : sel.kind === "area" ? <AreaDetails plant={sel.plant || plant} area={sel.area} at={at} onPick={onPick} />
        : sel.kind === "crew" ? <CrewDetails line={sel.line} at={at} />
        : <LineDetails l={snap.lines.find(x => x.line_id === sel.id)} onPick={onPick} />}
    </section>
  );
}

// fetch details for the hour shown; keeps the last answer while the next one loads
function useDetails(url) {
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => { let on = true; setErr(null); api(url).then(x => on && setD({ url, x })).catch(e => on && setErr(e.message)); return () => { on = false; }; }, [url]);
  return [d?.x, err, !d || d.url !== url];
}
const Loading = () => <div className="sp-skel" role="status" aria-label="Loading details"><i /><i style={{ width: "70%" }} /><i style={{ width: "85%" }} /><i style={{ width: "60%" }} /></div>;
const Gen = ({ children }) => <p className="tw-gen"><Icon n="info" size={12} /> {children}</p>;
const Head = ({ icon, title, sub, pill, tone = "info" }) => (
  <div className="tw-d-head"><span className="tw-m-ic"><Icon n={icon} size={18} /></span><div><h3>{title}</h3><small className="muted">{sub}</small></div>
    {pill && <span className={"sp-pill t-" + tone}>{pill}</span>}</div>);
const KV = ({ items }) => <div className="tw-d-kv">{items.filter(Boolean).map(([k, v, note, cls]) => <span key={k}><small>{k}</small><b className={cls || ""}>{v}</b>{note != null && <small className="muted">{note}</small>}</span>)}</div>;
const Bars = ({ rows, k, label }) => {
  const max = Math.max(1, ...rows.map(r => r[k]));
  return <div className="tw-d-bars" role="img" aria-label={label}>{rows.map(r => <span key={r.day} title={`${fmtDay(r.day)}: ${N(r[k])}`}><i style={{ height: `${(100 * r[k]) / max}%` }} /></span>)}</div>;
};
function History({ h }) {
  if (!h?.events?.length) return <small className="muted">No maintenance recorded yet.</small>;
  return (<>
    <KV items={[["Repairs, 30 days", N(h.repairs_30d)], ["Repairs in all", N(h.repairs_total)], h.mttr_min != null && ["Average repair", `${h.mttr_min} min`],
      ["Maintenance cost this year", `₹${K(h.cost_ytd_inr)}`]]} />
    <ol className="tw-d-hist">{h.events.map(e => (
      <li key={e.maintenance_id} className={"t-" + (e.kind === "REPAIR" ? "warn" : "ok")}><span className="sp-bullet" />
        <div><b>{e.kind === "REPAIR" ? "Repair" : "Planned service"} · {fmtShort(e.started_at)} · {e.duration_min} min</b>
          <small>{e.work_done}{e.parts_replaced && e.parts_replaced !== "-" ? ` · replaced ${e.parts_replaced}` : ""}</small>
          <small className="muted">{e.technician} · ₹{N(e.cost_inr)}</small></div></li>))}</ol>
  </>);
}

function MachineDetails({ id, at }) {
  const [m, err, loading] = useDetails(`/api/twin/machine/${encodeURIComponent(id)}?at=${at}`);
  if (err) return <p className="bad">{err}</p>;
  if (!m || (loading && m.machine_id !== id)) return <Loading />;
  const c = m.condition, s = m.supply, sv = m.service;
  const st = STATE[c.state] || STATE.no_data;
  return (<>
    <Head icon={MICON[m.type] || "gear"} title={m.name} sub={`${m.type} · ${m.machine_id} · ${m.line_id} · ${m.plant_id}`} pill={c.label} tone={c.tone} />
    {m.own_records && <Gen>{m.generated_line ? "Generated bay: all this machine's records are generated from the plant's recorded lines." : "Machine-level records are generated from its line's recorded hours."}</Gen>}

    <h4>Condition</h4>
    <ul className="tw-d-list">{c.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
    <KV items={[["Now", st[0], c.now_wait_reason ? nice(c.now_wait_reason).toLowerCase() : c.now_down_min ? `${c.now_down_min} min stopped` : null, st[1]],
      ["Own stops, 7 days", `${N(c.own_down_7d_min)} min`, `normal ${N(c.normal_down_7d_min)}`], ["Running, 7 days", pct(c.run_7d_pct), `waited ${N(c.wait_7d_min)} min`],
      c.normal_rejects_pct != null && ["Rejects found, 7 days", `${c.rejects_7d_pct}%`, `normal ${c.normal_rejects_pct}%`],
      c.temp_now_c != null && ["Temperature", `${c.temp_now_c} °C`, c.temp_24h ? `24 h: ${c.temp_24h[0]}–${c.temp_24h[1]} °C` : null],
      c.power_now_kw != null && ["Power this hour", `${c.power_now_kw} kWh`],
      c.mtbf_h != null && ["Between failures", `${N(c.mtbf_h)} h`, "running hours per repair"],
      ["Criticality", m.criticality === "A" ? "A · line stops without it" : "B · supporting"], ["In service since", fmtDay(m.commissioned), `${Math.round(m.age_days / 30.4)} months`]]} />

    <h4>Supply now</h4>
    {!s ? <p className="muted">Nothing on its line this hour.</p> : (<>
      <KV items={[["Making", s.product_id, s.production_order_id], ["Rate · this hour", `${N(s.made_this_hour)} / ${N(s.rate_per_hour)}`, "units made / target"],
        ["Crew", s.team, `shift ${s.shift}`], s.upstream && [`Fed by ${s.upstream.line_id}`, `${N(s.upstream.boards)} boards`, (STATE[s.upstream.state] || STATE.no_data)[0]]]} />
      <div className="sp-table tw-d-table"><table>
        <thead><tr><th>Part</th><th className="num" title="Needed per unit">Per unit</th><th className="num" title="Needed per hour at the target rate">Per hour</th><th className="num" title="Usable stock">Stock</th><th className="num" title="Days of production the stock covers">Cover (d)</th></tr></thead>
        <tbody>{s.parts.map(p => (
          <tr key={p.material_id}><td>{p.material_id}</td><td className="num">{p.per_unit}</td><td className="num">{N(p.need_per_hour)}</td><td className="num">{p.free == null ? "–" : N(p.free)}</td>
            <td className={"num " + (p.cover_days != null && p.cover_days < 1 ? "bad" : p.cover_days != null && p.cover_days < 3 ? "warn" : "")}>{p.cover_days ?? "–"}</td></tr>))}</tbody>
      </table></div>
    </>)}

    <h4>Units processed</h4>
    <div className="tw-d-out">{m.output.map(o => (
      <span key={o.label}><small>{o.label}</small><b>{K(o.units)}</b><small className="muted">{o.days > 1 ? `${N(o.per_day)} a day · ` : ""}running {pct(o.run_pct)}{o.energy_kwh != null ? ` · ${K(o.energy_kwh)} kWh` : ""}</small></span>))}</div>
    <Bars rows={m.daily} k="units" label="Units per day, last 30 days" />
    <small className="muted">Last 30 days. Records start {fmtDay(m.records_from)}.</small>

    <h4>Service and repairs</h4>
    <KV items={[["Last service", fmtDay(sv.last)], ["Next service", fmtDay(sv.next), sv.days_to_next < 0 ? "overdue" : sv.days_to_next === 0 ? "due today" : `in ${sv.days_to_next} days`, sv.days_to_next <= 2 ? "warn" : ""],
      ["Interval", `every ${sv.interval_days} days`, sv.basis === "maintenance records" ? "from its maintenance records" : "from its commissioning date"]]} />
    <History h={m.maintenance} />
    {m.last_repair && <p className="tw-d-note">Fix recorded by {m.last_repair.by} on {fmtDay(m.last_repair.date)}: “{m.last_repair.fix}”</p>}
    {m.open_problems?.length > 0 && <p className="tw-d-note bad">Open problem: {m.open_problems[0].description}</p>}
  </>);
}

function VehicleDetails({ id, at }) {
  const [v, err, loading] = useDetails(`/api/twin/vehicle/${encodeURIComponent(id)}?at=${at}`);
  if (err) return <p className="bad">{err}</p>;
  if (!v || (loading && v.vehicle_id !== id)) return <Loading />;
  const c = v.condition, n = v.now, unit = v.load_unit;
  return (<>
    <Head icon={v.type === "agv" ? "box" : "truck"} title={v.name} sub={`${v.model} · ${v.vehicle_id}`} pill={c.label} tone={c.tone} />
    <Gen>Vehicle records are generated from the plant's production and stock movements.</Gen>
    <h4>Now</h4>
    {n ? <KV items={[["State", nice(n.state)], ["Battery", `${n.battery_pct}%`, `${v.battery_kwh} kWh pack`, n.battery_pct < 35 ? "warn" : ""],
      ["This hour", `${N(n.trips)} trips`, `${N(n.loads)} ${unit} · ${n.km} km`], v.line_id && ["Serves", v.line_id]]} /> : <p className="muted">No record this hour.</p>}
    <h4>Condition</h4>
    <ul className="tw-d-list">{c.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
    <KV items={[["Faults, 30 days", `${N(c.faults_30d_min)} min`], ["Working, 30 days", `${c.run_30d_h} h`], ["Charges, 30 days", N(c.charges_30d)]]} />
    <h4>{unit === "units" ? "Units moved" : "Pallets moved"}</h4>
    <div className="tw-d-out">{v.output.map(o => (
      <span key={o.label}><small>{o.label}</small><b>{K(o.loads)}</b><small className="muted">{o.days > 1 ? `${N(o.per_day)} a day · ` : ""}{N(o.trips)} trips · {K(o.km)} km</small></span>))}</div>
    <h4>Service and repairs</h4>
    <KV items={[["Last service", fmtDay(v.service.last)], ["Next service", fmtDay(v.service.next), v.service.days_to_next < 0 ? "overdue" : `in ${v.service.days_to_next} days`, v.service.days_to_next <= 3 ? "warn" : ""],
      ["Interval", `every ${v.service.interval_days} days`]]} />
    <History h={v.maintenance} />
  </>);
}

function LorryDetails({ v, snap, plant }) {
  const flow = (snap.flow || []).find(x => x.plant_id === plant) || {};
  const ds = snap.dispatch || {};
  return (<>
    <Head icon="truck" title={v.name} sub={`${v.id} · ${plant}`} pill="Lorry" />
    <h4>Now</h4>
    <p>{v.role === "in" ? (flow.inbound ? `Bringing materials: ${N(flow.inbound)} units in transit to this plant` : "Parked: nothing in transit")
      : (ds.orders ? `${ds.orders} orders due today across the company · ${N(ds.shipped)} of ${N(ds.units)} units shipped` : "Parked: no orders due today")}</p>
    <p className="tw-d-note">Lorries belong to carriers, so the plant keeps no records of them; what they carry comes from the stock in transit and the orders due.</p>
  </>);
}

function WarehouseDetails({ plant, wh, at }) {
  const [w, err, loading] = useDetails(`/api/twin/warehouse/${plant}/${wh}?at=${at}`);
  const [showLots, setShowLots] = useState(false);
  if (err) return <p className="bad">{err}</p>;
  if (!w || (loading && w.warehouse_id !== wh)) return <Loading />;
  const T = w.totals;
  return (<>
    <Head icon="box" title={w.name} sub={`${w.warehouse_id} · ${plant} · stock count of ${fmtDay(w.day)}`} pill={`${N(T.materials)} materials`} />
    <KV items={[["On hand", K(T.on_hand), `${N(T.lots)} lots`], ["Usable", K(T.usable), "on hand − allocated − blocked − on hold"], ["Allocated", K(T.allocated)],
      ["Blocked · on hold", `${K(T.blocked)} · ${K(T.quality_hold)}`, `${N(T.on_hold_lots)} lots not available`, T.on_hold_lots ? "warn" : ""],
      ["In transit", K(T.in_transit)], ["Value", `₹${K(T.value_inr)}`]]} />
    {w.storage && <KV items={[["Store", w.storage.store, w.storage.racking], ...(w.storage.bulk_store ? [["Also held in", "Bulk floor store", w.storage.bulk_store]] : [])]} />}
    <Bars rows={w.trend} k="on_hand" label="Stock on hand, last 30 counts" />
    <small className="muted">Stock on hand at each daily count (last 30).</small>
    <div className="sp-table tw-d-table"><table>
      <thead><tr><th>Material</th><th className="num">On hand</th><th className="num">Usable</th><th className="num">Safety</th><th className="num" title="Days of production the plant's stock covers">Cover (d)</th></tr></thead>
      <tbody>{w.materials.map(m => (
        <tr key={m.material_id}><td>{m.material_id}{m.expiry ? <><br /><small className="muted">expires {fmtDay(m.expiry)}</small></> : null}</td><td className="num">{N(m.on_hand)}</td>
          <td className={"num " + (m.usable < m.safety ? "warn" : "")}>{N(m.usable)}</td><td className="num">{N(m.safety)}</td>
          <td className={"num " + (m.cover_days != null && m.cover_days < 1 ? "bad" : m.cover_days != null && m.cover_days < 3 ? "warn" : "")}>{m.cover_days ?? "–"}</td></tr>))}</tbody>
    </table></div>
    <button className="lnk" onClick={() => setShowLots(s => !s)}>{showLots ? "Hide lots" : `Show the ${N(w.lots.length)} lots`}</button>
    {showLots && <div className="sp-table tw-d-table"><table>
      <thead><tr><th>Lot</th><th>Status</th><th className="num">On hand</th><th className="num">Hold</th></tr></thead>
      <tbody>{w.lots.map(l => <tr key={l.lot_id}><td>{l.lot_id}<br /><small className="muted">{l.material_id}</small></td><td>{nice(l.inventory_status)}</td>
        <td className="num">{N(l.on_hand_qty)}</td><td className="num">{N(l.blocked_qty + l.quality_hold_qty)}</td></tr>)}</tbody>
    </table></div>}
    <small className="muted">Cover uses the plant's daily usage from the production records.</small>
  </>);
}

function AreaDetails({ plant, area, at, onPick }) {
  const [a, err, loading] = useDetails(`/api/twin/area/${plant}/${area}?at=${at}`);
  if (err) return <p className="bad">{err}</p>;
  if (!a || (loading && a.kind !== area)) return <Loading />;
  const ds = a.dispatch;
  const icon = { receiving: "truck", shipping: "send", packing: "cart", gate: "lock", office: "factory" }[area] || "factory";
  return (<>
    <Head icon={icon} title={a.title} sub={plant} />
    {area === "receiving" && <>
      <KV items={[["In transit to the plant", K(a.in_transit), "units"], ["Received today", a.pallets_received_today == null ? "–" : `${N(a.pallets_received_today)} pallets`, "by forklift 1"],
        ["Stores on hand", K(a.stores_on_hand), "raw materials + SMT kits"], ["Lots on hold", N(a.on_hold_lots), null, a.on_hold_lots ? "warn" : ""]]} />
      <h4>On the way, by material</h4>
      <ul className="tw-d-machines">{a.in_transit_by_material.map(m => <li key={m.material_id}><span>{m.material_id}</span><small className="muted">{N(m.qty)}</small></li>)}</ul>
      <button className="lnk" onClick={() => onPick({ kind: "warehouse", wh: "RM-WH-01", plant })}>Open raw materials ›</button>
    </>}
    {(area === "shipping" || area === "gate") && <>
      <KV items={[["Orders due today", N(ds.orders), "whole company"], ["Units shipped", `${N(ds.shipped)} / ${N(ds.units)}`], ["Delivered", N(ds.delivered)],
        ["Finished goods here", K(a.fg_on_hand)], ["Shipped today", a.pallets_shipped_today == null ? "–" : `${N(a.pallets_shipped_today)} pallets`, "by forklift 2"],
        area === "gate" && ["Received today", a.pallets_received_today == null ? "–" : `${N(a.pallets_received_today)} pallets`],
        area === "gate" && ["Inbound in transit", K(a.in_transit), "units"]]} />
      {area === "gate" && <p className="tw-d-note">Visitors and lorry gate times aren't recorded; movements come from the stock and dispatch records.</p>}
      <button className="lnk" onClick={() => onPick({ kind: "warehouse", wh: "FG-WH-01", plant })}>Open finished goods ›</button>
    </>}
    {area === "packing" && <>
      <KV items={[["Packed this hour", N(a.packed_this_hour), "phones off final assembly"], ["Packed today", K(a.packed_today)], ["Finished goods on hand", K(a.fg_on_hand)]]} />
      <ul className="tw-d-machines">{a.lines.map(l => <li key={l.line_id}><button className="lnk" onClick={() => onPick({ kind: "line", id: l.line_id })}>{l.line_id}{l.generated ? " (generated)" : ""}</button>
        <small className="muted">{N(l.good_hour)} this hour · {K(l.good_today)} today</small></li>)}</ul>
    </>}
    {area === "office" && <>
      <KV items={[["Good units today", K(a.good_today)], ["Stock value", `₹${K(a.stock_value_inr)}`], ["Open problems", N(a.open_problems), null, a.open_problems ? "bad" : ""],
        ["Open alerts", N(a.open_alerts), null, a.open_alerts ? "warn" : ""]]} />
      <h4>Lines and crews on shift</h4>
      <ul className="tw-d-machines">{a.lines.map(l => (
        <li key={l.line_id}><button className="lnk" onClick={() => onPick({ kind: "line", id: l.line_id })}>{l.line_id}{l.generated ? " (generated)" : ""}</button>
          <small className={(STATE[l.state] || STATE.no_data)[1]}>{(STATE[l.state] || STATE.no_data)[0]}{l.team ? ` · ${l.team}, shift ${l.shift}` : ""} · {K(l.good_today)} today</small></li>))}</ul>
    </>}
  </>);
}

function CrewDetails({ line, at }) {
  const [c, err, loading] = useDetails(`/api/twin/crew/${encodeURIComponent(line)}?at=${at}`);
  if (err) return <p className="muted">{err}</p>;
  if (!c || (loading && c.line_id !== line)) return <Loading />;
  return (<>
    <Head icon="users" title={`Crew ${c.team}`} sub={`${c.line_id} · shift ${c.shift} · ${c.plant_id}`} />
    {c.generated && <Gen>Generated bay: this crew's hours are generated.</Gen>}
    <KV items={[["Hours on this line", N(c.hours_on_line)], ["Good units an hour", N(c.good_per_hour)],
      ["Of target", pct(c.team_pct_of_target), `line average ${pct(c.line_pct_of_target)}`, c.team_pct_of_target < c.line_pct_of_target - 3 ? "warn" : ""],
      ["Rejects", `${c.reject_pct}%`], ["Stops on its hours", `${N(c.stops_min)} min`]]} />
    <small className="muted">Names and headcount aren't recorded; crews are known by their team code.</small>
  </>);
}

function LineDetails({ l, onPick }) {
  if (!l) return null;
  return (<>
    <Head icon="factory" title={`Line ${l.line_id}`} sub={`${l.kind === "SMT" ? "Surface-mount" : "Final assembly"} line · plant ${l.plant_id}`} pill={(STATE[l.state] || STATE.no_data)[0]} tone={(STATE[l.state] || STATE.no_data)[1]} />
    {l.generated && <Gen>Generated bay for the twin: its records are generated; planning uses the recorded lines only.</Gen>}
    <KV items={[l.hour && ["Making", l.hour.product_id, l.hour.production_order_id], l.hour && ["This hour", `${N(l.hour.good)} / ${N(l.hour.target)}`, "good / target"],
      l.hour && ["Shift · crew", `${l.hour.shift} · ${l.hour.team}`], l.hour && ["Rejects", N(l.hour.reject)], ["OEE today", pct(l.today.oee)], ["Good today", `${N(l.today.good)} / ${N(l.today.target)}`]]} />
    <h4>Machines</h4>
    <ul className="tw-d-machines">{l.machines.map(m => (
      <li key={m.machine_id}><button className="lnk" onClick={() => onPick({ kind: "machine", id: m.machine_id, line: l.line_id, type: m.type })}>{m.type} · {m.machine_id}</button>
        <small className={(STATE[m.state] || STATE.no_data)[1]}>{(STATE[m.state] || STATE.no_data)[0]} · {m.maintenance_due_days === 0 ? "service due today" : `service in ${m.maintenance_due_days} d`}</small></li>))}</ul>
    {l.hour && <button className="lnk" onClick={() => onPick({ kind: "crew", line: l.line_id })}>Crew on shift ›</button>}
  </>);
}

function Line({ l, sel, onSelect }) {
  const [label, tone] = STATE[l.state];
  const h = l.hour, o = l.hour_oee;
  return (
    <div className={`tw-line t-${tone} s-${l.state}` + (sel?.kind === "line" && sel.id === l.line_id ? " on" : "")}>
      <button className="tw-line-h" onClick={() => onSelect({ kind: "line", id: l.line_id })} aria-label={`Line ${l.line_id}: ${label}`}>
        <span className="tw-led" /><b>{l.line_id}</b><span className={"sp-pill t-" + tone}>{label}</span>
        {h && <small className="muted">{h.product_id} · {h.production_order_id} · shift {h.shift} · {h.team}</small>}
      </button>
      <div className="tw-conveyor" style={{ "--belt": l.state === "running" ? "1.1s" : "2.6s" }}>
        <i className="tw-belt" aria-hidden="true" />
        {l.machines.map(m => (
          <button key={m.machine_id} className={"tw-m" + (sel?.kind === "machine" && sel.id === m.machine_id ? " on" : "")}
            onClick={() => onSelect({ kind: "machine", id: m.machine_id })} aria-label={`${m.name}, ${m.type}: ${label}`}>
            <span className="tw-led" />
            <span className="tw-m-ic"><Icon n={MICON[m.type] || "gear"} size={18} /></span>
            <b>{m.type}</b><small>{m.name}</small>
            {m.maintenance_due_days <= 3 && <em className="tw-due">{m.maintenance_due_days === 0 ? "Maintenance today" : `Maintenance in ${m.maintenance_due_days} d`}</em>}
          </button>
        ))}
      </div>
      <div className="tw-metrics">
        <Ring value={o.oee} />
        <div className="tw-apq">
          {[["Availability", o.availability], ["Performance", o.performance], ["Quality", o.quality]].map(([k, v]) => (
            <div key={k} className="tw-apq-r"><small>{k}</small><span><i style={{ width: `${v ?? 0}%` }} /></span><b>{pct(v)}</b></div>
          ))}
        </div>
        <div className="tw-hour">
          <small className="muted">This hour</small>
          {h ? <><div><Num value={h.good} fmt={N} /><small className="muted"> / {N(h.target)} good</small></div>
            <small className={h.downtime_min ? (h.downtime_min >= 45 ? "bad" : "warn") : "muted"}>{h.downtime_min ? `${h.downtime_min} min down · ${reason(h.downtime_reason)}` : "No stops"}</small></>
            : <small className="muted">No record for this hour</small>}
          <Spark trend={l.trend} />
        </div>
      </div>
    </div>
  );
}

// the journey this hour: inventory -> line -> product -> logistics -> truck -> out of the plant
function FlowStrip({ flow, dispatch, plant }) {
  if (!flow) return null;
  const rows = flow.filter(x => !plant || x.plant_id === plant);
  const sum = k => rows.reduce((t, r) => t + (r[k] || 0), 0);
  const moving = sum("assembled") > 0;
  const steps = [
    ["truck", "Inbound", sum("inbound"), "units in transit to the plant"],
    ["box", "Inventory", sum("stores"), "raw materials and SMT kits on hand"],
    ["layers", "Issued to line", sum("issued"), "parts consumed this hour"],
    ["grid", "SMT", sum("boards"), "boards built this hour"],
    ["factory", "Assembly", sum("assembled"), "products finished this hour"],
    ["cart", "Logistics", sum("fg"), "finished goods packed and staged"],
    ["send", "Dispatch", dispatch?.orders || 0, `orders due today · ${N(dispatch?.shipped || 0)} of ${N(dispatch?.units || 0)} units shipped`],
  ];
  return (
    <section className="sp-card tw-flow" aria-label="Material flow this hour">
      <header className="sp-ch"><div><h3>From inventory to the truck</h3>
        <small className="sp-sub-t">{plant ? `Plant ${plant}` : "Both plants"} · this hour · dispatch is for the whole company (orders carry no plant)</small></div></header>
      <ol className={"tw-flow-steps" + (moving ? " moving" : "")}>
        {steps.map(([ic, t, v, note], i) => (
          <li key={t}>
            <span className="tw-flow-ic"><Icon n={ic} size={16} /></span>
            <small className="muted">{i + 1} · {t}</small>
            <Num value={v} fmt={K} />
            <small className="muted tw-flow-note">{note}</small>
            {i < steps.length - 1 && <i className="tw-flow-arrow" aria-hidden="true" />}
          </li>
        ))}
      </ol>
    </section>
  );
}

function Ring({ value }) {
  const v = useTween(value ?? 0), R = 26, C = 2 * Math.PI * R;
  const tone = value == null ? "var(--text-muted)" : value >= 75 ? "var(--chart-c4)" : value >= 60 ? "var(--brand-orange)" : "var(--depth-0)";
  return (
    <div className="tw-ring" role="img" aria-label={`OEE ${value ?? "not available"}${value == null ? "" : " percent"}`}>
      <svg viewBox="0 0 64 64" width="64" height="64">
        <circle cx="32" cy="32" r={R} fill="none" strokeWidth="6" style={{ stroke: "var(--muted-fill)" }} />
        <circle cx="32" cy="32" r={R} fill="none" strokeWidth="6" strokeLinecap="round" transform="rotate(-90 32 32)"
          strokeDasharray={`${(Math.max(0, Math.min(100, v)) / 100) * C} ${C}`} style={{ stroke: tone }} />
      </svg>
      <div><b>{value == null ? "–" : Math.round(v)}%</b><small>OEE</small></div>
    </div>
  );
}

// last hours: good units as bars against the target (dash); a stopped hour shows as a short red bar
function Spark({ trend }) {
  if (!trend?.length) return null;
  const max = Math.max(...trend.map(t => Math.max(t.good, t.target)), 1);
  return (
    <div className="tw-spark" role="img" aria-label={`Good units over the last ${trend.length} hours`}>
      {trend.map(t => (
        <span key={t.ts} title={`${fmtShort(t.ts)}: ${N(t.good)} good of ${N(t.target)}${t.downtime_min ? ` · ${t.downtime_min} min down` : ""}`}>
          <i className={t.downtime_min >= 45 ? "bad" : t.downtime_min ? "warn" : ""} style={{ height: `${(100 * t.good) / max}%` }} />
        </span>
      ))}
    </div>
  );
}

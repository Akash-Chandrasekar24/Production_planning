import { useEffect, useState } from "react";
import { api } from "../api.js";
import { N } from "./ui.jsx";
import { Icon, K } from "./widgets.jsx";
import { Logo, NeonDialog } from "./IntegrationsView.jsx";
import { Segmented } from "./Filters.jsx";
import Pipeline, { StageDetail } from "./Pipeline.jsx";

// Overview: the first page. What the app is (architecture), why (problem) and how (agents), what it reads (data sources),
// and the plant right now in one card that opens into a detailed, section-by-section view.
const PLANT = { "IN-TN-01": "Hosur", "IN-KA-01": "Bengaluru" };
const STATE = { running: ["Running", "ok"], slowed: ["Slowed", "warn"], down: ["Down", "bad"], waiting: ["Waiting", "info"], idle: ["Idle", "info"], no_data: ["No data", "info"] };
const st = s => STATE[s] || STATE.no_data;
const pct = (a, b) => (b ? Math.round((100 * a) / b) : 0);
const nice = v => { const t = String(v).replace(/_/g, " ").toLowerCase(); return t.charAt(0).toUpperCase() + t.slice(1); };
const day = d => new Date(d + "T00:00:00").toLocaleDateString("en", { weekday: "short", day: "numeric" });

const PROBLEM = [
  "Orders arrive from many channels, each with its own due date and priority, for two plants (Hosur and Bengaluru) and their circuit-board and final-assembly lines.",
  "Plans were made by hand, so they didn't check whether materials were in stock, whether the lines had the hours, or whether the crews were there.",
  "Changeovers piled up, late orders were found too late, and nobody could see quickly what a machine stop or a material shortage would do to the plan.",
];
const VIEW_KEY = "ov.view";
const readView = () => { try { return localStorage.getItem(VIEW_KEY) || "arch"; } catch { return "arch"; } };

const Bar = ({ v, max, tone = "", label }) => (
  <span className={"ov-bar " + tone} role="img" aria-label={label}><i style={{ width: `${Math.min(100, pct(v, max))}%` }} /></span>);

export default function Overview({ setTab, onError }) {
  const [twin, setTwin] = useState(null);
  const [ins, setIns] = useState(null);
  const [crews, setCrews] = useState(null);
  const [neon, setNeon] = useState(null);
  const [conns, setConns] = useState([]);
  const [showNeon, setShowNeon] = useState(false);
  const [detail, setDetail] = useState(false);
  const [view, setView] = useState(readView);
  const pickView = v => { setView(v); try { localStorage.setItem(VIEW_KEY, v); } catch { /* storage may be blocked */ } };
  useEffect(() => {
    const soft = (p, f) => api(p).then(f).catch(() => f(false));
    soft("/api/twin", setTwin); soft("/api/insights?days=30", setIns); soft("/api/plan-tools/crews?days=14", setCrews);
    soft("/api/integrations/connectors", c => setConns(c || []));
    api("/api/integrations/neon").then(setNeon).catch(() => setNeon({ connected: false, message: "Neon could not be reached right now." }));
  }, []);

  const lines = twin?.lines || [];
  const machines = lines.flatMap(l => (l.machines || []).map(m => ({ ...m, line_id: l.line_id, plant_id: l.plant_id })));
  const mCount = Object.keys(STATE).map(s => [s, machines.filter(m => (m.state || "no_data") === s).length]).filter(([, n]) => n);
  const dueSoon = machines.filter(m => m.maintenance_due_days != null && m.maintenance_due_days <= 7).length;
  const days = Object.values((crews?.rows || []).reduce((a, r) => {
    const d = (a[r.date] ||= { date: r.date, needed: 0, available: 0, gap: 0, overtime: 0, rows: [] });
    d.needed += r.needed; d.available += r.available; d.gap += r.gap; d.overtime += r.overtime_h; d.rows.push(r);
    return a;
  }, {}));
  const stock = ins?.stock, floor = twin?.floor;
  const data = { twin, ins, crews, lines, machines, mCount, dueSoon, days, stock, floor };

  if (detail) return <Detail {...data} onBack={() => setDetail(false)} />;

  return (
    <div className="page sp-enter ov">
      <div className="sp-head"><div><h1>Overview</h1><small className="muted">How the system works, what it solves, and how deliveries and the plants stand right now</small></div></div>
      <div className="ov-top">
        <Segmented label="View" options={[{ value: "arch", label: "Architecture & operations" }, { value: "exec", label: "Executive overview" }]} value={view} onChange={pickView} />
        <button className={"ov-neon t-" + (!neon ? "info" : neon.connected ? "ok" : "bad")} onClick={() => setShowNeon(true)}
          aria-label={!neon ? "Checking the Neon database connection" : neon.connected ? "Neon database connected: see its tables" : "Neon database not reachable"}>
          <Logo kind="neon" />
          <span className="ov-neon-t"><b><i className="ov-neon-dot" />{!neon ? "Checking Neon…" : neon.connected ? "Neon database connected" : "Neon not reachable"}</b>
            <small>{!neon ? "PostgreSQL" : neon.connected ? `${N(neon.tables.length)} tables · ${K(neon.rows)} rows · ${N(neon.response_ms)} ms` : neon.message}</small></span>
          <Icon n="right" size={14} />
        </button>
      </div>
      {view === "arch" ? <ArchOps setTab={setTab} /> : <div className="ov-grid">
        <section className="sp-card ov-why">
          <header className="ov-h"><h3>The problem</h3></header>
          <ul className="ov-problem">{PROBLEM.map(p => <li key={p}>{p}</li>)}</ul>
          <p className="ov-people"><Icon n="users" size={14} /> People decide: the production manager approves each plan, then each plant's production head approves it for their plant, and the approved plan is emailed to the team.</p>
        </section>

        <section className="sp-card ov-src">
          <header className="ov-h"><h3>Data sources</h3><small className="muted">Select a source to see its tables</small></header>
          <button className="ov-srcrow" onClick={() => setShowNeon(true)}>
            <Logo kind="neon" />
            <div><b>Neon database</b>
              <small className="muted">{!neon ? "Checking the connection…" : neon.connected ? `${N(neon.tables.length)} tables · ${N(neon.rows)} rows · answered in ${N(neon.response_ms)} ms` : neon.message}</small></div>
            <span className={"sp-pill t-" + (!neon ? "info" : neon.connected ? "ok" : "bad")}>{!neon ? "Checking" : neon.connected ? "Connected" : "Not reachable"}</span>
          </button>
          {neon?.connected && (
            <div className="ov-groups">
              {[["planning", "Planning data"], ["plant", "Plant and twin data"], ["app", "App records"]].map(([g, l]) => {
                const t = neon.tables.filter(x => x.group === g);
                return t.length > 0 && <button key={g} className="ov-grp" onClick={() => setShowNeon(true)}><b>{l}</b><small className="muted">{t.length} tables · {K(t.reduce((a, x) => a + x.rows, 0))} rows</small>
                  <small className="ov-peek">{t.slice(0, 3).map(x => x.label).join(", ")}{t.length > 3 ? "…" : ""}</small></button>;
              })}
            </div>)}
          {conns.map(c => (
            <button key={c.id} className="ov-srcrow" onClick={() => setTab("Connections")}>
              <Logo kind={c.kind} /><div><b>{c.name}</b><small className="muted">{c.kind.replace(/_/g, " ")}</small></div>
              <span className={"sp-pill t-" + (c.status === "CONNECTED" ? "ok" : c.status === "FAILED" ? "bad" : "info")}>{c.status === "CONNECTED" ? "Connected" : c.status === "FAILED" ? "Failed" : "Not tested"}</span>
            </button>))}
          <button className="ov-srcrow" onClick={() => setTab("Connections")}>
            <span className="sp-logo ov-csv"><Icon n="upload" size={18} /></span>
            <div><b>Purchase-order uploads</b><small className="muted">CSV sheets uploaded on Connections start the agents</small></div>
            <Icon n="right" size={16} />
          </button>
        </section>

        <section className="sp-card ov-now" role="button" tabIndex={0} aria-label="Plant right now: open the detailed view"
          onClick={() => setDetail(true)} onKeyDown={e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setDetail(true); } }}>
          <header className="ov-h"><h3>Plant right now</h3><span className="lnk">See the details <Icon n="arrow" size={14} /></span></header>
          <div className="ov-tiles">
            <div className="ov-tile"><small><Icon n="factory" size={14} /> Production</small>
              {floor ? <><b>{N(floor.good)} <em>of {N(floor.target)} this hour</em></b><Bar v={floor.good} max={floor.target} label={`${pct(floor.good, floor.target)}% of target`} />
                <small className="muted">OEE {floor.oee}% · {floor.running} of {floor.lines} lines running</small></> : <Wait v={twin} />}</div>
            <div className="ov-tile"><small><Icon n="box" size={14} /> Inventory</small>
              {stock ? <><b>{N(stock.below_safety)} <em>of {N(stock.materials)} materials below safety stock</em></b>
                <ul className="ov-mini">{stock.lowest_cover.slice(0, 3).map(m => <li key={m.material_id}><span>{m.material_id}</span><Bar v={m.usable} max={m.safety} tone={m.cover < 1 ? "bad" : "ok"} label={`${m.cover}× safety stock`} /></li>)}</ul></> : <Wait v={ins} />}</div>
            <div className="ov-tile"><small><Icon n="users" size={14} /> Manpower</small>
              {days.length ? <><b>{N(days.reduce((a, d) => a + d.gap, 0))} <em>people short over the next {days.length} days</em></b>
                <div className="ov-cols" role="img" aria-label="People available against needed, per day">{days.slice(0, 7).map(d => (
                  <span key={d.date} className={d.gap ? "gap" : ""} title={`${day(d.date)}: ${d.available} of ${d.needed}`}><i style={{ height: `${Math.min(100, pct(d.available, d.needed))}%` }} /></span>))}</div></> : <Wait v={crews} />}</div>
            <div className="ov-tile"><small><Icon n="gear" size={14} /> Machines</small>
              {machines.length ? <><b>{N(machines.length)} <em>machines · {dueSoon} due for service within a week</em></b>
                <span className="ov-stack" role="img" aria-label={mCount.map(([s, n]) => `${n} ${st(s)[0]}`).join(", ")}>{mCount.map(([s, n]) => <i key={s} className={st(s)[1]} style={{ flex: n }} />)}</span>
                <small className="muted">{mCount.map(([s, n]) => `${n} ${st(s)[0].toLowerCase()}`).join(" · ")}</small></> : <Wait v={twin} />}</div>
          </div>
        </section>
      </div>}
      {showNeon && <NeonDialog neon={neon} onClose={() => setShowNeon(false)} onError={onError} />}
    </div>
  );
}

// where to act on each solution (page, link text)
const SOL_PAGE = { choice: ["Plan", "See the five plans"], materials: ["Materials & capacity", "See materials to buy"], disruptions: ["Digital twin", "See the lines now"],
  bottlenecks: ["Materials & capacity", "See line capacity"], delivery: ["Review & approve", "Review and approve the plan"] };

// ---------------------------------------------------------------- Architecture & operations: pipeline, what the flow solves, line load
function ArchOps({ setTab }) {
  const [stage, setStage] = useState("core");
  const [sol, setSol] = useState(null);
  const [openSol, setOpenSol] = useState(null);           // the solution opened to show its details
  const [load, setLoad] = useState(null);
  const [cell, setCell] = useState(null);
  useEffect(() => {
    api("/api/overview/solutions").then(setSol).catch(() => setSol(false));
    api("/api/overview/line-load").then(setLoad).catch(() => setLoad(false));
  }, []);
  const ltone = p => (p >= 95 ? "bad" : p >= 85 ? "warn" : p >= 50 ? "info" : "ok");      // how full a line is
  const pick = cell && load ? load.rows[cell[0]].cells[cell[1]] : null;
  return (
    <div className="ao-wrap">
      <div className="ao-r1">
      <section className="sp-card ao-arch">
        <header className="ov-h"><h3><Icon n="chart" size={16} /> System architecture pipeline</h3>
          <span className="ov-h-r"><small className="muted">Select a stage or an item to see how it works</small>
            <button className="lnk" onClick={() => setTab("System architecture")}>Full page <Icon n="expand" size={14} /></button></span></header>
        <Pipeline sel={stage} onSelect={setStage} compact />
      </section>
        <section className="sp-card">
          <header className="ov-h"><h3><Icon n="sparkle" size={16} /> What the agentic flow solves</h3>{sol?.items && <span className="sv4-chip">{sol.items.length} core problems</span>}</header>
          {sol === null ? <p className="muted" role="status">Loading…</p> : sol === false ? <p className="muted">Not available right now.</p> : (
            <ol className="sv3 sv4">{sol.items.map((it, i) => {
              const on = openSol === it.key, go = SOL_PAGE[it.key];
              return (
                <li key={it.key} className={`sv3-item t-${it.tone} p${i % 5}` + (on ? " on" : "")}>
                  <button type="button" className="sv3-row" aria-expanded={on} onClick={() => setOpenSol(k => (k === it.key ? null : it.key))}>
                    <span className="sv4-num" aria-hidden="true">{String(i + 1).padStart(2, "0")}</span>
                    <span className="sv3-t"><b>{it.title}</b></span>
                    <span className="sv4-ic" aria-hidden="true"><Icon n={it.icon} size={18} /></span>
                  </button>
                  {on && (
                    <div className="sv3-detail">
                      <div className="sv3-fig"><strong>{it.value}</strong><small>{it.label}</small></div>
                      <dl>
                        <div><dt>The problem</dt><dd>{it.problem}</dd></div>
                        <div><dt>How the agent solves it</dt><dd>{it.text}</dd></div>
                      </dl>
                      {go && <button type="button" className="lnk sv3-go" onClick={() => setTab(go[0])}>{go[1]} <Icon n="arrow" size={12} /></button>}
                    </div>)}
                </li>);
            })}</ol>)}
        </section>
      </div>
      <div className="ao-r2">
        <section className="sp-card"><StageDetail stage={stage} /></section>
        <section className="sp-card">
          <header className="ov-h"><h3><Icon n="factory" size={16} /> Line load heatmap</h3>
            {load && <span className="ao-chip">{load.rows.length} lines · {load.weeks.length} weeks</span>}</header>
          {load === null ? <p className="muted" role="status">Loading…</p> : load === false ? <p className="muted">Not available right now.</p> : <>
            <small className="muted">How full each production line is in the plan, week by week</small>
            <div className="ao-heat-wrap"><table className="ao-heat ll">
              <thead><tr><th>Line</th>{load.weeks.map(w => <th key={w.key}>{w.label}<small>{w.sub}</small></th>)}</tr></thead>
              <tbody>{load.rows.map((r, ri) => (
                <tr key={r.line_id}><th><b>{r.line_id}</b><small>{r.plant}</small></th>
                  {r.cells.map((x, ci) => <td key={ci}>{x ? (
                    <button className={`ao-cell t-${ltone(x.pct)}` + (cell && cell[0] === ri && cell[1] === ci ? " on" : "")} onClick={() => setCell(c => (c && c[0] === ri && c[1] === ci ? null : [ri, ci]))}
                      aria-label={`${r.line_id}, ${load.weeks[ci].label}: ${x.pct}% full, ${x.hours} hours booked`}><b>{x.pct}%</b><small>{N(x.hours)} h</small></button>)
                    : <span className="ao-empty">–</span>}</td>)}
                </tr>))}</tbody></table></div>
            <div className="ll-legend" aria-hidden="true"><span className="t-ok">Room to spare</span><span className="t-info">Well used</span><span className="t-warn">Nearly full</span><span className="t-bad">Full</span></div>
            {pick ? <div className="ao-pick"><b>{load.rows[cell[0]].line_id} · {load.weeks[cell[1]].label} ({load.weeks[cell[1]].sub})</b>
              <small className="muted">{pick.pct}% full · {N(pick.hours)} h booked · {N(pick.free_h)} h free. Products on the line:</small>
              <ul>{pick.products.length ? pick.products.map(p => <li key={p.product_id}><b>{p.product_id}</b> · {N(p.units)} units</li>) : <li>Nothing planned</li>}</ul></div>
              : <small className="ao-next"><Icon n="alert" size={12} /><span>{load.first_full ? <><b>{load.overloaded}</b> line-weeks are full; first: <b>{load.first_full[0]}</b>, {load.first_full[1].toLowerCase()}.</> : "No line is full in these weeks."} Select a cell for its products.</span></small>}
          </>}
        </section>
      </div>
    </div>
  );
}

const Wait = ({ v }) => <small className="muted">{v === false ? "Not available right now." : "Loading…"}</small>;

// The detailed view: one section per visual.
function Detail({ twin, ins, lines, machines, mCount, dueSoon, days, stock, floor, onBack }) {
  const L = ins?.lines;
  const maxWh = Math.max(1, ...(twin?.stock?.warehouses || []).map(w => w.on_hand));
  const maxDown = Math.max(1, ...(L?.downtime_by_reason || []).map(r => r.minutes));
  return (
    <div className="page sp-enter ov">
      <div className="sp-head"><div><button className="lnk" onClick={onBack}><Icon n="left" size={14} /> Overview</button><h1>Plant right now</h1>
        <small className="muted">{twin?.at ? `Recorded data as of ${new Date(twin.at).toLocaleString("en", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}` : "Production, inventory, manpower and machines"}</small></div></div>
      <nav className="ov-jump" aria-label="Sections">{[["ov-prod", "Production"], ["ov-inv", "Inventory"], ["ov-man", "Manpower"], ["ov-mac", "Machines"]].map(([id, l]) =>
        <a key={id} href={"#" + id} onClick={e => { e.preventDefault(); document.getElementById(id)?.scrollIntoView({ behavior: "smooth" }); }}>{l}</a>)}</nav>

      <section id="ov-prod" className="sp-card ov-sec">
        <header className="ov-h"><h3><Icon n="factory" size={16} /> Production</h3></header>
        {floor && <div className="ov-kpis">
          <div><small>This hour</small><b>{N(floor.good)}</b><small className="muted">of {N(floor.target)} target</small></div>
          <div><small>Today so far</small><b>{N(floor.today_good)}</b><small className="muted">good units</small></div>
          <div><small>OEE</small><b>{floor.oee}%</b><small className="muted">all lines</small></div>
          <div><small>Lines running</small><b>{floor.running} of {floor.lines}</b><small className="muted">{N(floor.downtime_min)} min stopped this hour</small></div>
          {L && <div><small>Last {ins.period?.days} days</small><b>{L.attainment_pct}%</b><small className="muted">of target made</small></div>}</div>}
        <div className="ov-two">
          <div><h4>Each line today</h4>
            <table className="ov-tbl"><thead><tr><th>Line</th><th>State</th><th>Good of target</th><th className="num">OEE</th></tr></thead>
              <tbody>{lines.map(l => <tr key={l.line_id}><td><b>{l.line_id}</b> <small className="muted">{PLANT[l.plant_id]}</small></td>
                <td><span className={"sp-pill t-" + st(l.state)[1]}>{st(l.state)[0]}</span></td>
                <td><Bar v={l.today?.good} max={l.today?.target} label={`${pct(l.today?.good, l.today?.target)}%`} /> <small className="muted">{K(l.today?.good)} / {K(l.today?.target)}</small></td>
                <td className="num">{l.today?.oee ?? "–"}%</td></tr>)}</tbody></table></div>
          {L && <div><h4>Share of target made, day by day</h4>
            <div className="ov-colchart" role="img" aria-label="Share of target made per day">{L.daily_attainment.map(d => (
              <span key={d.date} title={`${d.date}: ${d.pct}%`} className={d.pct < 85 ? "low" : ""}><i style={{ height: `${Math.min(100, d.pct)}%` }} /></span>))}</div>
            <small className="muted">{L.period?.from} to {L.period?.to} · orange = under 85%</small>
            <h4>Where the stopped time went</h4>
            <ul className="ov-hbars">{L.downtime_by_reason.filter(r => r.reason && r.reason !== "NONE").map(r => <li key={r.reason}><span>{nice(r.reason)}</span><Bar v={r.minutes} max={maxDown} tone="warn" label={`${r.minutes} minutes`} /><em>{N(r.minutes)} min</em></li>)}</ul></div>}
        </div>
      </section>

      <section id="ov-inv" className="sp-card ov-sec">
        <header className="ov-h"><h3><Icon n="box" size={16} /> Inventory</h3></header>
        {stock && <div className="ov-kpis">
          <div><small>Stock count</small><b>{stock.snapshot}</b><small className="muted">latest</small></div>
          <div><small>Materials</small><b>{N(stock.materials)}</b><small className="muted">counted</small></div>
          <div><small>Below safety stock</small><b className={stock.below_safety ? "bad" : ""}>{N(stock.below_safety)}</b><small className="muted">materials</small></div></div>}
        <div className="ov-two">
          <div><h4>Lowest cover</h4>
            <table className="ov-tbl"><thead><tr><th>Material</th><th className="num">Usable</th><th className="num">Safety</th><th className="num">On the way</th><th>Cover</th></tr></thead>
              <tbody>{(stock?.lowest_cover || []).map(m => <tr key={m.material_id}><td><b>{m.material_id}</b></td><td className="num">{N(m.usable)}</td><td className="num">{N(m.safety)}</td>
                <td className="num">{N(m.in_transit)}</td><td><Bar v={m.usable} max={m.safety} tone={m.cover < 1 ? "bad" : "ok"} label={`${m.cover}×`} /> <small className="muted">{m.cover ?? "–"}× safety</small></td></tr>)}</tbody></table></div>
          <div><h4>Warehouses</h4>
            <ul className="ov-hbars">{(twin?.stock?.warehouses || []).map(w => <li key={w.plant_id + w.warehouse_id}><span>{w.warehouse_id} <small className="muted">{PLANT[w.plant_id]}</small></span>
              <Bar v={w.on_hand} max={maxWh} label={`${w.on_hand} on hand`} /><em>{K(w.on_hand)} · {pct(w.usable, w.on_hand)}% usable</em></li>)}</ul>
            <h4>Material flow today</h4>
            {(twin?.flow || []).map(f => <div key={f.plant_id} className="ov-flow"><b>{PLANT[f.plant_id]}</b>
              {[["Received", f.inbound], ["In stores", f.stores], ["Issued", f.issued], ["Boards", f.boards], ["Assembled", f.assembled], ["Finished goods", f.fg]].map(([l, v], i) =>
                <span key={l}>{i > 0 && <Icon n="right" size={12} />}<small className="muted">{l}</small> {K(v)}</span>)}</div>)}</div>
        </div>
      </section>

      <section id="ov-man" className="sp-card ov-sec">
        <header className="ov-h"><h3><Icon n="users" size={16} /> Manpower</h3></header>
        {days.length ? <>
          <div className="ov-kpis">
            <div><small>Shifts planned</small><b>{N(days.reduce((a, d) => a + d.rows.length, 0))}</b><small className="muted">over {days.length} days</small></div>
            <div><small>People short</small><b className={days.some(d => d.gap) ? "bad" : ""}>{N(days.reduce((a, d) => a + d.gap, 0))}</b><small className="muted">across all shifts</small></div>
            <div><small>Overtime to cover</small><b>{N(days.reduce((a, d) => a + d.overtime, 0))} h</b><small className="muted">hours</small></div></div>
          <h4>People available against needed, per day</h4>
          <div className="ov-crew" role="img" aria-label="People available against needed per day">{days.map(d => (
            <div key={d.date} className={d.gap ? "gap" : ""}><span className="ov-crew-bar" title={`${d.available} of ${d.needed}`}><i style={{ height: `${Math.min(100, pct(d.available, Math.max(d.needed, d.available)))}%` }} />
              <s style={{ bottom: `${pct(d.needed, Math.max(d.needed, d.available))}%` }} /></span><small>{day(d.date)}</small><b>{d.available}/{d.needed}</b></div>))}</div>
          <small className="muted">Bar = people available · line = people needed · orange = a shift is short</small>
          {days.some(d => d.gap) && <><h4>Short shifts</h4>
            <table className="ov-tbl"><thead><tr><th>Day</th><th>Shift</th><th>Plant</th><th>Team</th><th className="num">Needed</th><th className="num">Available</th><th className="num">Overtime</th></tr></thead>
              <tbody>{days.flatMap(d => d.rows.filter(r => r.gap)).map(r => <tr key={r.date + r.shift + r.plant_id}><td>{day(r.date)}</td><td>{r.shift}</td><td>{PLANT[r.plant_id]}</td><td>{r.team}</td>
                <td className="num">{r.needed}</td><td className="num bad">{r.available}</td><td className="num">{r.overtime_h} h</td></tr>)}</tbody></table></>}
        </> : <p className="muted">No crew plan is available right now.</p>}
      </section>

      <section id="ov-mac" className="sp-card ov-sec">
        <header className="ov-h"><h3><Icon n="gear" size={16} /> Machines</h3></header>
        <div className="ov-kpis">
          <div><small>Machines</small><b>{N(machines.length)}</b><small className="muted">on {lines.length} lines</small></div>
          {mCount.map(([s, n]) => <div key={s}><small>{st(s)[0]}</small><b className={st(s)[1]}>{n}</b><small className="muted">machines</small></div>)}
          <div><small>Service within a week</small><b className={dueSoon ? "warn" : ""}>{dueSoon}</b><small className="muted">machines</small></div></div>
        <table className="ov-tbl"><thead><tr><th>Machine</th><th>Type</th><th>Line</th><th>State</th><th>Importance</th><th className="num">Service due</th><th className="num">Stopped this hour</th></tr></thead>
          <tbody>{machines.map(m => <tr key={m.machine_id}><td><b>{m.machine_id}</b> <small className="muted">{m.name}</small></td><td>{m.type}</td><td>{m.line_id} <small className="muted">{PLANT[m.plant_id]}</small></td>
            <td><span className={"sp-pill t-" + st(m.state)[1]}>{st(m.state)[0]}</span></td><td>{{ A: "Critical", B: "Important", C: "Standard" }[m.criticality] || m.criticality}</td>
            <td className="num">{m.maintenance_due_days != null ? `in ${m.maintenance_due_days} days` : "–"}</td><td className="num">{N((m.down_min || 0) + (m.wait_min || 0))} min</td></tr>)}</tbody></table>
      </section>
    </div>
  );
}

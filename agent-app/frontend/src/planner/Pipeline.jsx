import { siFastapi, siGmail, siJsonwebtokens, siNginx, siNvidia, siPostgresql, siPython, siReact, siSqlalchemy } from "simple-icons";
import { Icon } from "./widgets.jsx";

// The system as five stages, left to right: where data comes from, the secure gateway it enters through, the single source of truth with the
// business rules and the agentic planning, how people use it, and what it changes for the business. Each item has its logo; selecting one
// shows what it is and what it does here. Logos are the brands' own marks in their own colours (simple-icons).
// in the brand's own colour (from simple-icons); a near-black mark uses the text colour so it stays visible in dark mode
// Neon (the hosted PostgreSQL the data lives in): not in simple-icons, so its mark is drawn here in the brand green
const siNeon = { title: "Neon", hex: "00E599", path: "M0 4.828C0 2.16 2.172 0 4.851 0h14.298C21.828 0 24 2.161 24 4.828v10.37c0 1.833-2.335 2.628-3.47 1.184l-4.125-5.249v6.367c0 2.396-1.951 4.338-4.358 4.338H4.851C2.172 21.838 0 19.677 0 17.01V4.828Zm4.851-.966a.97.97 0 0 0-.97.966v12.181c0 .534.435.966.97.966h7.31c.268 0 .368-.216.368-.483v-7.28c0-1.832 2.335-2.627 3.47-1.183l4.126 5.248V4.828c0-.534.043-.966-.483-.966H4.851Z" };
const dark = hex => { const n = parseInt(hex, 16); return ((n >> 16) & 255) * 0.299 + ((n >> 8) & 255) * 0.587 + (n & 255) * 0.114 < 60; };
const Logo = ({ icon, size = 18 }) => (
  <svg viewBox="0 0 24 24" width={size} height={size} role="img" aria-label={icon.title} fill={dark(icon.hex) ? "currentColor" : `#${icon.hex}`}><path d={icon.path} /></svg>);

// item: [logo (simple-icon) or app icon name, title, one line, short tags, details {what, here, data}]
export const STAGES = [
  { key: "source", n: 1, title: "Source systems", sub: "Data integration", tone: "red", icon: "factory",
    cards: [
      [siNeon, "ERP & MES", "ERP and shop-floor data, kept in Neon", "Neon / SAP / MES", { what: "The plants' business and shop-floor systems (ERP for orders, materials and bills of materials; the MES for what the lines actually made), held in a Neon PostgreSQL database.",
        here: "The agent reads customer and production orders, bills of materials, routings, stock and the hourly production records from them. Nothing is typed in twice.",
        data: "Customer orders, production orders, BOMs, routings, stock, machine output" }],
      ["sheet", "Excel & files", "Purchase-order uploads (CSV, XLSX)", "CSV / XLSX / uploads", { what: "Purchase-order sheets from customers, uploaded on the Connections page as Excel or CSV.",
        here: "Every row is checked (product, quantity, date, priority, duplicates). A sheet with a bad row is refused as a whole and every problem is listed; a good sheet starts the agent.",
        data: "New purchase orders, the upload history" }],
      [siPostgresql, "Databases & APIs", "PostgreSQL, SQL Server, Oracle, REST", "PostgreSQL / REST / OData", { what: "Direct connections to other databases and web services.",
        here: "Admins set up a connection once on Connections; its tables are brought in on request and checked like any upload.",
        data: "Any table the plants keep elsewhere: stock, suppliers, prices" }],
    ],
    about: "Everything planning needs comes from the systems the plants already use: customer and production orders, bills of materials and routings, stock counts, machine output, crews and supplier details.",
    systems: "ERP (SAP), shop-floor production records, purchase-order sheets, database connections",
    data: "Customer orders, production orders, bills of materials, routings, stock, machines, shift calendar, crews, suppliers",
    steps: ["Purchase orders are uploaded on Connections, as Excel or CSV", "Data connections bring in ERP and database tables on request",
      "Production records arrive by the hour from the shop floor", "Nothing is typed in twice: each source is read where it lives"] },
  { key: "gateway", n: 2, title: "Gateway", sub: "Secure entry point", tone: "blue", icon: "lock", layout: "hub", hub: ["Secure entry", "HTTPS / REST / roles"],
    cards: [
      [siNginx, "Nginx reverse proxy", "One secure address; HTTPS, routing, compression", "Nginx", { what: "Nginx is a web server that sits in front of the application as its single public address (a reverse proxy).",
        here: "It accepts every request over a secure connection (HTTPS), compresses responses and passes each request to the FastAPI backend. It is the only part reachable from outside.",
        data: "Requests and their responses; no planning data is stored here" }],
      [siFastapi, "FastAPI backend", "Every request served by the planning service", "FastAPI", { what: "FastAPI is the Python web framework the planning service is built with.",
        here: "It serves the app's pages and data, runs the checks on uploads, and hands planning work to the agent's worker so pages stay fast while the agent plans.",
        data: "Reads and writes the shared database; validates every request (Pydantic)" }],
      [siJsonwebtokens, "Sign-in & permissions", "Who you are, and what your role may do", "Sign-in", { what: "Everyone signs in with their own account; each account has a role.",
        here: "The role decides what a person sees and does: the production manager approves plans, production heads approve for their plant, engineers plan, admins manage data connectors and accounts.",
        data: "Accounts, roles, plant access, the record of who did what" }],
    ],
    about: "Every request from people and systems comes in through one gateway. Nginx receives it and passes it to the FastAPI backend, which checks who is signing in and what their role is allowed to do before anything is read or changed.",
    systems: "Nginx (reverse proxy), FastAPI backend, sign-in and role permissions",
    data: "Sign-ins, roles, plant access, request log",
    steps: ["Nginx is the single entry point: secure connection, routing and compression", "The FastAPI backend serves the app, the data and the planning agent",
      "Everyone signs in; each role sees and does only what it is allowed to", "Admin sees everything and manages the data connectors; decisions stay with the production manager and production heads"] },
  { key: "core", n: 3, title: "Data & planning layer", sub: "Single source of truth", tone: "purple", icon: "nodes",
    cards: [
      [siNeon, "Shared database", "One copy every step reads and writes", "Neon PostgreSQL / audit trail", { what: "One PostgreSQL database (hosted on Neon) holds the data, the plans and every decision.",
        here: "Every page and the agent read the same copy, so everyone sees the same numbers. Plans, approvals, tasks and the audit trail are kept here.",
        data: "Plans and their jobs, approvals, tasks, forecasts, uploads, audit trail" }],
      [siSqlalchemy, "Business rules & KPIs", "Time fences, safety stock, priorities, OEE", "Priorities / KPIs / OEE", { what: "The planning rules and success measures the plants agreed on.",
        here: "Frozen first two weeks, safety stock kept back, priorities, penalty-first sequencing, and the measures every plan is scored on: on-time delivery, utilisation, changeovers, cost.",
        data: "Settings for each rule; each plan's scores" }],
      [siPython, "AI agentic planning", "Checks, plans and re-plans; people decide", "Five plans / materials / risks", { what: "The planning agent: a set of cooperating checks and a finite-capacity planning engine.",
        here: "It reads every new order, checks stock, machines and crews, and builds five complete plans that keep the customers' dates, then recommends one and says why. It never approves or releases anything.",
        data: "Five plans per run, each with jobs on real shift time, materials to buy and cost" }],
    ],
    about: "One database holds the data, the plans and every decision. The planning rules and success measures live here, and the AI agent turns demand into a plan the plants can run: capacity first, then materials, then a finite schedule.",
    systems: "Neon PostgreSQL, the planning engine, the AI agent",
    data: "Plans and their jobs, approvals, volume decisions, forecasts, tasks, audit trail",
    steps: ["The agent analyses orders, stock, machines and crews, and makes five plans", "Materials are planned: what stock covers, what to order and by when",
      "Jobs are placed on real shift time, with the first two weeks frozen", "The production manager chooses one plan; nothing is released by the agent itself"] },
  { key: "use", n: 4, title: "Consumption", sub: "Apps, reports & alerts", tone: "navy", icon: "chart", layout: "screen", hub: ["Plans, reports & answers", "Dashboards / plan document / alerts"],
    cards: [
      [siReact, "Planning app", "Dashboards, plans, plan document, twin", "React", { what: "The web app people use, built with React.",
        here: "Each role gets its pages: the five plans and the plan document, review and approvals, materials and capacity, the digital twin and the dashboards.",
        data: "Everything is read live from the shared database" }],
      [siGmail, "Reports & documents", "Plan document, approval email", "Email", { what: "The printable production plan and the email that goes out with it.",
        here: "When the production manager approves a plan, its details are emailed to the production heads and senior officials. Agent alerts stay in the app; no other emails are sent.",
        data: "The approved plan's summary and its jobs and material lists" }],
      [siNvidia, "Ask in plain words", "The planning assistant answers from the data", "Assistant", { what: "The planning assistant: ask a question in plain words and it answers from the plan and the data.",
        here: "Answers come from Domo AI using the app's own figures; NVIDIA safety models screen questions and answers first.",
        data: "Reads plans and data; it cannot change anything" }],
    ],
    about: "People see and act on the plan where they work: the app's pages for each role, the printable plan document, the approval email and the assistant that answers questions from the data.",
    systems: "The planning app, email, the planning assistant",
    data: "Every page reads the same shared database, so everyone sees the same numbers",
    steps: ["The production manager chooses a plan; each production head approves it for their plant", "Plant heads run the day from the plan and the digital twin",
      "The approved plan goes by email to the production heads", "Anyone can ask the assistant a question about the plan"] },
  { key: "outcome", n: 5, title: "Business outcome", sub: "Insights & actions", tone: "green", icon: "target",
    cards: [
      ["trend", "Data-driven decisions", "Approve with cost, risk and dates in view", "On time / penalties / cost", { what: "Decisions taken with the facts on the page.",
        here: "Every plan shows on-time delivery against the customers' dates, late penalties, extra spend and total cost side by side, so the choice is clear.",
        data: "Each plan's scores and the agent's reasons" }],
      ["bell", "Alerts & early actions", "Shortages, stops, overloads, blocked jobs", "Shortages / overloads", { what: "Problems raised early enough to act on.",
        here: "Material shortages, overloaded weeks, stopped lines and blocked jobs become a task for the person who can fix them, in the app's notifications.",
        data: "Tasks and notifications by role and plant" }],
      ["target", "Operational impact", "On-time delivery, utilisation, OEE", "Efficiency / quality / yield", { what: "The results, measured the same way every day.",
        here: "On-time delivery, plan adherence, line utilisation, changeovers and yield are tracked against the plan, and feed the next plan.",
        data: "Success measures over time" }],
    ],
    about: "The point of it all: decisions made on facts, problems raised early enough to act on, and better delivery and use of the lines, measured every day.",
    systems: "Success measures, alerts, the risk log",
    data: "On-time delivery, plan adherence, utilisation, changeovers, OEE, inventory turns",
    steps: ["Every plan is scored on the same measures before it is approved", "Problems raise a task for the person who can fix them",
      "Committed dates are tracked against actual progress", "Results feed the next plan: the loop closes"] },
];

const mark = (ic, size) => (typeof ic === "string" ? <Icon n={ic} size={size} /> : <Logo icon={ic} size={size} />);

// row tints, like the reference: each item its own soft colour
const TINT = { source: ["blue", "green", "purple"], core: ["purple", "blue", "green"], outcome: ["yellow", "green", "blue"] };

// sel: a stage key ("core") or one item of it ("core:1"). One design everywhere (the Overview and the full page).
export default function Pipeline({ sel, onSelect, compact }) {
  const [sk, si] = String(sel || "").split(":");
  const on = (st, j) => sk === st.key && si === String(j);
  const pickItem = (st, j) => onSelect?.(`${st.key}:${j}`);
  return (
    <div className={"pa" + (compact ? " compact" : "")} role="list">
      {STAGES.map((st, i) => (
        <div key={st.key} className="pa-step" role="listitem">
          {i > 0 && <span className="pa-arrow" aria-hidden="true"><Icon n="right" size={20} /></span>}
          <div className={`pa-stage t-${st.tone} k-${st.key}` + (sk === st.key ? " on" : "")}>
            <button type="button" className="pa-head" aria-pressed={sk === st.key && si == null} onClick={() => onSelect?.(st.key)}>
              <span className="pa-hic"><Icon n={st.icon} size={26} /></span>
              <b>{st.n}. {st.title}</b><small>({st.sub})</small>
            </button>
            {st.layout === "hub" ? (
              <div className="pa-body pa-hub">
                <span className="pa-cluster" aria-hidden="true">{st.cards.map(([ic], j) => <span key={j} className={`pa-orb o${j}`}>{mark(ic, 22)}</span>)}</span>
                <span className="pa-hub-t">{st.hub[0]}</span>
                <span className="pa-chips">{st.cards.map(([, t, , tag], j) => (
                  <button type="button" key={t} className={"pa-chip" + (on(st, j) ? " on" : "")} aria-pressed={on(st, j)} title={t} onClick={() => pickItem(st, j)}>{tag}</button>))}</span>
                <small className="pa-foot">({st.hub[1]})</small>
              </div>
            ) : st.layout === "screen" ? (
              <div className="pa-body pa-screenwrap">
                <span className="pa-screen" aria-hidden="true"><span className="pa-bars"><i /><i /><i /></span><span className="pa-pie" /></span>
                <span className="pa-stand" aria-hidden="true" />
                <span className="pa-hub-t">{st.hub[0]}</span>
                <small className="pa-foot">({st.hub[1]})</small>
                <span className="pa-tiles">{st.cards.map(([ic, t, , tag], j) => (
                  <button type="button" key={t} className={"pa-tile" + (on(st, j) ? " on" : "")} aria-pressed={on(st, j)} title={t} aria-label={t} onClick={() => pickItem(st, j)}>
                    <span>{mark(ic, 24)}</span></button>))}</span>
                <small className="pa-foot">({st.cards.map(c => c[3]).join(" / ")})</small>
              </div>
            ) : (
              <div className="pa-body pa-rows">{st.cards.map(([ic, t, , tag], j) => (
                <button type="button" key={t} className={`pa-row c-${(TINT[st.key] || [])[j] || "blue"}` + (on(st, j) ? " on" : "")} aria-pressed={on(st, j)} title={`${t} (${tag})`} onClick={() => pickItem(st, j)}>
                  <span className="pa-ric">{mark(ic, 24)}</span><span className="pa-rt"><b>{t}</b><small>({tag})</small></span></button>))}
              </div>
            )}
          </div>
        </div>))}
    </div>
  );
}

export function StageDetail({ stage }) {
  const [sk, si] = String(stage || "").split(":");
  const st = STAGES.find(s => s.key === sk) || STAGES[0];
  const item = si != null ? st.cards[Number(si)] : null;
  if (item) {
    const [ic, t, d, , x] = item;
    return (
      <div className={`pl-detail t-${st.tone}`}>
        <header><span className="pl-ic pl-logo">{mark(ic, 22)}</span><h3>{t}</h3><span className="pl-tag">Stage {st.n} · {st.title}</span></header>
        <p className="pl-lead">{d}</p>
        <div className="pl-facts pl-facts3">
          <div><small>What it is</small><span>{x.what}</span></div>
          <div><small>What it does here</small><span>{x.here}</span></div>
          <div><small>Data</small><span>{x.data}</span></div>
        </div>
        <h4>Also in this stage</h4>
        <ul className="pl-also">{st.cards.filter(c => c !== item).map(([ic2, t2]) => <li key={t2}>{mark(ic2, 14)} {t2}</li>)}</ul>
      </div>
    );
  }
  return (
    <div className={`pl-detail t-${st.tone}`}>
      <header><span className="pl-ic"><Icon n={st.icon} size={18} /></span><h3>{st.n}. {st.title}</h3><span className="pl-tag">Stage {st.n} · {st.sub}</span></header>
      <p>{st.about}</p>
      <div className="pl-facts"><div><small>Systems</small><span>{st.systems}</span></div><div><small>Data</small><span>{st.data}</span></div></div>
      <h4>How it works</h4>
      <ol>{st.steps.map(x => <li key={x}>{x}</li>)}</ol>
      <small className="muted pl-hint">Select any item in the stage to see what it is and what it does here.</small>
    </div>
  );
}

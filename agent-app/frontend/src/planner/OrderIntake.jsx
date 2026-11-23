import { useEffect, useRef, useState } from "react";
import PlanOptions from "./PlanOptions.jsx";
import { VolumeGate } from "./FlowPages.jsx";
import { PAGES, api, auth, can } from "../api.js";
import { N } from "./ui.jsx";
import { Icon, K } from "./widgets.jsx";

// Order intake, on the Connections page: upload a purchase-order sheet (CSV or Excel) and the whole agent workflow is laid out.
// "Run the agent" sends the orders and opens Review & approve, where the run plays out step by step (tables read, the agent's
// reasoning as it thinks), ends with the recommendation, and the production manager approves the plan.

const STEPS = [
  { key: "read", title: "Reading your file", icon: "upload", reads: ["Your sheet", "Product list", "Sales channels", "Delivery dates"] },
  { key: "lines", title: "Production lines", icon: "factory", reads: ["Hourly production records", "Machine list", "Maintenance records", "Line stops"] },
  { key: "stock", title: "Materials and stock", icon: "box", reads: ["Daily stock counts", "Bills of materials", "Stock in transit", "What open orders need"] },
  { key: "plants", title: "Both plants", icon: "truck", reads: ["Work waiting for parts", "Stock in each plant", "Line time in each plant", "Crew rosters"] },
  { key: "plan", title: "Production plan", icon: "calendar", reads: ["Open customer orders", "Open production orders", "Shift calendar", "Line speeds and yields"] },
  { key: "impact", title: "Orders already promised", icon: "layers", reads: ["Promised delivery dates", "Supply each order relies on", "Supplier lead times"] },
  { key: "decide", title: "Decision", icon: "check", reads: ["Approval rules", "Who looks after each plant"] },
];
const AFTER = [
  { title: "Recommendation", icon: "sparkle", text: "When everything finishes, when the new orders are ready, and what to do" },
  { title: "Approval", icon: "lock", text: "The production manager approves the plan; its orders are then frozen" },
];
const READ = 450;                                     // the agent spends this long opening and scanning each table
const KEY = "upload.job";
const day = v => (v ? new Date(String(v).slice(0, 10) + "T00:00").toLocaleDateString("en", { day: "numeric", month: "short" }) : "–");
const reduced = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
const remember = v => { try { v ? sessionStorage.setItem(KEY, v) : sessionStorage.removeItem(KEY); } catch { /* storage may be blocked */ } };
const b64 = buf => { let s = ""; const a = new Uint8Array(buf); for (let i = 0; i < a.length; i += 0x8000) s += String.fromCharCode(...a.subarray(i, i + 0x8000)); return btoa(s); };
export const SHEET_TYPES = ".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

// pickRef.current(file) is how the Upload button on the page hands a file in. Nothing shows until there is a file or a run.
export default function OrderIntake({ role, pickRef, onStarted, onError }) {
  const [file, setFile] = useState(null);             // {name, text, rows, units, preview}
  const [errs, setErrs] = useState(null);
  const [busy, setBusy] = useState("");               // "reading" | "sending"
  const ref = useRef();
  const allowed = can("intake", role);
  const reset = () => { setFile(null); setErrs(null); };
  const show = () => setTimeout(() => ref.current?.scrollIntoView({ behavior: reduced() ? "auto" : "smooth", block: "start" }), 50);

  const pick = async f => {
    if (!f || !allowed) return;
    setFile(null); setErrs(null); show();
    const xlsx = /\.xlsx$/i.test(f.name);
    if (!xlsx && !/\.csv$/i.test(f.name) && f.type !== "text/csv") { setErrs({ msg: "Choose a CSV file or an Excel sheet (.xlsx), with a header row.", rows: [] }); return; }
    setBusy("reading");
    try {
      const text = xlsx ? (await api("/api/orders/sheet", "POST", { filename: f.name, data: b64(await f.arrayBuffer()) })).csv : await f.text();
      const lines = text.trim().split(/\r?\n/).filter(Boolean);
      const head = (lines[0] || "").toLowerCase().split(",").map(h => h.trim());
      const qi = head.findIndex(h => ["qty", "quantity", "order_qty"].includes(h));
      setFile({ name: f.name, text, rows: Math.max(0, lines.length - 1), units: qi < 0 ? null : lines.slice(1).reduce((t, l) => t + (+l.split(",")[qi] || 0), 0),
        preview: lines.slice(0, 6).map(l => l.split(",")) });
    } catch (e) {
      if (e.api?.code === "UNREADABLE_FILE") setErrs({ msg: e.message, rows: [] }); else onError(e);
    } finally { setBusy(""); }
  };
  if (pickRef) pickRef.current = pick;
  const send = async () => {
    setBusy("sending"); setErrs(null);
    try {
      const r = await api("/api/orders/upload", "POST", { filename: file.name, csv: file.text });
      remember(r.job_id); setFile(null); onStarted?.(r.job_id);
    } catch (e) {
      if (e.api?.code === "INVALID_ORDERS" || e.api?.code === "UNREADABLE_FILE") setErrs({ msg: e.message, rows: e.api.detail || [] });
      else onError(e);
    } finally { setBusy(""); }
  };

  if (!file && !errs && !busy) return <div ref={ref} />;
  return (
    <section className="sp-card oi" ref={ref} aria-label="Purchase order upload and agent workflow">
      <header className="sp-ch">
        <div><h3>Agent workflow for this sheet</h3>
          <small className="sp-sub-t">Check the orders and every step the agent will take. Running it opens Review &amp; approve, where you watch it work.</small></div>
        <div className="sp-ch-r">
          <button className="sp-icon-btn" aria-label="Close" title="Close" onClick={reset} disabled={busy === "sending"}><Icon n="close" size={14} /></button>
        </div>
      </header>
      {busy === "reading" ? <p className="muted" role="status">Reading the sheet…</p>
        : (
          <>
            {file && (
              <div className="oi-file">
                <div className="uo-file-h">
                  <span className="uo-zone-ic sm" aria-hidden="true"><Icon n="file" size={20} /></span>
                  <div><b>{file.name}</b><small className="muted">{N(file.rows)} order{file.rows === 1 ? "" : "s"}{file.units != null ? ` · ${K(file.units)} units` : ""}</small></div>
                </div>
                <div className="sp-table uo-prev"><table>
                  <thead><tr>{file.preview[0].map((h, i) => <th key={i}>{h}</th>)}</tr></thead>
                  <tbody>{file.preview.slice(1).map((r, i) => <tr key={i}>{r.map((v, j) => <td key={j}>{v}</td>)}</tr>)}</tbody>
                </table></div>
                {file.rows > 5 && <small className="muted">Showing the first 5 of {N(file.rows)} orders.</small>}
              </div>
            )}
            {errs && (
              <div className="uo-errs" role="alert"><b className="bad">{errs.msg}</b>
                {errs.rows.length > 0 && <ul>{errs.rows.slice(0, 8).map(e => <li key={e.row}><b>Row {e.row} · {e.customer_order_id}</b> <span className="muted">{e.problems.join("; ")}</span></li>)}</ul>}
              </div>
            )}
            {file && (
              <>
                <ol className="oi-plan" aria-label="What the agent will do">
                  {STEPS.map((s, i) => (
                    <li key={s.key} style={{ "--i": i }}>
                      <span className="oi-n"><Icon n={s.icon} size={16} /></span>
                      <b><small>Step {i + 1}</small>{s.title}</b>
                      <ul>{s.reads.map(r => <li key={r}>{r}</li>)}</ul>
                    </li>))}
                  {AFTER.map((s, i) => (
                    <li key={s.title} className="after" style={{ "--i": STEPS.length + i }}>
                      <span className="oi-n"><Icon n={s.icon} size={16} /></span>
                      <b><small>Then</small>{s.title}</b>
                      <p>{s.text}</p>
                    </li>))}
                </ol>
                <div className="sp-row">
                  <button className="sp-btn primary oi-run" disabled={!!busy || !file.rows} onClick={send}>
                    {busy === "sending" ? "Starting…" : <><Icon n="play" size={14} /> Run the agent on {file.rows} order{file.rows === 1 ? "" : "s"}</>}</button>
                </div>
              </>
            )}
          </>
        )}
    </section>
  );
}

// ------------------------------------------------------------------ the checks, as they happen on the server
function Workflow({ jobId, initial, replay, onLost, openPlan, onFinished, approval }) {
  const [job, setJob] = useState(initial || null);
  const [shown, setShown] = useState(() => (initial && !replay ? STEPS.length : 0));   // the step on screen
  const [peek, setPeek] = useState(null);             // a finished step opened again
  const ready = useRef(false);                        // the step on screen has read its tables and said what it thinks
  useEffect(() => {
    if (initial) return;                              // a finished run: nothing to follow
    let live = true, t;
    const poll = async () => {
      try {
        const j = await api(`/api/orders/jobs/${jobId}`);
        if (!live) return;
        setJob(j);
        if (j.status === "running") t = setTimeout(poll, 500);
      } catch (e) { if (live && e.api?.code === "JOB_NOT_FOUND") onLost(); else if (live) t = setTimeout(poll, 1500); }
    };
    poll();
    return () => { live = false; clearTimeout(t); };
  }, [jobId]);                                           // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {                                      // move on once the server finished the step and it was on screen long enough
    const t = setInterval(() => {
      if (!job) return;
      setShown(s => {
        const cur = job.steps[STEPS[s]?.key];
        if (s < STEPS.length && cur?.state === "done" && (reduced() || ready.current)) { ready.current = false; return s + 1; }
        return s;
      });
    }, 150);
    return () => clearInterval(t);
  }, [job]);
  // finished = every step shown AND the server saved the result (the last step is ticked a moment before the result is written)
  const done = !!job && shown >= STEPS.length && job.status === "done" && !!job.result;
  useEffect(() => { if (done) onFinished?.(job); }, [done, job]);                  // eslint-disable-line react-hooks/exhaustive-deps
  if (!job) return <section className="uo-flow"><p className="muted" role="status">Starting the checks…</p></section>;
  const step = STEPS[Math.min(shown, STEPS.length - 1)];
  const st = job.steps[step.key] || {};
  const failed = job.status === "failed";
  return (
    <>
      <section className={"uo-flow" + (done ? " finished" : "")} aria-busy={!done && !failed}>
        <ol className="uo-steps" aria-label="Checks">
          {STEPS.map((s, i) => {
            const state = i < shown || (i === shown && job.steps[s.key]?.state === "done") ? "done" : i === shown && !failed ? "now" : "wait";
            const open = done && peek === i;
            return (
              <li key={s.key} className={state + (open ? " peek" : "")} aria-current={state === "now" ? "step" : undefined}>
                <span className="uo-node" aria-hidden="true"><Icon n={state === "done" ? "check" : s.icon} size={15} /></span>
                <div><b>{done ? <button className="lnk uo-peek" onClick={() => setPeek(open ? null : i)} aria-pressed={open}>{s.title}</button> : s.title}</b>
                  <small className="muted">{state === "done" ? (job.steps[s.key]?.facts?.[0] || "Done") : state === "now" ? "Working…" : "Waiting"}</small></div>
              </li>
            );
          })}
        </ol>
        <div className="uo-stage" aria-live="polite">
          {failed ? (
            <div className="uo-fail" role="alert"><Icon n="alert" size={20} /><div><b>The orders were received, but the checks could not finish.</b>
              <span className="muted">Nothing was changed. Try again later, or tell your administrator.</span></div></div>
          ) : done && peek != null ? (
            <>
              <div className="uo-stage-h"><small className="muted">Step {peek + 1} of {STEPS.length} · <button className="lnk" onClick={() => setPeek(null)}>Back to the summary</button></small>
                <h2>{STEPS[peek].title}</h2><p>{job.steps[STEPS[peek].key]?.detail}</p></div>
              <AgentRead key={"peek" + peek} step={STEPS[peek]} st={job.steps[STEPS[peek].key] || {}} still />
            </>
          ) : done ? (
            <Summary r={job.result} openPlan={openPlan} />
          ) : (
            <>
              <div className="uo-stage-h"><small className="muted">Step {shown + 1} of {STEPS.length}</small><h2>{step.title}</h2>
                <p>{st.detail || "Getting ready…"}</p></div>
              <AgentRead key={step.key} step={step} st={st} onReady={() => { ready.current = true; }} />
            </>
          )}
        </div>
      </section>
      {done && approval}
    </>
  );
}

// The agent at work on one step: it opens each source table in turn and scans its rows, while its reasoning is typed out live on
// the right (every thought comes from the server, built from what the checks found). Ready once the tables are read and it has spoken.
const TYPE_MS = 10, PAUSE_MS = 180;
const tone = t => (t.startsWith("Urgent:") ? ["alert", "bad"] : t.startsWith("Noticed:") ? ["search", "warn"] : t.startsWith("…") ? ["more", ""] : ["sparkle", ""]);
function AgentRead({ step, st, onReady, still }) {
  const n = step.reads.length, thoughts = st.thoughts || [];
  const instant = still || reduced();
  const [ti, setTi] = useState(() => (instant ? n : 0));        // the table being read; n = all read
  const [ty, setTy] = useState({ line: 0, ch: 0 });             // the thought being typed and how far
  const listRef = useRef();
  useEffect(() => {
    if (ti >= n) return;
    const t = setTimeout(() => setTi(i => i + 1), READ);
    return () => clearTimeout(t);
  }, [ti, n]);
  const line = instant ? thoughts.length : ty.line;
  useEffect(() => {                                             // type the next thought once it has arrived
    if (instant || ty.line >= thoughts.length) return;
    const full = thoughts[ty.line];
    const t = ty.ch < full.length ? setTimeout(() => setTy(x => ({ ...x, ch: x.ch + 3 })), TYPE_MS)
      : setTimeout(() => setTy(x => ({ line: x.line + 1, ch: 0 })), PAUSE_MS);
    return () => clearTimeout(t);
  }, [ty, thoughts.length, instant]);                          // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { const el = listRef.current; if (el) el.scrollTop = el.scrollHeight; }, [ty, thoughts.length]);
  const read = ti >= n, said = st.state === "done" && line >= thoughts.length, found = read && said;
  useEffect(() => { if (found) onReady?.(); }, [found]);        // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div className="ag-read">
      <div className={"ag-bot" + (found ? " ok" : "")}>
        <span className="ag-bot-ic" aria-hidden="true"><Icon n={found ? "check" : "sparkle"} size={16} /></span>
        <span><b>Agent</b> {found ? "has read every table and reports back" : read ? "is thinking about what it read…" : <>is opening <em>{step.reads[ti]}</em></>}</span>
        <span className="ag-count">{Math.min(ti, n)} of {n} read</span>
      </div>
      <div className="ag-body">
        <ol className="ag-tables" aria-label="Tables the agent reads">
          {step.reads.map((r, i) => {
            const state = i < ti ? "done" : i === ti ? "now" : "wait";
            return (
              <li key={r} className={"ag-table " + state}>
                <header>
                  <Icon n="grid" size={13} /><span>{r}</span>
                  <em className="ag-badge">{state === "done" ? <><Icon n="check" size={11} /> Read</> : state === "now" ? "Reading" : "Queued"}</em>
                </header>
                <div className="ag-rows" aria-hidden="true">
                  {Array.from({ length: 5 }, (_, k) => <i key={k} style={{ "--k": k }}><b /><b /><b /><b /></i>)}
                  <span className="ag-scan" />
                </div>
              </li>);
          })}
        </ol>
        <section className={"ag-think" + (found ? " done" : "")} aria-label="Agent reasoning">
          <header><span className="ag-live" aria-hidden="true" /><b>Agent reasoning</b><small>{found ? "Done" : "Thinking live"}</small></header>
          <ol ref={listRef} aria-hidden="true">
            {thoughts.slice(0, line + 1).map((t, i) => {
              const [ic, tn] = tone(t), typing = i === line && !instant;
              return (
                <li key={i} className={(typing ? "now " : "") + tn}>
                  <Icon n={ic} size={12} /><span>{typing ? t.slice(0, ty.ch) : t}{typing && <i className="ag-caret" />}</span>
                </li>);
            })}
            {line >= thoughts.length && !said && <li className="ag-wait"><span className="uo-dots"><i /><i /><i /></span> Thinking…</li>}
          </ol>
          <p className="sr-only" aria-live="polite">{thoughts[line - 1] || ""}</p>
          {found && st.facts?.length > 0 && (
            <div className="ag-conc"><small>Concluded</small>
              {st.facts.map((f, i) => <span key={f} style={{ "--i": i }}><Icon n="check" size={11} /> {f}</span>)}</div>)}
        </section>
      </div>
    </div>
  );
}

function Summary({ r, openPlan }) {
  const n = r.orders.length, act = r.needs_action;
  return (
    <div className="uo-sum">
      <span className={"uo-sum-ic " + (act ? "warn" : "ok")} aria-hidden="true"><Icon n={act ? "alert" : "check"} size={24} /></span>
      <h2>{act ? `${act} of ${n} order${n === 1 ? "" : "s"} need${act === 1 ? "s" : ""} attention` : `All ${n} order${n === 1 ? "" : "s"} fit the plan`}</h2>
      <p className="muted">{act ? "The people who look after the plan and each plant have been told what to do. Nothing changes until a production manager approves."
        : "The current production plan already covers them. Nobody needs to do anything."}</p>
      <div className="uo-sum-k">
        <span><b>{n - act}</b><small className="muted">fit the plan</small></span>
        <span><b className={act ? "bad" : ""}>{act}</b><small className="muted">need attention</small></span>
        <span><b>{r.schedule_id ? "Yes" : "No"}</b><small className="muted">new plan for approval</small></span>
      </div>
      {r.schedule_id && <button className="sp-btn primary" onClick={() => openPlan(r.schedule_id)}>Review the new plan <Icon n="right" size={14} /></button>}
    </div>
  );
}

// The agent's recommendation, answer first: what to do, then three plain questions (new orders ready, everything finished, what holds
// things up), the key dates in order, today against the plan, and the to-do list. Every figure comes from the server.
// rupees the Indian way: crore, lakh, thousand

// ------------------------------------------------------------------ Review & approve: the agent's latest run, then the decision
const when = v => (v ? new Date(v).toLocaleString("en", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "");
// Plans the agent made by adding a what-if's change: waiting for the production manager here.
function ChangedPlans({ role, onError, onDecided }) {
  const [rows, setRows] = useState([]);
  const [ack, setAck] = useState({});
  const [busy, setBusy] = useState("");
  const load = async () => {
    try {
      const l = (await api("/api/schedules?limit=50")).filter(x => x.adopted && x.status === "PENDING_APPROVAL");
      setRows(await Promise.all(l.map(async x => ({ ...x, cmp: await api(`/api/compare?base=${x.base_id}&ids=${x.id}`).catch(() => null) }))));
    } catch { /* shown again on the next visit */ }
  };
  useEffect(() => { load(); }, []);                                // eslint-disable-line react-hooks/exhaustive-deps
  if (!rows.length) return null;
  const may = can("approve", role);
  const decide = async (x, to) => {
    setBusy(x.id + to);
    try { await api(`/api/schedules/${x.id}/${to}`, "POST", to === "approve" ? { acknowledge_risks: true } : {}); await load(); onDecided?.(); }
    catch (e) { onError?.(e); } finally { setBusy(""); }
  };
  return (
    <section className="sp-card cp">
      <header className="sp-ch"><div><h3>Plans changed from a what-if</h3><small className="sp-sub-t">Someone added a what-if to the plan and the agent re-planned with it</small></div></header>
      {rows.map(x => {
        const [b, w] = x.cmp || [];
        const pair = (k, fm) => (b && w && b[k] != null && w[k] != null ? <><b>{fm(b[k])}</b> <Icon n="arrow" size={12} /> <b>{fm(w[k])}</b></> : "–");
        return (
          <article key={x.id} className="cp-row">
            <div className="cp-t"><b>{x.name}</b><small className="muted">by {x.created_by === "planning agent" ? "the planning agent" : x.created_by} · {x.created_at ? when(x.created_at) : ""}</small></div>
            <dl className="cp-k">
              <div><dt>On-time delivery</dt><dd>{pair("on_time_qty_pct", v => `${v}%`)}</dd></div>
              <div><dt>Late jobs</dt><dd>{pair("late_jobs", N)}</dd></div>
              <div><dt>Units not scheduled</dt><dd>{pair("unscheduled_qty", N)}</dd></div>
            </dl>
            {may ? (
              <div className="cp-a">
                <label className="cp-ack"><input type="checkbox" checked={!!ack[x.id]} onChange={e => setAck(a => ({ ...a, [x.id]: e.target.checked }))} /> I have looked at the risks</label>
                <button className="sp-btn" disabled={!!busy} onClick={() => decide(x, "reject")}>Reject</button>
                <button className="sp-btn primary" disabled={!ack[x.id] || !!busy} onClick={() => decide(x, "approve")}><Icon n="check" size={14} /> Approve</button>
              </div>
            ) : <small className="muted cp-wait">Waiting for the production manager</small>}
          </article>);
      })}
    </section>
  );
}

// Approvals: the history of approved plans. The production manager approves, then each plant's production head; the plan email goes out
// at the manager's approval. Production heads approve for their plant here; the production manager can delete an approval.
const MAIL = { SENT: "Emailed", PARTLY_SENT: "Emailed to some people; some emails failed", FAILED: "The email could not be sent",
  NOT_CONFIGURED: "Not emailed: email is not set up", NO_EMAIL: "Not emailed: no one in the team has an email address" };
function Approvals({ role, onError, onDecided }) {
  const [rows, setRows] = useState(null);
  const [busy, setBusy] = useState("");
  const load = async () => { try { setRows(await api("/api/approved-plans")); } catch { setRows([]); } };
  useEffect(() => { load(); }, []);                                // eslint-disable-line react-hooks/exhaustive-deps
  if (rows === null) return null;
  const run = async (key, fn) => { setBusy(key); try { await fn(); await load(); onDecided?.(); } catch (e) { onError?.(e); } finally { setBusy(""); } };
  const waiting = rows.filter(x => !x.fully_approved).length;
  const mayDelete = can("approve", role);
  const remove = x => {
    if (!window.confirm(`Delete the approval of \u201c${x.name}\u201d? The plan is closed, its approval history is removed and its orders can be changed again. This can't be undone.`)) return;
    run("d" + x.id, () => api(`/api/approvals/${x.id}`, "DELETE"));
  };
  return (
    <details className="sp-card ps ps-mini ap-hist">
      <summary><Icon n="check" size={16} /><span><b>Approvals</b>{" · "}{rows.length ? `${rows.length} approved plan${rows.length === 1 ? "" : "s"}${waiting ? `, ${waiting} waiting for the production heads` : ""}` : "no approvals yet"}</span><em>Show</em></summary>
      {rows.length === 0 ? <p className="muted ap-empty">No plan has been approved yet. When the production manager chooses a plan it appears here, with each plant's approval.</p>
        : rows.map(x => {
          const mine = role === "plant_head" && x.stages.find(st => st.plant && st.plant === auth.plant && !st.done);
          return (
            <article key={x.id} className={"ps-row" + (x.fully_approved ? " ps-full" : "")}>
              <div className="ps-t"><b>{x.fully_approved && <Icon n="check" size={14} />} {x.name}</b>
                <small className="muted">{x.fully_approved ? `Fully approved ${when(x.fully_approved_at)}` : `${x.stages.filter(st => st.done).length} of ${x.stages.length} stages approved`}
                  {x.email ? ` · ${MAIL[x.email.status] || ""}${x.email.sent ? ` to ${x.email.sent} ${x.email.sent === 1 ? "person" : "people"}` : ""}` : ""}</small></div>
              <ol className="ps-steps">{x.stages.map(st => (
                <li key={st.label} className={st.done ? "on" : ""}><span className="ps-dot"><Icon n={st.done ? "check" : "clock"} size={12} /></span>
                  <div><b>{st.label}</b><small className="muted">{st.done ? `${st.by} · ${when(st.at)}` : "Waiting"}</small></div></li>))}
              </ol>
              <div className="ap-acts">
                {mine && <button className="sp-btn primary" disabled={!!busy} onClick={() => run(x.id, () => api(`/api/schedules/${x.id}/plant-approve`, "POST"))}>
                  <Icon n="check" size={14} /> Approve for {mine.plant}</button>}
                {mayDelete && x.email && x.email.status !== "SENT" && <button className="lnk" disabled={!!busy} onClick={() => run("m" + x.id, () => api(`/api/schedules/${x.id}/email-plan`, "POST"))}>Send the email again</button>}
                {mayDelete && <button className="sp-btn danger" disabled={!!busy} onClick={() => remove(x)}><Icon n="close" size={14} /> {busy === "d" + x.id ? "Deleting…" : "Delete approval"}</button>}
              </div>
            </article>);
        })}
    </details>
  );
}

export function ReviewFlow({ role, setTab, onError, onDecided, ran = true, onRan, onMsg, onOpenPlan, after }) {
  const [got, setGot] = useState(null);               // {job, live} | {none: true}
  const [replay, setReplay] = useState(0);
  const [fin, setFin] = useState(null);
  const [tick, setTick] = useState(0);                  // a decision anywhere on the page refreshes the approval stages
  const decided = () => { setTick(t => t + 1); onDecided?.(); };
  const apRef = useRef();
  const load = async () => {
    setFin(null); setReplay(0);
    let jid = null;
    try { jid = sessionStorage.getItem(KEY); } catch { /* storage may be blocked */ }
    try {
      if (jid) {
        try { const j = await api(`/api/orders/jobs/${jid}`); remember(null); return setGot({ job: j, live: j.status === "running" }); }
        catch { remember(null); }
      }
      const j = await api("/api/orders/jobs/latest");
      setGot({ job: j, live: j.status === "running" });
    } catch (e) { if (e.api?.code === "JOB_NOT_FOUND") setGot({ none: true }); else { onError(e); setGot({ none: true }); } }
  };
  useEffect(() => { load(); }, []);                    // eslint-disable-line react-hooks/exhaustive-deps
  const job = got?.job;
  const toApproval = () => apRef.current?.scrollIntoView({ behavior: reduced() ? "auto" : "smooth", block: "start" });
  return (
    <div className="page sp-enter uo-page">
      <div className="sp-head">
        <div><h1>Review &amp; approve</h1><small className="muted">Watch the agent work through the latest purchase orders, then choose one of its five plans</small></div>
        <div className="sp-head-r">
          {job && fin && <button className="sp-btn" onClick={() => { setFin(null); setReplay(r => r + 1); }}><Icon n="play" size={14} /> Replay the agent&apos;s work</button>}
          <button className="sp-icon-btn" onClick={load} aria-label="Refresh" title="Refresh"><Icon n="refresh" size={16} /></button>
        </div>
      </div>
      <VolumeGate role={role} onError={onError} onDone={load} quiet />
      <ChangedPlans role={role} onError={onError} onDecided={decided} />
      <Approvals key={tick} role={role} onError={onError} onDecided={decided} />
      {!got ? <section className="uo-flow"><p className="muted" role="status">Loading the latest run…</p></section>
        : got.none || (!ran && !got.live) ? (
          <section className="sp-card rf-empty">
            <span className="uo-zone-ic" aria-hidden="true"><Icon n="upload" size={26} /></span>
            <b>No purchase orders have been run through the agent yet</b>
            <p className="muted">Upload a sheet on the Connections page and run the agent. Its work plays out here, ending with the plan to approve.</p>
            {PAGES[role]?.includes("Connections") && <button className="sp-btn primary" onClick={() => setTab("Connections")}><Icon n="upload" size={14} /> Upload a sheet</button>}
          </section>
        ) : (
          <>
            <p className="rf-meta"><Icon n="file" size={14} /> <b>{job.file}</b> · run by {job.by || "–"}{job.started_at ? ` · ${when(job.started_at)}` : ""}
              {got.live && <span className="sp-pill t-info">Running now</span>}</p>
            <Workflow key={job.id + ":" + replay} jobId={job.id} initial={got.live ? null : job} replay={replay > 0} onLost={load}
              openPlan={toApproval} onFinished={f => { setFin(f); onRan?.(); }}
              approval={fin && <div ref={apRef}><PlanOptions role={role} onError={onError} onMsg={onMsg} onChosen={id => { decided(); onOpenPlan?.(id); }} /></div>} />
          </>
        )}
      {after}
    </div>
  );
}

import { useEffect, useMemo, useRef, useState } from "react";
import { api, can } from "../api.js";
import { N } from "./ui.jsx";
import { DataTable, EmptyState, KpiStrip } from "./PlanningViews.jsx";
import { Icon } from "./widgets.jsx";
import { FilterBar } from "./Filters.jsx";
import OrderIntake, { SHEET_TYPES } from "./OrderIntake.jsx";

// Connections: upload purchase-order sheets for the agent, connect SAP and databases, import their tables, and choose which data
// planning uses. Every control calls the API.

const LOGO = { neon: "neon.png", sap_odata: "sap.svg", postgresql: "postgresql.svg", mysql: "mysql.svg", mssql: "mssql.svg", oracle: "oracle.svg" };
export const Logo = ({ kind }) => <span className="sp-logo"><img src={`/logos/${LOGO[kind]}`} alt="" width="28" height="28" /></span>;
const TONE = { CONNECTED: "ok", FAILED: "bad", NOT_TESTED: "info", SUCCEEDED: "ok", RUNNING: "info" };
const nice = s => { const t = String(s ?? "").replace(/_/g, " ").toLowerCase(); return t.charAt(0).toUpperCase() + t.slice(1); };
const when = v => (v ? new Date(v + (String(v).endsWith("Z") ? "" : "Z")).toLocaleString("en", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "–");
const where = c => c.config.base_url || [c.config.host, c.config.port].filter(Boolean).join(":") + (c.config.database ? ` / ${c.config.database}` : "");
const ACT = { connector_created: "Connection added", connector_updated: "Connection changed", connector_deleted: "Connection removed",
  connector_tested: "Connection tested", connector_synced: "Data imported", connector_sync_failed: "Import failed",
  dataset_source_switched: "Planning switched to imported data", dataset_source_reset: "Planning switched back to built-in data" };

export default function IntegrationsView({ role, onMsg, onError, onStarted }) {
  const pickRef = useRef();                        // the Upload button hands its file to the agent workflow panel
  const [drag, setDrag] = useState(false);
  const [cat, setCat] = useState([]);
  const [conns, setConns] = useState([]);
  const [sets, setSets] = useState([]);
  const [syncs, setSyncs] = useState([]);
  const [acts, setActs] = useState([]);
  const [neon, setNeon] = useState(null);          // the app's own Neon database: always there, opened with "View data"
  const [showNeon, setShowNeon] = useState(false);
  const [form, setForm] = useState(null);          // {kind, conn?}
  const [imp, setImp] = useState(null);            // connection being imported from
  const [busy, setBusy] = useState("");
  const [cf, setCf] = useState({});                // connection filters
  const [hf, setHf] = useState({});                // import-history filters
  const admin = role === "admin";

  const load = async () => {
    const [a, b, c, d, e] = await Promise.all(["/catalog", "/connectors", "/datasets", "/syncs", "/activity"].map(p => api("/api/integrations" + p)));
    setCat(a); setConns(b); setSets(c); setSyncs(d); setActs(e);
    api("/api/integrations/neon").then(setNeon).catch(() => setNeon({ connected: false, message: "Neon could not be reached right now." }));
  };
  useEffect(() => { load().catch(onError); }, []);   // eslint-disable-line react-hooks/exhaustive-deps

  const act = (key, fn) => async (...a) => {
    setBusy(key);
    try { await fn(...a); await load(); } catch (e) { onError(e); } finally { setBusy(""); }
  };
  const test = id => act("t" + id, async () => {
    const r = await api(`/api/integrations/connectors/${id}/test`, "POST");
    onMsg(r.ok ? r.message : `Connection failed: ${r.message}`, !r.ok);
  })();
  const remove = c => {
    if (!confirm(`Remove “${c.name}”? Its imported data is deleted and any dataset using it goes back to built-in data.`)) return;
    act("d" + c.id, async () => { await api(`/api/integrations/connectors/${c.id}`, "DELETE"); onMsg("Connection removed"); })();
  };
  const switchSource = (ds, syncId) => act("s" + ds, async () => {
    if (syncId) { const r = await api(`/api/integrations/datasets/${ds}/source`, "PUT", { sync_id: syncId }); onMsg(`Planning now uses the imported data (${N(r.rows)} rows).`); }
    else { await api(`/api/integrations/datasets/${ds}/source`, "DELETE"); onMsg("Planning uses the built-in data again."); }
  })();

  const label = k => cat.find(x => x.kind === k)?.label || k;
  const shownConns = conns.filter(c => (!cf.kind || c.kind === cf.kind) && (!cf.status || c.status === cf.status)
    && (!cf.q || `${c.name} ${where(c)}`.toLowerCase().includes(cf.q.toLowerCase())));
  const shownSyncs = syncs.filter(s => (!hf.dataset || s.dataset === hf.dataset) && (!hf.status || s.status === hf.status));
  const fromConn = sets.filter(x => x.source.kind === "connector").length;
  const lastSync = syncs.find(x => x.status === "SUCCEEDED");

  return (
    <div className="page sp-enter">
      <div className="sp-head">
        <div><h1>Connections</h1><small className="muted">Upload purchase-order sheets for the agent, connect your ERP and databases, and choose what planning uses</small></div>
        <div className="sp-head-r"><button className="sp-icon-btn" onClick={() => load().catch(onError)} aria-label="Refresh" title="Refresh"><Icon n="refresh" size={16} /></button></div>
      </div>
      {!admin && (
        <div className="sp-callout t-info">
          <span className="sp-callout-ic"><Icon n="lock" size={18} /></span>
          <div className="sp-callout-t"><b>View only</b><span>You can upload purchase-order sheets. Only an admin can add connections, import data or change what planning uses.</span></div>
        </div>
      )}
      <div className="sp-flow">
        <OrderIntake role={role} pickRef={pickRef} onStarted={onStarted} onError={onError} />
        <KpiStrip items={[
          { label: "Connections", value: N(conns.length + 1) }, { label: "Connected", value: N(conns.filter(c => c.status === "CONNECTED").length + (neon?.connected ? 1 : 0)), tone: "ok" },
          { label: "Need attention", value: N(conns.filter(c => c.status === "FAILED").length), tone: conns.some(c => c.status === "FAILED") ? "bad" : "" },
          { label: "Datasets from connections", value: `${fromConn} of ${sets.length || 8}` },
          { label: "Last import", value: lastSync ? when(lastSync.finished_at) : "–", note: lastSync ? `${N(lastSync.rows)} rows · ${lastSync.dataset_label}` : null },
        ]} />

        <section className="sp-card">
          <header className="sp-ch"><div><h3>Add a connection</h3><small className="sp-sub-t">Read-only: the app only reads from these systems and never writes back</small></div></header>
          <div className="sp-catalog">
            <div className={"sp-connector oi-card" + (drag ? " over" : "")} onDragOver={e => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
              onDrop={e => { e.preventDefault(); setDrag(false); pickRef.current?.(e.dataTransfer.files?.[0]); }}>
              <span className="sp-logo oi-logo"><Icon n="upload" size={20} /></span>
              <div><b>Purchase orders</b><small className="muted">Sheet · CSV or Excel</small></div>
              <p className="muted">Upload new purchase orders. The agent checks them against lines, stock and both plants, then recommends a plan.</p>
              <label className={"sp-btn primary" + (can("intake", role) ? "" : " is-off")}>
                <Icon n="upload" size={16} /> Upload sheet
                <input type="file" accept={SHEET_TYPES} className="sr-only" disabled={!can("intake", role)}
                  onChange={e => { pickRef.current?.(e.target.files?.[0]); e.target.value = ""; }} />
              </label>
            </div>
            <div className="sp-connector is-on">
              <Logo kind="neon" />
              <div><b>Neon</b><small className="muted">Database · built in</small></div>
              <p className="muted">The app's own Postgres database: planning data, plans, uploads and agent records.</p>
              <button className="sp-btn" onClick={() => setShowNeon(true)}><span className={"nd-dot" + (neon?.connected ? " ok" : "")} /> {neon?.connected ? "Connected · View data" : "View"}</button>
            </div>
            {cat.map(k => (
              <div key={k.kind} className="sp-connector">
                <Logo kind={k.kind} />
                <div><b>{k.label}</b><small className="muted">{k.group}</small></div>
                <p className="muted">{k.description}</p>
                <button className="sp-btn" disabled={!admin} onClick={() => setForm({ kind: k.kind })}><Icon n="plus" size={16} /> Connect</button>
              </div>
            ))}
          </div>
        </section>

        <section className="sp-card">
          <header className="sp-ch"><div><h3>Your connections</h3><small className="sp-sub-t">Test a connection, then import a table into one of the planning datasets</small></div></header>
          {conns.length > 0 && <FilterBar value={cf} onChange={setCf} className="sp-filterbar-in" fields={[
            { key: "kind", label: "Type", all: "All types", options: cat.filter(k => conns.some(c => c.kind === k.kind)).map(k => ({ value: k.kind, label: k.label })) },
            { key: "status", label: "Status", all: "Any status", options: [{ value: "CONNECTED", label: "Connected" }, { value: "FAILED", label: "Failed" }, { value: "NOT_TESTED", label: "Not tested" }] },
            { key: "q", label: "Find a connection…", type: "search" },
          ]} />}
          <div className="sp-conns">
            <button className="sp-conn nd-conn" onClick={() => setShowNeon(true)}>
              <Logo kind="neon" />
              <div className="sp-conn-t">
                <b>Neon database</b>
                <small className="muted">Built in · {neon?.connected ? `${N(neon.tables.length)} tables · ${N(neon.rows)} rows` : "Postgres"}</small>
                <small className={neon && !neon.connected ? "bad" : "muted"}>{!neon ? "Checking the connection…" : neon.connected ? `Checked ${when(neon.checked_at)} · answered in ${N(neon.response_ms)} ms` : neon.message}</small>
              </div>
              <span className={"sp-pill t-" + (!neon ? "info" : neon.connected ? "ok" : "bad")}>{!neon ? "Checking" : neon.connected ? "Connected" : "Not reachable"}</span>
              <span className="sp-conn-a"><span className="sp-btn primary"><Icon n="grid" size={16} /> View data</span></span>
            </button>
          </div>
          {!conns.length ? <p className="muted">No other connections yet. Pick a system above to connect it.</p>
            : !shownConns.length ? <p className="muted">No connections match these filters.</p> : (
            <div className="sp-conns">{shownConns.map(c => (
              <div key={c.id} className="sp-conn">
                <Logo kind={c.kind} />
                <div className="sp-conn-t">
                  <b>{c.name}</b>
                  <small className="muted">{label(c.kind)} · {where(c)}</small>
                  <small className={c.status === "FAILED" ? "bad" : "muted"}>{c.status === "FAILED" ? c.last_error : c.last_checked_at ? `Checked ${when(c.last_checked_at)}` : "Not tested yet"}</small>
                </div>
                <span className={"sp-pill t-" + TONE[c.status]}>{nice(c.status)}</span>
                <div className="sp-conn-a">
                  <button className="sp-btn" disabled={!admin || !!busy} onClick={() => test(c.id)}><Icon n="refresh" size={16} /> {busy === "t" + c.id ? "Testing…" : "Test"}</button>
                  <button className="sp-btn primary" disabled={!admin || c.status !== "CONNECTED"} title={c.status !== "CONNECTED" ? "Test the connection first" : ""} onClick={() => setImp(c)}><Icon n="download" size={16} /> Import data</button>
                  <button className="sp-icon-btn" disabled={!admin} aria-label="Edit" title="Edit" onClick={() => setForm({ kind: c.kind, conn: c })}><Icon n="gear" size={16} /></button>
                  <button className="sp-icon-btn" disabled={!admin || !!busy} aria-label="Remove" title="Remove" onClick={() => remove(c)}><Icon n="close" size={16} /></button>
                </div>
              </div>))}
            </div>
          )}
        </section>

        <section className="sp-card">
          <header className="sp-ch"><div><h3>Data used for planning</h3><small className="sp-sub-t">Each dataset uses the built-in data unless you switch it to an import. The import is checked before planning switches.</small></div></header>
          <div className="sp-table"><table>
            <thead><tr><th>Dataset</th><th className="num">Rows</th><th>Source now</th><th>Use for planning</th></tr></thead>
            <tbody>{sets.map(x => {
              const options = syncs.filter(s => s.dataset === x.dataset && s.status === "SUCCEEDED");
              return (
                <tr key={x.dataset}>
                  <td><b>{x.label}</b></td>
                  <td className="num">{N(x.rows)}</td>
                  <td>{x.source.kind === "connector"
                    ? <span className="sp-row sp-left"><span className="sp-pill t-ok">{x.source.connector}</span><small className="muted">{x.source.object} · imported {when(x.source.synced_at)}</small></span>
                    : <span className="sp-pill t-info">Built-in data</span>}</td>
                  <td>
                    <select className="sp-inline-select" disabled={!admin || busy === "s" + x.dataset || (!options.length && x.source.kind !== "connector")}
                      value={x.source.kind === "connector" ? x.source.sync_id : ""} onChange={e => switchSource(x.dataset, e.target.value)} aria-label={`Source for ${x.label}`}>
                      <option value="">Built-in data</option>
                      {options.map(s => <option key={s.id} value={s.id}>{conns.find(c => c.id === s.connector_id)?.name || "Connection"} · {s.source} · {when(s.finished_at)} ({N(s.rows)} rows)</option>)}
                    </select>
                  </td>
                </tr>);
            })}</tbody>
          </table></div>
        </section>

        <div className="sp-two">
          <DataTable title="Import history" sub="Most recent first" rows={shownSyncs} pageSize={8} empty={syncs.length ? "No imports match these filters." : "No imports yet."}
            right={syncs.length > 0 && <FilterBar value={hf} onChange={setHf} className="sp-filterbar-in sp-filterbar-mini" fields={[
              { key: "dataset", label: "Into", all: "All datasets", options: [...new Set(syncs.map(s => s.dataset))].map(d => ({ value: d, label: syncs.find(s => s.dataset === d).dataset_label })) },
              { key: "status", label: "Result", all: "Any result", options: [{ value: "SUCCEEDED", label: "Succeeded" }, { value: "FAILED", label: "Failed" }] },
            ]} />} cols={[
            { key: "started_at", label: "When", render: r => when(r.started_at) },
            { key: "connector_id", label: "From", render: r => `${conns.find(c => c.id === r.connector_id)?.name || "Removed"} · ${r.source}` },
            { key: "dataset_label", label: "Into" }, { key: "rows", label: "Rows", num: true },
            { key: "status", label: "Result", render: r => <span className={"sp-pill t-" + (TONE[r.status] || "bad")} title={r.error || ""}>{nice(r.status)}</span> },
          ]} />
          <section className="sp-card">
            <header className="sp-ch"><div><h3>Activity</h3><small className="sp-sub-t">Who changed what, and when</small></div></header>
            {!acts.length ? <p className="muted">Nothing yet.</p> : (
              <ol className="sp-timeline">{acts.map((e, i) => (
                <li key={i}><span className="sp-bullet" /><div><b>{ACT[e.action] || nice(e.action)}</b><small className="muted">{e.actor} · {when(e.ts)}</small></div></li>))}
              </ol>)}
          </section>
        </div>
      </div>

      {showNeon && <NeonDialog neon={neon} onClose={() => setShowNeon(false)} onError={onError} />}
      {form && <ConnectionForm kind={cat.find(k => k.kind === form.kind)} conn={form.conn} onClose={() => setForm(null)}
        onSaved={async (c, msg) => { setForm(null); onMsg(msg); await load(); if (c) test(c.id); }} onError={onError} />}
      {imp && <ImportDialog conn={imp} sets={sets} onClose={() => setImp(null)} onError={onError}
        onDone={async s => { setImp(null); onMsg(`Imported ${N(s.rows)} rows into ${s.dataset_label}. Choose it under “Data used for planning” to use it.`); await load(); }} />}
    </div>
  );
}

function Modal({ title, sub, onClose, children, wide }) {
  const ref = useRef();
  useEffect(() => { ref.current?.showModal(); }, []);
  return (
    <dialog ref={ref} className={"sp-dialog" + (wide ? " wide" : "")} onClose={onClose} aria-label={title}>
      <div className="sp-row"><div><h2>{title}</h2>{sub && <small className="muted">{sub}</small>}</div>
        <button className="sp-icon-btn" aria-label="Close" onClick={() => ref.current?.close()}><Icon n="close" size={16} /></button></div>
      {children}
    </dialog>
  );
}

function ConnectionForm({ kind, conn, onClose, onSaved, onError }) {
  const [name, setName] = useState(conn?.name || kind.label);
  const [vals, setVals] = useState(() => ({ ...(kind.default_port ? { port: kind.default_port } : {}), ...(conn?.config || {}) }));
  const [busy, setBusy] = useState(false);
  async function save(e) {
    e.preventDefault();
    setBusy(true);
    const config = {}, secrets = {};
    kind.fields.forEach(f => { (f.type === "secret" ? secrets : config)[f.key] = vals[f.key] ?? ""; });
    try {
      const c = conn ? await api(`/api/integrations/connectors/${conn.id}`, "PUT", { name, kind: kind.kind, config, secrets })
        : await api("/api/integrations/connectors", "POST", { name, kind: kind.kind, config, secrets });
      onSaved(c, conn ? "Connection saved. Testing it now…" : "Connection added. Testing it now…");
    } catch (err) { onError(err); setBusy(false); }
  }
  return (
    <Modal title={conn ? `Edit ${conn.name}` : `Connect ${kind.label}`} sub="Passwords are encrypted before they are saved and are never shown again." onClose={onClose}>
      <form className="sp-form" onSubmit={save}>
        <label className="sp-field"><small>Connection name</small><input required maxLength={100} value={name} onChange={e => setName(e.target.value)} /></label>
        {kind.fields.map(f => (
          <label key={f.key} className="sp-field"><small>{f.label}</small>
            {f.type === "select" ? <select value={vals[f.key] || f.options[0]} onChange={e => setVals({ ...vals, [f.key]: e.target.value })}>{f.options.map(o => <option key={o}>{o}</option>)}</select>
              : <input type={f.type === "secret" ? "password" : f.type === "number" ? "number" : f.type === "url" ? "url" : "text"}
                  required={f.required && !(f.type === "secret" && conn?.has_secret)} autoComplete={f.type === "secret" ? "new-password" : "off"}
                  placeholder={f.type === "secret" && conn?.has_secret ? "Saved – leave blank to keep" : f.type === "url" ? "https://…" : ""}
                  value={vals[f.key] ?? ""} onChange={e => setVals({ ...vals, [f.key]: e.target.value })} />}
          </label>))}
        <div className="sp-row sp-right">
          <button type="button" className="sp-btn" onClick={onClose}>Cancel</button>
          <button className="sp-btn primary" disabled={busy}>{busy ? "Saving…" : conn ? "Save and test" : "Connect and test"}</button>
        </div>
      </form>
    </Modal>
  );
}

function ImportDialog({ conn, sets, onClose, onDone, onError }) {
  const [objs, setObjs] = useState(null);
  const [src, setSrc] = useState("");
  const [ds, setDs] = useState(sets[0]?.dataset || "");
  const [pv, setPv] = useState(null);
  const [map, setMap] = useState({});
  const [busy, setBusy] = useState(false);
  useEffect(() => { api(`/api/integrations/connectors/${conn.id}/objects`).then(setObjs).catch(e => { onError(e); setObjs([]); }); }, [conn.id]);   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    setPv(null); setMap({});
    if (src) api(`/api/integrations/connectors/${conn.id}/preview?source=${encodeURIComponent(src)}`).then(setPv).catch(onError);
  }, [src]);   // eslint-disable-line react-hooks/exhaustive-deps
  const need = sets.find(x => x.dataset === ds)?.columns || [];
  const match = useMemo(() => {
    const lower = Object.fromEntries((pv?.columns || []).map(c => [c.toLowerCase(), c]));
    return Object.fromEntries(need.map(c => [c, map[c] || lower[c.toLowerCase()] || ""]));
  }, [pv, need, map]);
  const missing = need.filter(c => !match[c]);
  async function run() {
    setBusy(true);
    try { onDone(await api(`/api/integrations/connectors/${conn.id}/sync`, "POST", { source: src, dataset: ds, mapping: Object.fromEntries(Object.entries(map).filter(([, v]) => v)) })); }
    catch (e) { onError(e); setBusy(false); }
  }
  return (
    <Modal wide title={`Import from ${conn.name}`} sub="Rows are copied into the app's database. Planning keeps using its current data until you switch the dataset." onClose={onClose}>
      <div className="sp-form two">
        <label className="sp-field"><small>Table or data set in {conn.name}</small>
          <select value={src} onChange={e => setSrc(e.target.value)} disabled={!objs}>
            <option value="">{objs ? `Choose one of ${objs.length}…` : "Loading…"}</option>{(objs || []).map(o => <option key={o}>{o}</option>)}</select></label>
        <label className="sp-field"><small>Planning dataset it feeds</small>
          <select value={ds} onChange={e => { setDs(e.target.value); setMap({}); }}>{sets.map(x => <option key={x.dataset} value={x.dataset}>{x.label}</option>)}</select></label>
      </div>
      {pv && (
        <>
          <div className="sp-mapping">
            <small className="muted">{missing.length ? `${missing.length} of ${need.length} columns need a match` : `All ${need.length} columns matched`}</small>
            <div className="sp-mapgrid">{need.map(c => (
              <label key={c} className={"sp-map" + (match[c] ? " is-ok" : " is-miss")}>
                <span>{c}</span>
                <select value={match[c]} onChange={e => setMap({ ...map, [c]: e.target.value })} aria-label={`Source column for ${c}`}>
                  <option value="">Not matched</option>{pv.columns.map(s => <option key={s}>{s}</option>)}</select>
              </label>))}
            </div>
          </div>
          <div className="sp-table sp-preview"><table>
            <thead><tr>{pv.columns.map(c => <th key={c}>{c}</th>)}</tr></thead>
            <tbody>{pv.rows.slice(0, 6).map((r, i) => <tr key={i}>{pv.columns.map(c => <td key={c}>{r[c] == null ? "–" : String(r[c])}</td>)}</tr>)}</tbody>
          </table></div>
        </>
      )}
      <div className="sp-row sp-right">
        <button className="sp-btn" onClick={onClose}>Cancel</button>
        <button className="sp-btn primary" disabled={!pv || missing.length > 0 || busy} onClick={run}><Icon n="download" size={16} /> {busy ? "Importing…" : "Import"}</button>
      </div>
    </Modal>
  );
}

// Neon: what the app reads from its own database. Tables grouped by what they feed; planning data and a few safe records can be previewed.
const GROUPS = [["planning", "Planning data", "What every plan is built from"], ["plant", "Plant and twin data", "Machines, crews, shipments and suppliers for the twin and the agents"],
  ["app", "App records", "Plans, uploads, agent work and accounts the app keeps"]];

export function NeonDialog({ neon, onClose, onError }) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(null);           // table being previewed
  const [pv, setPv] = useState(null);
  useEffect(() => {
    setPv(null);
    if (open?.preview) api(`/api/integrations/neon/tables/${encodeURIComponent(open.name)}?limit=25`).then(setPv).catch(onError);
  }, [open]);   // eslint-disable-line react-hooks/exhaustive-deps
  const tables = (neon?.tables || []).filter(t => !q || `${t.label} ${t.name} ${t.columns.join(" ")}`.toLowerCase().includes(q.toLowerCase()));
  const cell = v => (v == null ? "–" : typeof v === "object" ? JSON.stringify(v) : String(v));
  return (
    <Modal wide title="Neon database" sub={neon?.connected ? `Connected · checked ${when(neon.checked_at)}` : neon?.message || "Checking the connection…"} onClose={onClose}>
      {neon?.connected && (
        <>
          <div className="nd-kpis">
            <div><small>Status</small><b className="ok"><span className="nd-dot ok" /> Connected</b></div>
            <div><small>Response</small><b>{N(neon.response_ms)} ms</b></div>
            <div><small>Tables</small><b>{N(neon.tables.length)}</b></div>
            <div><small>Rows</small><b>{N(neon.rows)}</b></div>
            <div><small>Planning reads from</small><b>{neon.used_for_planning ? "Neon" : "Built-in files"}</b></div>
          </div>
          {open ? (
            <div className="nd-detail">
              <button className="lnk" onClick={() => setOpen(null)}><Icon n="left" size={14} /> All tables</button>
              <h3>{open.label} <small className="muted">{open.name} · {N(open.rows)} rows · {open.columns.length} columns</small></h3>
              <div className="nd-cols">{open.columns.map(c => <span key={c}>{c}</span>)}</div>
              {!open.preview ? <p className="muted nd-private"><Icon n="lock" size={14} /> These records stay private to the app, so only the columns are shown.</p>
                : !pv ? <p className="muted" role="status">Loading the first rows…</p> : (
                <>
                  <div className="sp-table sp-preview"><table>
                    <thead><tr>{pv.columns.map(c => <th key={c}>{c}</th>)}</tr></thead>
                    <tbody>{pv.rows.map((r, i) => <tr key={i}>{pv.columns.map(c => <td key={c}>{cell(r[c])}</td>)}</tr>)}</tbody>
                  </table></div>
                  <small className="muted">First {pv.rows.length} of {N(open.rows)} rows</small>
                </>)}
            </div>
          ) : (
            <>
              <input className="nd-find" type="search" placeholder="Find a table or column…" value={q} onChange={e => setQ(e.target.value)} aria-label="Find a table or column" />
              {GROUPS.map(([g, title, sub]) => {
                const list = tables.filter(t => t.group === g);
                return list.length > 0 && (
                  <section key={g} className="nd-group">
                    <header><b>{title}</b><small className="muted">{sub} · {list.length} tables · {N(list.reduce((a, t) => a + t.rows, 0))} rows</small></header>
                    <div className="nd-grid">{list.map(t => (
                      <button key={t.name} className="nd-table" onClick={() => setOpen(t)}>
                        <b>{t.label}{!t.preview && <Icon n="lock" size={12} />}</b>
                        <small className="muted">{N(t.rows)} rows · {t.columns.length} columns</small>
                        <small className="nd-peek">{t.columns.slice(0, 4).join(", ")}{t.columns.length > 4 ? "…" : ""}</small>
                      </button>))}
                    </div>
                  </section>);
              })}
              {!tables.length && <p className="muted">No table or column matches “{q}”.</p>}
            </>
          )}
        </>
      )}
    </Modal>
  );
}

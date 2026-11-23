import { useEffect, useState } from "react";
import { ROLES, api } from "../api.js";
import { Icon } from "./widgets.jsx";
import { Team } from "./AgentView.jsx";

// Admin: who can sign in, their position and (for a production head) their plant. Passwords are set here and never shown again.
const PLANTS = ["IN-TN-01", "IN-KA-01"];
const BLANK = { username: "", name: "", role: "engineer", plant_id: PLANTS[0], password: "" };
const when = v => (v ? new Date(String(v).endsWith("Z") ? v : v + "Z").toLocaleString("en", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "Never");

export default function UsersView({ onMsg, onError }) {
  const [list, setList] = useState(null);
  const [v, setV] = useState(BLANK);
  const [busy, setBusy] = useState(false);
  const [reset, setReset] = useState(null);         // {id, password}
  const load = () => api("/api/users").then(setList).catch(onError);
  useEffect(() => { load(); }, []);                 // eslint-disable-line react-hooks/exhaustive-deps
  const [team, setTeam] = useState([]);             // who receives tasks and the approved-plan emails
  const loadTeam = () => api("/api/agent/team").then(setTeam).catch(onError);
  useEffect(() => { loadTeam(); }, []);             // eslint-disable-line react-hooks/exhaustive-deps

  const add = async e => {
    e.preventDefault(); setBusy(true);
    try { await api("/api/users", "POST", { ...v, plant_id: v.role === "plant_head" ? v.plant_id : null }); onMsg(`${v.name} can now sign in as ${v.username}.`); setV(BLANK); await load(); }
    catch (x) { onError(x); } finally { setBusy(false); }
  };
  const patch = async (u, body, msg) => {
    try { await api(`/api/users/${u.id}`, "PATCH", body); onMsg(msg); await load(); } catch (x) { onError(x); }
  };

  return (
    <div className="us-page">
      <section className="sp-card us-add">
        <h3><Icon n="plus" size={16} /> Add a person</h3>
        <form onSubmit={add} className="us-form">
          <label className="sp-field"><small>Full name</small><input required maxLength={100} value={v.name} onChange={e => setV({ ...v, name: e.target.value })} /></label>
          <label className="sp-field"><small>Username</small><input required minLength={3} maxLength={64} pattern="[A-Za-z0-9._\-]+" title="Letters, numbers, dot, dash or underscore"
            value={v.username} onChange={e => setV({ ...v, username: e.target.value })} /></label>
          <label className="sp-field"><small>Position</small><select value={v.role} onChange={e => setV({ ...v, role: e.target.value })}>
            {ROLES.map(r => <option key={r.id} value={r.id}>{r.label}</option>)}</select></label>
          {v.role === "plant_head" && <label className="sp-field"><small>Plant</small><select value={v.plant_id} onChange={e => setV({ ...v, plant_id: e.target.value })}>
            {PLANTS.map(p => <option key={p}>{p}</option>)}</select></label>}
          <label className="sp-field"><small>First password (8 or more characters)</small><input type="password" autoComplete="new-password" required minLength={8} maxLength={200}
            value={v.password} onChange={e => setV({ ...v, password: e.target.value })} /></label>
          <button className="sp-btn primary" disabled={busy}><Icon n="plus" size={16} /> {busy ? "Adding…" : "Add person"}</button>
        </form>
      </section>
      <section className="sp-card">
        <h3><Icon n="users" size={16} /> People who can sign in</h3>
        {!list ? <p className="muted" role="status">Loading…</p> : (
          <div className="us-table-wrap"><table className="us-table">
            <thead><tr><th>Name</th><th>Username</th><th>Position</th><th>Plant</th><th>Last sign-in</th><th>Status</th><th><span className="sr-only">Actions</span></th></tr></thead>
            <tbody>{list.map(u => (
              <tr key={u.id} className={u.active ? "" : "off"}>
                <td><b>{u.name}</b></td><td>{u.username}</td>
                <td><select aria-label={`Position of ${u.name}`} value={u.role} onChange={e => patch(u, { role: e.target.value, plant_id: e.target.value === "plant_head" ? (u.plant_id || PLANTS[0]) : null }, `${u.name}'s position changed. They sign in again.`)}>
                  {ROLES.map(r => <option key={r.id} value={r.id}>{r.label}</option>)}</select></td>
                <td>{u.role === "plant_head" ? <select aria-label={`Plant of ${u.name}`} value={u.plant_id || PLANTS[0]} onChange={e => patch(u, { plant_id: e.target.value }, `${u.name} now leads ${e.target.value}.`)}>
                  {PLANTS.map(p => <option key={p}>{p}</option>)}</select> : <span className="muted">All</span>}</td>
                <td>{when(u.last_login)}</td>
                <td><span className={"us-pill " + (u.active ? "on" : "")}>{u.active ? "Active" : "Switched off"}</span></td>
                <td><div className="us-acts">
                  <button className="lnk" onClick={() => setReset({ id: u.id, name: u.name, password: "" })}>Reset password</button>
                  <button className="lnk" onClick={() => patch(u, { active: !u.active }, u.active ? `${u.name} can no longer sign in.` : `${u.name} can sign in again.`)}>{u.active ? "Switch off" : "Switch on"}</button>
                </div></td>
              </tr>))}</tbody>
          </table></div>)}
        {reset && (
          <form className="us-reset" onSubmit={async e => { e.preventDefault(); await patch({ id: reset.id }, { password: reset.password }, `New password set for ${reset.name}. They sign in again.`); setReset(null); }}>
            <label className="sp-field"><small>New password for {reset.name}</small><input type="password" autoComplete="new-password" autoFocus required minLength={8} maxLength={200}
              value={reset.password} onChange={e => setReset({ ...reset, password: e.target.value })} /></label>
            <button className="sp-btn primary">Set password</button><button type="button" className="sp-btn" onClick={() => setReset(null)}>Cancel</button>
          </form>)}
      </section>
      <Team team={team} can plants={PLANTS} reload={loadTeam} onMsg={onMsg} onError={onError} />
    </div>
  );
}

import { useEffect, useState } from "react";
// Signed-in session: the token from /api/auth/login is sent with every call; role and plant come from the person's account on the
// server (the copies here only decide what to show). A 401 means the sign-in ended: onExpired shows the login page again.
const TK = "session.token";
// "Keep me signed in": the token stays in localStorage; otherwise in sessionStorage (gone when the browser closes)
const keep = (t, remember) => { try { localStorage.removeItem(TK); sessionStorage.removeItem(TK); if (t) (remember ? localStorage : sessionStorage).setItem(TK, t); } catch { /* storage may be blocked */ } };
export const auth = { token: (() => { try { return localStorage.getItem(TK) || sessionStorage.getItem(TK) || ""; } catch { return ""; } })(), role: "", user: "", name: "", plant: "", onExpired: null };
export function signIn(token, u, remember = true) { auth.token = token; keep(token, remember); setUser(u); }
export function setUser(u) { Object.assign(auth, { role: u.role, user: u.username, name: u.name, plant: u.plant_id || "" }); }
export function signOut() { auth.token = ""; keep(""); Object.assign(auth, { role: "", user: "", name: "", plant: "" }); }

// Where the API lives: VITE_API_URL at build time (e.g. https://planner-api.onrender.com on Vercel); empty = this site's own /api.
// Opened on this computer (localhost): always this site's own /api, so a local build talks to the local backend.
const LOCAL = ["localhost", "127.0.0.1"].includes(location.hostname);
export const API_BASE = LOCAL ? "" : (import.meta.env.VITE_API_URL || "").replace(/\/+$/, "");

export async function api(path, method = "GET", body, raw) {
  const r = await fetch(API_BASE + path, {
    method,
    headers: { ...(auth.token ? { Authorization: `Bearer ${auth.token}` } : {}), "Content-Type": "application/json" },
    body: body !== undefined ? JSON.stringify(body) : method === "POST" ? "{}" : undefined,
  });
  if (raw && r.ok) return r;
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    const e = new Error(`${j.error?.message || r.statusText} (${j.error?.code || r.status})`);
    e.api = j.error || {};
    e.status = r.status;
    if (r.status === 401 && auth.token && !path.startsWith("/api/auth/login")) { signOut(); auth.onExpired?.(); }
    throw e;
  }
  return j;
}

// Positions. Each sees only its own pages; the server still checks every action (Policy.role_actions).
export const ROLES = [
  { id: "engineer", label: "Engineer" },                        // runs planning day to day: orders, plans, tools; cannot approve
  { id: "production_manager", label: "Production manager" },    // approves plans for both plants
  { id: "plant_head", label: "Production head" },               // leads one plant: sees that plant only
  { id: "admin", label: "Admin" },                              // everything, plus accounts and connections
];
// pages follow the agent's workflow: upload a sheet in Connections -> the agent checks it -> Plan -> Review & approve
// What-if runs re-plans, which only engineers (and admins) may do: the server refuses it for the others
const VIEW = ["Dashboard", "Digital twin", "Connections", "Demand", "Plan", "Review & approve", "Material readiness", "Materials & capacity", "Plan document", "Plan history", "Previous plans"];
const ENG = [...VIEW, "What-if"];
export const PAGES = {
  engineer: ["Overview", ...ENG, "System architecture"],             // Overview is first: the page the app opens on
  production_manager: ["Overview", ...VIEW, "System architecture"],
  plant_head: ["Overview", ...VIEW, "System architecture"],
  admin: ["Overview", ...VIEW, "System architecture", "Users"],                 // sees everything; decides nothing but the data connectors
};
const ACTIONS = { intake: ["engineer", "planner", "production_manager", "plant_head", "admin"], generate: ["engineer", "planner"], export: ["engineer", "planner", "production_manager", "plant_head", "admin"],
  approve: ["production_manager", "planner"], publish: ["production_manager", "planner"] };
export const can = (action, role = auth.role) => (ACTIONS[action] || []).includes(role);


// Live updates: one server-sent-events connection per tab (/api/stream) instead of several pages polling. fetch() so the sign-in
// token goes in a header (never in the URL). When it drops it reconnects with back-off; until then callers fall back to polling.
const liveSubs = new Set();
export const live = { up: false, last: null };
let liveTimer = null, liveBusy = false, liveWait = 1000;
async function liveRun() {
  if (liveBusy || !auth.token) return;
  liveBusy = true;
  try {
    const r = await fetch(API_BASE + "/api/stream", { headers: { Authorization: `Bearer ${auth.token}` } });
    if (!r.ok || !r.body) throw new Error(String(r.status));
    live.up = true; liveWait = 1000;
    const rd = r.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await rd.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const msg = buf.slice(0, i); buf = buf.slice(i + 2);
        const data = msg.split("\n").filter(l => l.startsWith("data: ")).map(l => l.slice(6)).join("");
        if (data) { try { live.last = JSON.parse(data); liveSubs.forEach(f => f(live.last)); } catch { /* ignore a bad message */ } }
      }
    }
  } catch { /* reconnect below */ }
  live.up = false; liveBusy = false;
  if (liveSubs.size) { clearTimeout(liveTimer); liveTimer = setTimeout(liveRun, liveWait); liveWait = Math.min(30000, liveWait * 2); }
}
export function onLive(fn) {
  liveSubs.add(fn);
  if (live.last) fn(live.last);
  liveRun();
  return () => liveSubs.delete(fn);
}

// Has the planning agent finished a run since the app started? Until it has, pages show no plan and no warnings.
let ranState = null;
const ranSubs = new Set();
export async function refreshRan() {
  try { ranState = await api("/api/orders/agent-ran"); ranSubs.forEach(f => f(ranState)); } catch { /* keep the last answer */ }
}
export function useAgentRan() {
  const [v, setV] = useState(ranState);
  useEffect(() => {
    ranSubs.add(setV);
    if (!ranState) refreshRan();
    const off = onLive(m => { if (!ranState || ranState.ran !== m.ran) refreshRan(); });     // pushed when it changes
    const t = setInterval(() => { if (!live.up) refreshRan(); }, 20000);                 // polling only while the stream is down
    return () => { ranSubs.delete(setV); clearInterval(t); off(); };
  }, []);
  return v ? v.ran : null;                              // null while asking
}

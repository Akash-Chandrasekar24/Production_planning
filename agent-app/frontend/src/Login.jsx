import { useState } from "react";
import { api, signIn } from "./api.js";
import { GwcMark } from "./planner/Brand.jsx";
import { Icon } from "./planner/widgets.jsx";
import PlantArt from "./PlantArt.jsx";

// Manufacturing mark: a factory with a turning gear. Theme tokens only.
export function MfgMark({ size = 44 }) {
  return (
    <svg viewBox="0 0 64 64" width={size} height={size} role="img" aria-label="Manufacturing">
      <path d="M6 54V30l12 8v-8l12 8v-8l12 8V14h10v40z" style={{ fill: "var(--brand-blue-soft)", stroke: "var(--brand-blue)" }} strokeWidth="2.5" strokeLinejoin="round" />
      <path d="M44 14V8h6v6" style={{ stroke: "var(--brand-blue)" }} fill="none" strokeWidth="2.5" strokeLinejoin="round" />
      <path d="M12 46h6M24 46h6M36 46h6" style={{ stroke: "var(--brand-purple)" }} strokeWidth="3" strokeLinecap="round" />
      <g className="lg-gear" style={{ transformOrigin: "50px 46px" }}>
        <circle cx="50" cy="46" r="7" style={{ fill: "var(--brand-orange)" }} />
        <path d="M50 35v4M50 53v4M39 46h4M57 46h4M42.2 38.2l2.8 2.8M55 51l2.8 2.8M42.2 53.8L45 51M55 41l2.8-2.8" style={{ stroke: "var(--brand-orange)" }} strokeWidth="3" strokeLinecap="round" />
        <circle cx="50" cy="46" r="2.6" style={{ fill: "var(--brand-white)" }} />
      </g>
    </svg>
  );
}

const FEATURES = [
  ["sparkle", "Every purchase order checked", "The agent tests new orders against lines, crews and parts the moment they arrive."],
  ["trend", "A plan with dates you can trust", "See when all production finishes, and when the new orders will be ready."],
  ["lock", "Approve once, orders freeze", "Approved orders are locked, so a later upload can't change them."],
];
const FLOW = [["upload", "Purchase order"], ["sparkle", "Agent check"], ["calendar", "Plan"], ["check", "Approve"]];

export default function Login({ onIn, expired }) {
  const [v, setV] = useState({ username: "", password: "" });
  const [remember, setRemember] = useState(true);
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(expired ? "Your session ended. Please sign in again." : "");
  const submit = async e => {
    e.preventDefault();
    setBusy(true); setErr("");
    try {
      const r = await api("/api/auth/login", "POST", v);
      signIn(r.token, r.user, remember);
      onIn(r.user);
    } catch (x) {
      setErr(x.status === 401 ? "That username and password don't match." : x.status === 429 ? "Too many wrong passwords. Please wait a few minutes and try again."
        : "We couldn't sign you in right now. Please try again.");
    } finally { setBusy(false); }
  };
  return (
    <main className="lg-page">
      <section className="lg-side">
        <div className="lg-logos">
          <span className="lg-brand"><GwcMark size={36} /><span><b>GWC DATA.AI</b><small>Production Planning</small></span></span>
          <span className="lg-sep" aria-hidden="true" />
          <MfgMark size={36} />
        </div>
        <div className="lg-body">
          <form className="lg-form" onSubmit={submit} aria-describedby={err ? "lg-err" : undefined}>
            <span className="lg-eyebrow"><i aria-hidden="true" /> AI-assisted production planning</span>
            <h1>SIGN IN</h1>
            <p className="lg-title">Welcome back</p>
            <p className="lg-lead">One place to plan the Hosur and Bengaluru plants. Sign in with the account your administrator gave you.</p>
            <label className="lg-field"><span>Username</span>
              <span className="lg-in"><Icon n="users" size={16} />
                <input autoFocus autoComplete="username" value={v.username} required maxLength={64} onChange={e => setV({ ...v, username: e.target.value })} /></span></label>
            <label className="lg-field"><span>Password</span>
              <span className="lg-in lg-pw"><Icon n="lock" size={16} />
                <input type={show ? "text" : "password"} autoComplete="current-password" value={v.password} required maxLength={200}
                  onChange={e => setV({ ...v, password: e.target.value })} />
                <button type="button" className="lnk" onClick={() => setShow(s => !s)} aria-pressed={show}>{show ? "Hide" : "Show"}</button>
              </span></label>
            <label className="lg-check"><input type="checkbox" checked={remember} onChange={e => setRemember(e.target.checked)} /> Keep me signed in on this computer</label>
            {err && <p id="lg-err" className="lg-err" role="alert"><Icon n="alert" size={14} /> {err}</p>}
            <button className="lg-go" disabled={busy}><span>{busy ? "Signing in…" : "Sign in"}</span>{!busy && <Icon n="arrow" size={16} />}</button>
          </form>
          <ul className="lg-feats">{FEATURES.map(([ic, t, d]) => (
            <li key={t}><span className="lg-feat-ic"><Icon n={ic} size={16} /></span><span><b>{t}</b><small>{d}</small></span></li>))}
          </ul>
        </div>
        <p className="lg-foot">Need an account or forgot your password? <span>Ask your administrator.</span></p>
      </section>
      <section className="lg-art">
        <div className="lg-hud">
          <span className="lg-chip"><i aria-hidden="true" /> Hosur · IN-TN-01</span>
          <span className="lg-chip"><i aria-hidden="true" /> Bengaluru · IN-KA-01</span>
        </div>
        <PlantArt />
        <ol className="lg-flow" aria-label="How planning works">{FLOW.map(([ic, t], i) => (
          <li key={t} style={{ "--i": i }}><span><Icon n={ic} size={14} /></span>{t}</li>))}
        </ol>
        <span className="lg-scan" aria-hidden="true" />
      </section>
    </main>
  );
}

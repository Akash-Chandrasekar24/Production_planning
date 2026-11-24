import { Component, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import "./theme.css";
import "./app.css";
import { PAGES, ROLES, api, auth, live, onLive, setUser as setAuthUser, signOut } from "./api.js";
import Login from "./Login.jsx";
import Planner from "./Planner.jsx";
import { Icon } from "./planner/widgets.jsx";
import { Segmented } from "./planner/Filters.jsx";
import { BrandLockup, GwcMark } from "./planner/Brand.jsx";
import AssistantFab from "./planner/AssistantFab.jsx";

// The sidebar: one entry per page, in this order. A page that holds several views shows them as tabs at its top: [view, tab label].
const NAV = [
  ["Overview", "chart", [["Overview", "Overview"]]],
  ["Dashboard", "home", [["Dashboard", "Dashboard"]]],
  ["Connections", "upload", [["Connections", "Connections"]]],
  ["Materials", "box", [["Demand", "Demand"], ["Material readiness", "Material readiness"], ["Materials & capacity", "Materials & capacity"]]],
  ["Plan", "calendar", [["Plan", "Current plan"], ["Plan history", "Plan history"], ["Plan document", "Plan document"], ["What-if", "What-if"]]],
  ["Approvals", "check", [["Review & approve", "Review & approve"], ["Previous plans", "Previous plans"]]],
  ["Digital twin", "factory", [["Digital twin", "Digital twin"]]],
  ["System architecture", "grid", [["System architecture", "System architecture"]]],
  ["Users", "users", [["Users", "Users"]]],
];
const VIEWS = NAV.flatMap(([, , items]) => items.map(([v]) => v));
const pageOf = v => NAV.find(([, , items]) => items.some(([x]) => x === v));
const fromHash = () => decodeURIComponent(location.hash.slice(1)).replace(/^Planner\//, "");
const allowed = (role, v) => (VIEWS.includes(v) && PAGES[role].includes(v) ? v : PAGES[role][0]);   // any other page opens the role's first page
const store = (k, v) => { try { localStorage.setItem(k, v); } catch { /* storage may be blocked */ } };
const read = (k, d) => { try { return localStorage.getItem(k) ?? d; } catch { return d; } };

// Sidebar: one highlight glides to the hovered/focused item, and one marks the current page (it slides when the page changes).
// Collapsed = icon rail with tooltips (hover and keyboard focus). On small screens the sidebar is a drawer.
function SideNav({ view, go, rail, pending, theme, toggleTheme, role, ran }) {
  const listRef = useRef();
  const [hov, setHov] = useState(null);          // {y, h} of the hovered item, null = hidden (keeps last place so it glides)
  const [last, setLast] = useState({ y: 0, h: 40 });
  const [act, setAct] = useState(null);

  const measure = el => ({ y: el.offsetTop, h: el.offsetHeight });
  const place = useCallback(() => {
    const el = listRef.current?.querySelector(".sp-nav.on");
    setAct(el ? measure(el) : null);
  }, []);
  useLayoutEffect(place, [view, rail, place]);
  useEffect(() => {
    const ro = new ResizeObserver(place);
    if (listRef.current) ro.observe(listRef.current);
    return () => ro.disconnect();
  }, [place]);

  const track = e => {
    const el = e.target.closest?.(".sp-nav");
    if (el && listRef.current?.contains(el)) { const m = measure(el); setHov(m); setLast(m); }
  };
  const item = (name, ic, views, extra) => {
    const on = views.includes(view);
    return (
      <button key={name} className={"sp-nav" + (on ? " on" : "")} aria-current={on ? "page" : undefined} data-tip={name}
        aria-label={rail ? name : undefined} onClick={() => go(on ? view : views[0])}>
        <span className="sp-nav-ic"><Icon n={ic} size={18} /></span><span className="sp-nav-t">{name}</span>{extra}
      </button>
    );
  };
  const pos = hov || last;
  return (
    <>
      <nav className="sp-navlist" ref={listRef} aria-label="Pages" onMouseOver={track} onFocus={track}
        onMouseLeave={() => setHov(null)} onBlur={e => { if (!listRef.current?.contains(e.relatedTarget)) setHov(null); }}>
        <span className={"sp-glide sp-glide-hover" + (hov ? " show" : "")} style={{ transform: `translateY(${pos.y}px)`, height: pos.h }} aria-hidden="true" />
        {act && <span className="sp-glide sp-glide-active" style={{ transform: `translateY(${act.y}px)`, height: act.h }} aria-hidden="true" />}
        <div className="sp-navgroup">
          {NAV.map(([name, ic, items]) => [name, ic, items.map(([v]) => v).filter(v => PAGES[role].includes(v))]).filter(([, , vs]) => vs.length)
            .map(([name, ic, vs]) => item(name, ic, vs, name === "Approvals" && pending > 0
              ? <em className="sp-badge" aria-label={`${pending} awaiting review`}>{pending}</em> : null))}
        </div>
      </nav>
      <div className="sp-art" aria-hidden="true"><img src="/art/eco-factory.webp" alt="" loading="lazy" decoding="async" /></div>
      <div className="sp-side-foot">
        <button className="sp-nav" data-tip={theme === "light" ? "Dark mode" : "Light mode"} aria-label={rail ? (theme === "light" ? "Dark mode" : "Light mode") : undefined} onClick={toggleTheme}>
          <span className="sp-nav-ic"><Icon n={theme === "light" ? "moon" : "sun"} size={18} /></span><span className="sp-nav-t">{theme === "light" ? "Dark" : "Light"} mode</span></button>
        {PAGES[role].includes("Review & approve") && ran && <div className="sp-promo">
          <span className="sp-promo-ic"><Icon n="check" size={18} /></span>
          <b>{pending ? `${pending} plan${pending > 1 ? "s" : ""} awaiting review` : "No plans awaiting review"}</b>
          <small>{pending ? (role === "production_manager" ? "Review and approve a plan before it is released." : "The production manager reviews and approves plans.") : "New plans will appear here."}</small>
          <button onClick={() => go("Review & approve")}>Open review <Icon n="arrow" size={14} /></button>
        </div>}
      </div>
    </>
  );
}

const ago = v => {
  const s = (Date.now() - new Date(String(v).endsWith("Z") ? v : v + "Z")) / 1000;
  return s < 60 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
};
const SEV = { critical: "bad", warn: "warn", info: "info" };

function Inbox({ inbox, onClose, onOpen, reload }) {
  const items = inbox?.items || [];
  return (
    <div className="sp-menu sp-inbox" role="dialog" aria-label="Notifications">
      <header className="sp-row"><b>Notifications</b>
        {inbox?.unread > 0 && <button className="lnk" onClick={async () => { try { await api("/api/notifications/read-all", "POST"); } catch { /* try again later */ } reload(); }}>Mark all as read</button>}
        <button className="sp-icon-btn" aria-label="Close notifications" onClick={onClose}><Icon n="close" size={14} /></button></header>
      {!inbox ? <p className="muted" role="status">Loading…</p> : !items.length ? <p className="muted">Nothing for you right now. Tasks and alerts from the agents appear here.</p> : (
        <ul>{items.map(n => (
          <li key={n.id}><button className={"sp-note t-" + (SEV[n.severity] || "info") + (n.read ? "" : " unread")} onClick={() => onOpen(n)}>
            <span className="sp-bullet" />
            <span><b>{n.title}</b>{n.body && <small>{n.body.split("\n")[0]}</small>}
              <small className="muted">{ago(n.created_at)}{n.member ? ` · for ${n.member}` : ""}{n.email_status === "SENT" ? " · emailed" : n.email_status === "FAILED" ? " · email failed" : ""}</small></span>
          </button></li>))}</ul>)}
      <footer className="sp-row">
        <small className="muted">{inbox?.email ? "Also sent by email to people with an address." : "Email isn't set up yet: notifications show here only."}</small>
      </footer>
    </div>
  );
}

// Sign-in gate: with a saved token, check it; otherwise (or when it has ended) show the login page.
// A page that fails to draw shows a message instead of a blank screen, and the error goes to the server log.
class Guard extends Component {
  state = { err: null };
  static getDerivedStateFromError(err) { return { err }; }
  componentDidCatch(err, info) {
    api("/api/client-error", "POST", { page: this.props.page, message: String(err?.message || err).slice(0, 500),
      stack: String(err?.stack || "").slice(0, 2000) + "\n" + String(info?.componentStack || "").slice(0, 1500) }).catch(() => {});
  }
  componentDidUpdate(prev) { if (prev.page !== this.props.page && this.state.err) this.setState({ err: null }); }
  render() {
    if (!this.state.err) return this.props.children;
    return (
      <div className="page"><div className="sp-card" role="alert">
        <h3>This page could not be shown</h3>
        <p className="muted">Something went wrong while drawing it. The other pages still work, and the problem has been recorded.</p>
        <button className="sp-btn primary" onClick={() => location.reload()}>Reload the page</button>
      </div></div>);
  }
}

export default function App() {
  const [me, setMe] = useState(auth.token ? undefined : null);          // undefined = checking, null = signed out
  const [expired, setExpired] = useState(false);
  useEffect(() => {
    auth.onExpired = () => { setExpired(true); setMe(null); };
    if (auth.token) api("/api/auth/me").then(u => { setAuthUser(u); setMe(u); }).catch(() => { signOut(); setMe(null); });
  }, []);
  useEffect(() => { document.documentElement.dataset.theme = read("theme", "light"); }, []);
  if (me === undefined) return <div className="lg-wait" role="status">Signing you in…</div>;
  if (!me) return <Login expired={expired} onIn={u => { setExpired(false); setMe(u); }} />;
  return <Shell me={me} onSignOut={async () => { try { await api("/api/auth/logout", "POST"); } catch { /* signed out here anyway */ } signOut(); setMe(null); }} />;
}

function Shell({ me, onSignOut }) {
  const [theme, setTheme] = useState(() => read("theme", "light"));
  const [nav, setNav] = useState(() => (read("nav2", "open") === "open" ? "open" : "rail"));
  const [drawer, setDrawer] = useState(false);       // small screens only
  const role = me.role, plant = me.plant_id || "";
  const [view, setView] = useState(() => allowed(role, fromHash()));
  const [inbox, setInbox] = useState(null);           // {items, unread, email}
  const [bell, setBell] = useState(false);
  const bellRef = useRef();
  const [q, setQ] = useState("");
  const [jump, setJump] = useState(null);
  const [meta, setMeta] = useState({ pending: 0, notes: 0 });
  const [menu, setMenu] = useState(false);
  const [scrolled, setScrolled] = useState(false);
  const searchRef = useRef();
  const meRef = useRef();
  const rail = nav === "rail";


  // the bell: my notifications from the agents (also sent by email when set up)
  const pullInbox = useCallback(() => api("/api/notifications?limit=30").then(setInbox).catch(() => {}), []);
  useEffect(() => {
    setInbox(null);
    pullInbox();
    let seen = null;
    const off = onLive(m => { const k = `${m.unread}|${m.newest}`; if (seen !== null && k !== seen) pullInbox(); seen = k; });   // new or read: refresh
    const t = setInterval(() => { if (!live.up) pullInbox(); }, 30000);                                                       // fallback
    return () => { clearInterval(t); off(); };
  }, [role, plant, pullInbox]);

  useEffect(() => { document.documentElement.dataset.theme = theme; store("theme", theme); }, [theme]);
  useEffect(() => { store("nav2", nav); }, [nav]);
  useEffect(() => { history.replaceState(null, "", "#" + encodeURIComponent(view)); }, [view]);
  useEffect(() => {
    const k = e => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); searchRef.current?.focus(); }
      if (e.key === "Escape") { setDrawer(false); setMenu(false); }
    };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, []);
  useEffect(() => {                      // close the profile menu / notifications on an outside click
    if (!menu && !bell) return;
    const off = e => { if (!meRef.current?.contains(e.target)) setMenu(false); if (!bellRef.current?.contains(e.target)) setBell(false); };
    document.addEventListener("pointerdown", off);
    return () => document.removeEventListener("pointerdown", off);
  }, [menu, bell]);

  const go = v => { setView(allowed(role, v)); setDrawer(false); };
  const toggleTheme = () => setTheme(t => (t === "light" ? "dark" : "light"));
  const initials = (me.name || me.username || "?").split(/\s+/).map(w => w[0]).join("").slice(0, 2).toUpperCase();

  return (
    <div className={"app sp-app" + (rail ? " rail" : "") + (drawer ? " drawer-open" : "")}>
      <aside className="side sp-side" aria-label="Main navigation">
        <div className="sp-brand">
          {rail ? <GwcMark size={34} /> : <BrandLockup />}
          <button className="sp-icon-btn sp-collapse" aria-label={rail ? "Expand navigation" : "Collapse navigation"} aria-expanded={!rail}
            title={rail ? "Expand" : "Collapse"} onClick={() => setNav(rail ? "open" : "rail")}><Icon n={rail ? "right" : "left"} size={16} /></button>
          <button className="sp-icon-btn sp-drawer-x" aria-label="Close navigation" onClick={() => setDrawer(false)}><Icon n="close" size={16} /></button>
        </div>
        <SideNav view={view} go={go} rail={rail} pending={meta.pending} theme={theme} toggleTheme={toggleTheme} role={role} ran={meta.ran} />
      </aside>
      <div className="sp-scrim" onClick={() => setDrawer(false)} aria-hidden="true" />
      <section className="main" onScrollCapture={e => { if (e.target.classList?.contains("page")) setScrolled(e.target.scrollTop > 4); }}>
        <header className={"sp-top" + (scrolled ? " scrolled" : "")}>
          <button className="sp-icon-btn sp-menu-btn" aria-label="Open navigation" aria-expanded={drawer} onClick={() => setDrawer(true)}><Icon n="menu" size={18} /></button>
          <div className="sp-title"><b>GWC Data.AI</b><span className="sp-title-x" aria-hidden="true">×</span><span>Production Planning Agentic System</span></div>
          <div className="sp-top-r">
            <form className="sp-search" role="search" onSubmit={e => { e.preventDefault(); if (q.trim()) setJump({ order: q.trim(), t: Date.now() }); }}>
              <Icon n="search" size={16} />
              <input ref={searchRef} value={q} onChange={e => setQ(e.target.value)} placeholder="Search an order…" aria-label="Search an order" />
              <kbd>Ctrl K</kbd>
            </form>
            <button className="sp-icon-btn" aria-label={theme === "light" ? "Dark mode" : "Light mode"} title={theme === "light" ? "Dark mode" : "Light mode"} onClick={toggleTheme}>
              <Icon n={theme === "light" ? "sun" : "moon"} size={18} /></button>
            <div className="sp-me" ref={bellRef}>
              <button className="sp-icon-btn sp-bell" aria-haspopup="dialog" aria-expanded={bell} onClick={() => setBell(b => !b)}
                aria-label={inbox?.unread ? `${inbox.unread} unread notification${inbox.unread > 1 ? "s" : ""}` : "Notifications"} title="Notifications">
                <Icon n="bell" size={18} />{meta.ran && inbox?.unread > 0 ? <em className="sp-bell-n">{inbox.unread > 99 ? "99+" : inbox.unread}</em> : null}
              </button>
              {bell && <Inbox inbox={inbox} onClose={() => setBell(false)} reload={pullInbox}
                onOpen={async n => { setBell(false); if (!n.read) { try { await api(`/api/notifications/${n.id}/read`, "POST"); } catch { /* still open it */ } pullInbox(); } go(VIEWS.includes(n.link) ? n.link : "Review & approve"); }} />}
            </div>
            <div className="sp-me" ref={meRef}>
              <button className="sp-avatar" aria-label="Your profile" aria-haspopup="dialog" aria-expanded={menu} onClick={() => setMenu(m => !m)}>{initials}</button>
              {menu && (
                <div className="sp-menu" role="dialog" aria-label="Profile">
                  <div className="sp-who"><b>{me.name}</b><small className="muted">{me.username}</small>
                    <span className="sp-who-role">{(ROLES.find(r => r.id === role) || {}).label || role}{plant ? ` · ${plant}` : ""}</span></div>
                  <button className="sp-btn" onClick={onSignOut}><Icon n="lock" size={14} /> Sign out</button>
                </div>
              )}
            </div>
          </div>
        </header>
        <SubTabs view={view} role={role} go={go} />
        <Guard page={view}><Planner role={role} tab={view} setTab={go} jump={jump} onMeta={setMeta} /></Guard>
      </section>
      <AssistantFab />
    </div>
  );
}

// The views inside the current page, as tabs (only when the page has more than one for this role)
function SubTabs({ view, role, go }) {
  const pg = pageOf(view);
  const tabs = (pg ? pg[2] : []).filter(([v]) => PAGES[role].includes(v));
  if (tabs.length < 2) return null;
  return (
    <div className="sp-subtabs"><span className="sp-subtabs-t">{pg[0]}</span>
      <Segmented label={pg[0]} value={view} onChange={go} options={tabs.map(([v, label]) => ({ value: v, label }))} /></div>);
}

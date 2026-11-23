import { useEffect, useRef, useState } from "react";
import { BorderBeam } from "border-beam";
import { api } from "../api.js";
import { BurstLogo } from "./AssistantCard.jsx";
import { IDEAS } from "./Assistant.jsx";
import { Icon } from "./widgets.jsx";
import AnswerChart, { parseChartLine } from "./AnswerChart.jsx";

// The AI assistant, on every page: a button in the bottom-right corner (the sunburst logo, with its hover turn and glow) opens a
// floating chat window. The conversation stays while the window is closed; "New chat" starts over.
function usePageTheme() {
  const get = () => (document.documentElement.dataset.theme === "dark" ? "dark" : "light");
  const [t, setT] = useState(get);
  useEffect(() => {
    const m = new MutationObserver(() => setT(get()));
    m.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => m.disconnect();
  }, []);
  return t;
}

// The assistant answers in light markdown: paragraphs, bullet lists, **bold** and tables. Rendered as plain elements (no HTML injected).
const bold = t => t.split(/(\*\*[^*]+\*\*)/g).map((p, i) => (p.startsWith("**") && p.endsWith("**") ? <b key={i}>{p.slice(2, -2)}</b> : p));
const cells = l => l.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
function Md({ text }) {
  const lines = String(text || "").split(/\r?\n/), out = [];
  for (let i = 0; i < lines.length;) {
    const l = lines[i];
    if (l.trim().startsWith("|")) {
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) { if (!/^\s*\|?\s*:?-{2,}/.test(lines[i])) rows.push(cells(lines[i])); i++; }
      // the assistant names the chart on the line after the table: [chart: line | y: Units | title: ...]
      let j = i; while (j < lines.length && !lines[j].trim()) j++;
      const spec = j < lines.length ? parseChartLine(lines[j]) : null;
      if (spec) i = j + 1;
      if (rows.length > 2) out.push(<AnswerChart key={"c" + i} head={rows[0]} rows={rows.slice(1)} spec={spec} />);
      out.push(<div key={i} className="ai-tbl"><table><thead><tr>{rows[0].map((c, j) => <th key={j}>{bold(c)}</th>)}</tr></thead>
        <tbody>{rows.slice(1).map((r, k) => <tr key={k}>{r.map((c, j) => <td key={j}>{bold(c)}</td>)}</tr>)}</tbody></table></div>);
    } else if (/^\s*[-*•]\s+/.test(l)) {
      const items = [];
      while (i < lines.length && /^\s*[-*•]\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*[-*•]\s+/, "")); i++; }
      out.push(<ul key={i}>{items.map((t, k) => <li key={k}>{bold(t)}</li>)}</ul>);
    } else {
      if (l.trim() && !parseChartLine(l)) out.push(<p key={i}>{bold(l.replace(/^#+\s*/, ""))}</p>);   // a stray chart line is never shown as text
      i++;
    }
  }
  return <>{out}</>;
}

export default function AssistantFab() {
  const [open, setOpen] = useState(false);
  const [msgs, setMsgs] = useState([]);              // [{q, a?, warnings?, error?, pending?}]
  const [q, setQ] = useState("");
  const [cid, setCid] = useState(null);
  const [focus, setFocus] = useState(false);
  const busy = msgs.some(m => m.pending);
  const theme = usePageTheme();
  const list = useRef(), input = useRef(), btn = useRef();

  useEffect(() => { if (open) setTimeout(() => input.current?.focus(), 60); }, [open]);
  useEffect(() => { list.current?.scrollTo({ top: list.current.scrollHeight, behavior: "smooth" }); }, [msgs]);
  useEffect(() => {
    if (!open) return;
    const k = e => { if (e.key === "Escape") { setOpen(false); btn.current?.focus(); } };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [open]);

  async function ask(text) {
    const message = (text ?? q).trim();
    if (!message || busy) return;
    setQ("");
    setMsgs(m => [...m, { q: message, pending: true }]);
    try {
      const r = await api("/api/chat", "POST", { conversation_id: cid, message });
      setCid(r.conversation_id);
      setMsgs(m => m.map((x, i) => (i === m.length - 1 ? { q: message, a: r.answer, warnings: r.warnings } : x)));
    } catch {
      setMsgs(m => m.map((x, i) => (i === m.length - 1 ? { q: message, error: "The assistant isn't available right now. Please try again later." } : x)));
    }
  }

  return (
    <>
      <button ref={btn} className={"ai-fab" + (open ? " on" : "")} onClick={() => setOpen(o => !o)} aria-haspopup="dialog" aria-expanded={open}
        aria-label={open ? "Close the AI assistant" : "Open the AI assistant"} title="AI assistant">
        <BurstLogo busy={busy} size={40} />
      </button>
      {open && (
        <section className="ai-win" role="dialog" aria-label="AI assistant" aria-modal="false">
          <header className="ai-win-h">
            <BurstLogo busy={busy} size={26} />
            <div><b>AI assistant</b><small className="muted">Orders, materials, capacity and the plan, in plain language</small></div>
            {msgs.length > 0 && <button className="sp-btn ai-new" onClick={() => { setMsgs([]); setCid(null); }} disabled={busy}>New chat</button>}
            <button className="sp-icon-btn" aria-label="Close" onClick={() => setOpen(false)}><Icon n="close" size={16} /></button>
          </header>
          <div className="ai-msgs" ref={list} aria-live="polite">
            {!msgs.length && (
              <div className="ai-hello">
                <p>Ask me anything about production. For example:</p>
                <div className="ai-ideas">{IDEAS.map(i => <button key={i} onClick={() => ask(i)}>{i}</button>)}</div>
              </div>
            )}
            {msgs.map((m, i) => (
              <div key={i} className="ai-turn">
                <p className="ai-q">{m.q}</p>
                <div className="ai-a">
                  {m.pending ? <p className="muted ai-typing"><i /><i /><i /> Looking into it… this can take up to half a minute.</p>
                    : m.error ? <p className="bad">{m.error}</p> : <Md text={m.a} />}
                  {m.warnings?.map((w, k) => <div key={k} className="warnnote">{w}</div>)}
                </div>
              </div>
            ))}
          </div>
          <BorderBeam size="md" colorVariant="ocean" theme={theme} strength={0.7} active={focus || busy} className="sp-beam ai-beam">
            <form className="sp-ask" onSubmit={e => { e.preventDefault(); ask(); }}>
              <Icon n="help" size={16} />
              <input ref={input} value={q} onChange={e => setQ(e.target.value)} onFocus={() => setFocus(true)} onBlur={() => setFocus(false)}
                maxLength={2000} placeholder="Ask me anything…" aria-label="Ask the assistant" />
              <button type="submit" disabled={busy || !q.trim()} aria-label="Ask"><Icon n="arrow" size={16} /></button>
            </form>
          </BorderBeam>
        </section>
      )}
    </>
  );
}

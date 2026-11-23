import { useEffect, useState } from "react";
import { BorderBeam } from "border-beam";
import { IDEAS, useAsk } from "./Assistant.jsx";
import { Icon } from "./widgets.jsx";

// Sunburst logo for the assistant. Colour comes from the theme (orange); hover: spring turn + thicker rays + glow; spins while working.
const RAYS = [[-78, 40], [-48, 44], [-18, 36], [12, 46], [40, 38], [68, 43], [98, 39], [128, 45], [158, 37], [190, 44], [222, 40], [252, 42]];
export function BurstLogo({ busy = false, size = 72 }) {
  return (
    <svg className={"sp-burst" + (busy ? " busy" : "")} viewBox="-50 -50 100 100" width={size} height={size} role="img" aria-label="AI assistant">
      <g>
        {RAYS.map(([deg, len], i) => {
          const r = (deg * Math.PI) / 180;
          return <line key={i} x1="0" y1="0" x2={Math.cos(r) * len} y2={Math.sin(r) * len} strokeLinecap="round" />;
        })}
        <circle r="14" />
      </g>
    </svg>
  );
}

// Follows the app's light/dark switch (set on <html data-theme>), so the beam matches the page.
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

export default function AssistantCard() {
  const a = useAsk(), r = a.res, theme = usePageTheme();
  const [focus, setFocus] = useState(false);
  const send = text => { if ((text ?? a.q).trim() && !a.busy) a.ask(text); };

  return (
    <div className="sp-ai">
      {r ? (
        <div className="sp-ai-ans" aria-live="polite" key={r.question + (r.pending ? "…" : "")}>
          <div className="sp-row sp-left"><BurstLogo busy={!!r.pending} size={28} /><small className="muted">You asked: {r.question}</small></div>
          {r.pending ? <p className="muted">Looking into it… this can take up to half a minute.</p> : r.error ? <p className="bad">{r.error}</p> : <p>{r.answer}</p>}
          {r.warnings?.map((w, i) => <div key={i} className="warnnote">{w}</div>)}
          {!r.pending && <button className="lnk" onClick={a.clear}>New question</button>}
        </div>
      ) : (
        <>
          <div className="sp-orb"><BurstLogo busy={a.busy} /></div>
          <div className="sp-ideas">{IDEAS.map(i => <button key={i} disabled={a.busy} onClick={() => send(i)}>{i}</button>)}</div>
        </>
      )}
      {/* the beam rides the chat box border while you type or while the assistant is working; it rests otherwise */}
      <BorderBeam size="md" colorVariant="ocean" theme={theme} strength={0.7} active={focus || a.busy} className="sp-beam">
        <form className="sp-ask" onSubmit={e => { e.preventDefault(); send(); }}>
          <Icon n="help" size={16} />
          <input value={a.q} onChange={e => a.setQ(e.target.value)} onFocus={() => setFocus(true)} onBlur={() => setFocus(false)}
                 maxLength={2000} placeholder="Ask me anything…" aria-label="Ask the assistant" />
          <button type="submit" disabled={a.busy || !a.q.trim()} aria-label="Ask"><Icon n="arrow" size={16} /></button>
        </form>
      </BorderBeam>
    </div>
  );
}

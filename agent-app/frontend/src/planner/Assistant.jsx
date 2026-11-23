import { useState } from "react";
import { api } from "../api.js";

export const IDEAS = ["Why are jobs late?", "Which materials are short?", "Open orders per product?", "What if demand doubles?"];

// One question, one answer (no chat window). Follow-ups stay in the same server-side conversation until cleared.
export function useAsk() {
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState(null);       // {question, answer, steps, warnings} | {question, error}
  const [cid, setCid] = useState(null);

  async function ask(text) {
    const message = (text ?? q).trim();
    if (!message || busy) return;
    setBusy(true);
    setRes({ question: message, pending: true });
    try {
      const r = await api("/api/chat", "POST", { conversation_id: cid, message });
      setCid(r.conversation_id);
      setRes({ question: message, answer: r.answer, steps: r.steps, warnings: r.warnings });
      setQ("");
    } catch (e) {
      setRes({ question: message, error: "The assistant isn't available right now. Everything else on the dashboard still works; please try again later." });
    } finally { setBusy(false); }
  }
  return { q, setQ, busy, res, ask, clear: () => { setRes(null); setCid(null); } };
}

export function AnswerCard({ a }) {
  const r = a.res;
  if (!r) return null;
  return (
    <section className="bx-card s12 bx-answer" aria-live="polite">
      <header className="bx-ch">
        <div><h3>Assistant</h3><small>You asked: {r.question}</small></div>
        <button className="bx-circle sm" aria-label="Close answer" onClick={a.clear}>×</button>
      </header>
      {r.pending ? <p className="muted">Looking into it… this can take up to half a minute.</p>
        : r.error ? <p className="bad">{r.error}</p> : <p className="bx-answer-text">{r.answer}</p>}
      {r.warnings?.map((w, i) => <div key={i} className="warnnote">{w}</div>)}
    </section>
  );
}

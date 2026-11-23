import { useLayoutEffect, useRef, useState } from "react";
import { Icon } from "./widgets.jsx";

// Filters used on every page: labelled dropdown pills (native selects, so keyboard and screen readers work), a "Clear all"
// action, and an iOS-style segmented control. A set filter is highlighted so it is clear what the page is showing.

export const nice = s => { const t = String(s ?? "").replace(/_/g, " ").toLowerCase(); return t.charAt(0).toUpperCase() + t.slice(1); };
export const opts = (values, label = nice) => values.map(v => (typeof v === "object" ? v : { value: v, label: label(v) }));

/** fields: [{key, label, all, options:[{value,label}]}] (type: "search" | "date" for free inputs); value: {key: v}; onChange(next). */
export function FilterBar({ fields, value, onChange, right, busy, className = "" }) {
  const set = (k, v) => onChange({ ...value, [k]: v || undefined });
  const active = fields.filter(f => value[f.key]).length;
  return (
    <div className={"sp-filterbar " + className} role="group" aria-label="Filters">
      <span className="sp-filter-ic" aria-hidden="true"><Icon n="filter" size={16} /></span>
      {fields.map(f => {
        if (f.type === "search") {
          return (
            <label key={f.key} className={"sp-fpill sp-fsearch" + (value[f.key] ? " on" : "")}>
              <Icon n="search" size={14} />
              <input value={value[f.key] || ""} placeholder={f.label} aria-label={f.label} onChange={e => set(f.key, e.target.value)} />
            </label>
          );
        }
        if (f.type === "date") {
          return (
            <label key={f.key} className={"sp-fpill" + (value[f.key] ? " on" : "")}>
              <small>{f.label}</small><input type="date" value={value[f.key] || ""} aria-label={f.label} onChange={e => set(f.key, e.target.value)} />
            </label>
          );
        }
        return (
          <label key={f.key} className={"sp-fpill" + (value[f.key] ? " on" : "")}>
            <small>{f.label}</small>
            <select value={value[f.key] || ""} onChange={e => set(f.key, e.target.value)} aria-label={f.label} disabled={f.disabled}>
              <option value="">{f.all || "All"}</option>
              {f.options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            <Icon n="chevron" size={14} />
          </label>
        );
      })}
      {active > 0 && (
        <button className="sp-fclear" onClick={() => onChange({})} aria-label={`Clear ${active} filter${active > 1 ? "s" : ""}`}>
          <Icon n="close" size={12} /> Clear{active > 1 ? ` all (${active})` : ""}
        </button>
      )}
      {busy && <span className="sp-fbusy" role="status" aria-label="Updating" />}
      {right && <div className="sp-filterbar-r">{right}</div>}
    </div>
  );
}

/** iOS-style segmented control: the selected thumb slides to the chosen option. */
export function Segmented({ options, value, onChange, label }) {
  const ref = useRef();
  const [thumb, setThumb] = useState(null);
  useLayoutEffect(() => {
    const el = ref.current?.querySelector('[aria-pressed="true"]');
    if (el) setThumb({ x: el.offsetLeft, w: el.offsetWidth });
  }, [value, options.length]);
  return (
    <div className="sp-seg" ref={ref} role="group" aria-label={label}>
      {thumb && <span className="sp-seg-thumb" style={{ transform: `translateX(${thumb.x}px)`, width: thumb.w }} aria-hidden="true" />}
      {options.map(o => (
        <button key={o.value} aria-pressed={o.value === value} onClick={() => onChange(o.value)}>{o.label}</button>
      ))}
    </div>
  );
}

/** "Plant filter doesn't apply here" note, for cards whose data the active filter cannot narrow. */
export function NotFiltered({ keys }) {
  if (!keys?.length) return null;
  const names = keys.map(k => ({ plant: "plant", line: "line", product: "product", priority: "priority", channel: "channel" }[k] || k));
  return <span className="sp-nofilter" title={`This card's data has no ${names.join(" or ")}, so that filter does not change it.`}>
    <Icon n="filter" size={12} /> Not by {names.join(", ")}</span>;
}

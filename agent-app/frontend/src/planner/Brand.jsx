import { useId } from "react";

// GWC DATA.AI mark (redrawn as SVG from the company logo): four nodes joined by links. Colours are theme tokens.
export function GwcMark({ size = 36 }) {
  const id = useId().replace(/:/g, "");
  return (
    <svg viewBox="0 0 100 100" width={size} height={size} role="img" aria-label="GWC DATA.AI">
      <defs>
        <linearGradient id={`l${id}`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" style={{ stopColor: "var(--brand-purple)" }} /><stop offset="1" style={{ stopColor: "var(--brand-blue)" }} />
        </linearGradient>
        <linearGradient id={`o${id}`} x1="0" y1="1" x2="1" y2="0">
          <stop offset="0" style={{ stopColor: "color-mix(in srgb, var(--brand-orange) 70%, var(--depth-0))" }} /><stop offset="1" style={{ stopColor: "color-mix(in srgb, var(--brand-orange) 70%, var(--brand-white))" }} />
        </linearGradient>
      </defs>
      <g stroke={`url(#l${id})`} strokeWidth="5.5" strokeLinecap="round" fill="none">
        <path d="M16 30 L80 15 M16 30 L44 42 M44 42 L80 15 M44 42 L60 82 M80 15 L60 82" />
      </g>
      <circle cx="16" cy="30" r="12" style={{ fill: "var(--brand-purple)" }} />
      <circle cx="60" cy="82" r="11" style={{ fill: "var(--brand-blue)" }} />
      <circle cx="44" cy="42" r="14" fill={`url(#o${id})`} stroke={`url(#l${id})`} strokeWidth="5" />
      <circle cx="80" cy="15" r="10" fill={`url(#o${id})`} stroke={`url(#l${id})`} strokeWidth="5" />
    </svg>
  );
}

export function BrandLockup() {
  return (
    <div className="sp-brandlock">
      <GwcMark />
      <div><b>GWC DATA.AI</b><small>Production Planning</small></div>
    </div>
  );
}

import { Donut } from "./dzcharts.jsx";
import { K } from "./widgets.jsx";

// The chart for a table in an assistant answer. The assistant picks the chart from CHART_TYPES and writes it on the line after the
// table: [chart: <type> | y: <column>, <column> | title: <title>]. This file draws exactly that chart (no choices for the reader).
// Without such a line it falls back to a sensible default. Pure presentation of the answer's own figures.

export const CHART_TYPES = ["bar", "column", "line", "area", "donut", "pie", "grouped", "stacked"];
const PALETTE = ["var(--chart-c1)", "var(--chart-c3)", "var(--chart-c4)", "var(--brand-orange)", "var(--depth-0)", "var(--brand-blue)", "var(--text-muted)"];
const MULT = { k: 1e3, m: 1e6, b: 1e9, cr: 1e7, l: 1e5, lakh: 1e5 };
const clean = v => String(v ?? "").replace(/\*\*/g, "").trim();

export function num(v) {
  const t = clean(v).replace(/inr|₹|rs\.?/gi, "").replace(/,/g, "").trim();
  const m = t.match(/^(-?\d+(?:\.\d+)?)\s*(%|k|m|b|cr|l|lakh)?\b/i);
  if (!m) return null;
  const x = parseFloat(m[1]), u = (m[2] || "").toLowerCase();
  return u && u !== "%" ? x * MULT[u] : x;
}

// "[chart: line | y: Units, Value | title: Units per week]" -> {type, y: [...], title}
export function parseChartLine(line) {
  const m = String(line).trim().match(/^\[?\s*chart\s*:\s*([a-z-]+)\s*(.*?)\]?\s*$/i);
  if (!m) return null;
  const type = m[1].toLowerCase().replace("horizontal-bar", "bar").replace("vertical-bar", "column");
  if (!CHART_TYPES.includes(type) && type !== "none") return null;
  const part = k => (m[2].match(new RegExp(`\\|\\s*${k}\\s*:\\s*([^|\\]]+)`, "i")) || [])[1]?.trim();
  return { type, y: (part("y") || "").split(",").map(s => s.trim()).filter(Boolean), title: part("title") || "" };
}

export default function AnswerChart({ head, rows, spec }) {
  const codeLike = v => /^\s*\d{4}-(w?\d)|^\s*[a-z]{2,}-[a-z0-9]|^\s*\d{1,2}\s?[a-z]{3}\b/i.test(clean(v));   // dates, weeks, ids: labels, not numbers
  const isNum = j => rows.filter(r => num(r[j]) != null && !codeLike(r[j])).length >= Math.max(2, rows.length * 0.8);
  const numCols = head.map((h, j) => ({ h: clean(h), j })).slice(1).filter(({ j }) => isNum(j));
  const textCols = head.map((_, j) => j).filter(j => j === 0 || !isNum(j)).slice(0, 2);
  const lab = r => textCols.map(j => clean(r[j])).filter(Boolean).join(" · ");
  if (!numCols.length || rows.length < 2 || spec?.type === "none") return null;
  // the measures the assistant asked for (matched by column name), else the first number column
  const want = (spec?.y || []).map(y => numCols.find(c => c.h.toLowerCase() === y.toLowerCase()) || numCols.find(c => c.h.toLowerCase().includes(y.toLowerCase()))).filter(Boolean);
  const series = (want.length ? want : numCols.slice(0, 1)).slice(0, 4);
  const timeLike = rows.length >= 4 && rows.every(r => /\d{1,2}\s?[a-z]{3}|\d{4}-\d{2}|\d{4}-w\d|week|^q\d|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec/i.test(lab(r)));
  let type = spec?.type && CHART_TYPES.includes(spec.type) ? spec.type : series.length > 1 ? "grouped" : timeLike ? "line" : rows.length <= 8 ? "column" : "bar";
  const labels = rows.map(lab);
  const data = series.map(s => rows.map(r => num(r[s.j]) ?? 0));
  if ((type === "donut" || type === "pie") && (data[0].some(v => v < 0) || rows.length > 8)) type = "bar";      // shares need positive parts
  if ((type === "grouped" || type === "stacked") && series.length < 2) type = "column";
  const title = spec?.title || (series.length > 1 ? series.map(s => s.h).join(" and ") : series[0].h) + " by " + textCols.map(j => clean(head[j]).toLowerCase()).join(" and ");
  const one = labels.map((l, i) => ({ label: l, v: data[0][i] }));
  return (
    <figure className="ai-chart" aria-label={title}>
      <figcaption>{title}</figcaption>
      {type === "bar" ? <Bars pts={one} />
        : type === "column" ? <Columns labels={labels} data={[data[0]]} names={[series[0].h]} />
        : type === "line" || type === "area" ? <Line labels={labels} data={data} names={series.map(s => s.h)} fill={type === "area"} />
        : type === "donut" || type === "pie" ? <Donut parts={one.map((p, i) => ({ label: p.label, value: p.v, color: PALETTE[i % PALETTE.length] }))}
            center={type === "donut" ? K(one.reduce((t, p) => t + p.v, 0)) : ""} sub={type === "donut" ? series[0].h.slice(0, 18) : ""} fmt={K} size={120} pie={type === "pie"} />
        : <Columns labels={labels} data={data} names={series.map(s => s.h)} stacked={type === "stacked"} />}
    </figure>
  );
}

const Legend = ({ names }) => names.length > 1 ? (
  <ul className="ai-legend">{names.map((n, i) => <li key={n}><i style={{ background: PALETTE[i % PALETTE.length] }} />{n}</li>)}</ul>) : null;

function Bars({ pts }) {
  const top = Math.max(...pts.map(p => Math.abs(p.v)), 1e-9);
  return (
    <ul className="ai-bars">{pts.map((p, i) => (
      <li key={i}><span className="l" title={p.label}>{p.label}</span>
        <span className="t"><i style={{ width: `${Math.max(1.5, (100 * Math.abs(p.v)) / top)}%`, background: p.v < 0 ? "var(--depth-0)" : PALETTE[0] }} /></span>
        <b>{K(p.v)}</b></li>))}</ul>
  );
}

// vertical columns: one series, several side by side (grouped), or stacked
function Columns({ labels, data, names, stacked = false }) {
  const tot = labels.map((_, i) => data.reduce((t, s) => t + Math.max(0, s[i]), 0));
  const top = Math.max(...(stacked ? tot : data.flat().map(Math.abs)), 1e-9);
  return (
    <div className="ai-cols-wrap">
      <Legend names={names} />
      <div className="ai-cols" role="img" aria-label={labels.map((l, i) => `${l}: ${data.map((s, k) => `${names[k]} ${K(s[i])}`).join(", ")}`).join("; ")}>
        {labels.map((l, i) => (
          <div key={i} className="ai-col" title={`${l}: ${data.map((s, k) => `${names[k]} ${K(s[i])}`).join(", ")}`}>
            <b>{stacked ? K(tot[i]) : data.length === 1 ? K(data[0][i]) : ""}</b>
            <div className={"ai-col-bars" + (stacked ? " stack" : "")}>
              {data.map((s, k) => <i key={k} style={{ height: `${Math.max(2, (100 * Math.abs(s[i])) / top)}%`, background: PALETTE[k % PALETTE.length] }} />)}
            </div>
            <small>{l}</small>
          </div>))}
      </div>
    </div>
  );
}

function Line({ labels, data, names, fill }) {
  const all = data.flat(), mn = Math.min(...all), mx = Math.max(...all), pad = (mx - mn) * 0.15 || Math.abs(mx) * 0.05 || 1;
  const W = 300, H = 130, lo = mn - pad, hi = mx + pad, n = labels.length - 1 || 1, X0 = 40;
  const xy = s => s.map((v, i) => [X0 + (i / n) * (W - X0 - 12), 10 + (1 - (v - lo) / (hi - lo || 1)) * (H - 34)]);
  return (
    <div className="ai-cols-wrap">
      <Legend names={names} />
      <svg className="ai-line" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={labels.map((l, i) => `${l} ${data.map(s => K(s[i])).join(" / ")}`).join(", ")}>
        {data.map((s, k) => {
          const p = xy(s), d = p.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" "), c = PALETTE[k % PALETTE.length];
          return (
            <g key={k}>
              {fill && <path d={`${d} L${p[p.length - 1][0]},${H - 22} L${p[0][0]},${H - 22} Z`} style={{ fill: c, opacity: 0.16 }} />}
              {data.length === 1 && <text x={p[p.length - 1][0]} y={p[p.length - 1][1] - 6} className="v" textAnchor="end">{K(s[s.length - 1])}</text>}
              <path d={d} fill="none" strokeWidth="2" strokeLinejoin="round" style={{ stroke: c }} />
              {p.map(([x, y], i) => <circle key={i} cx={x} cy={y} r="2.6" style={{ fill: c }}><title>{`${labels[i]}: ${K(s[i])}`}</title></circle>)}
            </g>);
        })}
        <text x={X0 - 4} y="14" className="x" textAnchor="end">{K(mx)}</text>
        <text x={X0 - 4} y={H - 26} className="x" textAnchor="end">{K(mn)}</text>
        <line x1={X0} x2={W - 12} y1={H - 22} y2={H - 22} className="ax" />
        <text x={X0} y={H - 6} className="x">{labels[0]}</text>
        <text x={W - 12} y={H - 6} className="x" textAnchor="end">{labels[labels.length - 1]}</text>
      </svg>
    </div>
  );
}

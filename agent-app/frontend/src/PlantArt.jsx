import { useMemo } from "react";

// Live assembly floor for the sign-in page (an original isometric drawing): two conveyor lines carry parts past robot arms, workers
// and a quality gate; a forklift shuttles pallets. Parts ride the belts on SVG motion paths; arms and people move with CSS.
// Theme tokens only. With reduced motion everything is drawn still, parts spaced along the belts.
const S = 10, C = Math.cos(Math.PI / 6), N = Math.sin(Math.PI / 6), OX = 430, OY = 150;
const P = (x, y, z = 0) => [OX + (x - y) * C * S, OY + (x + y) * N * S - z * S];
const pts = a => a.map(p => P(...p).map(v => v.toFixed(1)).join(",")).join(" ");
const O = P(0, 0, 0);
const BELT = 2.5;                                                   // belt top height

// a box: top, right (+x) and left (+y) faces. tone: w white, g grey, c belt, b blue, o carton, k screen, f floor
function Box({ x, y, z = 0, w, d, h, tone = "w" }) {
  const t = [[x, y, z + h], [x + w, y, z + h], [x + w, y + d, z + h], [x, y + d, z + h]];
  const r = [[x + w, y, z], [x + w, y + d, z], [x + w, y + d, z + h], [x + w, y, z + h]];
  const l = [[x, y + d, z], [x + w, y + d, z], [x + w, y + d, z + h], [x, y + d, z + h]];
  return (
    <g className={"pa-" + tone}>
      <polygon points={pts(r)} className="f-r" /><polygon points={pts(l)} className="f-l" /><polygon points={pts(t)} className="f-t" />
    </g>
  );
}
// drawn around the origin, so it can ride a motion path
const Rel = ({ children }) => <g transform={`translate(${-O[0]},${-O[1]})`}>{children}</g>;

function Tank({ x, y, r, h, tone = "w" }) {
  const [cx, cy] = P(x, y, 0), rx = r * S * 1.22, ry = r * S * 0.7, top = cy - h * S;
  return (
    <g className={"pa-" + tone}>
      <path d={`M${cx - rx},${top} V${cy} A${rx},${ry} 0 0 0 ${cx + rx},${cy} V${top}`} className="f-r" />
      <ellipse cx={cx} cy={top} rx={rx} ry={ry} className="f-t" />
    </g>
  );
}

// a conveyor through points (axis-aligned turns): legs, frame, belt and running rollers
function Conveyor({ path }) {
  const segs = path.slice(1).map((b, i) => [path[i], b]);
  return (
    <g>
      {segs.map(([a, b], i) => {
        const flat = a[1] === b[1], x = Math.min(a[0], b[0]) - 1, y = Math.min(a[1], b[1]) - 1;
        const w = flat ? Math.abs(b[0] - a[0]) + 2 : 2, d = flat ? 2 : Math.abs(b[1] - a[1]) + 2;
        const legs = [];
        for (let k = 0; k <= (flat ? w : d) - 0.3; k += 4) legs.push(flat ? [x + k, y] : [x, y + k], flat ? [x + k, y + 1.7] : [x + 1.7, y + k]);
        return (
          <g key={i}>
            {legs.map(([lx, ly], j) => <Box key={j} x={lx} y={ly} w={0.3} d={0.3} h={1.8} tone="g" />)}
            <Box x={x} y={y} z={1.8} w={w} d={d} h={0.6} tone="g" />
            <Box x={x + 0.15} y={y + 0.15} z={2.4} w={w - 0.3} d={d - 0.3} h={0.1} tone="c" />
          </g>
        );
      })}
      <polyline points={pts(path.map(([x, y]) => [x, y, BELT + 0.02]))} className="pa-roll" />
    </g>
  );
}

const along = (path, t) => {                                        // point at fraction t of a polyline (by length)
  const segs = path.slice(1).map((b, i) => [path[i], b, Math.hypot(b[0] - path[i][0], b[1] - path[i][1])]);
  let d = t * segs.reduce((a, s) => a + s[2], 0);
  for (const [a, b, l] of segs) { if (d <= l) return [a[0] + (b[0] - a[0]) * d / l, a[1] + (b[1] - a[1]) * d / l]; d -= l; }
  return path[path.length - 1];
};

// parts riding a belt: n units evenly spaced, looping
function Parts({ path, n, dur, still }) {
  const d = "M" + path.map(([x, y]) => P(x, y, BELT).map(v => v.toFixed(1)).join(",")).join(" L");
  return Array.from({ length: n }, (_, k) => {
    const part = <Rel><Box x={-0.6} y={-0.6} w={1.2} d={1.2} h={k % 3 === 2 ? 0.6 : 0.9} tone={k % 3 === 2 ? "b" : "o"} /></Rel>;
    if (still) { const [x, y] = along(path, k / n), [px, py] = P(x, y, BELT); return <g key={k} transform={`translate(${px},${py})`}>{part}</g>; }
    return <g key={k}><animateMotion dur={`${dur}s`} repeatCount="indefinite" path={d} begin={`-${(k * dur / n).toFixed(2)}s`} />{part}</g>;
  });
}

// robot arm on a round base: shoulder and elbow swing over the belt
function Robot({ x, y, flip, delay = 0 }) {
  const [bx, by] = P(x, y, 1.4);
  return (
    <g>
      <Tank x={x} y={y} r={1} h={1.4} tone="g" />
      <g transform={`translate(${bx},${by}) scale(${flip ? -1.2 : 1.2},1.2)`}>
        <g className="pa-sh" style={{ animationDelay: `${delay}s` }}>
          <line x1="0" y1="0" x2="6" y2="-30" className="pa-arm" />
          <circle cx="0" cy="0" r="5" className="pa-joint" />
          <g transform="translate(6,-30)">
            <g className="pa-el" style={{ animationDelay: `${delay}s` }}>
              <line x1="0" y1="0" x2="24" y2="10" className="pa-arm" />
              <circle cx="0" cy="0" r="4.5" className="pa-joint" />
              <g transform="translate(24,10)"><path d="M-3 0v8M3 0v8" className="pa-claw" /><circle r="3" className="pa-joint" /></g>
            </g>
          </g>
        </g>
      </g>
    </g>
  );
}

// a worker in overalls and a hard hat; the arm works
function Worker({ x, y, delay = 0, flip }) {
  const [px, py] = P(x, y, 0);
  return (
    <g transform={`translate(${px},${py}) scale(${flip ? -1.25 : 1.25},1.25)`}>
      <ellipse cx="0" cy="0" rx="9" ry="4" className="pa-shadow" />
      <rect x="-5" y="-15" width="4" height="15" rx="1.5" className="pa-legs" /><rect x="1" y="-15" width="4" height="15" rx="1.5" className="pa-legs" />
      <rect x="-7" y="-31" width="14" height="18" rx="4" className="pa-suit" />
      <rect x="-7" y="-25" width="14" height="3" className="pa-stripe" />
      <g transform="translate(5,-28)"><g className="pa-work" style={{ animationDelay: `${delay}s` }}><line x1="0" y1="0" x2="9" y2="9" className="pa-limb" /></g></g>
      <circle cx="0" cy="-37" r="5.5" className="pa-skin" />
      <path d="M-6.5 -37.5a6.5 6.5 0 0 1 13 0z" className="pa-hat" /><rect x="-7.5" y="-38.5" width="15" height="2" rx="1" className="pa-hat" />
    </g>
  );
}

function Forklift({ from, to, dur, still }) {
  const body = (
    <Rel>
      <Box x={-1.2} y={-0.8} w={2.2} d={1.6} h={1} tone="o" />
      <Box x={-1.1} y={-0.7} z={1} w={1.2} d={1.4} h={0.1} tone="k" />
      <Box x={-1.1} y={-0.7} z={2.1} w={1.3} d={1.4} h={0.12} tone="g" />
      <Box x={1.0} y={-0.8} w={0.2} d={1.6} h={2.8} tone="g" />
      <Box x={1.2} y={-0.6} z={0.5} w={1.4} d={1.2} h={1} tone="o" />
    </Rel>
  );
  const a = P(...from), b = P(...to);
  if (still) return <g transform={`translate(${a[0]},${a[1]})`}>{body}</g>;
  return (
    <g>
      <animateMotion dur={`${dur}s`} repeatCount="indefinite" path={`M${a[0]},${a[1]} L${b[0]},${b[1]}`} keyPoints="0;1;1;0;0" keyTimes="0;0.4;0.5;0.9;1" calcMode="linear" />
      {body}
    </g>
  );
}

function Label({ x, y, z, text, sub }) {
  const [px, py] = P(x, y, z);
  return (
    <g className="pa-label">
      <line x1={px} y1={py + 34} x2={px} y2={P(x, y, BELT + 1)[1]} className="pa-stem" />
      <rect x={px - 56} y={py - 4} width="124" height="38" rx="8" className="pa-glass" />
      <circle cx={px + 56} cy={py + 9} r="3" className="pa-live" />
      <text x={px - 46} y={py + 13} className="pa-t">{text}</text>
      <text x={px - 46} y={py + 27} className="pa-s">{sub}</text>
    </g>
  );
}

const Beacon = ({ x, y, z, delay = 0 }) => {
  const [cx, cy] = P(x, y, z);
  return <g><circle cx={cx} cy={cy} r="9" className="pa-ring" style={{ animationDelay: `${delay}s` }} /><circle cx={cx} cy={cy} r="2.6" className="pa-beacon" /></g>;
};

const LINE1 = [[3, 6], [26, 6], [26, 16], [8, 16], [8, 26], [28, 26]];
const LINE2 = [[33, 9], [54, 9], [54, 20], [37, 20], [37, 32], [56, 32]];

export default function PlantArt() {
  const still = useMemo(() => typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches, []);
  return (
    <svg className="pa" viewBox="40 125 920 570" preserveAspectRatio="xMidYMid meet" role="img"
      aria-label="Illustration of a working assembly floor: conveyors carrying parts past robot arms, workers, a quality gate and a forklift">
      <Box x={0} y={0} w={60} d={44} h={0.5} tone="f" />
      {/* back: test cell and robots behind the belts */}
      <Box x={38} y={1} w={8} d={4} h={5} />
      <Box x={38.8} y={5} z={1.5} w={3} d={0.05} h={2.2} tone="k" />
      <Box x={47} y={1.5} w={4} d={3} h={3.2} />
      <Robot x={14} y={3} delay={0} />
      <Robot x={48} y={6.2} delay={-1.4} flip />
      <Conveyor path={LINE1} />
      <Conveyor path={LINE2} />
      <Parts path={LINE1} n={14} dur={26} still={still} />
      <Parts path={LINE2} n={14} dur={24} still={still} />
      {/* quality gate over line 2 */}
      <Box x={45} y={30.2} w={0.6} d={0.6} h={4.6} tone="g" />
      <Box x={45} y={33.3} w={0.6} d={0.6} h={4.6} tone="g" />
      <Box x={44.8} y={30.1} z={4.6} w={1} d={3.9} h={0.9} tone="b" />
      <polygon points={pts([[45.3, 31, 4.6], [45.3, 33.1, 4.6], [45.3, 33.1, BELT], [45.3, 31, BELT]])} className="pa-scan" />
      {/* robots and people between and in front of the belts */}
      <Robot x={29.5} y={11} delay={-0.7} />
      <Robot x={45} y={23.5} delay={-2.1} flip />
      <Worker x={15} y={20.5} delay={0} />
      <Worker x={33} y={27} delay={-0.6} />
      <Worker x={51} y={35.5} delay={-1.1} flip />
      {/* workbench with a screen */}
      <Box x={17} y={31} w={7} d={2.6} h={1.7} />
      <Box x={18.5} y={31.4} z={1.7} w={2.4} d={0.2} h={1.6} tone="k" />
      <Worker x={21} y={35} delay={-0.3} flip />
      {/* stores rack with cartons */}
      {[0, 4, 8, 12].map(i => <Box key={"p" + i} x={2 + i} y={36} w={0.3} d={0.3} h={5} tone="b" />)}
      {[1.6, 3.3, 5].map((z, k) => (
        <g key={"s" + k}>
          <Box x={2} y={36} z={z - 0.15} w={12.3} d={2.7} h={0.15} tone="b" />
          {k < 2 && [0.5, 2.3, 4.5, 6.3, 8.6, 10.4].filter((_, j) => (j + k) % 3 !== 2).map(x => <Box key={x} x={2 + x} y={36.5} z={z} w={1.4} d={1.6} h={1.1} tone="o" />)}
        </g>
      ))}
      {[0, 4, 8, 12].map(i => <Box key={"q" + i} x={2 + i} y={38.4} w={0.3} d={0.3} h={5} tone="b" />)}
      {/* pallets of finished goods */}
      {[[40, 37], [42, 37], [40, 39], [46, 38]].map(([x, y], i) => (
        <g key={"pl" + i}><Box x={x} y={y} w={1.8} d={1.8} h={0.3} tone="g" /><Box x={x + 0.1} y={y + 0.1} z={0.3} w={1.6} d={1.6} h={1.2} tone="o" />
          {i !== 2 && <Box x={x + 0.1} y={y + 0.1} z={1.5} w={1.6} d={1.6} h={1.2} tone="o" />}</g>))}
      <Forklift from={[24, 41]} to={[37, 41]} dur={11} still={still} />
      <Beacon x={42} y={3} z={5} />
      <Beacon x={45.3} y={32} z={5.5} delay={-0.8} />
      <Beacon x={20} y={31.4} z={3.4} delay={-1.5} />
      <Label x={16} y={6} z={9} text="SMT line" sub="Running" />
      <Label x={46} y={10} z={10} text="Final assembly" sub="On plan" />
      <Label x={45.3} y={32} z={9} text="Quality check" sub="Passing" />
    </svg>
  );
}

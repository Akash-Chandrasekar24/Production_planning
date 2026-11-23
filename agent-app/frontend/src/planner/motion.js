import { createElement, useEffect, useRef, useState } from "react";

// Smooth value changes for charts and figures. A critically damped spring (no overshoot, quick settle) is the motion Apple uses
// for interface changes: fast at first, then easing gently into place. Honour "reduce motion": values then change at once.

const K = 7;
const raw = t => 1 - (1 + K * t) * Math.exp(-K * t);
const spring = t => (t >= 1 ? 1 : raw(t) / raw(1));              // normalised so it lands exactly on 1
const reduced = () => typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

// Stretch or squeeze a series to n points so a line can morph when the period length changes (e.g. 7 -> 30 days).
function resample(a, n) {
  if (!Array.isArray(a) || !a.length) return Array(n).fill(null);
  if (a.length === n) return a;
  return Array.from({ length: n }, (_, i) => {
    const x = n === 1 ? 0 : (i * (a.length - 1)) / (n - 1), lo = Math.floor(x), hi = Math.min(a.length - 1, lo + 1), f = x - lo;
    return a[lo] == null || a[hi] == null ? a[hi] ?? a[lo] : a[lo] + (a[hi] - a[lo]) * f;
  });
}
const mix = (a, b, e) => (a == null || b == null ? b : a + (b - a) * e);

/** Animated copy of a number or an array of numbers (nulls allowed). Returns the in-between value on every frame. */
export function useTween(target, dur = 720) {
  const [val, setVal] = useState(target);
  const cur = useRef(target), raf = useRef(0);
  const key = JSON.stringify(target);
  useEffect(() => {
    const to = target;
    const arr = Array.isArray(to);
    const from = arr ? resample(Array.isArray(cur.current) ? cur.current : [], to.length) : cur.current;
    if (reduced() || to == null || (!arr && typeof from !== "number")) { cur.current = to; setVal(to); return undefined; }
    const t0 = performance.now();
    cancelAnimationFrame(raf.current);
    const step = now => {
      const e = spring(Math.min(1, (now - t0) / dur));
      const v = arr ? to.map((b, i) => mix(from[i], b, e)) : mix(from, to, e);
      cur.current = v;
      setVal(v);
      if (e < 1) raf.current = requestAnimationFrame(step);
    };
    raf.current = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf.current);
  }, [key]);                                                      // eslint-disable-line react-hooks/exhaustive-deps
  return val;
}

/** A number that rolls to its new value. `fmt` formats the in-between values the same way as the final one. */
export function Num({ value, fmt = v => String(v), as = "b", className }) {
  const v = useTween(typeof value === "number" ? value : null);
  const dec = typeof value === "number" && !Number.isInteger(value) ? 1 : 0;
  const shown = typeof value !== "number" ? fmt(value) : fmt(dec ? Math.round(v * 10) / 10 : Math.round(v));
  return createElement(as, { className }, shown);
}

"""Forecast agent: works out the weeks ahead for one section and says what it means for the plant.

How it works (each step is a thought the dashboard shows):
  1. Reads the weekly records for the section (and plant).
  2. Back-test: hides the last 4 recorded weeks, forecasts them with each method, and measures how far off each one was.
     Methods: same as last week, average of the last 4 weeks, damped trend, damped trend + the recent week-to-week pattern.
  3. Forecasts with the most accurate method; the likely range (80%) comes from that method's back-test error.
  4. Checks the forecast against the rest of the plant (demand against what the lines make, stock running out, availability,
     crews) and says what to do.
Inventory for both plants and the workforce are not statistical: the agent uses the planner's stock projection and the crew roster.
"""
from datetime import datetime
import numpy as np
import pandas as pd
from . import planning_tools as T, store

NAME = "forecast"
HOLD = 4                                                              # weeks hidden from the agent to test each method
CLOSE = 1.0                                                           # methods within this many points of the best count as equal
PREFER = ["pattern", "trend", "avg", "last"]                          # among equals, the one that keeps the most of the weekly shape


def _pattern(y, h):
    ma = np.convolve(np.pad(y, 1, mode="edge"), np.ones(3) / 3, mode="valid")
    pat = (y - ma)[-4:]
    return T._holt(y, h) + np.array([pat[i % len(pat)] for i in range(h)]) * 0.8


METHODS = {"last": ("same as last week", lambda y, h: np.repeat(y[-1], h), "Same as last week"),
           "avg": ("average of the last 4 weeks", lambda y, h: np.repeat(y[-4:].mean(), h), "4-week average"),
           "trend": ("damped trend", lambda y, h: T._holt(y, h), "Trend"),
           "pattern": ("damped trend with the recent week-to-week pattern", _pattern, "Trend + weekly pattern")}


def _fmt(sec, v):
    if v is None:
        return "–"
    if sec == "machinery":
        return f"{v:.1f}%"
    if sec == "workforce":
        return f"{v:.1f} people"
    return f"{v / 1000:.1f}K" if abs(v) >= 1000 else f"{v:.0f}"


def _day(w):
    return datetime.fromisoformat(w).strftime("%d %b").lstrip("0")


def _finished(plant=None):
    """Finished phones a week (final assembly lines), last 4 full weeks."""
    hp = store.D()["fact_hourly_production"]
    hp = hp[hp.line_id.str.startswith("FAL") & ((hp.plant_id == plant) if plant else True)]
    hp = hp.assign(w=pd.to_datetime(hp.event_date).dt.to_period("W-SUN"))
    days = hp.groupby("w").event_date.nunique()
    g = hp.groupby("w").good_qty.sum().loc[days[days >= 7].index].tail(4)
    return float(g.mean()) if len(g) else None


def _pick(y):
    """Back-test every method on the hidden weeks. The smallest average error wins; when several are about as close, the one that
    keeps the weekly shape (pattern, then trend) is used."""
    fit, real = y[:-HOLD], y[-HOLD:]
    tried = []
    for k, (label, fn, short) in METHODS.items():
        p = fn(fit, HOLD)
        tried.append({"key": k, "label": label, "short": short, "error_pct": round(float(np.mean(np.abs(p - real) / np.maximum(np.abs(real), 1))) * 100, 1),
                      "rmse": float(np.sqrt(np.mean((p - real) ** 2)))})
    tried.sort(key=lambda t: t["error_pct"])
    near = [t for t in tried if t["error_pct"] <= tried[0]["error_pct"] + CLOSE]
    best = min(near, key=lambda t: PREFER.index(t["key"]))
    return [best] + [t for t in tried if t is not best], len(near) > 1


def run(section: str, weeks: int = 8, plant: str | None = None) -> dict:
    s = T.forecast_series(section, weeks, plant)
    sec, think, found = section, [], []
    where = {"IN-TN-01": "Hosur", "IN-KA-01": "Bengaluru"}.get(plant, "both plants")
    h = s["history"]
    think.append(f"Reading {len(h)} recorded weeks of {s['label'].lower()} ({s['unit']}), {where}." if h
                 else f"No past weeks are recorded for {s['label'].lower()}; working from what is already planned.")
    tried = None
    if s.get("available") and len(h) >= HOLD + 4:
        y = np.array([p["v"] for p in h], dtype=float)
        tried, tie = _pick(y)
        best = tried[0]
        think.append(f"Hid the last {HOLD} weeks and forecast them {len(tried)} ways to see which comes closest to what really happened.")
        think.append(f"Using {best['label']}: " + (f"it is within {CLOSE:g} point of the closest and keeps the week-to-week shape."
                                                    if tie and best["error_pct"] > min(t["error_pct"] for t in tried) else "it came closest to what really happened."))
        f = METHODS[best["key"]][1](y, weeks)
        band = 1.28 * best["rmse"] * np.sqrt(np.arange(1, weeks + 1) / ((HOLD + 1) / 2))
        lo, hi = f - band, f + band
        if sec == "machinery":
            f, lo, hi = np.clip(f, 0, 100), np.clip(lo, 0, 100), np.clip(hi, 0, 100)
        else:
            f, lo = np.maximum(f, 0), np.maximum(lo, 0)
        r = 1 if sec == "machinery" else 0
        last = datetime.fromisoformat(h[-1]["week"])
        s["forecast"] = [{"week": (last + pd.Timedelta(weeks=i + 1)).date().isoformat(), "v": round(float(v), r), "lo": round(float(a), r),
                          "hi": round(float(b), r)} for i, (v, a, b) in enumerate(zip(f, lo, hi))]
        s["runs_out"] = None
        s["method"] = f"Forecast agent: {best['label']}, the closest of {len(tried)} methods on the last {HOLD} recorded weeks " \
                      f"(off by {best['error_pct']}% on average); shaded = likely range (80%)"
    elif sec == "workforce":
        think.append("Crews come from the roster minus planned leave; there is no attendance history to learn from.")

    fc = s["forecast"]
    if fc:                                                            # summary on the agent's own forecast
        avg_f = sum(p["v"] for p in fc) / len(fc)
        avg_h = sum(p["v"] for p in h[-4:]) / len(h[-4:]) if h else None
        s["summary"] = {"last": h[-1]["v"] if h else None, "next": fc[0]["v"], "forecast_avg": round(avg_f, 1),
                        "change_pct": round(100 * (avg_f - avg_h) / avg_h, 1) if avg_h else None}
        peak, low = max(fc, key=lambda p: p["v"]), min(fc, key=lambda p: p["v"])
        ch = s["summary"]["change_pct"]
        if ch is not None:
            think.append(f"Next {len(fc)} weeks average {_fmt(sec, avg_f)}, {abs(ch)}% {'above' if ch >= 0 else 'below'} the last 4 weeks; "
                         f"highest in the week of {_day(peak['week'])} ({_fmt(sec, peak['v'])}), lowest in the week of {_day(low['week'])} ({_fmt(sec, low['v'])}).")
        _check(sec, s, plant, weeks, avg_f, low, think, found)
    if tried:
        b = tried[0]["error_pct"]
        found.append({"tone": "good" if b <= 5 else "warn", "text": f"Forecast tested on the last {HOLD} weeks: off by {b}% on average"
                      + ("" if b <= 5 else ", so treat the weeks ahead as a guide") + "."})
    s.update(agent={"name": "Forecast agent", "ran_at": datetime.now().isoformat(timespec="seconds"), "thoughts": think,
                    "findings": found, "methods": [{k: t[k] for k in ("label", "short", "error_pct")} for t in tried or []]})
    return s


def _check(sec, s, plant, weeks, avg_f, low, think, found):
    """What the forecast means for the rest of the plant."""
    if sec in ("demand", "production"):
        # orders are finished phones: compare with final assembly only (production counts circuit boards and assembly together)
        mk = _finished()
        if sec == "production" and plant and mk:
            think.append(f"{'Hosur' if plant == 'IN-TN-01' else 'Bengaluru'} finishes {100 * (_finished(plant) or 0) / mk:.0f}% of the phones; orders are for both plants, so both are compared.")
        if sec == "demand":
            dem = avg_f
        else:
            dh = T.forecast_series("demand", 4, None)["history"][-4:]
            dem = sum(p["v"] for p in dh) / len(dh) if dh else None
        if mk and dem:
            think.append(f"Compared orders ({_fmt(sec, dem)} a week) with finished phones off final assembly over the last 4 weeks "
                         f"({_fmt(sec, mk)} a week).")
            gap = dem - mk
            if gap > 0.02 * mk:
                found.append({"tone": "bad", "text": f"Orders ({_fmt(sec, dem)} a week) run {_fmt(sec, gap)} a week ahead of finished output "
                              f"({_fmt(sec, mk)}): plan overtime, use both plants or agree later dates for the newest orders."})
            else:
                found.append({"tone": "good", "text": f"Finished output ({_fmt(sec, mk)} a week) keeps up with orders ({_fmt(sec, dem)} a week)."})
    if sec == "inventory" and not plant:
        # the total can look steady while single materials run out: check each one against the plan
        o = T.outlook(weeks)
        short = [m for m in o["materials"] if m.get("short_index") is not None]
        think.append(f"Checked each of the {len(o['materials'])} materials against what the plan needs, with usual deliveries.")
        if short:
            first = min(short, key=lambda m: m["short_index"])
            s["runs_out"] = o["weeks"][first["short_index"]]["start"]
            s["short_materials"] = len(short)
            found.append({"tone": "bad", "text": f"{len(short)} of {len(o['materials'])} materials run short under the plan, the first "
                          f"({first['material_id']}) in the week of {_day(s['runs_out'])}: place the material orders now (Materials & capacity, Purchasing)."})
        else:
            found.append({"tone": "good", "text": "Every material covers what the plan needs."})
    if sec == "inventory" and not s.get("runs_out"):
        if low["v"] < 0.5 * (s["history"][-1]["v"] if s["history"] else low["v"]):
            found.append({"tone": "warn", "text": f"Stock halves by the week of {_day(low['week'])} ({_fmt(sec, low['v'])}): check purchases due."})
        else:
            found.append({"tone": "good", "text": "Stock holds up over the weeks ahead."})
    if sec == "machinery":
        tgt = 85.0
        if low["v"] < tgt:
            found.append({"tone": "warn", "text": f"Availability dips to {_fmt(sec, low['v'])} in the week of {_day(low['week'])}, under the {tgt:.0f}% "
                          "target: bring planned services forward or keep a spare line ready."})
        else:
            found.append({"tone": "good", "text": f"Lines stay above {tgt:.0f}% available every week ahead."})
    if sec == "workforce":
        top = max(p["v"] for p in s["forecast"])
        if low["v"] < 0.92 * top:
            found.append({"tone": "warn", "text": f"Fewest people in the week of {_day(low['week'])} ({_fmt(sec, low['v'])} a shift): "
                          "move leave or plan overtime for that week."})
        else:
            found.append({"tone": "good", "text": "Crews stay steady over the weeks ahead."})

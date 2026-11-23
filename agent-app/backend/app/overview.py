"""The Overview page's live figures: what the agentic flow solves (with a number from the plan in use for each), and the customer
delivery heatmap (customers x delivery windows: share of units the plan finishes by their date)."""
from collections import defaultdict
from datetime import datetime, timedelta

from . import incidents, store, volume
from .db import Incident, SessionLocal

WINDOWS = 5                                    # overdue, then 4 weeks, then later


def _due_end(d) -> datetime:
    return datetime.combine(d, datetime.min.time()) + timedelta(days=1)


def _covered(s, inp, orders) -> dict:
    """Units of each customer order that open production orders already make, netted in the planner's order (priority, then date)."""
    from .config import Policy
    pol = Policy(**s.policy)
    if not pol.net_open_po_supply:
        return {}
    po, out = dict(inp.open_po_supply), {}
    for o in sorted(orders, key=lambda o: (not o.urgent, pol.priority_rank.get(o.priority, 99), o.due, o.order_id)):
        c = min(po.get(o.product_id, 0), o.qty)
        if c:
            po[o.product_id] -= c
            out[o.order_id] = c
    return out


def solutions() -> dict:
    s, _ = incidents._base()
    m = s.summary
    with SessionLocal() as db:
        open_p = db.query(Incident).filter_by(status="OPEN").count()
        fixed = db.query(Incident).filter_by(status="RESOLVED").count()
    util = round(100 * (m.get("avg_utilisation") or 0))
    otd = heatmap()["on_time_pct"]                              # same measure as the heatmap: scheduled on time or already in production
    from . import plan_sets
    ps = plan_sets.latest()
    pick = next((x for x in (ps or {}).get("plans", []) if x["schedule_id"] == ((ps or {}).get("chosen") or (ps or {}).get("recommended"))), None)
    items = [
        {"key": "choice", "icon": "layers", "title": "Five ready plans to choose from",
         "problem": "One plan, built by hand over days, no alternatives.",
         "text": "The agent makes five complete plans from live data, one of them with no late penalties; the production manager picks one.",
         "value": str(len(ps["plans"])) if ps else "–", "label": (f"plans · {'chosen: ' + pick['title'] if ps and ps.get('chosen') and pick else 'awaiting choice' if ps and ps['status'] == 'OPEN' else 'last run'}"
                                               if ps else "plans after the next run"),
         "tone": "warn" if ps and ps["status"] == "OPEN" else "ok"},
        {"key": "materials", "icon": "box", "title": "Shortages caught early",
         "problem": "Jobs start, then stop for a missing part.",
         "text": "Each plan works out what to order and by when; jobs start once parts can arrive.",
         "value": f"{m.get('purchase_wait_jobs', 0):,}", "label": "wait for parts", "tone": "warn" if m.get("purchase_wait_jobs") else "ok"},
        {"key": "disruptions", "icon": "alert", "title": "Fast recovery from disruptions",
         "problem": "A stopped line means hours of calls and a paper re-plan.",
         "text": "A stopped line gets recovery options and a recommended plan within minutes.",
         "value": f"{open_p}", "label": f"open · {fixed} fixed", "tone": "bad" if open_p else "ok"},
        {"key": "bottlenecks", "icon": "factory", "title": "Bottlenecks relieved",
         "problem": "One line overloaded while another waits.",
         "text": "Work is balanced across plants and the same product grouped to cut changeovers.",
         "value": f"{util}%", "label": f"line use · busiest {m.get('bottleneck_line') or '–'}", "tone": "warn" if util > 90 else "ok"},
        {"key": "delivery", "icon": "target", "title": "Delivery promises in sight",
         "problem": "Late orders found when the customer calls.",
         "text": "Every order is tracked against its date; risks reach the right person early.",
         "value": f"{otd if otd is not None else '–'}%", "label": f"units on time · {m.get('late_jobs', 0)} jobs at risk",
         "tone": "ok" if (otd or 0) >= 90 else "warn" if (otd or 0) >= 60 else "bad"},
    ]
    return {"plan": s.name, "plan_id": s.id, "items": items}


def heatmap(top: int = 6, sid: str | None = None) -> dict:
    """Customers x delivery windows. Each cell: orders, units, and the share of those units the plan finishes by their date
    (units not scheduled count as not on time)."""
    s = store.get(sid) if sid else incidents._base()[0]
    inp = store.inputs(s.id)
    t0 = inp.as_of.date()
    week0 = t0 - timedelta(days=t0.weekday())
    done = defaultdict(int)
    for a in s.assignments:
        done[(a.customer_order_id, a.fal_finish)] += a.qty
    on_time = defaultdict(int)
    orders = [o for o in inp.orders if o.status not in ("PRODUCTION_ORDER", "FORECAST")]
    due = {o.order_id: o.due for o in orders}
    for (oid, fin), q in done.items():
        if oid in due and fin <= _due_end(due[oid]):
            on_time[oid] += q
    covered = _covered(s, inp, orders)                         # already being made by open production orders (netted, no new job)

    def window(d):
        if d < t0:
            return 0
        k = (d - week0).days // 7
        return 1 + k if k < WINDOWS - 1 else WINDOWS

    labels = [{"key": 0, "label": "Overdue", "sub": "past due"}]
    for k in range(WINDOWS - 1):
        w = week0 + timedelta(days=7 * k)
        labels.append({"key": k + 1, "label": f"{w:%b} {w.day}", "sub": "this week" if k == 0 else f"week {k + 1}"})
    labels.append({"key": WINDOWS, "label": "Later", "sub": f"after {(week0 + timedelta(days=7 * (WINDOWS - 1))):%b} {(week0 + timedelta(days=7 * (WINDOWS - 1))).day}"})
    units = defaultdict(int)
    for o in orders:
        units[o.customer_id or "Other"] += o.qty
    best = [c for c, _ in sorted(units.items(), key=lambda kv: -kv[1])[:top]]
    cells, totals = defaultdict(lambda: {"orders": 0, "units": 0, "on_time": 0, "list": []}), defaultdict(int)
    for o in orders:
        c = o.customer_id if o.customer_id in best else "Other"
        cell = cells[(c, window(o.due))]
        cell["orders"] += 1
        cell["units"] += o.qty
        ok = min(o.qty, on_time.get(o.order_id, 0) + covered.get(o.order_id, 0))
        cell["on_time"] += ok
        cell["list"].append({"order_id": o.order_id, "product_id": o.product_id, "qty": o.qty, "due": o.due.isoformat(), "priority": o.priority,
                             "on_time_units": ok, "in_production": covered.get(o.order_id, 0)})
        totals[c] += 1
    rows = []
    for c in best + (["Other"] if any(k[0] == "Other" for k in cells) else []):
        row = {"customer": c, "orders": totals[c], "cells": []}
        for lb in labels:
            x = cells.get((c, lb["key"]))
            row["cells"].append(None if not x else {"orders": x["orders"], "units": x["units"], "pct": round(100 * x["on_time"] / x["units"]) if x["units"] else None,
                                                    "worst": sorted(x["list"], key=lambda r: r["on_time_units"] / r["qty"])[:8]})
        rows.append(row)
    nxt = next((lb for lb in labels[1:] if any(r["cells"][lb["key"]] for r in rows)), None)
    tot_u = sum(c["units"] for c in cells.values())
    return {"plan": s.name, "as_of": t0.isoformat(), "windows": labels, "rows": rows,
            "on_time_pct": round(100 * sum(c["on_time"] for c in cells.values()) / tot_u, 1) if tot_u else None,
            "next": {"label": nxt["label"], "customers": sum(1 for r in rows if r["cells"][nxt["key"]])} if nxt else None}


CITY = {"IN-TN-01": "Hosur", "IN-KA-01": "Bengaluru"}


def line_load(weeks: int = 4, sid: str | None = None) -> dict:
    """Production lines x the next weeks: how full each line is in the plan (booked / available line time), the hours booked and
    the products that run there. The heart of the plan: where capacity is short, where there is room."""
    s = store.get(sid) if sid else incidents._base()[0]
    inp = store.inputs(s.id)
    t0 = inp.as_of.date()
    w0 = t0 - timedelta(days=t0.weekday())
    keys = []
    for i in range(weeks):
        y, w, _ = (w0 + timedelta(weeks=i)).isocalendar()
        keys.append(f"{y}-W{w:02d}")
    cap = {(c["line_id"], c["week"]): c for c in s.capacity}
    prods = defaultdict(lambda: defaultdict(int))
    for a in s.assignments:
        for ln, t in ((a.smt_line, a.smt_start), (a.fal_line, a.fal_start)):
            y, w, _ = t.isocalendar()
            prods[(ln, f"{y}-W{w:02d}")][a.product_id] += a.qty
    rows = []
    for l in sorted(inp.lines, key=lambda l: (l.plant_id, l.kind != "SMT", l.line_id)):
        cells = []
        for k in keys:
            c = cap.get((l.line_id, k))
            top = sorted(prods[(l.line_id, k)].items(), key=lambda x: -x[1])[:3]
            cells.append(None if not c or not c["available_min"] else
                         {"pct": round(100 * (c["utilisation"] or 0)), "hours": round(c["used_min"] / 60), "free_h": round(max(0, c["available_min"] - c["used_min"]) / 60),
                          "products": [{"product_id": p, "units": q} for p, q in top]})
        rows.append({"line_id": l.line_id, "plant": CITY.get(l.plant_id, l.plant_id), "kind": l.kind, "cells": cells})
    cols = [{"key": k, "label": "This week" if i == 0 else f"Week {i + 1}", "sub": f"{w0 + timedelta(weeks=i):%d %b}"} for i, k in enumerate(keys)]
    full = [(r["line_id"], cols[i]["label"]) for r in rows for i, x in enumerate(r["cells"]) if x and x["pct"] >= 95]
    return {"plan": s.name, "weeks": cols, "rows": rows, "overloaded": len(full), "first_full": full[0] if full else None}

"""The plan for one plant in standard manufacturing terms, for its production head: master production schedule, time fences,
takt time against cycle time, capacity utilisation and the bottleneck, OEE, on-time delivery, plan adherence, changeovers,
how often each product is made (EPE), manufacturing lead time, material requirements (MRP) and inventory turnover.
Everything is worked out from the chosen plan and the plant's records; nothing here is typed in by hand."""
from collections import defaultdict
from datetime import datetime, timedelta

import pandas as pd

from . import planning_tools, store, twin
from .config import Policy



def _due_end(d) -> datetime:
    return datetime.fromisoformat(str(d)[:10]) + timedelta(days=1)


def _pct(a, b):
    return round(100 * a / b, 1) if b else None


def build(sid: str, plant: str) -> dict:
    s, inp = store.get(sid), store.inputs(sid)
    if s is None:
        raise KeyError(sid)
    jobs = [a for a in s.assignments if a.plant_id == plant]
    lines = [l for l in inp.lines if l.plant_id == plant]
    if not jobs:
        return {"schedule_id": s.id, "schedule_name": s.name, "plant_id": plant, "available": False}
    t0 = inp.as_of
    t1 = max(a.fal_finish for a in jobs)
    days = max(1.0, (t1 - t0).total_seconds() / 86400)
    units = sum(a.qty for a in jobs)

    # master production schedule: units to finish per product per week
    mps = defaultdict(lambda: defaultdict(int))
    for a in jobs:
        wk = (a.fal_finish - timedelta(days=a.fal_finish.weekday())).date().isoformat()
        mps[wk][a.product_id] += a.qty
    products = sorted({a.product_id for a in jobs})
    mps_rows = [{"week": w, **{p: mps[w].get(p, 0) for p in products}, "total": sum(mps[w].values())} for w in sorted(mps)]

    pol = Policy(**s.policy) if s.policy else Policy()
    # time fences, counted by when each job starts
    fence = {"frozen": [0, 0], "slushy": [0, 0], "flexible": [0, 0]}
    for a in jobs:
        d = (a.smt_start - t0).total_seconds() / 86400
        k = "frozen" if d <= pol.frozen_days else "slushy" if d <= pol.slushy_days else "flexible"
        fence[k][0] += 1
        fence[k][1] += a.qty
    locked = sum(1 for a in jobs if a.frozen)

    # available minutes per line within the plan, and the time each line is busy
    avail = {l.line_id: sum(sl.minutes for sl in l.slots if t0 <= sl.start < t1) for l in lines}
    rate = {l.line_id: l.rate_per_min for l in lines}
    busy = defaultdict(float)                                       # run time (units / line rate) plus setup, not clock time across shifts
    for a in jobs:
        busy[a.smt_line] += a.qty / rate[a.smt_line] + a.smt_setup_min if rate.get(a.smt_line) else 0
        busy[a.fal_line] += a.qty / rate[a.fal_line] + a.fal_setup_min if rate.get(a.fal_line) else 0
    util = [{"line_id": l.line_id, "stage": l.kind, "available_h": round(avail[l.line_id] / 60, 1), "busy_h": round(busy[l.line_id] / 60, 1),
             "utilisation_pct": _pct(busy[l.line_id], avail[l.line_id])} for l in lines]
    bottleneck = max(util, key=lambda u: u["utilisation_pct"] or 0)["line_id"] if util else None

    # takt time (available time / units the customers need) against cycle time (time per unit the line achieves), per stage
    takt = []
    for kind in ("SMT", "FAL"):
        ls = [l for l in lines if l.kind == kind]
        if not ls:
            continue
        av_s = sum(avail[l.line_id] for l in ls) * 60
        rate = sum(l.rate_per_min for l in ls)
        t, c = (av_s / units if units else None), (60 / rate if rate else None)
        takt.append({"stage": kind, "takt_s": round(t, 2) if t else None, "cycle_s": round(c, 2) if c else None,
                     "keeps_up": bool(t and c and c <= t)})

    # on-time delivery, manufacturing lead time, changeovers
    firm = [a for a in jobs if not a.customer_order_id.startswith("FC-")]          # forecast replenishment is not a customer delivery
    on_time = sum(a.qty for a in firm if a.fal_finish <= _due_end(a.due))
    forecast_units = units - sum(a.qty for a in firm)
    late_jobs = sum(1 for a in jobs if a.status == "LATE_RISK")
    lead_h = sum((a.fal_finish - a.smt_start).total_seconds() / 3600 for a in jobs) / len(jobs)
    setup_min = sum(a.smt_setup_min + a.fal_setup_min for a in jobs)
    changes = sum(1 for a in jobs if a.smt_setup_min > 0) + sum(1 for a in jobs if a.fal_setup_min > 0)

    # EPE (every product every ...): plan days / number of separate runs of the product on final assembly
    runs = defaultdict(int)
    for line in {a.fal_line for a in jobs}:
        prev = None
        for a in sorted((a for a in jobs if a.fal_line == line), key=lambda a: a.fal_start):
            if a.product_id != prev:
                runs[a.product_id] += 1
            prev = a.product_id
    epe = [{"product_id": p, "runs": runs[p], "every_days": round(days / runs[p], 1) if runs[p] else None,
            "units": sum(a.qty for a in jobs if a.product_id == p)} for p in products]

    # OEE today, from the plant's line records
    snap = twin.snapshot(store.D(), None)
    oee = [{"line_id": l["line_id"], **{k: (l.get("today") or {}).get(k) for k in ("availability", "performance", "quality", "oee")}}
           for l in snap["lines"] if l["plant_id"] == plant and not l.get("generated")]

    # plan adherence (finished production orders against their plan)
    try:
        adh = planning_tools.adherence()
        mine = next((r for r in adh.get("by_plant", []) if r.get("plant_id") == plant), None)
    except Exception:
        adh, mine = {}, None

    # MRP: what this plant must buy, when to order (lead time offset) and how much (pack size, minimum order)
    try:
        mrp = [r for r in planning_tools.mrp()["rows"] if r["plant_id"] == plant]
    except Exception:
        mrp = []

    # inventory turnover (estimate): yearly material use at cost / material stock value now
    d = store.D()
    hp = d["fact_hourly_production"]
    last = pd.Timestamp(hp.event_date.max())
    fal = hp[(hp.plant_id == plant) & hp.line_id.str.startswith("FAL") & (pd.to_datetime(hp.event_date) > last - pd.Timedelta(days=30))]
    inv = d["fact_inventory_daily_snapshot"]
    inv = inv[(inv.plant_id == plant) & inv.warehouse_id.isin(pol.material_warehouses)]
    inv = inv[inv.snapshot_date == inv.snapshot_date.max()]
    cost = inv.groupby("material_id").unit_cost_inr.first().to_dict()
    per_unit = defaultdict(float)
    for b in inp.bom:
        per_unit[b.product_id] += b.qty_per * (1 + b.scrap) * cost.get(b.material_id, 0)
    use_30 = sum(float(r.good_qty) * per_unit[r.product_id] for r in fal.itertuples())
    stock_value = float((inv.on_hand_qty * inv.unit_cost_inr).sum())
    turns = round(use_30 * 365 / 30 / stock_value, 1) if stock_value else None

    return {
        "schedule_id": s.id, "schedule_name": s.name, "status": s.status, "plant_id": plant, "available": True,
        "as_of": t0.isoformat(), "plan_end": t1.isoformat(), "plan_days": round(days, 1), "units": units, "jobs": len(jobs),
        "mps": {"products": products, "weeks": mps_rows},
        "time_fences": {"frozen_weeks": pol.frozen_days // 7, "slushy_weeks": pol.slushy_days // 7, "frozen_days": pol.frozen_days, "locked_jobs": locked,
                        **{k: {"jobs": v[0], "units": v[1]} for k, v in fence.items()}},
        "takt": takt, "utilisation": util, "bottleneck": bottleneck,
        "otd_pct": _pct(on_time, units - forecast_units), "late_jobs": late_jobs, "forecast_units": forecast_units,
        "purchase_jobs": sum(any(r.startswith("WAITS_FOR_PURCHASE") for r in a.reason_codes) for a in jobs),
        "lead_time_h": round(lead_h, 1), "changeovers": changes, "setup_h": round(setup_min / 60, 1), "epe": epe,
        "oee": oee, "adherence": {"on_plan_pct": (mine or {}).get("on_plan"), "orders": (mine or {}).get("orders"), "estimate": True} if mine else None,
        "mrp": {"rows": mrp[:12], "count": len(mrp), "late": sum(r["status"] == "late" for r in mrp), "buy_value_inr": sum(r["value_inr"] or 0 for r in mrp)},
        "inventory_turns": turns, "stock_value_inr": round(stock_value),
    }

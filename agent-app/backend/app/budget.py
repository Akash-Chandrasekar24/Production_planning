"""Demand stage: the forecast review (the model's forecast, the number people agree, and how accurate the model has been) and the
production budget per product: required production = expected sales + desired ending stock - stock on hand."""
from collections import defaultdict
from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import data, store
from .config import Policy
from .db import ForecastOverride, SessionLocal


def accuracy(weeks: int = 4) -> dict[str, float]:
    """Back-test per product: forecast the last `weeks` weeks from the weeks before them; error = mean absolute % error."""
    co = store.D()["fact_customer_order"]
    wk = pd.to_datetime(co.order_date).dt.to_period("W-SUN")
    last_full = pd.Period(pd.Timestamp(co.order_date.max()), "W-SUN") - 1
    g = co.assign(w=wk)[wk <= last_full].groupby(["product_id", "w"]).order_qty.sum().unstack(fill_value=0)
    out = {}
    for p, row in g.iterrows():
        y = row.values.astype(float)
        if len(y) >= 8 + weeks:
            f = data.holt(y[:-weeks], weeks)
            out[p] = round(float(np.mean(np.abs(f - y[-weeks:]) / np.maximum(y[-weeks:], 1))) * 100, 1)
    return out


def forecast_review(sid: str) -> dict:
    """Per product and week: the model's forecast, the agreed number (if reviewed), and the confirmed orders due that week."""
    inp = store.inputs(sid)
    with SessionLocal() as s:
        ov = {(o.product_id, o.week): o for o in s.query(ForecastOverride)}
    acc = accuracy()
    rows = []
    for p, weeks in sorted(inp.forecast.items()):
        for f in weeks:
            w0 = date.fromisoformat(f["week"])
            firm = sum(o.qty for o in inp.orders if o.product_id == p and o.status != "PRODUCTION_ORDER" and w0 <= o.due <= w0 + timedelta(days=6))
            o = ov.get((p, f["week"]))
            rows.append({"product_id": p, "week": f["week"], "model": f.get("model", f["qty"]), "agreed": o.qty if o else None,
                         "note": o.note if o else None, "by": o.set_by if o else None, "at": o.set_at if o else None, "confirmed": firm,
                         "for_stock": max(0, (o.qty if o else f.get("model", f["qty"])) - firm)})
    return {"schedule_id": sid, "weeks": sorted({r["week"] for r in rows}), "products": sorted(inp.forecast), "rows": rows,
            "accuracy_pct": acc, "method": "Holt's method with a damped trend on weekly ordered units",
            "applies": "Agreed numbers are used by the next plan the agent makes"}


def set_forecast(product_id: str, week: str, qty: int | None, note: str | None, user: str) -> None:
    """Agree a number for a product and week (qty None = go back to the model's forecast)."""
    date.fromisoformat(week)
    with SessionLocal() as s:
        o = s.get(ForecastOverride, (product_id, week))
        if qty is None:
            if o:
                s.delete(o)
        elif o:
            o.qty, o.note, o.set_by = qty, note, user
        else:
            s.add(ForecastOverride(product_id=product_id, week=week, qty=qty, note=note, set_by=user))
        s.commit()
    from . import coord
    coord.changed()                                                # the next plan (in any process) uses the agreed number
    store.audit(None, user, "forecast", "forecast_reviewed", {"product_id": product_id, "week": week, "qty": qty, "note": note})


def production_budget(sid: str) -> dict:
    """Per product: expected sales (confirmed orders + forecast beyond them), desired ending stock, stock on hand, required production,
    and what the plan actually makes. Required = expected sales + desired ending stock - beginning stock."""
    s, inp = store.get(sid), store.inputs(sid)
    if s is None:
        raise KeyError(sid)
    pol = Policy(**s.policy)
    orders = defaultdict(int)
    for o in inp.orders:
        orders[o.product_id] += o.qty
    rows = []
    for p in sorted(set(orders) | set(inp.forecast)):
        weeks = inp.forecast.get(p, [])
        beyond = 0
        for f in weeks:
            w0 = date.fromisoformat(f["week"])
            firm = sum(o.qty for o in inp.orders if o.product_id == p and o.status != "PRODUCTION_ORDER" and w0 <= o.due <= w0 + timedelta(days=6))
            beyond += max(0, f["qty"] - firm)
        ending = round(pol.fg_target_days * sum(f["qty"] for f in weeks) / (7 * len(weeks))) if weeks else 0
        begin = inp.fg_stock.get(p, 0)
        po = inp.open_po_supply.get(p, 0) if pol.net_open_po_supply else 0
        required = max(0, orders[p] + beyond + ending - begin - po)
        planned = sum(a.qty for a in s.assignments if a.product_id == p)
        rows.append({"product_id": p, "confirmed_orders": orders[p], "forecast_beyond_orders": beyond, "expected_sales": orders[p] + beyond,
                     "desired_ending_stock": ending, "beginning_stock": begin, "open_production_orders": po, "required": required,
                     "planned": planned, "gap": required - planned})
    tot = {k: sum(r[k] for r in rows) for k in ("expected_sales", "desired_ending_stock", "beginning_stock", "required", "planned", "gap")}
    return {"schedule_id": sid, "fg_target_days": pol.fg_target_days, "rows": rows, "total": tot,
            "formula": "Required production = expected sales + desired ending stock - stock on hand (- what open production orders still make)",
            "fg_stock_known": bool(inp.fg_stock)}


def rough_cut(sid: str) -> dict:
    s = store.get(sid)
    if s is None:
        raise KeyError(sid)
    rows = s.summary.get("rough_cut")
    if rows is None:                                            # plans made before the check existed: work it out from their inputs
        from . import planning
        inp = store.inputs(sid)
        rows = planning.plan(inp, Policy(**s.policy)).summary["rough_cut"]
    return {"schedule_id": sid, "weeks": rows, "overloaded": [r["week"] for r in rows if (r["load_pct"] or 0) > 100]}

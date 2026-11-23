"""Facts about the data for the dashboard, independent of any plan (deterministic; read-only).

Demand (open customer orders), supply (open production orders), stock vs safety stock, and line performance from the hourly
production records (schedule attainment = good units / target units, reject rate, downtime). Plan adherence per order cannot be
measured: hourly records cannot be reliably tied to production orders (see data-quality notes).
"""
from datetime import datetime, timedelta
import pandas as pd
from .config import Policy

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# ------------------------------------------------------------------ filters
# Which table columns each filter narrows. Customer orders carry no plant or line, and production records carry no channel or
# priority, so those filters leave them unchanged; `applies` reports this so the UI can say which cards a filter does not affect.
FILTER_COLS = {
    "fact_customer_order": {"product": "product_id", "priority": "priority", "channel": "channel"},
    "fact_production_order": {"plant": "plant_id", "product": "product_id", "priority": "priority"},
    "fact_hourly_production": {"plant": "plant_id", "line": "line_id", "product": "product_id"},
    "fact_inventory_daily_snapshot": {"plant": "plant_id"},          # product -> materials in that product's bill of materials
}
AREAS = {"orders": "fact_customer_order", "production": "fact_hourly_production", "supply": "fact_production_order", "stock": "fact_inventory_daily_snapshot"}
PRIORITY_ORDER = ["HIGH", "MEDIUM", "LOW"]


def options(d: dict) -> dict:
    """Values each filter can take, from the data (lines carry their plant so the UI can narrow them)."""
    hp, co = d["fact_hourly_production"], d["fact_customer_order"]
    pl = hp[["line_id", "plant_id"]].drop_duplicates().sort_values("line_id")
    return {"plant": sorted(set(hp.plant_id) | set(d["fact_production_order"].plant_id)),
            "line": [{"value": r.line_id, "plant": r.plant_id} for r in pl.itertuples()],
            "product": sorted(set(co.product_id) | set(hp.product_id)),
            "priority": [p for p in PRIORITY_ORDER if p in set(co.priority)],
            "channel": sorted(set(co.channel))}


def validate(d: dict, f: dict) -> dict:
    """Drop empty values; raise ValueError naming an unknown one."""
    opts, out = options(d), {}
    for k, v in f.items():
        if not v:
            continue
        allowed = [x["value"] for x in opts[k]] if k == "line" else opts[k]
        if v not in allowed:
            raise ValueError(f"Unknown {k} '{v}'")
        out[k] = v
    return out


def scope(d: dict, f: dict) -> dict:
    """The data narrowed by the filters (a new dict; the cached data is untouched)."""
    if not f:
        return d
    out = dict(d)
    for table, cols in FILTER_COLS.items():
        df = out[table]
        for key, col in cols.items():
            if f.get(key):
                df = df[df[col] == f[key]]
        out[table] = df
    if f.get("product"):
        bom = d["fact_bom_component"]
        mats = set(bom[bom.product_id == f["product"]].material_id)
        inv = out["fact_inventory_daily_snapshot"]
        out["fact_inventory_daily_snapshot"] = inv[inv.material_id.isin(mats)]
    return out


def supports() -> dict:
    """Per data area, every filter that can narrow it (whether or not it is set)."""
    return {area: sorted(set(FILTER_COLS[t]) | ({"product"} if t == "fact_inventory_daily_snapshot" else set())) for area, t in AREAS.items()}


def applies(f: dict) -> dict:
    """Per data area, which of the active filters actually narrow it."""
    res = {}
    for area, table in AREAS.items():
        keys = set(FILTER_COLS[table]) | ({"product"} if table == "fact_inventory_daily_snapshot" else set())
        res[area] = sorted(k for k in f if k in keys)
    return res


def _window(df, col, days, last=None):
    """Last `days` days of a table up to `last` (default: its own latest date) and the `days` before that.
    Filtered views pass the unfiltered latest date so every filter compares the same calendar days."""
    if last is None:
        if df.empty:
            return df, df, None, None
        last = pd.to_datetime(df[col]).max()
    last = pd.Timestamp(last).normalize()
    start = last - pd.Timedelta(days=days - 1)
    prev_start = start - pd.Timedelta(days=days)
    dt = pd.to_datetime(df[col])
    return df[(dt >= start) & (dt <= last)], df[(dt >= prev_start) & (dt < start)], start.date().isoformat(), last.date().isoformat()


def _anchors(full: dict) -> dict:
    return {"order_date": full["fact_customer_order"].order_date.max(), "event_date": full["fact_hourly_production"].event_date.max(),
            "as_of": datetime.fromisoformat(full["fact_inventory_daily_snapshot"].snapshot_date.max()) + timedelta(days=1)}


def _delta(cur, prev):
    return round(100 * (cur - prev) / prev, 1) if prev else None


def _pct(a, b, nd=1):
    return round(float(100 * a / b), nd) if b else None


def build(d: dict, pol: Policy | None = None, days: int = 30, f: dict | None = None) -> dict:
    """Dashboard facts. `d` is the full data; `f` the validated filters (see scope/validate)."""
    pol, f = pol or Policy(), f or {}
    A = _anchors(d)
    full, d = d, scope(d, f)
    co, po, inv, hp = d["fact_customer_order"], d["fact_production_order"], d["fact_inventory_daily_snapshot"], d["fact_hourly_production"]
    as_of = A["as_of"]                                                                   # same plan-start rule as the planner
    day = as_of.date().isoformat()

    o = co[co.order_status.isin(pol.open_statuses)].assign(open_qty=lambda x: x.order_qty - x.shipped_qty)
    o = o[o.open_qty > 0]
    due = o.confirmed_delivery_date
    soon = (as_of + timedelta(days=7)).date().isoformat()
    month = (as_of + timedelta(days=30)).date().isoformat()
    buckets = {"Overdue": o[due < day], "Due in 7 days": o[(due >= day) & (due < soon)], "Due in 8-30 days": o[(due >= soon) & (due < month)],
               "Later": o[due >= month]}

    open_po = po[po.order_status.isin(pol.open_po_statuses)]

    snap = inv[inv.snapshot_date == full["fact_inventory_daily_snapshot"].snapshot_date.max()]
    snap = snap.assign(usable=snap.on_hand_qty - snap.allocated_qty - snap.blocked_qty - snap.quality_hold_qty)
    m = snap.groupby("material_id").agg(usable=("usable", "sum"), safety=("safety_stock_qty", "sum"), in_transit=("in_transit_qty", "sum"))
    m["cover"] = m.usable / m.safety.where(m.safety > 0)
    low = m.sort_values("cover").head(6)

    lines = hp.groupby("line_id").agg(target=("target_qty", "sum"), good=("good_qty", "sum"), actual=("actual_qty", "sum"),
                                      reject=("reject_qty", "sum"), downtime=("downtime_min", "sum"), hours=("event_id", "count"))
    daily = hp.groupby("event_date").agg(target=("target_qty", "sum"), good=("good_qty", "sum")).tail(30)
    reasons = hp[hp.downtime_min > 0].groupby("downtime_reason").downtime_min.sum().sort_values(ascending=False)
    tot = lines.sum()

    period = _period(co, o, hp, days, A)
    return _clean({
        "filters": {"applied": f, "applies": applies(f), "supports": supports(), "options": options(full)},
        "period": period,
        "plan_start": day,
        "demand": {
            "open_orders": int(len(o)), "open_qty": int(o.open_qty.sum()),
            "by_product": {k: int(v) for k, v in o.groupby("product_id").open_qty.sum().items()},
            "by_priority": {k: int(v) for k, v in o.groupby("priority").open_qty.sum().items()},
            "due_buckets": [{"bucket": k, "orders": int(len(v)), "qty": int(v.open_qty.sum())} for k, v in buckets.items()],
            "status_counts": {k: int(v) for k, v in co.order_status.value_counts().items()},
        },
        "supply": {"open_production_orders": int(len(open_po)), "open_po_qty": int(open_po.order_qty.sum()),
                   "by_product": {k: int(v) for k, v in open_po.groupby("product_id").order_qty.sum().items()},
                   "status_counts": {k: int(v) for k, v in po.order_status.value_counts().items()}},
        "stock": {"snapshot": full["fact_inventory_daily_snapshot"].snapshot_date.max(), "materials": int(len(m)), "below_safety": int((m.usable < m.safety).sum()),
                  "lowest_cover": [{"material_id": i, "usable": int(r.usable), "safety": int(r.safety), "in_transit": int(r.in_transit),
                                    "cover": round(float(r.cover), 2) if r.cover == r.cover else None} for i, r in low.iterrows()]},
        "lines": {
            "attainment_pct": _pct(tot.good, tot.target), "reject_pct": _pct(tot.reject, tot.actual, 2),
            "downtime_min": int(tot.downtime),
            "per_line": [{"line_id": i, "attainment_pct": _pct(r.good, r.target), "reject_pct": _pct(r.reject, r.actual, 2),
                          "downtime_min": int(r.downtime), "good_units": int(r.good)} for i, r in lines.iterrows()],
            "daily_attainment": [{"date": i, "pct": _pct(r.good, r.target)} for i, r in daily.iterrows()],
            "downtime_by_reason": [{"reason": k, "minutes": int(v)} for k, v in reasons.items()],
            "period": {"from": hp.event_date.min(), "to": hp.event_date.max()},
        },
    })


def _period(co, open_orders, hp, days: int, A: dict) -> dict:
    """Period-over-period figures: the last `days` days vs the `days` before, anchored on the unfiltered latest dates."""
    oc, op, of, ot = _window(co, "order_date", days, A["order_date"])
    pc, pp, pf, pt = _window(hp, "event_date", days, A["event_date"])
    rev = lambda x: float((x.order_qty * x.unit_price_inr).sum())
    att = lambda x: _pct(x.good_qty.sum(), x.target_qty.sum()) if len(x) else None
    kpis = [
        {"key": "orders", "label": "Orders received", "value": int(len(oc)), "prev": int(len(op)), "better": "up"},
        {"key": "units", "label": "Units ordered", "value": int(oc.order_qty.sum()), "prev": int(op.order_qty.sum()), "better": "up"},
        {"key": "output", "label": "Good units", "value": int(pc.good_qty.sum()), "prev": int(pp.good_qty.sum()), "better": "up"},
        {"key": "downtime", "label": "Downtime", "value": round(float(pc.downtime_min.sum()) / 1440, 2), "prev": round(float(pp.downtime_min.sum()) / 1440, 2),
         "better": "down", "unit": "days"},
        {"key": "revenue", "label": "Revenue", "value": round(rev(oc)), "prev": round(rev(op)), "better": "up", "unit": "inr"},
        {"key": "attainment", "label": "Attainment", "value": att(pc), "prev": att(pp), "better": "up", "unit": "pct"},
    ]
    for k in kpis:
        k["delta_pct"] = _delta(k["value"], k["prev"])
    # each figure's day-by-day values over the period, for the small trend line on its card
    od, pd_ = oc.assign(rv=oc.order_qty * oc.unit_price_inr).groupby("order_date"), pc.groupby("event_date")
    daily = {"orders": od.size(), "units": od.order_qty.sum(), "revenue": od.rv.sum(), "output": pd_.good_qty.sum(), "downtime": pd_.downtime_min.sum(),
             "attainment": (pd_.good_qty.sum() / pd_.target_qty.sum().replace(0, float("nan")) * 100)}
    for k in kpis:
        k["spark"] = [None if pd.isna(v) else round(float(v), 1) for v in daily[k["key"]].sort_index().values]
    cur_d, prev_d = pc.groupby("event_date").good_qty.sum(), pp.groupby("event_date").good_qty.sum()
    prev_v = list(prev_d.values)
    series = [{"date": dte, "v": int(v), "p": int(prev_v[i]) if i < len(prev_v) else None} for i, (dte, v) in enumerate(cur_d.items())]
    wd = cur_d.groupby(pd.to_datetime(cur_d.index).dayofweek).mean()
    ch_c, ch_p = oc.channel.value_counts(), op.channel.value_counts()
    prods = []
    for prod, g in oc.groupby("product_id"):
        gp = op[op.product_id == prod]
        prods.append({"product_id": prod, "orders": int(len(g)), "units": int(g.order_qty.sum()), "revenue_inr": round(rev(g)),
                      "revenue_delta_pct": _delta(rev(g), rev(gp)), "open_units": int(open_orders[open_orders.product_id == prod].open_qty.sum())})
    kept = lambda x: round(float(100 * (x.confirmed_delivery_date <= x.requested_delivery_date).mean()), 1) if len(x) else None
    return {
        "days": days, "orders_range": [of, ot], "production_range": [pf, pt], "kpis": kpis,
        "revenue": {"value_inr": round(rev(oc)), "delta_pct": _delta(rev(oc), rev(op))},
        "output": {"total": int(cur_d.sum()), "delta_pct": _delta(float(cur_d.sum()), float(prev_d.sum())), "attainment_pct": att(pc),
                   "prev_attainment_pct": att(pp), "series": series},
        "weekday": _weekday(pc),
        "weekday_weeks": _weekday_weeks(pc),
        "channels": [{"channel": c, "orders": int(ch_c.get(c, 0)), "delta_pct": _delta(int(ch_c.get(c, 0)), int(ch_p.get(c, 0)))} for c in sorted(set(co.channel))],
        "products": sorted(prods, key=lambda r: -r["revenue_inr"]),
        "promise": {"pct": kept(oc), "prev_pct": kept(op)},
    }


# ----------------------------------------------------------------------------------------------- weekday + drill-through details
def _clean(o):
    """JSON-safe: numpy scalars -> python, NaN -> None, floats rounded."""
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if hasattr(o, "item"):
        o = o.item()
    if isinstance(o, float):
        return None if o != o or o in (float("inf"), float("-inf")) else (int(o) if o.is_integer() else round(o, 2))
    return o


def _day_table(pc):
    """Production per calendar day (all lines)."""
    g = pc.groupby("event_date").agg(good=("good_qty", "sum"), target=("target_qty", "sum"), actual=("actual_qty", "sum"),
                                     reject=("reject_qty", "sum"), downtime=("downtime_min", "sum"))
    g["wd"] = pd.to_datetime(g.index).dayofweek
    return g


def _weekday(pc) -> list[dict]:
    """Per day of week: averages per calendar day, attainment, rejects, number of days, best line."""
    g = _day_table(pc)
    line_day = pc.groupby(["event_date", "line_id"]).good_qty.sum().reset_index()
    line_day["wd"] = pd.to_datetime(line_day.event_date).dt.dayofweek
    best = line_day.groupby(["wd", "line_id"]).good_qty.mean()
    out = []
    for i in range(7):
        x = g[g.wd == i]
        if not len(x):
            out.append({"day": DAYS[i], "avg_good_units": 0, "days": 0})
            continue
        b = best[i] if i in best.index.get_level_values(0) else None
        out.append({"day": DAYS[i], "avg_good_units": round(float(x.good.mean())), "avg_target": round(float(x.target.mean())),
                    "attainment_pct": _pct(x.good.sum(), x.target.sum()), "avg_downtime_min": round(float(x.downtime.mean())),
                    "reject_pct": round(float(100 * x.reject.sum() / x.actual.sum()), 2) if x.actual.sum() else None, "days": int(len(x)),
                    "best_line": b.idxmax() if b is not None else None, "best_line_avg": round(float(b.max())) if b is not None else None})
    return out


def _weekday_weeks(pc) -> list[dict]:
    """The same per-day figures for each calendar week (Monday to Sunday) in the period, newest first."""
    if pc.empty:
        return []
    mon = pd.to_datetime(pc.event_date) - pd.to_timedelta(pd.to_datetime(pc.event_date).dt.dayofweek, unit="D")
    out = []
    for m, g in pc.assign(_mon=mon.dt.strftime("%Y-%m-%d")).groupby("_mon"):
        out.append({"start": m, "label": pd.Timestamp(m).strftime("%d %b"), "days_recorded": int(g.event_date.nunique()), "days": _weekday(g.drop(columns="_mon"))})
    return sorted(out, key=lambda w: w["start"], reverse=True)


def _cols(*spec):
    return [{"key": k, "label": lab, "num": num} for k, lab, num in spec]


_PROD_COLS = _cols(("good", "Good units", True), ("target", "Target", True), ("attainment", "Attainment %", True), ("reject_pct", "Rejects %", True),
                   ("downtime", "Downtime min", True))


def _prod_table(df, by, label):
    g = df.groupby(by).agg(good=("good_qty", "sum"), target=("target_qty", "sum"), actual=("actual_qty", "sum"), reject=("reject_qty", "sum"),
                           downtime=("downtime_min", "sum")).reset_index()
    g["attainment"] = (100 * g.good / g.target).round(1)
    g["reject_pct"] = (100 * g.reject / g.actual).round(2)
    return {"type": "table", "title": f"By {label.lower()}", "columns": _cols((by, label, False)) + _PROD_COLS,
            "rows": g[[by, "good", "target", "attainment", "reject_pct", "downtime"]].to_dict("records")}


def _series(cur, prev, datecol, values):
    """values: function(df grouped by date) -> Series; previous period aligned by position."""
    c, p = values(cur, datecol), values(prev, datecol)
    pv = list(p.values)
    return [{"label": d, "v": v, "p": pv[i] if i < len(pv) else None} for i, (d, v) in enumerate(c.items())]


def _stats(cur, prev, unit=None):
    ch = _delta(cur, prev) if cur is not None and prev is not None else None
    return {"type": "stats", "items": [{"label": "This period", "value": cur, "unit": unit}, {"label": "Previous period", "value": prev, "unit": unit},
                                       {"label": "Change", "value": ch, "unit": "delta"}]}


ORDER_METRIC = {"orders": ("Orders received", "customer_order_id", "count", None), "units": ("Units ordered", "order_qty", "sum", None),
                "revenue": ("Revenue", "value", "sum", "inr")}
PROD_METRIC = {"output": ("Good units", "good_qty", None), "downtime": ("Downtime hours", "downtime_h", None), "attainment": ("Attainment", None, "pct")}
DRILLS = ["orders", "units", "revenue", "output", "downtime", "attainment", "weekday", "products", "promise", "due", "supply", "stock", "lines"]


def detail(d: dict, key: str, days: int = 30, pol: Policy | None = None, f: dict | None = None) -> dict | None:
    """Detailed view behind a dashboard card: a title plus sections (stats / series / bars / table). None for unknown keys.
    `d` is the full data; `f` the validated filters (the same ones the dashboard used)."""
    pol, f = pol or Policy(), f or {}
    if key not in DRILLS:
        return None
    A = _anchors(d)
    full_inv = d["fact_inventory_daily_snapshot"]
    d = scope(d, f)
    co = d["fact_customer_order"].assign(value=lambda x: x.order_qty * x.unit_price_inr)
    hp, po, inv = d["fact_hourly_production"], d["fact_production_order"], d["fact_inventory_daily_snapshot"]
    hp = hp.assign(downtime_h=hp.downtime_min / 60)                # downtime is shown in hours (and days on the dashboard), not minutes
    oc, op, of, ot = _window(co, "order_date", days, A["order_date"])
    pc, pp, pf, pt = _window(hp, "event_date", days, A["event_date"])
    as_of = A["as_of"]
    day = as_of.date().isoformat()
    snap_day = full_inv.snapshot_date.max()
    o = co[co.order_status.isin(pol.open_statuses)].assign(open_qty=lambda x: x.order_qty - x.shipped_qty)
    o = o[o.open_qty > 0]
    S = []

    if key in ORDER_METRIC:
        title, col, how, unit = ORDER_METRIC[key]
        agg = lambda df, by: df.groupby(by)[col].agg(how)
        cur, prev = float(oc[col].agg(how)), float(op[col].agg(how))
        S += [_stats(cur, prev, unit), {"type": "series", "title": f"{title} per day", "unit": unit, "points": _series(oc, op, "order_date", agg)}]
        for by, lab in (("product_id", "Product"), ("channel", "Channel"), ("priority", "Priority")):
            S.append({"type": "bars", "title": f"By {lab.lower()}", "unit": unit, "rows": [{"label": k, "value": v} for k, v in agg(oc, by).sort_values(ascending=False).items()]})
        cust = oc.groupby("customer_id").agg(orders=("customer_order_id", "count"), units=("order_qty", "sum"), revenue=("value", "sum")).reset_index().sort_values({"customer_order_id": "orders", "order_qty": "units", "value": "revenue"}[col], ascending=False)
        S.append({"type": "table", "title": "By customer", "columns": _cols(("customer_id", "Customer", False), ("orders", "Orders", True), ("units", "Units", True), ("revenue", "Revenue ₹", True)),
                  "rows": cust.to_dict("records")})
        top = oc.sort_values("value" if key == "revenue" else "order_qty", ascending=False).head(15)
        S.append({"type": "table", "title": "Largest orders this period", "columns": _cols(("customer_order_id", "Order", False), ("order_date", "Date", False), ("customer_id", "Customer", False),
                  ("product_id", "Product", False), ("order_qty", "Units", True), ("value", "Value ₹", True), ("priority", "Priority", False), ("order_status", "Status", False)),
                  "rows": top[["customer_order_id", "order_date", "customer_id", "product_id", "order_qty", "value", "priority", "order_status"]].to_dict("records")})
        return _clean({"title": title, "range": [of, ot], "sections": S})

    if key in PROD_METRIC or key == "lines":
        title, col, unit = PROD_METRIC.get(key, ("Line performance", "good_qty", None))
        if col:
            agg = lambda df, by: df.groupby(by)[col].sum()
            cur, prev = float(pc[col].sum()), float(pp[col].sum())
        else:
            agg = lambda df, by: (100 * df.groupby(by).good_qty.sum() / df.groupby(by).target_qty.sum()).round(1)
            cur, prev = _pct(pc.good_qty.sum(), pc.target_qty.sum()), _pct(pp.good_qty.sum(), pp.target_qty.sum())
        S += [_stats(cur, prev, unit), {"type": "series", "title": f"{title} per day", "unit": unit, "points": _series(pc, pp, "event_date", agg)},
              _prod_table(pc, "line_id", "Line"), _prod_table(pc, "shift_id", "Shift")]
        reasons = pc[pc.downtime_min > 0]
        S.append({"type": "bars", "title": "Downtime by reason (hours)", "rows": [{"label": "reason not recorded" if k == "NONE" else k.lower().replace("_", " "), "value": round(v, 1)}
                                                                                  for k, v in reasons.groupby("downtime_reason").downtime_h.sum().sort_values(ascending=False).items()]})
        mat = reasons.pivot_table(index="downtime_reason", columns="line_id", values="downtime_h", aggfunc="sum", fill_value=0).round(1).reset_index()
        lines = [c for c in mat.columns if c != "downtime_reason"]
        mat["downtime_reason"] = mat.downtime_reason.map(lambda k: "reason not recorded" if k == "NONE" else k.lower().replace("_", " "))
        S.append({"type": "table", "title": "Downtime hours: reason × line", "columns": _cols(("downtime_reason", "Reason", False), *[(l, l, True) for l in lines]),
                  "rows": mat.to_dict("records")})
        return _clean({"title": title, "range": [pf, pt], "sections": S})

    if key == "weekday":
        wk = _weekday(pc)
        S.append({"type": "table", "title": "Every day of the week", "columns": _cols(("day", "Day", False), ("days", "Days counted", True), ("avg_good_units", "Avg good units", True),
                  ("avg_target", "Avg target", True), ("attainment_pct", "Attainment %", True), ("avg_downtime_min", "Avg downtime min", True), ("reject_pct", "Rejects %", True),
                  ("best_line", "Best line", False)), "rows": wk})
        ld = pc.groupby(["event_date", "line_id"]).good_qty.sum().reset_index()
        ld["day"] = pd.to_datetime(ld.event_date).dt.dayofweek.map(lambda i: DAYS[i])
        m = ld.pivot_table(index="day", columns="line_id", values="good_qty", aggfunc="mean").reindex(DAYS).round(0).reset_index()
        lines = [c for c in m.columns if c != "day"]
        S.append({"type": "table", "title": "Average good units per day: day × line", "columns": _cols(("day", "Day", False), *[(l, l, True) for l in lines]), "rows": m.to_dict("records")})
        return _clean({"title": "Busiest production day", "range": [pf, pt], "sections": S})

    if key == "products":
        g = oc.groupby("product_id").agg(orders=("customer_order_id", "count"), units=("order_qty", "sum"), revenue=("value", "sum")).reset_index()
        gp = op.groupby("product_id").value.sum()
        g["revenue_change_pct"] = g.apply(lambda r: _delta(r.revenue, gp.get(r.product_id, 0)), axis=1)
        g["open_units"] = g.product_id.map(o.groupby("product_id").open_qty.sum()).fillna(0)
        g["open_orders"] = g.product_id.map(o.groupby("product_id").size()).fillna(0)
        S.append({"type": "table", "title": "Products this period", "columns": _cols(("product_id", "Product", False), ("orders", "Orders", True), ("units", "Units", True),
                  ("revenue", "Revenue ₹", True), ("revenue_change_pct", "Revenue change %", True), ("open_orders", "Open orders", True), ("open_units", "Open units", True)),
                  "rows": g.to_dict("records")})
        for by, lab in (("channel", "channel"), ("priority", "priority"), ("order_status", "order status")):
            m = oc.pivot_table(index="product_id", columns=by, values="order_qty", aggfunc="sum", fill_value=0).reset_index()
            cs = [c for c in m.columns if c != "product_id"]
            S.append({"type": "table", "title": f"Units ordered by product × {lab}", "columns": _cols(("product_id", "Product", False), *[(c, c.replace("_", " ").title(), True) for c in cs]),
                      "rows": m.to_dict("records")})
        return _clean({"title": "Products", "range": [of, ot], "sections": S})

    if key == "promise":
        x = oc.assign(kept=oc.confirmed_delivery_date <= oc.requested_delivery_date,
                      later_days=(pd.to_datetime(oc.confirmed_delivery_date) - pd.to_datetime(oc.requested_delivery_date)).dt.days)
        rate = lambda df: round(float(100 * df.kept.mean()), 1) if len(df) else None
        prev = op.assign(kept=op.confirmed_delivery_date <= op.requested_delivery_date)
        S.append(_stats(rate(x), rate(prev), "pct"))
        for by, lab in (("channel", "channel"), ("product_id", "product"), ("priority", "priority")):
            S.append({"type": "bars", "title": f"Promise kept by {lab} (%)", "unit": "pct", "max": 100,
                      "rows": [{"label": k, "value": round(float(100 * v.kept.mean()), 1)} for k, v in x.groupby(by)]})
        late = x[x.later_days > 0].sort_values("later_days", ascending=False).head(25)
        S.append({"type": "table", "title": f"Confirmed later than requested ({int((x.later_days > 0).sum())} orders; longest delays first)",
                  "columns": _cols(("customer_order_id", "Order", False), ("customer_id", "Customer", False), ("product_id", "Product", False), ("requested_delivery_date", "Requested", False),
                                   ("confirmed_delivery_date", "Confirmed", False), ("later_days", "Days later", True), ("priority", "Priority", False)),
                  "rows": late[["customer_order_id", "customer_id", "product_id", "requested_delivery_date", "confirmed_delivery_date", "later_days", "priority"]].to_dict("records")})
        return _clean({"title": "Delivery promise kept", "range": [of, ot], "sections": S})

    if key == "due":
        x = o.assign(days_overdue=(as_of - pd.to_datetime(o.confirmed_delivery_date)).dt.days)
        over = x[x.confirmed_delivery_date < day]
        S.append({"type": "stats", "items": [{"label": "Overdue orders", "value": int(len(over))}, {"label": "Overdue units", "value": int(over.open_qty.sum())},
                                             {"label": "Average days overdue", "value": round(float(over.days_overdue.mean()), 1) if len(over) else None}]})
        for by, lab in (("product_id", "product"), ("priority", "priority"), ("customer_id", "customer")):
            S.append({"type": "bars", "title": f"Overdue units by {lab}", "rows": [{"label": k, "value": v} for k, v in over.groupby(by).open_qty.sum().sort_values(ascending=False).items()]})
        top = over.sort_values(["days_overdue", "open_qty"], ascending=False).head(50)
        S.append({"type": "table", "title": "Overdue orders (most overdue first)", "columns": _cols(("customer_order_id", "Order", False), ("customer_id", "Customer", False), ("product_id", "Product", False),
                  ("priority", "Priority", False), ("confirmed_delivery_date", "Due", False), ("days_overdue", "Days overdue", True), ("open_qty", "Open units", True), ("order_status", "Status", False)),
                  "rows": top[["customer_order_id", "customer_id", "product_id", "priority", "confirmed_delivery_date", "days_overdue", "open_qty", "order_status"]].to_dict("records")})
        return _clean({"title": "Due dates", "range": [day, day], "sections": S})

    if key == "supply":
        open_po = po[po.order_status.isin(pol.open_po_statuses)]
        t = pd.DataFrame({"open_demand": o.groupby("product_id").open_qty.sum(), "open_po_units": open_po.groupby("product_id").order_qty.sum(),
                          "open_pos": open_po.groupby("product_id").size()}).fillna(0).reset_index().rename(columns={"index": "product_id"})
        t["coverage"] = (t.open_po_units / t.open_demand).round(2)
        S.append({"type": "table", "title": "Coverage by product", "columns": _cols(("product_id", "Product", False), ("open_demand", "Open demand", True), ("open_po_units", "Open production-order units", True),
                  ("open_pos", "Open production orders", True), ("coverage", "Coverage ×", True)), "rows": t.to_dict("records")})
        S.append({"type": "bars", "title": "Open production-order units by plant", "rows": [{"label": k, "value": v} for k, v in open_po.groupby("plant_id").order_qty.sum().items()]})
        S.append({"type": "bars", "title": "Open production orders by status", "rows": [{"label": k.replace("_", " ").lower(), "value": v} for k, v in open_po.order_status.value_counts().items()]})
        lst = open_po.sort_values("order_qty", ascending=False).head(30)
        S.append({"type": "table", "title": "Largest open production orders", "columns": _cols(("production_order_id", "Production order", False), ("plant_id", "Plant", False), ("product_id", "Product", False),
                  ("order_qty", "Units", True), ("priority", "Priority", False), ("planned_finish", "Planned finish", False), ("order_status", "Status", False)),
                  "rows": lst[["production_order_id", "plant_id", "product_id", "order_qty", "priority", "planned_finish", "order_status"]].assign(planned_finish=lambda z: z.planned_finish.str[:10]).to_dict("records")})
        return _clean({"title": "Demand vs production orders", "range": [day, day], "sections": S})

    if key == "stock":
        snap = inv[inv.snapshot_date == snap_day]
        snap = snap.assign(usable=snap.on_hand_qty - snap.allocated_qty - snap.blocked_qty - snap.quality_hold_qty, value=lambda z: z.on_hand_qty * z.unit_cost_inr)
        m = snap.groupby("material_id").agg(on_hand=("on_hand_qty", "sum"), usable=("usable", "sum"), safety=("safety_stock_qty", "sum"), in_transit=("in_transit_qty", "sum"),
                                            quality_hold=("quality_hold_qty", "sum"), value=("value", "sum")).reset_index()
        m["cover"] = (m.usable / m.safety).round(2)
        S.append({"type": "table", "title": f"All materials on the stock count of {snap_day} (lowest cover first)", "columns": _cols(("material_id", "Material", False),
                  ("on_hand", "On hand", True), ("usable", "Usable", True), ("safety", "Safety stock", True), ("cover", "Cover ×", True), ("in_transit", "In transit", True),
                  ("quality_hold", "Quality hold", True), ("value", "Value ₹", True)), "rows": m.sort_values("cover").to_dict("records")})
        w = snap.groupby("warehouse_id").agg(on_hand=("on_hand_qty", "sum"), usable=("usable", "sum"), value=("value", "sum")).reset_index()
        S.append({"type": "table", "title": "By warehouse", "columns": _cols(("warehouse_id", "Warehouse", False), ("on_hand", "On hand", True), ("usable", "Usable", True), ("value", "Value ₹", True)),
                  "rows": w.to_dict("records")})
        return _clean({"title": "Stock vs safety stock", "range": [snap_day] * 2, "sections": S})
    return None

"""Planning tools built on the planning engine and the records. Read-only: nothing here approves, releases or changes anything.

The "working plan" is what the lines really have to make: the plan everyone works from plus the work still owed on open
production orders (in progress = material already issued; released = needs material from its plant's stock).

  promise      can we ship N units of a product by a date? (promise date, plant, what it pushes back)
  adherence    plan vs actual for production orders, on-time-in-full for customer orders, orders falling behind
  maintenance  planned services in the horizon, the work they clash with, and a quieter shift nearby
  changeovers  changeovers in the records, and the run order that cuts set-up time without making orders later
  mrp          time-phased material needs per plant, the date each part runs short, and what to buy by when
  forecast     weekly demand forecast per product (with its back-tested error) and the load it puts on the lines
  crews        people needed per plant and shift against the roster and leave; shifts that need overtime
  cost         what a plan costs: material, labour, line time, changeovers, overtime, late penalties, carrying stock
  yields       recorded yield per line and product, and the material and time rejects add
"""
import bisect
import math
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
import numpy as np
import pandas as pd
from . import data, incidents, planning, store
from .config import Policy
from .domain import Order

SHIFTS = {"A": 6, "B": 14, "C": 22}                       # shift start hours (8-hour shifts, as in the calendar)
GEN_NOTE = "generated"
_cache: dict = {}
_lock = threading.Lock()


# ------------------------------------------------------------------ the working plan
def work_orders() -> list[Order]:
    po = incidents._remaining()
    return [Order(order_id=r.production_order_id, product_id=r.product_id, qty=int(r.remaining), due=date.fromisoformat(str(r.due_date)[:10]),
                  priority=r.priority, status="PRODUCTION_ORDER", customer_id=r.customer_order_id, plant_id=r.plant_id,
                  kitted=r.order_status == "IN_PROGRESS")
            for r in po.itertuples() if r.remaining > 0]


def working(scope: str | None = None, pol: Policy | None = None):
    """(schedule, inputs) of the working plan. scope="plant": each plant uses only its own stock. Cached for 5 minutes."""
    base, inp = incidents._base()
    key = (base.id, scope, pol.model_dump_json() if pol else None, int(time.time() // 300))
    with _lock:
        if key not in _cache:
            if scope:
                inp = data.to_inputs(store.D(), inp.as_of.isoformat(), Policy(material_scope=scope))
            x = inp.model_copy(deep=True)
            have = {o.order_id for o in x.orders}
            x.orders += [o for o in work_orders() if o.order_id not in have]   # never twice
            if len(_cache) > 12:
                _cache.clear()
            p = pol or Policy(**{**base.policy, **({"material_scope": scope} if scope else {})})
            _cache[key] = (planning.plan(x, p, name="Working plan"), x)
        return _cache[key]


def _gen(name: str) -> pd.DataFrame | None:
    return store.D().get(name)


def _lines(inp) -> dict:
    return {l.line_id: l for l in inp.lines}


def _due_end(d: str | date) -> datetime:
    return datetime.combine(date.fromisoformat(str(d)[:10]), datetime.min.time()) + timedelta(days=1)


def _stock_left(ref, x) -> dict:
    """Pooled usable stock (above safety) per material after the plan's jobs take their material."""
    left = defaultdict(float)
    for st in x.stock:
        left[st.material_id] += max(0.0, st.available - st.safety)
    kit = {o.order_id for o in x.orders if o.kitted}
    bom = defaultdict(list)
    for b in x.bom:
        bom[b.product_id].append(b)
    for a in ref.assignments:
        if a.customer_order_id not in kit:
            for b in bom[a.product_id]:
                left[b.material_id] -= a.qty * b.qty_per * (1 + b.scrap)
    return left


def buy_plan(ref, x, product_id: str, qty: int) -> tuple[list[dict], str]:
    """Parts to buy to make `qty` more units (pooled stock left after the plan), and the earliest date if they are ordered today:
    the longest supplier lead time plus the time to make them on half of both plants' final assembly."""
    sup = _gen("gen_material_supplier")
    supi = sup.set_index("material_id") if sup is not None else None
    left = _stock_left(ref, x)
    buy = []
    for b in x.bom:
        if b.product_id != product_id:
            continue
        need = qty * b.qty_per * (1 + b.scrap) / 0.98
        if left.get(b.material_id, 0.0) < need:
            buy.append({"material_id": b.material_id, "qty": math.ceil(need - max(0.0, left.get(b.material_id, 0.0))),
                        "lead_days": int(supi.lead_time_days.get(b.material_id, 14)) if supi is not None else 14,
                        "supplier": supi.supplier_id.get(b.material_id) if supi is not None else None})
    lead = max((m["lead_days"] for m in buy), default=0)
    per_day = sum(l.rate_per_min for l in x.lines if l.kind == "FAL") * 3 * 450 * 0.5
    buy.sort(key=lambda m: -m["lead_days"])
    return buy, (x.as_of + timedelta(days=lead + math.ceil(qty / max(per_day, 1)))).date().isoformat()


# ------------------------------------------------------------------ 1. can we promise?
def promise(product_id: str, qty: int, due: date | None, priority: str = "HIGH") -> dict:
    ref, x = working()
    if product_id not in {b.product_id for b in x.bom}:
        raise ValueError(f"unknown product {product_id}")
    due = due or (x.as_of.date() + timedelta(days=14))
    y = x.model_copy(deep=True)
    prices = [o.unit_price_inr for o in x.orders if o.product_id == product_id and o.unit_price_inr]
    price = sum(prices) / len(prices) if prices else None          # valued like the product's orders, so penalty-first plans rank it fairly
    y.orders.append(Order(order_id="PROMISE-CHECK", product_id=product_id, qty=qty, due=due, priority=priority, status="ENQUIRY",
                          unit_price_inr=price))
    s = planning.plan(y, Policy(**ref.policy), name="Promise check")
    mine = [a for a in s.assignments if a.customer_order_id == "PROMISE-CHECK"]
    made = sum(a.qty for a in mine)
    by_due = sum(a.qty for a in mine if a.fal_finish <= _due_end(due))
    finish = max((a.fal_finish for a in mine), default=None)
    rest = qty - made
    rest_date, buy = None, []
    if rest > 0:                                                   # not all from stock and line time: buy the parts it lacks, then make it
        buy, rest_date = buy_plan(ref, x, product_id, rest)
    delayed = incidents._delays(s, ref)
    late_before = set(incidents._late(ref))
    newly_late = [k for k in incidents._late(s) if k not in late_before and k != "PROMISE-CHECK"]
    ok = made == qty and finish is not None and finish <= _due_end(due)
    date_ = finish.date().isoformat() if made == qty else rest_date
    answer = "yes" if ok else "with_purchase" if rest > 0 and date_ and date.fromisoformat(date_) <= due else "no"
    return {"product_id": product_id, "qty": qty, "due": due.isoformat(), "priority": priority, "as_of": x.as_of.isoformat(),
            "answer": answer, "can_promise": ok, "promise_date": date_,
            "plants": sorted({a.plant_id for a in mine}), "made_in_plan": made, "ready_by_due": by_due,
            "short": {"qty": rest, "buy": buy, "earliest_date": rest_date,
                      "assumes": "parts ordered today, and this order gets them first when they arrive"} if rest > 0 else None,
            "pushes_back": {"orders": len(delayed), "newly_late": len(newly_late), "examples": sorted(delayed.items(), key=lambda kv: -kv[1])[:8],
                            "newly_late_orders": newly_late[:10]},
            "jobs": [{"plant_id": a.plant_id, "qty": a.qty, "smt_line": a.smt_line, "fal_line": a.fal_line, "start": a.smt_start, "finish": a.fal_finish} for a in mine]}


# ------------------------------------------------------------------ 2. plan vs actual
def adherence() -> dict:
    d = store.D()
    po, hp = d["fact_production_order"].copy(), d["fact_hourly_production"]
    end = pd.Timestamp(hp.event_ts.max())
    conf = d.get("gen_po_confirmation")
    if conf is None:
        return {"available": False}
    g = conf.set_index("production_order_id").rename(columns={"good_qty": "made", "actual_start": "first", "actual_finish": "last"})[["made", "first", "last"]]
    po = po.join(g, on="production_order_id")
    po["made"] = po.made.fillna(0).astype(int)
    ps, pf = pd.to_datetime(po.planned_start), pd.to_datetime(po.planned_finish)
    done = po[po.order_status.isin(["COMPLETED", "CLOSED"]) & po["last"].notna()].copy()
    fin = pd.to_datetime(done["last"])
    done["late_vs_plan_d"] = ((fin - pd.to_datetime(done.planned_finish)).dt.total_seconds() / 86400).round(1)
    done["late_vs_due_d"] = ((fin - pd.to_datetime(done.due_date) - pd.Timedelta(days=1)).dt.total_seconds() / 86400).round(1)
    done["qty_ok"] = done.made >= 0.98 * done.order_qty
    openpo = po[po.order_status.isin(["RELEASED", "IN_PROGRESS"])].copy()
    span = (pf - ps).dt.total_seconds().reindex(openpo.index).clip(lower=3600)
    frac = ((end - ps.reindex(openpo.index)).dt.total_seconds() / span).clip(0, 1)
    openpo["expected"] = (frac * openpo.order_qty).round().astype(int)
    openpo["gap"] = openpo.expected - openpo.made
    behind = openpo[(openpo.gap > 0.1 * openpo.order_qty) & (frac > 0)].sort_values("gap", ascending=False)
    weekly = done.assign(week=fin.dt.strftime("%G-W%V")).groupby("week").agg(
        orders=("production_order_id", "count"), on_plan=("late_vs_plan_d", lambda s: round(100 * (s <= 1).mean(), 1)),
        on_due=("late_vs_due_d", lambda s: round(100 * (s <= 0).mean(), 1)), in_full=("qty_ok", lambda s: round(100 * s.mean(), 1))).reset_index()
    by_plant = done.groupby("plant_id").agg(orders=("production_order_id", "count"), on_plan=("late_vs_plan_d", lambda s: round(100 * (s <= 1).mean(), 1)),
                                            on_due=("late_vs_due_d", lambda s: round(100 * (s <= 0).mean(), 1)), in_full=("qty_ok", lambda s: round(100 * s.mean(), 1)),
                                            avg_late_d=("late_vs_plan_d", lambda s: round(float(s.clip(lower=0).mean()), 1))).reset_index()
    # on-time-in-full for customer orders (generated shipments)
    sh = d.get("gen_shipment")
    otif = None
    if sh is not None:
        co = d["fact_customer_order"]
        co = co[co.confirmed_delivery_date <= end.strftime("%Y-%m-%d")]
        s2 = sh.merge(co[["customer_order_id", "confirmed_delivery_date"]], on="customer_order_id")
        by = s2[s2.ship_date <= s2.confirmed_delivery_date].groupby("customer_order_id").qty.sum()
        anyb = s2[s2.ship_date <= s2.confirmed_delivery_date].customer_order_id.unique()
        c = co.assign(on_time=co.customer_order_id.isin(anyb), shipped_by_date=co.customer_order_id.map(by).fillna(0))
        c["in_full"] = c.shipped_by_date >= 0.98 * c.order_qty
        c["otif"] = c.on_time & c.in_full
        c["fill"] = (c.shipped_by_date / c.order_qty).clip(upper=1)
        w = c.assign(week=pd.to_datetime(c.confirmed_delivery_date).dt.strftime("%G-W%V")).groupby("week").agg(
            orders=("customer_order_id", "count"), otif=("otif", lambda s: round(100 * s.mean(), 1)), on_time=("on_time", lambda s: round(100 * s.mean(), 1)),
            fill=("fill", lambda s: round(100 * s.mean(), 1))).reset_index()
        otif = {"orders": int(len(c)), "otif_pct": round(100 * c.otif.mean(), 1), "on_time_pct": round(100 * c.on_time.mean(), 1),
                "in_full_pct": round(100 * c.in_full.mean(), 1), "fill_rate_pct": round(100 * c.fill.mean(), 1), "by_week": w.tail(12).to_dict("records"),
                "by_priority": c.groupby("priority").otif.mean().mul(100).round(1).to_dict(), "source": GEN_NOTE,
                "note": "The order records show 75-100 % of each order shipped, delivered orders included, so few orders count as in full."}
    n = len(done)
    return {"as_of": end.isoformat(), "finished_orders": n, "available": True, "source": "production confirmations (generated)",
            "on_plan_pct": round(100 * (done.late_vs_plan_d <= 1).mean(), 1) if n else None,
            "on_due_pct": round(100 * (done.late_vs_due_d <= 0).mean(), 1) if n else None,
            "in_full_pct": round(100 * done.qty_ok.mean(), 1) if n else None,
            "by_week": weekly.tail(12).to_dict("records"), "by_plant": by_plant.to_dict("records"),
            "behind": [{"production_order_id": r.production_order_id, "plant_id": r.plant_id, "product_id": r.product_id, "qty": int(r.order_qty),
                        "expected_by_now": int(r.expected), "made": int(r.made), "gap": int(r.gap), "planned_finish": r.planned_finish, "due": r.due_date,
                        "status": r.order_status} for r in behind.head(25).itertuples()],
            "behind_count": int(len(behind)),
            "worst": done.sort_values("late_vs_plan_d", ascending=False).head(10)[["production_order_id", "plant_id", "product_id", "order_qty", "made",
                                                                                   "planned_finish", "last", "late_vs_plan_d", "due_date"]].to_dict("records"),
            "otif": otif}


# ------------------------------------------------------------------ 3. maintenance in the plan
_load_idx: dict = {}                       # id(plan) -> (plan, {line: (starts, finishes)}): built once per plan, not per question


def _line_index(ref) -> dict:
    hit = _load_idx.get(id(ref))
    if hit and hit[0] is ref:
        return hit[1]
    by = defaultdict(list)
    for a in ref.assignments:
        by[a.smt_line].append((a.smt_start, a.smt_finish))
        by[a.fal_line].append((a.fal_start, a.fal_finish))
    idx = {}
    for ln, iv in by.items():
        iv.sort()
        reach, top = [], None                                 # latest finish so far: always sorted, even if jobs overlap
        for _, f in iv:
            top = f if top is None or f > top else top
            reach.append(top)
        idx[ln] = (reach, iv)
    if len(_load_idx) > 16:
        _load_idx.clear()
    _load_idx[id(ref)] = (ref, idx)
    return idx


def _load_on(ref, line: str, start: datetime, end: datetime) -> float:
    """Minutes of planned work on a line between start and end. Only the jobs around the window are looked at (binary search on
    the latest finish so far), instead of every job of the plan."""
    li = _line_index(ref).get(line)
    if not li:
        return 0.0
    reach, iv = li
    m = 0.0
    i = bisect.bisect_right(reach, start)                         # first job that could still be running at `start`
    while i < len(iv) and iv[i][0] < end:
        s, f = iv[i]
        m += max(0.0, (min(f, end) - max(s, start)).total_seconds() / 60)
        i += 1
    return m


def maintenance() -> dict:
    ref, x = working()
    out = []
    for w in x.maintenance:
        day0 = w["start"].replace(hour=0)
        here = _load_on(ref, w["line_id"], w["start"], w["end"])
        best = None
        for k in range(-3, 4):                                    # the same service in another shift within 3 days
            for sh, h in SHIFTS.items():
                s = day0 + timedelta(days=k, hours=h)
                if s < x.as_of or s == w["start"]:
                    continue
                e = s + timedelta(minutes=w["minutes"])
                load = _load_on(ref, w["line_id"], s, e)
                if best is None or load < best[0] - 1e-6:
                    best = (load, s, sh)
        sug = None
        if best and here - best[0] >= 30:
            sug = {"start": best[1], "shift": best[2], "planned_work_min": round(best[0]), "frees_min": round(here - best[0])}
        out.append({"line_id": w["line_id"], "machines": w["machine_ids"], "start": w["start"], "end": w["end"], "minutes": round(w["minutes"]),
                    "planned_work_min": round(here), "suggest": sug})
    return {"as_of": x.as_of.isoformat(), "services": out, "blocked_min": round(sum(w["minutes"] for w in x.maintenance)),
            "source": "maintenance records (generated)" if _gen("gen_maintenance_event") is not None else "service intervals"}


# ------------------------------------------------------------------ 4. changeovers
def changeovers() -> dict:
    d = store.D()
    hp = d["fact_hourly_production"].sort_values(["line_id", "event_ts"])
    last30 = hp[hp.event_date > (pd.Timestamp(hp.event_date.max()) - pd.Timedelta(days=30)).strftime("%Y-%m-%d")]
    hist = []
    for l, g in last30.groupby("line_id"):
        sw = int((g.product_id != g.product_id.shift()).sum() - 1)
        co_min = int(g[g.downtime_reason == "CHANGEOVER"].downtime_min.sum())
        hist.append({"line_id": l, "switches": sw, "changeover_min": co_min, "min_per_switch": round(co_min / sw, 1) if sw > 0 else None,
                     "lost_units": int(round(co_min * (g.target_qty.mean() / 60)))})
    ref, x = working()
    base = Policy(**ref.policy)
    opts = []
    for w in sorted({0, 7, 14, 21, base.batch_window_days}):
        s = ref if w == base.batch_window_days else planning.plan(x, base.model_copy(update={"batch_window_days": w}), name=f"window {w}")
        lud = sum(a.qty * a.late_days for a in s.assignments)
        opts.append({"window_days": w, "changeovers": s.summary["changeovers"], "changeover_min": s.summary["changeover_min"],
                     "late_jobs": s.summary["late_jobs"], "late_unit_days": round(lud), "current": w == base.batch_window_days, "_s": s})
    cur = next(o for o in opts if o["current"])
    ok = [o for o in opts if o["late_unit_days"] <= cur["late_unit_days"] * 1.02 + 1]
    best = min(ok, key=lambda o: (o["changeover_min"], o["late_unit_days"]))
    seq = defaultdict(list)
    for r in planning.dispatch_list(best["_s"]):
        if len(seq[r["line_id"]]) < 12:
            seq[r["line_id"]].append({"seq": r["seq"], "product_id": r["product_id"], "order": r["customer_order_id"], "qty": r["qty"], "start": r["start"],
                                      "setup_min": r["setup_min"]})
    for o in opts:
        o.pop("_s")
    return {"history": hist, "options": opts, "recommended": best["window_days"], "current": cur["window_days"],
            "saves_min": cur["changeover_min"] - best["changeover_min"], "sequence": seq}


# ------------------------------------------------------------------ 5. material requirements and purchase suggestions
def mrp() -> dict:
    ref, x = working("plant")
    sup = _gen("gen_material_supplier")
    supi = sup.set_index("material_id") if sup is not None else None
    bom = defaultdict(list)
    for b in x.bom:
        bom[b.product_id].append(b)
    yl = x.yields
    def mf(plant, product):
        f = 1.0
        for l in x.lines:
            if l.plant_id == plant:
                f /= yl.get(f"{l.line_id}|{product}") or yl.get(f"{l.line_id}|*") or 1.0
        return f
    need = defaultdict(lambda: defaultdict(float))           # (plant, material) -> day -> qty
    for a in ref.assignments:
        o = next((o for o in x.orders if o.order_id == a.customer_order_id), None)
        if o is not None and o.kitted:
            continue
        for b in bom[a.product_id]:
            need[(a.plant_id, b.material_id)][a.smt_start.date()] += a.qty * b.qty_per * (1 + b.scrap) * mf(a.plant_id, a.product_id)
    home = {o.order_id: o.plant_id for o in x.orders}
    for u in ref.unscheduled:                                 # what could not be planned for lack of material is needed now
        if u.reason.startswith("MATERIAL"):
            p = home.get(u.customer_order_id) or "IN-KA-01"
            for b in bom[u.product_id]:
                need[(p, b.material_id)][x.as_of.date()] += u.qty * b.qty_per * (1 + b.scrap)
    stock = {(s.plant_id, s.material_id): s for s in x.stock}
    today = x.as_of.date()
    rows = []
    for (p, m), byday in sorted(need.items()):
        st = stock.get((p, m))
        have, safety = (st.available, st.safety) if st else (0.0, 0.0)
        bal, short_day, low = have, None, have
        for dday in sorted(byday):
            bal -= byday[dday]
            if short_day is None and bal < safety:
                short_day = dday
            low = min(low, bal)
        total = sum(byday.values())
        if short_day is None:
            continue
        lead = int(supi.lead_time_days.get(m, 14)) if supi is not None else 14
        pack = int(supi.pack_qty.get(m, 1)) if supi is not None else 1
        moq = int(supi.moq.get(m, 0)) if supi is not None else 0
        qty = max(moq, math.ceil(max(0.0, safety - low) / pack) * pack)
        order_by = short_day - timedelta(days=lead)
        rows.append({"plant_id": p, "material_id": m, "need_qty": round(total), "stock_qty": round(have), "safety_qty": round(safety),
                     "short_on": short_day.isoformat(), "short_qty": round(max(0.0, safety - low)), "buy_qty": qty,
                     "supplier": supi.supplier_id.get(m) if supi is not None else None, "lead_days": lead, "order_by": order_by.isoformat(),
                     "late_by_days": max(0, (today - order_by).days), "status": "late" if order_by < today else "order now" if (order_by - today).days <= 2 else "planned",
                     "value_inr": round(qty * (st.unit_cost_inr or 0)) if st else None})
    rows.sort(key=lambda r: (r["status"] != "late", r["order_by"]))
    return {"as_of": x.as_of.isoformat(), "scope": "each plant's own stock", "rows": rows,
            "materials_checked": len(need), "suppliers_source": GEN_NOTE if sup is not None else None,
            "buy_value_inr": sum(r["value_inr"] or 0 for r in rows)}


# ------------------------------------------------------------------ 6. forecast
_holt = data.holt


def forecast(weeks: int = 12) -> dict:
    d = store.D()
    co = d["fact_customer_order"]
    ref, x = working()
    wk = pd.to_datetime(co.order_date).dt.to_period("W-SUN")
    last_full = pd.Period(pd.Timestamp(co.order_date.max()), "W-SUN") - 1
    g = co.assign(w=wk)[wk <= last_full].groupby(["product_id", "w"]).order_qty.sum().unstack(fill_value=0)
    future = [last_full + i for i in range(1, weeks + 1)]
    prods, err = {}, {}
    for p, row in g.iterrows():
        y = row.values.astype(float)
        if len(y) >= 8:
            bt = _holt(y[:-4], 4)
            err[p] = round(float(np.mean(np.abs(bt - y[-4:]) / np.maximum(y[-4:], 1))) * 100, 1)
        prods[p] = {"history": [{"week": str(k.start_time.date()), "qty": int(v)} for k, v in row.items()],
                    "forecast": [{"week": str(f.start_time.date()), "qty": int(round(v))} for f, v in zip(future, _holt(y, weeks))], "mape_pct": err.get(p)}
    # load on the lines: forecast units per week vs what both plants can make in a week (net of planned services)
    lines = _lines(x)
    cap = {}
    for kind in ("SMT", "FAL"):
        ls = [l for l in lines.values() if l.kind == kind]
        cap[kind] = sum(l.rate_per_min for l in ls) * 21 * Policy().default_shift_minutes
    firm = co[co.order_status.isin(Policy().open_statuses)].assign(q=lambda z: z.order_qty - z.shipped_qty)
    fw = pd.to_datetime(firm.confirmed_delivery_date).dt.to_period("W-SUN")
    firm_w = firm.assign(w=fw).groupby("w").q.sum()
    load = []
    for i, f in enumerate(future):
        fc = sum(v["forecast"][i]["qty"] for v in prods.values())
        units = max(fc, int(firm_w.get(f, 0)))
        load.append({"week": str(f.start_time.date()), "forecast": fc, "firm_due": int(firm_w.get(f, 0)), "capacity_units": int(min(cap.values())),
                     "load_pct": round(100 * units / max(1, min(cap.values())), 1)})
    return {"method": "Holt's method with a damped trend on weekly ordered units; error = back-test on the last 4 weeks", "weeks": weeks,
            "products": prods, "load": load, "capacity_units_per_week": {k: int(v) for k, v in cap.items()},
            "over_capacity_weeks": [l["week"] for l in load if l["load_pct"] > 100]}


# ------------------------------------------------------------------ 7. crews
def crews(days: int = 14, ref=None, x=None) -> dict:
    cr, lv = _gen("gen_crew"), _gen("gen_crew_leave")
    if cr is None:
        return {"available": False, "rows": []}
    if ref is None:
        ref, x = working()
    lines = _lines(x)
    leave = lv.set_index(["date", "plant_id", "shift_id"]).people_on_leave.to_dict() if lv is not None else {}
    rows = []
    for k in range(days):
        day = x.as_of.replace(hour=0, minute=0) + timedelta(days=k)
        for sh, h in SHIFTS.items():
            s, e = day + timedelta(hours=h), day + timedelta(hours=h + 8)
            for r in cr[cr.shift_id == sh].itertuples():
                active = [l for l in lines.values() if l.plant_id == r.plant_id and _load_on(ref, l.line_id, s, e) >= 30]
                need = sum(r.crew_size_smt if l.kind == "SMT" else r.crew_size_fal for l in active)
                avail = r.headcount - int(leave.get((day.strftime("%Y-%m-%d"), r.plant_id, sh), 0))
                smt_ok = not any(l.kind == "SMT" for l in active) or r.smt_certified >= r.crew_size_smt
                gap = max(0, need - avail)
                rows.append({"date": day.date().isoformat(), "shift": sh, "plant_id": r.plant_id, "team": r.team_id, "lines": [l.line_id for l in active],
                             "needed": need, "available": avail, "gap": gap, "overtime_h": gap * 8, "smt_certified_ok": bool(smt_ok)})
    short = [r for r in rows if r["gap"] > 0 or not r["smt_certified_ok"]]
    return {"available": True, "rows": rows, "short_shifts": short, "overtime_h": sum(r["overtime_h"] for r in rows), "source": GEN_NOTE}


# ------------------------------------------------------------------ 8. cost of a plan
def _rates() -> dict:
    r = _gen("gen_cost_rate")
    return r.set_index("plant_id").to_dict("index") if r is not None else {}


def cost(sid: str) -> dict:
    s = store.get(sid)
    if s is None:
        raise KeyError(sid)
    base_id = store.base_of(sid)
    return _cost(s, store.inputs(sid), store.inputs(base_id) if base_id else None)


def _cost(s, inp, base_inp=None) -> dict:
    sid = s.id
    rates = _rates()
    if not rates:
        return {"schedule_id": sid, "available": False}
    lines = _lines(inp)
    cr = _gen("gen_crew")
    size = {"SMT": int(cr.crew_size_smt.iloc[0]), "FAL": int(cr.crew_size_fal.iloc[0])} if cr is not None else {"SMT": 6, "FAL": 9}
    price = {o.order_id: o.unit_price_inr or 0 for o in inp.orders}
    c = defaultdict(float, {k: 0.0 for k in ("material", "labour", "line_time", "changeovers", "overtime", "late_penalty", "carrying")})
    for a in s.assignments:
        R = rates.get(a.plant_id) or next(iter(rates.values()))
        for kind, ln, setup in (("SMT", a.smt_line, a.smt_setup_min), ("FAL", a.fal_line, a.fal_setup_min)):
            L = lines.get(ln)
            run = a.qty / L.rate_per_min if L else 0
            per_min = R["smt_line_inr_per_min"] if kind == "SMT" else R["fal_line_inr_per_min"]
            c["line_time"] += run * per_min
            c["changeovers"] += setup * (per_min + size[kind] * R["labour_inr_per_hour"] / 60)
            c["labour"] += run * size[kind] * R["labour_inr_per_hour"] / 60
        c["late_penalty"] += a.late_days * a.qty * price.get(a.customer_order_id, 0) * R["late_penalty_pct_per_day"] / 100
        early = max(0.0, (_due_end(a.due) - a.fal_finish).total_seconds() / 86400)
        unit_mat = (a.material_cost_inr or 0) / a.qty if a.qty else 0
        c["carrying"] += early * a.qty * unit_mat * R["carrying_pct_per_year"] / 100 / 365
    c["material"] = s.summary.get("material_cost_inr", 0) or 0
    if base_inp is not None:                                        # overtime: shift minutes this what-if added over its base plan
        b = {l.line_id: l for l in base_inp.lines}
        for l in inp.lines:
            R = rates.get(l.plant_id) or next(iter(rates.values()))
            extra = sum(max(0.0, s1.minutes - s0.minutes) for s1, s0 in zip(l.slots, b[l.line_id].slots)) if l.line_id in b else 0
            c["overtime"] += extra * size[l.kind] * R["labour_inr_per_hour"] * R["overtime_multiplier"] / 60
    out = {k: round(v) for k, v in c.items()}
    return {"schedule_id": sid, "name": s.name, "available": True, "total_inr": sum(out.values()), "parts": out,
            "per_unit_inr": round(sum(out.values()) / max(1, s.summary.get("scheduled_qty", 0))) if s.summary.get("scheduled_qty") else None,
            "rates_source": GEN_NOTE}


def costs(base_id: str) -> list[dict]:
    """The working plan first (what the lines really make: the plan plus open production orders), then the plan and its what-ifs."""
    ref, x = working()
    w = _cost(ref, x)
    w["name"], w["schedule_id"] = "Working plan (incl. open production orders)", None
    ids = [base_id] + [r["id"] for r in store.list_(200) if r["base_id"] == base_id]
    return [w] + [cost(i) for i in ids[:12]]


# ------------------------------------------------------------------ 9. yields
def yields() -> dict:
    d = store.D()
    hp = d["fact_hourly_production"]
    pol = Policy()
    y = data.yields(hp, pol.yield_days)
    rows = []
    for k, v in y.items():
        l, p = k.split("|")
        if p == "*":
            continue
        avg = y.get(f"{l}|*")
        rows.append({"line_id": l, "product_id": p, "yield_pct": round(100 * v, 2), "line_avg_pct": round(100 * avg, 2) if avg else None,
                     "extra_material_pct": round(100 * (1 / v - 1), 2), "time_vs_line_pct": round(100 * (avg / v - 1), 2) if avg else None})
    h = hp.assign(w=pd.to_datetime(hp.event_date).dt.strftime("%G-W%V")).groupby(["w", "line_id"])[["good_qty", "actual_qty"]].sum()
    trend = (h.good_qty / h.actual_qty * 100).round(2).unstack().tail(12)
    return {"days": pol.yield_days, "rows": sorted(rows, key=lambda r: r["yield_pct"]), "in_plan": pol.yield_aware,
            "trend": [{"week": w, **{k: (None if pd.isna(v) else v) for k, v in r.items()}} for w, r in trend.iterrows()]}


# ------------------------------------------------------------------ 10. plan ahead: forecast, capacity, materials, and what to do when
def outlook(weeks: int = 12) -> dict:
    """The weeks ahead, so problems are handled before they happen.

    Demand per week   firm orders due that week, or the forecast of orders still to come (forecast of orders received, due about
                      two weeks later: the usual order-to-delivery time), whichever is higher.
    Capacity per week what final assembly in both plants really made per week over the last 4 weeks, less planned maintenance
                      and the share of the crew on leave (Diwali and the rest) that week.
    Production plan   make demand up to capacity; what does not fit carries to the next week (a backlog to recover).
    Materials         each material's stock (usable, in transit counted) goes down by what that plan needs (bill of materials,
                      scrap and yield). The week it falls below safety stock is when it runs short; a purchase must be ordered a
                      supplier lead time earlier, sized for four weeks of use and rounded up to the supplier's pack size.
    Actions           those purchases, plus extra shifts for overloaded weeks (or building ahead in an earlier week with room),
                      dated and most urgent first."""
    d = store.D()
    ref, x = working()
    t0 = x.as_of.date()
    W = [t0 + timedelta(weeks=i) for i in range(weeks)]          # weeks start on the plan's first day
    label = lambda dd: dd.strftime("%d %b")
    widx = lambda dd: (dd - t0).days // 7

    # demand: firm open orders by due week, and the forecast of orders still to come
    co = d["fact_customer_order"]
    firm = co[co.order_status.isin(Policy().open_statuses)].assign(q=lambda z: z.order_qty - z.shipped_qty)
    firm = firm[firm.q > 0]
    firm_w = defaultdict(lambda: defaultdict(float))
    for r in firm.itertuples():
        i = max(0, widx(date.fromisoformat(str(r.confirmed_delivery_date)[:10])))   # overdue orders count in the first week
        if i < weeks:
            firm_w[r.product_id][i] += r.q
    fc = forecast(weeks)
    prods = sorted(fc["products"])
    # orders still to come: each forecast week's orders fall due over the next weeks the way delivered orders did (order -> due)
    done = co[co.order_status == "DELIVERED"]
    lead = ((pd.to_datetime(done.confirmed_delivery_date) - pd.to_datetime(done.order_date)).dt.days // 7).clip(0, 6)
    share_lead = lead.value_counts(normalize=True).sort_index().to_dict() if len(lead) else {1: 0.5, 2: 0.5}
    fc_w = {p: [0.0] * weeks for p in prods}
    for p in prods:
        for j, f in enumerate(fc["products"][p]["forecast"][:weeks]):
            for k, sh in share_lead.items():
                if j + k < weeks:
                    fc_w[p][j + k] += f["qty"] * sh
    dem = {p: [firm_w[p][i] + fc_w[p][i] for i in range(weeks)] for p in prods}

    # capacity: what final assembly really made per week (last 4 weeks), less maintenance and crew on leave
    hp = d["fact_hourly_production"]
    last = pd.Timestamp(hp.event_date.max())
    recent = hp[(hp.line_id.str.startswith("FAL")) & (hp.event_date > (last - pd.Timedelta(days=28)).strftime("%Y-%m-%d"))]
    per_week = float(recent.good_qty.sum()) / 4
    fal = [l for l in x.lines if l.kind == "FAL"]
    rate_min = sum(l.rate_per_min for l in fal) / max(1, len(fal))
    maint = defaultdict(float)
    for w in x.maintenance:
        i = widx(w["start"].date())
        if 0 <= i < weeks and w["line_id"].startswith("FAL"):
            maint[i] += w["minutes"] * rate_min
    lv, cr = _gen("gen_crew_leave"), _gen("gen_crew")
    leave = defaultdict(float)
    if lv is not None and cr is not None:
        head = float(cr.headcount.sum())                          # people rostered per day (all plants and shifts)
        lv = lv.assign(i=pd.to_datetime(lv.date).dt.date.map(widx))
        for i, g in lv[(lv.i >= 0) & (lv.i < weeks)].groupby("i"):
            share = float(g.people_on_leave.sum()) / (head * 7)
            leave[int(i)] = max(0.0, share - 0.08)                # the usual ~8 % absence is already in what the lines made
    cap = [max(0.0, per_week * (1 - leave[i]) - maint[i]) for i in range(weeks)]

    # production plan with backlog
    tot = [sum(dem[p][i] for p in prods) for i in range(weeks)]
    made, backlog, carry = [], [], 0.0
    for i in range(weeks):
        want = tot[i] + carry
        m = min(cap[i], want)
        made.append(m)
        carry = want - m
        backlog.append(carry)
    share = {p: [(dem[p][i] / tot[i]) if tot[i] else 0 for i in range(weeks)] for p in prods}

    # materials: weekly use for that plan, projected stock, when each runs short, what to buy by when
    bom = defaultdict(list)
    for b in x.bom:
        bom[b.product_id].append(b)
    yl = x.yields
    def mf(p):
        ys = [v for k, v in yl.items() if k.endswith("|" + p)]
        return 1 / (np.prod(ys[:2]) if len(ys) >= 2 else 0.97)
    use = defaultdict(lambda: [0.0] * weeks)
    for i in range(weeks):                                        # material to meet demand (adding capacity is the other action)
        for p in prods:
            units = tot[i] * share[p][i]
            for b in bom[p]:
                use[b.material_id][i] += units * b.qty_per * (1 + b.scrap) * mf(p)
    stock, safety, cost = defaultdict(float), defaultdict(float), {}
    for st in x.stock:
        stock[st.material_id] += st.available
        safety[st.material_id] += st.safety
        cost[st.material_id] = st.unit_cost_inr or 0
    sup = _gen("gen_material_supplier")
    supi = sup.set_index("material_id") if sup is not None else None
    # usual deliveries: what arrived per week over the last 4 weeks (rise in stock + what the lines used), pooled across plants
    inv = d["fact_inventory_daily_snapshot"]
    st_d = inv.groupby(["snapshot_date", "material_id"]).on_hand_qty.sum().unstack(fill_value=0).sort_index()
    fa = hp[hp.line_id.str.startswith("FAL")]
    bomdf = d["fact_bom_component"].assign(per=lambda z: z.component_qty * (1 + z.scrap_factor))
    used = fa.groupby(["event_date", "product_id"]).actual_qty.sum().reset_index().merge(bomdf[["product_id", "material_id", "per"]], on="product_id")
    used_d = used.assign(u=used.actual_qty * used.per).groupby(["event_date", "material_id"]).u.sum().unstack(fill_value=0)
    rec = (st_d.diff().add(used_d.reindex(st_d.index).fillna(0), fill_value=0)).clip(lower=0).tail(28)
    inflow = (rec.sum() / 4).to_dict()                          # units per week
    mats, actions = [], []
    for m_id in sorted(use):
        bal, proj, with_buy, short_i, buys = stock[m_id], [], [], None, []
        lead = int(supi.lead_time_days.get(m_id, 14)) if supi is not None else 14
        pack = int(supi.pack_qty.get(m_id, 1)) if supi is not None else 1
        moq = int(supi.moq.get(m_id, 0)) if supi is not None else 0
        arriving = defaultdict(float)
        b2 = stock[m_id]
        inw = float(inflow.get(m_id, 0.0))
        for i in range(weeks):
            bal += inw - use[m_id][i]
            proj.append(round(bal))
            b2 += inw + arriving[i] - use[m_id][i]
            if b2 < safety[m_id]:                                 # plan a delivery that keeps the next four weeks covered
                if short_i is None:
                    short_i = i
                need = safety[m_id] - b2 + max(0.0, sum(use[m_id][i + 1:i + 5]) - 4 * inw)   # four weeks beyond usual deliveries
                qty = max(moq, math.ceil(need / pack) * pack)
                arrive = W[i]
                order_by = arrive - timedelta(days=lead)
                buys.append({"order_by": order_by.isoformat(), "arrives": arrive.isoformat(), "week": label(W[i]), "qty": qty, "lead_days": lead,
                             "supplier": supi.supplier_id.get(m_id) if supi is not None else None, "late_by_days": max(0, (t0 - order_by).days),
                             "value_inr": round(qty * cost.get(m_id, 0))})
                b2 += qty
            with_buy.append(round(b2))
        weekly = sum(use[m_id]) / weeks
        gap = weekly - inw
        cover = None if gap <= 0 else round(max(0.0, stock[m_id] - safety[m_id]) / gap, 1)
        mats.append({"material_id": m_id, "stock": round(stock[m_id]), "safety": round(safety[m_id]), "weekly_use": round(weekly), "weekly_in": round(inw),
                     "cover_weeks": cover,
                     "short_week": label(W[short_i]) if short_i is not None else None, "short_index": short_i, "projected": proj, "with_purchases": with_buy,
                     "buys": buys})
        for b in buys[:3]:
            late = b["late_by_days"] > 0
            actions.append({"date": b["order_by"], "kind": "buy", "severity": "bad" if late else "warn" if (date.fromisoformat(b["order_by"]) - t0).days <= 7 else "info",
                            "title": f"Order {b['qty']:,} × {m_id} from {b['supplier'] or 'the supplier'}",
                            "detail": (f"{b['late_by_days']} days late already: ask {b['supplier']} to expedite. " if late else "")
                                      + f"Needed by the week of {b['week']} ({b['lead_days']}-day lead time) to stay above safety stock."})
    over_w = [i for i in range(weeks) if tot[i] > 1.02 * cap[i]]
    rate_h = rate_min * 60 * max(1, len(fal))                    # units per hour, both final-assembly lines
    if len(over_w) >= max(3, weeks // 3):                          # a lasting gap: say it once, with its size
        gap_u = sum(tot[i] - cap[i] for i in over_w) / len(over_w)
        actions.append({"date": W[over_w[0]].isoformat(), "kind": "capacity", "severity": "bad",
                        "title": f"Demand is above capacity in {len(over_w)} of {weeks} weeks, by about {round(gap_u):,} units a week",
                        "detail": f"Final assembly makes about {round(per_week):,} units a week. Options: an extra shift or line (about {math.ceil(gap_u / rate_h)} "
                                  f"line-hours a week), agree later dates for low-priority orders, or move work between plants. Without action the backlog "
                                  f"reaches {round(backlog[-1]):,} units by the week of {label(W[-1])}."})
    else:
        for i in over_w:
            over = tot[i] - cap[i]
            room = [j for j in range(max(0, i - 3), i) if cap[j] - made[j] > 0.05 * cap[j]]
            actions.append({"date": (W[i] - timedelta(days=7)).isoformat(), "kind": "capacity", "severity": "warn",
                            "title": f"Week of {label(W[i])}: {round(over):,} units more than the lines can make",
                            "detail": (f"Build ahead in the week of {label(W[room[-1]])} (spare room), or " if room else "")
                                      + f"add about {math.ceil(over / rate_h)} hours of overtime on final assembly"
                                      + (f"; {round(100 * leave[i])}% of the crew is on leave that week" if leave[i] > 0.02 else "") + "."})
    for i in range(weeks):
        if leave[i] > 0.1:
            actions.append({"date": (W[i] - timedelta(days=10)).isoformat(), "kind": "crew", "severity": "warn",
                            "title": f"Week of {label(W[i])}: {round(100 * (leave[i] + 0.08))}% of the crew on leave",
                            "detail": f"Capacity drops by about {round(per_week * leave[i]):,} units. Agree leave cover or build ahead the week before."})
    if maint:
        actions.append({"date": t0.isoformat(), "kind": "maintenance", "severity": "info",
                        "title": f"Planned maintenance holds about {round(sum(maint.values())):,} units of final-assembly time over {weeks} weeks",
                        "detail": "Already taken out of capacity week by week; see the weekly table. Keep services in their slots."})
    sev = {"bad": 0, "warn": 1, "info": 2}
    actions.sort(key=lambda a: (a["date"], sev[a["severity"]]))
    mats.sort(key=lambda m: (m["short_index"] if m["short_index"] is not None else 99, m["material_id"]))
    wk = [{"week": label(W[i]), "start": W[i].isoformat(), "firm": round(sum(firm_w[p][i] for p in prods)),
           "forecast": round(sum(fc_w[p][i] for p in prods)), "demand": round(tot[i]), "capacity": round(cap[i]),
           "planned": round(made[i]), "backlog": round(backlog[i]), "load_pct": round(100 * tot[i] / cap[i], 1) if cap[i] else None,     # this week's demand against capacity
           "maintenance_units": round(maint[i]), "leave_pct": round(100 * leave[i], 1)} for i in range(weeks)]
    out_ = {"as_of": t0.isoformat(), "weeks": wk, "materials": mats, "actions": actions,
            "summary": {"demand": round(sum(tot)), "capacity": round(sum(cap)), "planned": round(sum(made)), "backlog_end": round(backlog[-1]),
                        "weeks_over": sum(1 for w in wk if (w["load_pct"] or 0) > 100), "materials_to_buy": sum(1 for m in mats if m["buys"]),
                        "buy_value_inr": sum(b["value_inr"] for m in mats for b in m["buys"]),
                        "late_orders_to_place": sum(1 for m in mats for b in m["buys"] if b["late_by_days"] > 0),
                        "actions_this_week": sum(1 for a in actions if (date.fromisoformat(a["date"]) - t0).days <= 7)},
            "forecast_error_pct": {p: v["mape_pct"] for p, v in fc["products"].items()},
            "method": "Demand: firm orders by due week plus the damped-trend forecast of orders still to come, spread over the weeks "
                      "they fall due the way delivered orders did. Capacity: what final assembly made per week over the last 4 weeks, less planned "
                      "maintenance and crew leave. Materials: stock plus usual deliveries less what meeting demand needs (bill of materials, scrap, yield); usual "
                      "deliveries (last 4 weeks) continue; extra purchases keep four weeks above safety stock, ordered one supplier lead time ahead.",
            "sources": {"suppliers": "generated" if sup is not None else None, "crew_leave": "generated" if lv is not None else None}}
    out_["reasoning"] = reasoning(out_)
    return out_


# ------------------------------------------------------------------ 10. history and forecast, one series per section (dashboard card)
SECTIONS = {"demand": ("Demand", "units ordered a week"), "production": ("Production", "good units made a week"),
            "inventory": ("Inventory", "usable material in stock, end of week"), "machinery": ("Machinery", "line availability, % of shift time running"),
            "workforce": ("Workforce", "people available per shift, average")}


def forecast_series(section: str, weeks: int = 8, plant: str | None = None) -> dict:
    """What happened week by week (history) and what is expected next (forecast) for one section, so both can be drawn on one chart.
    Demand, production and machinery: Holt's damped trend on the weekly history. Inventory (both plants): the outlook's projected stock
    (usual deliveries in, the plan's needs out); for one plant, the same damped trend. Workforce: the roster minus planned leave."""
    if section not in SECTIONS:
        raise KeyError(section)
    d = store.D()
    wk = lambda s: pd.to_datetime(s).dt.to_period("W-SUN")
    label, unit = SECTIONS[section]
    hist, fc, method = pd.Series(dtype=float), [], ""

    def trend(h: pd.Series, pct: bool = False):
        """Damped trend, plus the recent week-to-week pattern carried forward (what each of the last 4 weeks did against its
        neighbours), with a likely range (80%) that widens with distance from the last recorded week."""
        y = h.values.astype(float)
        f = _holt(y, weeks) if len(y) >= 3 else np.repeat(y[-1] if len(y) else 0.0, weeks)
        band = np.zeros(weeks)
        if len(y) >= 6:
            ma = pd.Series(y).rolling(3, center=True, min_periods=1).mean().values
            pat = (y - ma)[-4:]
            f = f + np.array([pat[i % len(pat)] for i in range(weeks)]) * 0.8
            sd = float(np.std(np.diff(y[-8:]))) / np.sqrt(2)
            band = 1.28 * sd * np.sqrt(np.arange(1, weeks + 1)) * 0.75
        lo, hi = f - band, f + band
        if pct:
            f, lo, hi = np.clip(f, 0, 100), np.clip(lo, 0, 100), np.clip(hi, 0, 100)
        else:
            f, lo = np.maximum(f, 0), np.maximum(lo, 0)
        last, r = h.index[-1], (1 if pct else 0)
        return [{"week": str((last + i + 1).start_time.date()), "v": round(float(v), r), "lo": round(float(l), r), "hi": round(float(u), r)}
                for i, (v, l, u) in enumerate(zip(f, lo, hi))]

    if section == "demand":
        co = d["fact_customer_order"]
        g = co.assign(w=wk(co.order_date)).groupby("w").order_qty.sum()
        hist = g[g.index <= pd.Period(pd.Timestamp(co.order_date.max()), "W-SUN") - 1]
        fc, method = trend(hist), "Trend of weekly ordered units with the recent week-to-week pattern; shaded = likely range (80%)"
    elif section in ("production", "machinery"):
        hp = d["fact_hourly_production"]
        if plant:
            hp = hp[hp.plant_id == plant]
        hp = hp.assign(w=wk(hp.event_date))
        full = hp.groupby("w").event_date.nunique()
        keep = full[full >= 7].index                                  # full weeks only
        if section == "production":
            hist = hp.groupby("w").good_qty.sum().loc[keep]
            fc, method = trend(hist), "Trend of weekly good units with the recent week-to-week pattern; shaded = likely range (80%)"
        else:
            g = hp.groupby("w").agg(down=("downtime_min", "sum"), hours=("hour", "size"))
            hist = (100 * (1 - g.down / (g.hours * 60))).loc[keep].round(1)
            fc, method = trend(hist, pct=True), "Trend of weekly line availability with the recent week-to-week pattern; shaded = likely range (80%)"
    elif section == "inventory":
        inv = d["fact_inventory_daily_snapshot"]
        inv = inv[inv.warehouse_id.isin(Policy().material_warehouses)]
        if plant:
            inv = inv[inv.plant_id == plant]
        free = (inv.on_hand_qty - inv.allocated_qty - inv.blocked_qty - inv.quality_hold_qty + inv.in_transit_qty).clip(lower=0)
        daily = inv.assign(free=free).groupby("snapshot_date").free.sum()
        daily.index = pd.to_datetime(daily.index)
        hist = daily.groupby(daily.index.to_period("W-SUN")).last()
        if plant:
            fc, method = trend(hist), "Trend of this plant's weekly stock with the recent week-to-week pattern; shaded = likely range (80%)"
        else:
            o = outlook(weeks)
            fc = [{"week": w["start"], "v": int(sum(max(0, m["projected"][i]) for m in o["materials"]))} for i, w in enumerate(o["weeks"])]
            method = "Planner's projection: stock now, plus usual deliveries, minus what the plan needs each week"
    else:                                                               # workforce: known from the roster and planned leave
        cr, lv = d.get("gen_crew"), d.get("gen_crew_leave")
        if cr is None or lv is None:
            return {"section": section, "label": label, "unit": unit, "history": [], "forecast": [], "available": False}
        if plant:
            cr, lv = cr[cr.plant_id == plant], lv[lv.plant_id == plant]
        head = cr.headcount.sum()
        g = lv.assign(w=wk(lv.date)).groupby("w").agg(leave=("people_on_leave", "sum"), days=("date", "nunique"))
        shifts = max(1, len(cr))
        fc = [{"week": str(k.start_time.date()), "v": round(float(head - r.leave / max(1, r.days)) / shifts, 1)} for k, r in g.head(weeks).iterrows()]
        method = "Crew roster minus planned leave (no past attendance is recorded)"
    h = [{"week": str(k.start_time.date()), "v": round(float(v), 1)} for k, v in hist.tail(13).items()]
    last = h[-1]["v"] if h else None
    nxt = fc[0]["v"] if fc else None
    avg_f = sum(p["v"] for p in fc) / len(fc) if fc else None
    avg_h = sum(p["v"] for p in h[-4:]) / len(h[-4:]) if h else None
    out_wk = next((p["week"] for p in fc if p["v"] <= 0), None) if section == "inventory" else None
    return {"section": section, "label": label, "unit": unit, "plant": plant, "available": True, "method": method, "history": h, "forecast": fc,
            "runs_out": out_wk,
            "summary": {"last": last, "next": nxt, "forecast_avg": round(avg_f, 1) if avg_f is not None else None,
                        "change_pct": round(100 * (avg_f - avg_h) / avg_h, 1) if avg_f is not None and avg_h else None}}


# ------------------------------------------------------------------ 11. the use case's seven success measures, for one plan
_kpi_cache: dict = {}                     # plan id (or None = the working plan) -> (time, answer)
KPI_S = 120


def kpis(sid: str | None = None) -> dict:
    """The seven success measures, kept for two minutes (they read several plans from the database)."""
    hit = _kpi_cache.get(sid)
    if hit and time.monotonic() - hit[0] < KPI_S:
        return hit[1]
    out = _kpis(sid)
    _kpi_cache[sid] = (time.monotonic(), out)
    return out


def _kpis(sid: str | None = None) -> dict:
    """Plan adherence, schedule attainment, on-time delivery, changeover time, WIP, capacity utilisation and schedule stability.
    Plan figures come from the plan (default: the plan everyone works from); attainment and adherence from what the floor made."""
    base, _ = incidents._base()
    s = store.get(sid) if sid else base
    if s is None:
        raise KeyError(sid)
    m, d = s.summary, store.D()
    # schedule attainment: good units against target, last 30 days of production records
    hp = d["fact_hourly_production"]
    last = pd.Timestamp(hp.event_date.max())
    cur = hp[pd.to_datetime(hp.event_date) > last - pd.Timedelta(days=30)]
    att = round(100 * cur.good_qty.sum() / cur.target_qty.sum(), 1) if cur.target_qty.sum() else None
    # plan adherence: finished production orders done within a day of their planned finish (production confirmations)
    try:
        adh = adherence()
    except Exception:                                                  # missing confirmations: shown as not available
        adh = {}
    otif = (adh.get("otif") or {}).get("otif_pct")
    # stability: % of the earlier plan's orders that kept plant, quantity and start
    stab, vs = m.get("stability_pct"), "plan it replaced"
    if stab is None:
        ids = [r["id"] for r in store.list_(50) if not store.is_what_if(r)]
        prev = base if base.id != s.id else (store.get(ids[ids.index(s.id) + 1]) if s.id in ids and ids.index(s.id) + 1 < len(ids) else None)
        if prev is not None:
            stab, vs = planning.diff(prev, s)[1], ("approved plan" if prev.id == base.id and base.status in ("APPROVED", "PUBLISHED") else "previous plan")
    # WIP (estimate): units waiting between circuit boards and final assembly, averaged over the plan's days
    a = s.assignments
    days = max(1.0, (max(x.fal_finish for x in a) - min(x.smt_start for x in a)).total_seconds() / 86400) if a else 1.0
    wip = round(m.get("wip_unit_days_proxy", 0) / days) if a else None
    util = m.get("avg_utilisation")
    tone = lambda v, good, ok: None if v is None else "ok" if v >= good else "warn" if v >= ok else "bad"
    items = [
        {"key": "adherence", "label": "Plan adherence", "value": adh.get("on_plan_pct"), "unit": "%", "tone": tone(adh.get("on_plan_pct"), 90, 75),
         "note": f"of {adh['finished_orders']} finished orders on plan (±1 day)" if adh.get("finished_orders") else "no finished orders to compare",
         "estimate": True},
        {"key": "attainment", "label": "Schedule attainment", "value": att, "unit": "%", "tone": tone(att, 95, 85), "note": "good units vs target, 30 days"},
        {"key": "on_time", "label": "On-time delivery", "value": m.get("on_time_qty_pct"), "unit": "%", "tone": tone(m.get("on_time_qty_pct"), 95, 80),
         "note": "of units on time in this plan" + (f" · {otif}% fully on time so far" if otif is not None else "")},
        {"key": "changeover", "label": "Changeover time", "value": round(m.get("changeover_min", 0) / 60, 1) if a else None, "unit": "h", "tone": None,
         "note": f"{m.get('changeovers', 0)} product changes"},
        {"key": "wip", "label": "Work in progress", "value": wip, "unit": "units", "tone": None,
         "note": "units between board and final assembly, per day", "estimate": True},
        {"key": "utilisation", "label": "Capacity utilisation", "value": round(100 * util, 1) if util is not None else None, "unit": "%",
         "tone": None if util is None else "bad" if util > 0.95 else "ok" if util >= 0.6 else "warn",
         "note": f"average · busiest {m['bottleneck_line']}" if m.get("bottleneck_line") else "average line use"},
        {"key": "stability", "label": "Schedule stability", "value": stab, "unit": "%", "tone": tone(stab, 80, 60),
         "note": f"orders unchanged vs {vs}" if stab is not None else "no earlier plan to compare with"},
    ]
    return {"schedule_id": s.id, "schedule_name": s.name, "status": s.status, "items": items}


# ------------------------------------------------------------------ 12. material readiness: can the plan be built with the stock there is
def readiness(sid: str | None = None) -> dict:
    """Uses the planner's own material rules (stock at plan start minus safety stock, pooled or per plant, rejects per line):
    room   how many more phones the stock left after this plan can build, per product, and the material that stops it
    held   orders the plan could not (fully) schedule for lack of material, with their value and the limiting material
    kits   material to issue to each line each day, and the day the plan uses the last of a material
    risk   scheduled jobs short of material (partly covered, or fixed jobs the stock no longer covers), and the jobs that use the last of it"""
    import re
    s = store.get(sid) if sid else incidents._base()[0]
    if s is None:
        raise KeyError(sid)
    inp, pol = store.inputs(s.id), Policy(**s.policy)
    bom = defaultdict(list)
    for b in inp.bom:
        bom[b.product_id].append(b)
    reserve = pol.respect_safety_stock
    start = defaultdict(float)
    for st in inp.stock:
        start[(st.plant_id, st.material_id)] += max(0.0, st.available - (st.safety if reserve else 0))
    pooled = any(k[0] == "ALL" for k in start)
    key = (lambda p: "ALL") if pooled else (lambda p: p)
    pair = defaultdict(list)                                           # the planner's line pair per plant: first SMT and first FAL line
    for kind in ("SMT", "FAL"):
        for ln in inp.lines:
            if ln.kind == kind and not any(x.startswith(kind) for x in pair[ln.plant_id]):
                pair[ln.plant_id].append(ln.line_id)
    yl = inp.yields if pol.yield_aware else {}

    def mf(plant, product):                                            # material per good unit, as the planner counts it
        f = 1.0
        for ln in pair.get(plant, []):
            y = yl.get(f"{ln}|{product}") or yl.get(f"{ln}|*")
            f /= y if y else 1.0
        return f

    orders = {o.order_id: o for o in inp.orders}
    left = dict(start)
    used_by_day = []                                                   # (job, material use)
    for a in sorted(s.assignments, key=lambda a: a.smt_start):
        o = orders.get(a.customer_order_id)
        use = {} if (o and o.kitted) else {b.material_id: a.qty * b.qty_per * (1 + b.scrap) * mf(a.plant_id, a.product_id) for b in bom[a.product_id]}
        for m, q in use.items():
            left[(key(a.plant_id), m)] = left.get((key(a.plant_id), m), 0.0) - q
        used_by_day.append((a, use))

    # room to build more, per stock scope and product
    scopes = sorted({k[0] for k in start})
    prods = sorted(bom)
    room = []
    for sc in scopes:
        plant = None if sc == "ALL" else sc
        for p in prods:
            f = mf(plant, p) if plant else min(mf(x, p) for x in pair) if pair else 1.0   # both plants: the plant that uses least
            n, lim = None, None
            for b in bom[p]:
                k = math.floor(max(0.0, left.get((sc, b.material_id), 0.0)) / (b.qty_per * (1 + b.scrap) * f) + 1e-9)
                if n is None or k < n:
                    n, lim = k, b.material_id
            made = sum(a.qty for a, _ in used_by_day if a.product_id == p and key(a.plant_id) == sc)
            room.append({"scope": "Both plants" if sc == "ALL" else sc, "product_id": p, "planned_qty": made, "more_qty": n or 0, "limiting": lim})

    # orders held back by material
    from .intake_assess import _prices
    price = _prices(inp)                                               # orders without a price: the product's usual price
    held = []
    for u in s.unscheduled:
        if u.reason not in ("MATERIAL_SHORT", "MATERIAL_BLOCKED"):
            continue
        o, m = orders.get(u.customer_order_id), re.search(r"limiting (\S+)", u.detail or "")
        held.append({"order_id": u.customer_order_id, "product_id": u.product_id, "qty": u.qty, "priority": u.priority,
                     "due": str(o.due) if o else None, "partly": u.reason == "MATERIAL_SHORT", "limiting": m.group(1) if m else None,
                     "value_inr": round(u.qty * price.get(u.customer_order_id, 0))})
    held.sort(key=lambda r: (pol.priority_rank.get(r["priority"], 99), r["due"] or ""))

    # kits per line and day; the day a material's planned stock is used up
    run = dict(start)
    kits, out_on, last_jobs = {}, {}, set()
    for a, use in used_by_day:
        day = a.smt_start.date().isoformat()
        r = kits.setdefault((day, a.smt_line), {"date": day, "line_id": a.smt_line, "plant_id": a.plant_id, "jobs": 0, "phones": 0,
                                                "parts": 0, "runs_out": []})
        r["jobs"] += 1
        r["phones"] += a.qty
        for m, q in use.items():
            k = (key(a.plant_id), m)
            run[k] = run.get(k, 0.0) - q
            r["parts"] += round(q)
            # ponytail: "used up" = under 1% of the start stock left (the planner's float rounding); exact once stock is in whole units
            if k not in out_on and run[k] <= 0.01 * max(1.0, start.get(k, 0.0)):
                out_on[k] = day
                r["runs_out"].append(m)
                last_jobs.add((a.customer_order_id, a.smt_start))
    kits = sorted(kits.values(), key=lambda r: (r["date"], r["line_id"]))

    # jobs at risk
    risk = []
    for a, use in used_by_day:
        why = "partly covered by stock" if "PARTIAL_MATERIAL" in a.reason_codes else "stock no longer covers this fixed job" if a.material_short \
            else "uses the last of a material" if (a.customer_order_id, a.smt_start) in last_jobs else None
        if why:
            risk.append({"order_id": a.customer_order_id, "product_id": a.product_id, "qty": a.qty, "plant_id": a.plant_id, "line_id": a.smt_line,
                         "start": a.smt_start.isoformat(), "due": a.due, "late": a.status == "LATE_RISK", "why": why})
    return {"schedule_id": s.id, "schedule_name": s.name, "pooled": pooled, "safety_reserved": reserve,
            "room": room, "held": held, "held_units": sum(r["qty"] for r in held), "held_value_inr": sum(r["value_inr"] or 0 for r in held),
            "kits": kits, "runs_out": [{"material_id": m, "scope": "Both plants" if p == "ALL" else p, "date": d} for (p, m), d in sorted(out_on.items(), key=lambda x: x[1])],
            "risk": risk}


# ------------------------------------------------------------------ 13. AI reasoning on the weeks ahead (built from the outlook's own figures)
def _k(v) -> str:
    v = float(v or 0)
    return f"{v / 1e6:.1f}M" if abs(v) >= 1e6 else f"{v / 1e3:.1f}K" if abs(v) >= 1e3 else f"{v:,.0f}"


def _inr(v) -> str:
    v = float(v or 0)
    return f"₹{v / 1e7:.1f} Cr" if v >= 1e7 else f"₹{v / 1e5:.1f} L" if v >= 1e5 else f"₹{v:,.0f}"


def reasoning(o: dict) -> dict:
    """AI reasoning for the Coming weeks card: one short summary (3-4 sentences) picked at random from 10 differently worded summaries,
    each filled with the outlook's own figures. A new pick after each finished planning-agent run (its time seeds the choice), the same
    pick until the next run."""
    import random
    from . import intake_flow
    wk, mats, S = o["weeks"], o["materials"], o["summary"]
    if not wk:
        return {"at": None, "text": "", "pick": None, "of": 10}
    loaded = [w for w in wk if w["load_pct"] is not None] or wk
    peak = max(loaded, key=lambda w: w["load_pct"] or 0)
    quiet = min(loaded, key=lambda w: w["load_pct"] or 0)
    over = [w for w in loaded if (w["load_pct"] or 0) > 100]
    dem, cap = S.get("demand") or 0, S.get("capacity") or 0
    cover = min(100, round(100 * cap / dem)) if dem else 100
    firm, fc = sum(w["firm"] for w in wk), sum(w["forecast"] for w in wk)
    firm_pct = round(100 * firm / (firm + fc)) if firm + fc else 0
    short = next((m for m in mats if m.get("short_week")), None)
    buy_n, buy_v, late_n = S.get("materials_to_buy") or 0, S.get("buy_value_inr") or 0, S.get("late_orders_to_place") or 0
    leave = max(wk, key=lambda w: w["leave_pct"])
    backlog = S.get("backlog_end") or 0
    err = [v for v in (o.get("forecast_error_pct") or {}).values() if v is not None]
    err_pct = sum(err) / len(err) if err else None
    n = len(wk)
    f = dict(peak=peak["week"], peak_pct=f"{peak['load_pct'] or 0:.0f}", quiet=quiet["week"], quiet_pct=f"{quiet['load_pct'] or 0:.0f}",
             over=len(over), n=n, first_over=over[0]["week"] if over else None, dem=_k(dem), cap=_k(cap), cover=cover, firm_pct=firm_pct,
             mat=short["material_id"] if short else None, mat_week=short["short_week"] if short else None, buy_n=buy_n, buy_v=_inr(buy_v),
             late_n=late_n, leave=leave["week"], leave_pct=f"{leave['leave_pct']:.0f}", backlog=_k(backlog),
             err=f"{err_pct:.1f}" if err_pct is not None else None, acts=S.get("actions_this_week") or 0)
    cap_line = (f"{f['over']} of the next {n} weeks ask for more than the lines can make, starting the week of {f['first_over']}"
                if over else f"all {n} weeks fit within what the lines can make")
    mat_line = (f"{f['mat']} is the first material to run short, in the week of {f['mat_week']}" if short else "no material runs short")
    buy_line = (f"{buy_n} materials need buying (about {f['buy_v']})" + (f", and {late_n} purchase orders are already late to place" if late_n else "")
                if buy_n else "no extra purchases are needed")
    S10 = [
        f"Demand over the next {n} weeks is {f['dem']} units against {f['cap']} the lines can make, so about {cover}% can be built on time. "
        f"The pressure peaks in the week of {f['peak']} at {f['peak_pct']}% of capacity. {mat_line[0].upper() + mat_line[1:]}. "
        f"Start with the late purchase orders, then build ahead where there is room.",
        f"The week of {f['peak']} is the hardest, asking for {f['peak_pct']}% of what the lines can make, and {cap_line}. "
        f"Only {firm_pct}% of the demand ahead is firm, so the forecast part may still move. "
        f"{buy_line[0].upper() + buy_line[1:]}.",
        f"Materials, not just line time, limit the weeks ahead: {mat_line}. {buy_line[0].upper() + buy_line[1:]}. "
        f"With {f['dem']} units of demand and room for {f['cap']}, about {cover}% can be met on time. Expedite the late orders with the suppliers first.",
        f"{cap_line[0].upper() + cap_line[1:]}. By the end of the horizon about {f['backlog']} units are still waiting to be made. "
        f"The lightest week, {f['quiet']} at {f['quiet_pct']}% of capacity, is the best chance to build ahead. Keep an eye on {f['mat'] or 'material cover'} meanwhile.",
        f"{firm_pct}% of the next {n} weeks' demand is firm orders and the rest is forecast"
        + (f", which was off by {f['err']}% on recent weeks" if f['err'] else "") + f". Even so, the week of {f['peak']} needs {f['peak_pct']}% of capacity. "
        f"{buy_line[0].upper() + buy_line[1:]}, so purchasing is the first lever to pull.",
        f"The plan for the coming weeks is short of both time and parts: about {cover}% of demand fits the lines, and {mat_line}. "
        f"{late_n or 'No'} purchase order{'s are' if late_n != 1 else ' is'} already past the order-by date. "
        f"The week of {f['leave']} also loses about {f['leave_pct']}% of the crew to leave, so agree cover early.",
        f"Start with purchasing: {buy_line}. Then protect the week of {f['peak']}, which asks for {f['peak_pct']}% of what the lines can make. "
        f"{f['acts']} things on the list need doing this week, and the week of {f['quiet']} is the lightest, the place to build ahead.",
        f"Looking {n} weeks ahead, {cap_line}. {mat_line[0].upper() + mat_line[1:]}, which caps output before line time does. "
        f"About {f['backlog']} units would still be waiting at the end, so later promise dates or a second shift are worth discussing now.",
        f"Of {f['dem']} units due over the next {n} weeks, the lines can make about {f['cap']}, roughly {cover}%. "
        f"The busiest week is {f['peak']} ({f['peak_pct']}% of capacity) and the quietest is {f['quiet']} ({f['quiet_pct']}%). "
        f"Moving work into the quiet weeks and placing the {buy_n} purchases early gives the plan the best chance.",
        f"Three things decide the weeks ahead: line time ({cover}% of demand fits), parts ({mat_line}) and people "
        f"(about {f['leave_pct']}% of the crew is on leave in the week of {f['leave']}). "
        f"{buy_line[0].upper() + buy_line[1:]}. Place the late orders today and build ahead in the week of {f['quiet']}.",
    ]
    at = intake_flow.ran().get("at")
    i = random.Random(at or "before-first-run").randrange(len(S10))
    return {"at": at, "text": S10[i], "pick": i + 1, "of": len(S10)}


def _clear():
    _cache.clear()
    _kpi_cache.clear()


from . import coord as _coord  # noqa: E402
_coord.on_change(_clear)

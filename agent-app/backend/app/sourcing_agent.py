"""Sourcing agent: work that cannot start in its plant for lack of material, and whether the other plant can take it.

It never suggests a move just because the other plant has the parts. For each production order blocked for material at home
(each plant planned with its own stock), it checks the other plant on every count and only suggests a move when all pass:
  material     the other plant has every part for the remaining quantity, after its own planned work takes what it needs
  capacity     the planning engine fits the order on the other plant's lines (with that plant's planned services blocked)
  own orders   none of the other plant's orders becomes late or short of material because of it
  lines now    the other plant's lines are not down now and have no open problem
  maintenance  planned services on those lines during the job (already held out of the plan) are listed
  crew         the shifts the job uses have the people (roster and leave), or the overtime is stated
  product      the other plant has made this product recently, at a yield no worse than home
  gain         it finishes earlier than waiting at home for the parts (supplier lead time), and the time saved is worth more
               than the transfer cost
If making it there fails a check but sending the parts home would work (home lines free, other plant's orders unaffected), it
suggests that instead. Otherwise it records why no move helps. People decide; the agent changes nothing.
"""
import math
import threading
import time
from collections import defaultdict
from types import SimpleNamespace
from datetime import timedelta
from . import incidents, planning, planning_tools as T, store, twin
from .config import Policy

NAME = "sourcing agent"
PLANTS = ("IN-KA-01", "IN-TN-01")
CITY = {"IN-KA-01": "Bengaluru", "IN-TN-01": "Hosur"}
_cache: dict = {}
_lock = threading.Lock()


def _left_by_plant(ref, x) -> dict:
    """(plant, material) -> usable stock above safety after that plant's planned jobs take their material."""
    left = defaultdict(float)
    for st in x.stock:
        left[(st.plant_id, st.material_id)] += max(0.0, st.available - st.safety)
    kit = {o.order_id for o in x.orders if o.kitted}
    bom = defaultdict(list)
    for b in x.bom:
        bom[b.product_id].append(b)
    for a in ref.assignments:
        if a.customer_order_id not in kit:
            for b in bom[a.product_id]:
                left[(a.plant_id, b.material_id)] -= a.qty * b.qty_per * (1 + b.scrap)
    return left


def _plant_of(s) -> dict:
    return {incidents._cid(a.customer_order_id): a.plant_id for a in s.assignments}


def _unsched(s, plant_orders: set) -> int:
    return sum(u.qty for u in s.unscheduled if u.customer_order_id in plant_orders)


def analyse(limit: int = 8) -> dict:
    key = int(time.time() // 300)
    with _lock:
        if key in _cache:
            return _cache[key]
    ref, x = T.working("plant")
    pol = Policy(**ref.policy)
    d = store.D()
    orders = {o.order_id: o for o in x.orders}
    blocked = []
    for a in ref.assignments:                       # MRP planned them after a purchase: the other plant may have the parts now
        o = orders.get(a.customer_order_id)
        if (o is not None and o.status == "PRODUCTION_ORDER" and not o.kitted and o.plant_id in PLANTS
                and any(r.startswith("WAITS_FOR_PURCHASE") for r in a.reason_codes)):
            blocked.append((o, SimpleNamespace(qty=a.qty, reason="MATERIAL_WAITS_FOR_PURCHASE")))
    for u in ref.unscheduled:
        o = orders.get(u.customer_order_id)
        if o is not None and o.status == "PRODUCTION_ORDER" and not o.kitted and u.reason.startswith("MATERIAL") and o.plant_id in PLANTS:
            blocked.append((o, u))
    blocked.sort(key=lambda ou: (pol.priority_rank.get(ou[0].priority, 9), ou[0].due))
    left = _left_by_plant(ref, x)
    snap = twin.snapshot(d, None, trend_hours=1)
    state = {l["line_id"]: l for l in snap["lines"] if not l["generated"]}
    open_inc = defaultdict(list)
    for i in incidents.list_("OPEN", light=True):
        open_inc[i["plant_id"]].append(i)
    sup = T._gen("gen_material_supplier")
    lead = sup.set_index("material_id").lead_time_days.to_dict() if sup is not None else {}
    rates = T._rates()
    co = d["fact_customer_order"]
    price = co.groupby("product_id").unit_price_inr.mean().to_dict()
    yl = x.yields
    lines_of = defaultdict(list)
    for l in x.lines:
        lines_of[l.plant_id].append(l)
    bom = defaultdict(list)
    for b in x.bom:
        bom[b.product_id].append(b)
    base_plant = _plant_of(ref)
    out = []
    for o, u in blocked[:limit]:
        home, other = o.plant_id, next(p for p in PLANTS if p != o.plant_id)
        qty = u.qty
        checks, warn = [], []
        def chk(name, ok, detail, hard=True):
            checks.append({"check": name, "ok": bool(ok), "detail": detail, "hard": hard})
        # home: what is missing and how long the parts take
        mf = lambda p: math.prod(1 / (yl.get(f"{l.line_id}|{o.product_id}") or yl.get(f"{l.line_id}|*") or 1.0) for l in lines_of[p])
        need = {b.material_id: qty * b.qty_per * (1 + b.scrap) for b in bom[o.product_id]}
        miss_home = {m: round(n * mf(home) - max(0.0, left[(home, m)])) for m, n in need.items() if left[(home, m)] < n * mf(home)}
        wait_days = max((lead.get(m, 14) for m in miss_home), default=0)
        fal_rate = sum(l.rate_per_min for l in lines_of[home] if l.kind == "FAL")
        wait_finish = x.as_of + timedelta(days=wait_days, minutes=qty / max(fal_rate, 1e-6))
        # 1. material at the other plant, after its own planned work
        short_other = {m: round(n * mf(other) - max(0.0, left[(other, m)])) for m, n in need.items() if left[(other, m)] < n * mf(other)}
        chk("material", not short_other, "Has every part after its own planned work" if not short_other
            else "Also short of " + ", ".join(f"{m} ({v:,})" for m, v in short_other.items()))
        # 2-3. capacity and the other plant's own orders: plan it there
        y = x.model_copy(deep=True)
        for z in y.orders:
            if z.order_id == o.order_id:
                z.plant_id = other
        trial = planning.plan(y, pol, name="Sourcing trial")
        mine = [a for a in trial.assignments if a.customer_order_id == o.order_id]
        made = sum(a.qty for a in mine)
        fin = max((a.fal_finish for a in mine), default=None)
        chk("capacity", made >= qty, f"Fits on {', '.join(sorted({a.smt_line for a in mine} | {a.fal_line for a in mine}))}, finished {fin:%d %b %H:%M}"
            if made >= qty else "Cannot be planned there without the missing parts" if short_other else f"Only {made:,} of {qty:,} fit in the line time")
        at_other = {k for k, p in base_plant.items() if p == other} | {z.order_id for z in x.orders if z.plant_id == other}
        delayed = {k: h for k, h in incidents._delays(trial, ref).items() if k in at_other and k != o.order_id}
        late0 = set(incidents._late(ref))
        newly_late = [k for k in incidents._late(trial) if k in at_other and k not in late0 and k != o.order_id]
        new_short = _unsched(trial, at_other) - _unsched(ref, at_other)
        chk("own orders", not newly_late and new_short <= 0 and sum(delayed.values()) / max(1, len(delayed)) <= 24,
            "None of its own orders becomes late or short" if not delayed else
            f"{len(delayed)} of its orders move back {sum(delayed.values()) / len(delayed):.0f} h on average; {len(newly_late)} become late; "
            f"{max(0, new_short):,} units newly short")
        # 4. lines now and open problems
        ol = [state.get(l.line_id) for l in lines_of[other]]
        down = [l["line_id"] for l in ol if l and l["state"] == "down"]
        chk("lines now", not down, "Lines running: " + ", ".join(f"{l['line_id']} OEE today {l['today']['oee']}%" for l in ol if l)
            if not down else f"Down now: {', '.join(down)}")
        probs = open_inc.get(other, [])
        kinds = {"PO_DELAY": "production orders running late", "MACHINE_DOWN": "a line down"}
        chk("open problems", not probs, "No open problems" if not probs else f"{len(probs)} open: " + "; ".join(
            p.get("description") or kinds.get(p.get("kind"), "a problem on the floor") for p in probs[:3]))
        # 5. maintenance during the job
        win = (min((a.smt_start for a in mine), default=x.as_of), fin or x.as_of)
        mt = [w for w in x.maintenance if w["line_id"] in {l.line_id for l in lines_of[other]} and w["start"] < win[1] and w["end"] > win[0]]
        chk("maintenance", True, "No planned service during the job" if not mt else
            "Planned service already held out: " + ", ".join(f"{w['line_id']} {w['start']:%d %b} ({w['minutes']:.0f} min)" for w in mt), hard=False)
        # 6. crew on the shifts the job uses
        cr = T.crews(days=max(1, (win[1].date() - x.as_of.date()).days + 1), ref=trial, x=y) if fin else {"available": False}
        gaps = [r for r in cr.get("rows", []) if r["plant_id"] == other and r["gap"] > 0 and win[0].date().isoformat() <= r["date"] <= win[1].date().isoformat()]
        ot_h = sum(r["overtime_h"] for r in gaps)
        chk("crew", ot_h <= 24, "Enough people on every shift" if not gaps else f"{len(gaps)} shifts short: {ot_h} overtime hours needed",
            hard=ot_h > 24)
        # 7. product experience and yield
        y_home = [yl.get(f"{l.line_id}|{o.product_id}") for l in lines_of[home]]
        y_oth = [yl.get(f"{l.line_id}|{o.product_id}") for l in lines_of[other]]
        made_before = all(v is not None for v in y_oth)
        yo = math.prod(v for v in y_oth if v) if made_before else None
        yh = math.prod(v for v in y_home if v) if all(v is not None for v in y_home) else None
        chk("product", made_before and (yh is None or yo >= yh - 0.01),
            f"Made it in the last {pol.yield_days} days at {100 * yo:.1f}% yield" + (f" (home {100 * yh:.1f}%)" if yh else "") if made_before
            else "Has not made this product recently (would need a trial run)")
        # 8. gain vs waiting at home, and the cost of moving
        R = rates.get(other) or {}
        gain_days = max(0.0, (wait_finish - fin).total_seconds() / 86400) if fin else 0.0
        due_end = T._due_end(o.due)
        late_home = max(0.0, (wait_finish - due_end).total_seconds() / 86400)
        late_other = max(0.0, (fin - due_end).total_seconds() / 86400) if fin else late_home
        p_unit = price.get(o.product_id, 0)
        saved = (late_home - late_other) * qty * p_unit * R.get("late_penalty_pct_per_day", 0.5) / 100
        move_cost = qty * R.get("fg_transfer_inr_per_unit", 35)
        chk("gain", gain_days >= 1 and saved >= move_cost,
            f"Ready {gain_days:.1f} days sooner than waiting {wait_days} days for parts at home; saves ₹{saved:,.0f} of late penalty against "
            f"₹{move_cost:,.0f} to move the finished units" if fin else f"Not sooner than waiting {wait_days} days for parts at home")
        make_ok = all(c["ok"] for c in checks if c["hard"])
        # alternative: send the parts home instead
        alt = None
        if not make_ok and not short_other:
            y2 = x.model_copy(deep=True)
            for st in y2.stock:
                n = need.get(st.material_id, 0) * mf(home)
                if st.plant_id == other:
                    st.available -= n
                elif st.plant_id == home:
                    st.available += n
            t2 = planning.plan(y2, pol, name="Sourcing trial (send parts)")
            m2 = [a for a in t2.assignments if a.customer_order_id == o.order_id]
            f2 = max((a.fal_finish for a in m2), default=None)
            d2 = {k: h for k, h in incidents._delays(t2, ref).items() if k in at_other}
            ns2 = _unsched(t2, at_other) - _unsched(ref, at_other)
            value = sum(n * mf(home) * (next((s.unit_cost_inr for s in x.stock if s.material_id == m), 0) or 0) for m, n in need.items())
            cost2 = value * R.get("material_transfer_pct", 0.4) / 100 + R.get("truck_trip_inr", 6000)
            ok2 = sum(a.qty for a in m2) >= qty and f2 and f2 < wait_finish - timedelta(days=1) and not d2 and ns2 <= 0
            alt = {"ok": bool(ok2), "finish": f2, "cost_inr": round(cost2), "other_orders_delayed": len(d2), "newly_short": max(0, ns2),
                   "parts": {m: round(n * mf(home)) for m, n in need.items()}}
        verdict = "make_there" if make_ok else "send_parts" if alt and alt["ok"] else "no_move"
        out.append({"production_order_id": o.order_id, "product_id": o.product_id, "qty": qty, "priority": o.priority, "due": o.due.isoformat(),
                    "home": home, "other": other, "missing_at_home": miss_home, "wait_days": wait_days, "wait_finish": wait_finish,
                    "verdict": verdict, "checks": checks, "finish_there": fin, "send_parts": alt,
                    "cost": {"move_inr": round(move_cost), "late_penalty_saved_inr": round(saved)}})
    res = {"as_of": x.as_of.isoformat(), "blocked": len(blocked), "analysed": len(out), "items": out,
           "suggested": sum(i["verdict"] != "no_move" for i in out)}
    with _lock:
        _cache.clear()
        _cache[key] = res
    return res


def sense() -> dict:
    r = analyse()
    sigs = []
    for i in r["items"]:
        if i["verdict"] == "no_move":
            continue
        urgent = i["due"] <= (T._due_end(r["as_of"][:10]) + timedelta(days=2)).date().isoformat()     # due within 3 days (or past due)
        if i["verdict"] == "make_there":
            title = (f"Make {i['production_order_id']} ({i['qty']:,} × {i['product_id']}) in {CITY[i['other']]} instead of {CITY[i['home']]}: "
                     f"{CITY[i['home']]} lacks {', '.join(i['missing_at_home'])}; {CITY[i['other']]} has the parts, line time and crew, "
                     f"ready {i['finish_there']:%d %b} instead of waiting {i['wait_days']} days")
        else:
            title = (f"Send parts from {CITY[i['other']]} to {CITY[i['home']]} for {i['production_order_id']} ({i['qty']:,} × {i['product_id']}): "
                     f"{CITY[i['other']]}'s own orders are not affected; ready {i['send_parts']['finish']:%d %b}")
        sigs.append({"type": f"sourcing.{i['verdict']}", "agent": NAME, "severity": "critical" if urgent else "warn", "title": title,
                     "plant_id": i["other"], "key": f"sourcing:{i['production_order_id']}:{i['verdict']}:{i['other']}",
                     "detail": {"production_order_id": i["production_order_id"], "home": i["home"], "other": i["other"], "verdict": i["verdict"],
                                "checks": i["checks"], "missing_at_home": i["missing_at_home"], "cost": i["cost"]}})
    summary = (f"{r['blocked']} production orders blocked for material in their plant · {r['analysed']} checked against the other plant · "
               f"{r['suggested']} move{'s' if r['suggested'] != 1 else ''} worth making")
    return {"signals": sigs, "summary": summary}


from . import coord as _coord  # noqa: E402
_coord.on_change(_cache.clear)

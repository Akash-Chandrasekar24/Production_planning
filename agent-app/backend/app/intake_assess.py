"""What newly received orders need and what they do to the plan: materials, machine hours, which plant, the ways to run it
and the agent's recommendation. Read-only analysis for the upload flow (intake_flow.run)."""
import datetime as dt
import math
from collections import defaultdict

from . import data, incidents, inventory_agent, planning, planning_tools as T, scenarios, sourcing_agent, store
from .config import Policy
from .intake_common import CITY, log


def _assess(ids: list[str]) -> tuple[list[dict], dict]:
    pol = Policy()
    ref, x = T.working()
    orders = {o.order_id: o for o in x.orders}
    # the same plan without the new orders: what the new orders do to the orders already there
    y = x.model_copy(deep=True)
    y.orders = [o for o in y.orders if o.order_id not in set(ids)]
    before = planning.plan(y, Policy(**ref.policy), name="Before the new orders")
    delayed = incidents._delays(ref, before)
    late0 = set(incidents._late(before))
    newly_late = [k for k in incidents._late(ref) if k not in late0 and k not in ids]
    # supply the new orders take from orders already there: open production orders' units go to the highest priority first, so a
    # big high-priority order can leave existing orders uncovered (now short of parts or line time)
    def short_by(s):
        m = defaultdict(int)
        for u in s.unscheduled:
            m[incidents._cid(u.customer_order_id)] += u.qty
        return m
    sb, sa = short_by(before), short_by(ref)
    prod = {o.order_id: o.product_id for o in x.orders}
    displaced = defaultdict(lambda: [0, 0])                     # product -> [orders, units] newly short among existing orders
    for k, q in sa.items():
        if k not in ids and q > sb.get(k, 0):
            displaced[prod.get(k)][0] += 1
            displaced[prod.get(k)][1] += q - sb.get(k, 0)
    horizon = x.as_of.date() + dt.timedelta(days=pol.agent_urgent_days)
    out = []
    for oid in ids:
        o = orders.get(oid)
        if o is None:
            continue
        asg = [a for a in ref.assignments if incidents._cid(a.customer_order_id) == oid]
        un = [u for u in ref.unscheduled if incidents._cid(u.customer_order_id) == oid]
        made, short = sum(a.qty for a in asg), sum(u.qty for u in un)
        covered = max(0, o.qty - made - short)
        fin = max((a.fal_finish for a in asg), default=None)
        late = max((a.late_days for a in asg), default=0.0)
        if short:
            state, text = "blocked", f"{short:,} units cannot be planned: " + "; ".join(sorted({u.reason.replace('_', ' ').lower() for u in un}))
        elif late > 0:
            state, text = "late", f"Planned, but finishes {late:.1f} days after the date"
        elif made:
            state, text = "on_time", f"Planned on {', '.join(sorted({CITY.get(a.plant_id, a.plant_id) for a in asg}))}, ready {fin:%d %b}"
        else:
            state, text = "covered", "Covered by what open production orders still make"
        if covered and made + short:
            text += f" ({covered:,} units covered by open production orders)"
        why = []
        if o.qty >= pol.agent_min_new_units:
            why.append(f"large order: {o.qty:,} units (large means {pol.agent_min_new_units:,} or more)")
        if o.priority == "HIGH" and o.due <= horizon:
            why.append(f"high priority due within {pol.agent_urgent_days} days ({o.due:%d %b})")
        heavy = bool(why)                                         # large or urgent: worth a new plan straight away
        if state in ("blocked", "late"):
            why.append("the plan as it stands cannot meet it")
        item = {"order_id": oid, "product_id": o.product_id, "qty": o.qty, "due": o.due.isoformat(), "priority": o.priority,
                "result": state, "result_text": text, "covered_units": covered, "planned_units": made, "short_units": short,
                "ready": fin.isoformat() if fin else None, "needs_action": bool(why), "heavy": heavy, "why": why}
        dn, du = displaced.get(o.product_id, (0, 0))
        if why and du and state == "covered":
            item["result_text"] = (f"Covered by open production orders, but only by taking their units from {dn} existing "
                                   f"order{'s' if dn != 1 else ''}, which now lack {du:,} units")
        if why:                                                   # what it needs: parts, earliest date, effect on other orders, crew
            need = short + (du if state == "covered" else 0)
            buy, earliest = T.buy_plan(ref, x, o.product_id, need) if need else ([], None)
            inv = inventory_agent.context([o.product_id]) or {}
            cr = T.crews(days=max(1, min(21, (o.due - x.as_of.date()).days + 1)), ref=ref, x=x)
            item.update({"buy": buy, "earliest": earliest,
                         "pushes_back": {"orders": len(delayed), "newly_late": len(newly_late),
                                         "avg_hours": round(sum(delayed.values()) / len(delayed), 1) if delayed else 0,
                                         "left_short_orders": dn, "left_short_units": du},
                         "materials_at_risk": [{"material_id": m["material_id"], "cover_days": m.get("cover_days"), "shortfall": m.get("shortfall")}
                                               for m in {m["material_id"]: m for m in inv.get("shortfall", []) + inv.get("low_cover", [])}.values()][:6],
                         "crew_short_shifts": len(cr.get("short_shifts", [])), "overtime_h": cr.get("overtime_h", 0),
                         "plants": sorted({a.plant_id for a in asg})})
        out.append(item)
    return out, {"before": before, "ref": ref, "x": x}


def _stage(s, as_of: dt.datetime, only: set | None = None) -> dict:
    """Where production stands in one plan: orders, units planned and waiting, late orders, and when the planned work is all done."""
    a = [j for j in s.assignments if only is None or incidents._cid(j.customer_order_id) in only]
    u = [j for j in s.unscheduled if only is None or incidents._cid(j.customer_order_id) in only]
    planned, waiting = sum(j.qty for j in a), sum(j.qty for j in u)
    late = {incidents._cid(j.customer_order_id) for j in a if j.status == "LATE_RISK"}
    on_time = sum(j.qty for j in a if j.status != "LATE_RISK")
    fin = max((j.fal_finish for j in a), default=None)
    return {"orders": len({incidents._cid(j.customer_order_id) for j in a + u}), "planned_units": planned, "waiting_units": waiting,
            "waiting_orders": len({incidents._cid(j.customer_order_id) for j in u}), "late_orders": len(late),
            "late_days_max": round(max((j.late_days for j in a), default=0.0), 1),
            "on_time_pct": round(100 * on_time / planned, 1) if planned else None,
            "finish": fin.date().isoformat() if fin else None, "days": max(1, math.ceil((fin - as_of).total_seconds() / 86400)) if fin else None}


# ------------------------------------------------------------------ the planning outcome for the new orders
def _bom(x) -> dict:
    out = defaultdict(list)
    for b in x.bom:
        out[b.product_id].append(b)
    return out


def _materials(items: list[dict], x, before) -> list[dict]:
    """Every raw material the new orders use: what they need, what is in stock, what the orders already in the book take, what is free,
    and for anything short what to buy (supplier, lead time, arrival, cost). Stock is pooled across both plants, above safety stock."""
    sup = T._gen("gen_material_supplier")
    supi = sup.set_index("material_id") if sup is not None else None
    left = T._stock_left(before, x)                                  # usable stock left after the orders already in the book
    on_hand, safety, cost = defaultdict(float), defaultdict(float), {}
    for st in x.stock:
        on_hand[st.material_id] += max(0.0, st.available)
        safety[st.material_id] += st.safety
        cost[st.material_id] = st.unit_cost_inr
    bom, need, used = _bom(x), defaultdict(float), defaultdict(set)
    for i in items:
        due = dt.date.fromisoformat(i["due"])
        for b in bom[i["product_id"]]:
            if b.effective_from <= due <= b.effective_to:
                need[b.material_id] += i["qty"] * b.qty_per * (1 + b.scrap)
                used[b.material_id].add(i["order_id"])
    rows = []
    for m, q in need.items():
        free = max(0.0, left.get(m, 0.0))
        short = max(0.0, q - free)
        r = {"material_id": m, "needed": math.ceil(q), "in_stock": round(on_hand[m]), "safety": round(safety[m]),
             "taken": round(max(0.0, on_hand[m] - safety[m] - left.get(m, 0.0))), "free": round(free), "enough": short <= 0,
             "short": math.ceil(short), "orders": sorted(used[m]), "unit_cost_inr": cost.get(m)}
        if short > 0:
            lead = int(supi.lead_time_days.get(m, 14)) if supi is not None else 14
            r.update({"buy": math.ceil(short), "supplier": supi.supplier_id.get(m) if supi is not None else None, "lead_days": lead,
                      "arrives": (x.as_of + dt.timedelta(days=lead)).date().isoformat(),
                      "buy_cost_inr": round(math.ceil(short) * (cost.get(m) or 0)) if cost.get(m) else None})
        rows.append(r)
    return sorted(rows, key=lambda r: (r["enough"], -(r.get("lead_days") or 0), r["material_id"]))


def _line_free(x, before, line, start, end) -> float:
    """Plannable minutes on a line between start and end that the orders already in the book don't use."""
    avail = sum(s.minutes * max(0.0, (min(s.end, end) - max(s.start, start)).total_seconds()) / max(1.0, (s.end - s.start).total_seconds())
                for s in line.slots if s.start < end and s.end > start)
    used = 0.0
    for a in before.assignments:
        for ln, s0, f0, setup in ((a.smt_line, a.smt_start, a.smt_finish, a.smt_setup_min), (a.fal_line, a.fal_start, a.fal_finish, a.fal_setup_min)):
            if ln == line.line_id and s0 < end and f0 > start:
                span = max(1.0, (f0 - s0).total_seconds())
                used += (a.qty / line.rate_per_min + setup) * max(0.0, (min(f0, end) - max(s0, start)).total_seconds()) / span
    return max(0.0, avail - used)


def _machines(items: list[dict], x, before) -> dict:
    """Line time the new orders need on SMT and final assembly against the free time before their dates, line by line, with the
    machines on each line and any planned maintenance in that window."""
    if not items:
        return {"kinds": [], "lines": []}
    end = max(T._due_end(i["due"]) for i in items)
    end = max(end, x.as_of + dt.timedelta(days=1))
    units = sum(i["qty"] for i in items)
    lines = []
    for l in x.lines:
        free = _line_free(x, before, l, x.as_of, end)
        maint = [{"start": m["start"].isoformat(), "minutes": round(m["minutes"]), "machines": m.get("machine_ids", [])}
                 for m in x.maintenance if m["line_id"] == l.line_id and m["start"] < end and m["end"] > x.as_of]
        lines.append({"line_id": l.line_id, "plant_id": l.plant_id, "kind": l.kind, "machines": l.machine_ids,
                      "rate_per_hour": round(l.rate_per_min * 60), "free_hours": round(free / 60, 1), "maintenance": maint})
    kinds = []
    for k in ("SMT", "FAL"):
        ls = [l for l in x.lines if l.kind == k]
        if not ls:
            continue
        rate = sum(l.rate_per_min for l in ls) / len(ls)
        need_h = units / rate / 60
        free_h = sum(r["free_hours"] for r in lines if r["kind"] == k)
        kinds.append({"kind": k, "label": "SMT (circuit boards)" if k == "SMT" else "Final assembly", "need_hours": round(need_h, 1),
                      "free_hours": round(free_h, 1), "enough": free_h >= need_h, "short_hours": round(max(0.0, need_h - free_h), 1),
                      "by": (end - dt.timedelta(days=1)).date().isoformat()})
    return {"kinds": kinds, "lines": lines, "units": units}


def _units_by(x, before, due: str) -> int:
    """Units both plants can still make by a date: free time on the slower stage (SMT or final assembly) at line speed."""
    end, cap = T._due_end(due), {}
    for k in ("SMT", "FAL"):
        cap[k] = sum(_line_free(x, before, l, x.as_of, end) * l.rate_per_min for l in x.lines if l.kind == k)
    return int(min(cap.values())) if cap else 0


def _prices(x) -> dict:
    """Unit price per order; orders without one (production orders) at the product's usual price."""
    byp = defaultdict(list)
    for o in x.orders:
        if o.unit_price_inr:
            byp[o.product_id].append(o.unit_price_inr)
    med = {k: sorted(v)[len(v) // 2] for k, v in byp.items()}
    return {o.order_id: o.unit_price_inr or med.get(o.product_id, 0) for o in x.orders}


def _new_status(s, items: list[dict]) -> dict:
    """The new orders in one plan: ready date, on time, late, waiting for parts."""
    out = {"ready": None, "on_time": 0, "late": 0, "waiting": 0, "per_order": {}}
    for i in items:
        asg = [a for a in s.assignments if incidents._cid(a.customer_order_id) == i["order_id"]]
        un = sum(u.qty for u in s.unscheduled if incidents._cid(u.customer_order_id) == i["order_id"])
        fin = max((a.fal_finish for a in asg), default=None)
        late = any(a.late_days > 0 for a in asg)
        ready = fin.date().isoformat() if fin else (i["due"] if not un else None)
        state = "waiting" if un else "late" if late else "on_time"
        out[state] += 1
        out["per_order"][i["order_id"]] = {"state": state, "ready": ready}
        if ready and not un:
            out["ready"] = max(out["ready"] or ready, ready)
    return out


def _inr(v: float) -> str:
    v = abs(v)
    return f"₹{v / 1e7:.1f} Cr" if v >= 1e7 else f"₹{v / 1e5:.1f} L" if v >= 1e5 else f"₹{v / 1e3:.1f}K" if v >= 1e3 else f"₹{v:,.0f}"


def _options(items: list[dict], x, plan_sid: str | None) -> list[dict]:
    """Four ways to run the plan, each a real re-plan with a different strategy: the agent's balanced plan, the orders at risk first,
    crews working through breaks, and both plants sharing the load. For each: the outcome, what it costs, its pros and cons, and the
    reason the agent picks it or not (compared with the agent's pick)."""
    act = [i["order_id"] for i in items if i["needs_action"]]
    base = store.get(plan_sid) if plan_sid else None
    if base is None or not act:
        return []
    inp, pol = store.inputs(plan_sid), Policy(**base.policy)
    end = max(T._due_end(i["due"]) for i in items) + dt.timedelta(days=7)
    at_risk = sorted(set(incidents._late(base)) | {i["order_id"] for i in items if i["needs_action"] and i["result"] in ("late", "blocked")})
    everyone = [o.order_id for o in inp.orders]
    specs = [("planned", "Balanced plan", "As the agent planned",
              "Every order in due-date and priority order, each in the plant with its parts and line time. No overtime, no reshuffling.", None),
             ("rescue", "At-risk orders first", "Protect promised dates",
              f"The {len(at_risk)} orders that would finish late go to the front of the line on the same lines and shifts.",
              scenarios.Scenario(name="Option: at-risk orders first", expedite=at_risk)),
             ("overtime", "Work through breaks", "Add line time",
              "Crews work through the 30-minute break in every shift, adding about 6% line time, until a week after the new orders' dates.",
              scenarios.Scenario(name="Option: work through breaks", extra_time=[scenarios.ExtraTime(start=inp.as_of, end=end, minutes_per_shift=30)])),
             ("both_plants", "Both plants share the load", "Balance the plants",
              "Any order may run in either plant, and the new orders are split into two lots made in both plants at the same time.",
              scenarios.Scenario(name="Option: both plants share the load", release_plant=everyone, split_orders=act))]
    out = []
    for key, title, tag, how, sc in specs:
        o = _run_option(key, title, tag, how, sc, base, inp, pol, items, x)
        if o:
            out.append(o)
    if not out:
        return out
    return _reasons(out)


def _run_option(key, title, tag, how, sc, base, inp, pol, items, x) -> dict | None:
    """Plan one option from the agent's plan inputs and measure it: new orders, late orders, cost, value at risk."""
    try:
        xin = inp if sc is None else scenarios.apply(inp, sc)
        s = base if sc is None else planning.plan(xin, pol, previous=base, name=sc.name)
    except Exception:
        log.exception("plan option %s failed", key)
        return None
    price = _prices(xin)
    risk = sum(a.qty * price.get(a.customer_order_id, 0) for a in s.assignments if a.late_days > 0) \
        + sum(u.qty * price.get(u.customer_order_id, 0) for u in s.unscheduled)
    c = T._cost(s, xin, inp if sc is not None else None)
    st, ns = _stage(s, x.as_of), _new_status(s, items)
    parts = c.get("parts", {})
    return {"key": key, "title": title, "tag": tag, "how": how, "text": how, "scenario": sc.model_dump(mode="json") if sc else None,
                "new_ready": ns["ready"], "new_on_time": ns["on_time"], "new_late": ns["late"], "new_waiting": ns["waiting"],
                "per_order": ns["per_order"], "late_orders": st["late_orders"], "late_days_max": st["late_days_max"],
                "on_time_pct": st["on_time_pct"], "waiting_units": st["waiting_units"], "finish": st["finish"],
                "cost_inr": c.get("total_inr"), "overtime_inr": parts.get("overtime", 0), "changeover_inr": parts.get("changeovers", 0),
                "late_penalty_inr": parts.get("late_penalty", 0), "at_risk_inr": round(risk)}


def _reasons(out: list[dict]) -> list[dict]:
    """Mark the best value, then pros, cons and a reason for every option."""
    score = lambda o: (o["new_late"] + o["new_waiting"], (o["cost_inr"] or 0) + o["at_risk_inr"])
    best = min(out, key=score)
    best["best"] = True
    b0 = next((o for o in out if o["key"] == "planned"), out[0])
    for o in out:                                                  # pros, cons and the reason, against the balanced plan and the pick
        dl, dc = o["late_orders"] - b0["late_orders"], (o["cost_inr"] or 0) - (b0["cost_inr"] or 0)
        dr = o["at_risk_inr"] - b0["at_risk_inr"]
        df = (dt.date.fromisoformat(o["finish"]) - dt.date.fromisoformat(b0["finish"])).days if o["finish"] and b0["finish"] else 0
        pros, cons = [], []
        n_new = o["new_on_time"] + o["new_late"] + o["new_waiting"]
        (pros if o["new_on_time"] == n_new else cons).append(f"{o['new_on_time']} of {n_new} new orders on time" + (f", ready by {dt.date.fromisoformat(o['new_ready']):%d %b}" if o["new_ready"] else ""))
        if o["key"] == "planned":
            pros.append("No extra cost and no change to how the floor works")
            if o["late_orders"]:
                cons.append(f"{o['late_orders']} orders finish late (up to {o['late_days_max']:g} days)")
        else:
            if dl < 0:
                pros.append(f"{-dl} fewer late order{'s' if dl != -1 else ''} ({o['late_orders']} instead of {b0['late_orders']})")
            elif dl > 0:
                cons.append(f"{dl} more late order{'s' if dl != 1 else ''} than the balanced plan")
            if o["late_days_max"] < b0["late_days_max"]:
                pros.append(f"Worst delay {o['late_days_max']:g} days instead of {b0['late_days_max']:g}")
            if df < 0:
                pros.append(f"Planned work done {-df} day{'s' if df != -1 else ''} earlier ({dt.date.fromisoformat(o['finish']):%d %b})")
            if dr < -1e5:
                pros.append(f"{_inr(-dr)} less order value at risk")
            if dc > 1000:
                cons.append(f"Costs {_inr(dc)} more" + (f" (overtime {_inr(o['overtime_inr'])})" if o["overtime_inr"] else
                                                       f" (more changeovers: {_inr(o['changeover_inr'] - b0['changeover_inr'])})" if o["changeover_inr"] > b0["changeover_inr"] else ""))
            elif dc <= 1000:
                pros.append("Practically no extra cost")
            if key_same := (dl == 0 and abs(dc) <= 1000 and df == 0 and o["late_days_max"] == b0["late_days_max"]):
                cons.append("Same outcome as the balanced plan: nothing to gain")
            if o["key"] == "overtime":
                cons.append("Crews give up their breaks for weeks")
            if o["key"] == "both_plants":
                cons.append("Moves work and parts between Hosur and Bengaluru")
            if o["key"] == "rescue" and o["changeover_inr"] > b0["changeover_inr"]:
                cons.append("Lines change products more often")
        o["pros"], o["cons"] = pros, cons
        if o is best:
            why = [f"{o['late_orders']} late order{'s' if o['late_orders'] != 1 else ''}" + (f", the fewest of the {len(out)} plans" if o["late_orders"] == min(p["late_orders"] for p in out) else ""),
                   "no extra cost" if dc <= 1000 else f"only {_inr(dc)} more than the balanced plan"]
            o["reason"] = f"Agent's pick: all new orders on time with {why[0]}, for {why[1]}. Best balance of on-time delivery and cost." \
                if o["new_on_time"] == n_new else f"Agent's pick: the fewest new orders late or waiting, for {why[1]}."
        else:
            gap = []
            if o["new_late"] + o["new_waiting"] > best["new_late"] + best["new_waiting"]:
                gap.append(f"{o['new_late'] + o['new_waiting'] - best['new_late'] - best['new_waiting']} more new orders late or waiting")
            if o["late_orders"] > best["late_orders"]:
                gap.append(f"{o['late_orders'] - best['late_orders']} more late order{'s' if o['late_orders'] - best['late_orders'] != 1 else ''}")
            extra = (o["cost_inr"] or 0) - (best["cost_inr"] or 0)
            if extra > 1000:
                gap.append(f"costs {_inr(extra)} more")
            o["reason"] = f"Not picked: {' and '.join(gap) if gap else 'no better on any count'} than “{best['title']}”."
    return out


def _plants(items: list[dict], x, before, plan_sid: str | None) -> dict:
    """Where to make the new orders: both plants together, or one plant only. For one plant, each material comes from that plant's own
    stock first, then by truck from the other plant (only what it can spare after its own orders), then from the supplier. The day the
    last material is in, plus the days that plant's free line time needs to make the units, gives the ready date."""
    if not items:
        return {}
    pol = Policy()
    tdays = pol.transfer_days
    sup = T._gen("gen_material_supplier")
    supi = sup.set_index("material_id") if sup is not None else None
    rates = T._rates()
    ids = {i["order_id"] for i in items}
    xp = data.to_inputs(store.D(), x.as_of.isoformat(), Policy(material_scope="plant"))      # each plant with its own stock
    have = {o.order_id for o in xp.orders}
    xp.orders += [o for o in T.work_orders() if o.order_id not in have]
    yp = xp.model_copy(deep=True)
    yp.orders = [o for o in yp.orders if o.order_id not in ids]
    left = sourcing_agent._left_by_plant(planning.plan(yp, Policy(material_scope="plant"), name="Per plant, before the new orders"), yp)
    cost = {st.material_id: st.unit_cost_inr for st in xp.stock if st.unit_cost_inr}
    bom, need = _bom(x), defaultdict(float)
    for i in items:
        due = dt.date.fromisoformat(i["due"])
        for b in bom[i["product_id"]]:
            if b.effective_from <= due <= b.effective_to:
                need[b.material_id] += i["qty"] * b.qty_per * (1 + b.scrap)
    # free stock in total is what the planner leaves after the orders already in the book (both plants pooled, the same as the materials
    # check); it is split between the plants in proportion to each plant's own spare stock
    pooled = T._stock_left(before, x)
    for m in need:
        tot = sum(max(0.0, left.get((p_, m), 0.0)) for p_ in sourcing_agent.PLANTS)
        for p_ in sourcing_agent.PLANTS:
            left[(p_, m)] = max(0.0, left.get((p_, m), 0.0)) / tot * max(0.0, pooled.get(m, 0.0)) if tot > 0 else 0.0
    units = sum(i["qty"] for i in items)
    due_last = max(dt.date.fromisoformat(i["due"]) for i in items)

    def per_day(plants) -> float:                                   # units a day the free line time can make (slower stage), next 2 weeks
        cap = {}
        for k in ("SMT", "FAL"):
            cap[k] = sum(_line_free(x, before, l, x.as_of, x.as_of + dt.timedelta(days=14)) * l.rate_per_min
                         for l in x.lines if l.kind == k and l.plant_id in plants) / 14
        return min(cap.values()) if cap else 0.0

    def finish(mat_days: float, plants) -> dict:
        pdz = per_day(plants)
        make = math.ceil(units / pdz) if pdz > 0 else None
        ready = x.as_of.date() + dt.timedelta(days=math.ceil(mat_days) + make) if make is not None else None
        return {"materials_days": math.ceil(mat_days), "make_days": make, "units_per_day": round(pdz),
                "ready": ready.isoformat() if ready else None, "late_days": max(0, (ready - due_last).days) if ready else None}

    out = {"transfer_days": tdays, "due": due_last.isoformat(), "units": units, "choices": []}
    for home in sourcing_agent.PLANTS[::-1]:                       # Hosur first
        other = next(p for p in sourcing_agent.PLANTS if p != home)
        R = rates.get(home) or {}
        rows, t_val = [], 0.0
        for m, q in sorted(need.items()):
            here = max(0.0, left.get((home, m), 0.0))
            spare = max(0.0, left.get((other, m), 0.0))
            use = min(q, here)
            move = min(q - use, spare)
            buy = max(0.0, q - use - move)
            lead = int(supi.lead_time_days.get(m, 14)) if supi is not None else 14
            days = max(tdays if move > 0 else 0, lead if buy > 0 else 0)
            t_val += move * (cost.get(m) or 0)
            rows.append({"material_id": m, "need": math.ceil(q), "here": round(here), "other": round(spare), "use": math.ceil(use),
                         "transfer": math.ceil(move), "order": math.ceil(buy), "supplier": supi.supplier_id.get(m) if supi is not None and buy else None,
                         "lead_days": lead if buy else None, "days": days,
                         "order_cost_inr": round(math.ceil(buy) * (cost.get(m) or 0)) if buy else 0})
        moves = sum(r["transfer"] > 0 for r in rows)
        t_cost = round(t_val * R.get("material_transfer_pct", 0.4) / 100 + (R.get("truck_trip_inr", 6000) if moves else 0))
        f = finish(max((r["days"] for r in rows), default=0), {home})
        choice = {"key": f"only_{home}", "plant_id": home, "city": sourcing_agent.CITY[home], "other_city": sourcing_agent.CITY[other],
                  "title": f"{sourcing_agent.CITY[home]} only", "materials": rows, **f,
                  "from_stock": sum(r["transfer"] == 0 and r["order"] == 0 for r in rows), "to_transfer": moves,
                  "to_order": sum(r["order"] > 0 for r in rows), "transfer_cost_inr": t_cost,
                  "order_cost_inr": sum(r["order_cost_inr"] for r in rows),
                  "scenario": scenarios.Scenario(name=f"Option: {sourcing_agent.CITY[home]} only", keep_plant={i: home for i in ids}).model_dump(mode="json")}
        out["choices"].append(choice)
    # both plants: each plant's spare stock counts, nothing to truck; what is still short comes from the supplier; both plants' line time
    rows = []
    for m, q in sorted(need.items()):
        free = sum(max(0.0, left.get((p_, m), 0.0)) for p_ in sourcing_agent.PLANTS)
        buy = max(0.0, q - free)
        lead = int(supi.lead_time_days.get(m, 14)) if supi is not None else 14
        rows.append({"material_id": m, "need": math.ceil(q), "here": round(free), "other": None, "use": math.ceil(min(q, free)), "transfer": 0,
                     "order": math.ceil(buy), "supplier": supi.supplier_id.get(m) if supi is not None and buy else None,
                     "lead_days": lead if buy else None, "days": lead if buy else 0,
                     "order_cost_inr": round(math.ceil(buy) * (cost.get(m) or 0)) if buy else 0})
    f = finish(max((r["days"] for r in rows), default=0), set(sourcing_agent.PLANTS))
    out["choices"].insert(0, {"key": "both", "plant_id": None, "city": "Both plants", "title": "Both plants", **f, "materials": rows,
                              "from_stock": sum(r["order"] == 0 for r in rows), "to_transfer": 0, "to_order": sum(r["order"] > 0 for r in rows),
                              "transfer_cost_inr": 0, "order_cost_inr": sum(r["order_cost_inr"] for r in rows), "scenario": None})
    ok = [c for c in out["choices"] if c["ready"]]
    if ok:
        fastest = min(ok, key=lambda c: (c["ready"], c["transfer_cost_inr"] + c["order_cost_inr"]))
        fastest["fastest"] = True
    # the plant options as plans the production manager can approve
    base = store.get(plan_sid) if plan_sid else None
    if base is not None:
        inp, bpol = store.inputs(plan_sid), Policy(**base.policy)
        for c in out["choices"]:
            if c["scenario"]:
                sc = scenarios.Scenario(**c["scenario"])
                o = _run_option(c["key"], c["title"], "Make it in one plant", f"The new orders are made in {c['city']} only; materials not in {c['city']} "
                                f"come from {c['other_city']} by truck ({tdays:g} day) or from the supplier.", sc, base, inp, bpol, items, x)
                if o:
                    c["plan"] = {k: o[k] for k in ("new_ready", "new_on_time", "new_late", "new_waiting", "late_orders", "late_days_max", "cost_inr", "at_risk_inr")}
    return out


def _how(i: dict, mats: list[dict], x, before, options: list[dict]) -> list[dict]:
    """Concrete steps to handle one order that needs a decision, cheapest first, from what the checks found."""
    steps, due = [], dt.date.fromisoformat(i["due"])
    pb = i.get("pushes_back") or {}
    if i["result"] == "covered":
        steps.append({"kind": "use", "text": f"Ship it from production orders already running: they still make {i['covered_units']:,} units of {i['product_id']}"})
        if pb.get("left_short_units"):
            steps.append({"kind": "refill", "text": f"Those units were meant for {pb['left_short_orders']} existing orders, which now lack {pb['left_short_units']:,} units: "
                                                    "plan them again with the parts below"})
    can = _units_by(x, before, i["due"])
    if i["result"] != "covered":
        if can >= i["qty"]:
            steps.append({"kind": "lines", "text": f"The lines have room: both plants can still make {can:,} units by {due:%d %b}"})
        else:
            steps.append({"kind": "split", "text": f"Deliver in two parts: {max(0, can):,} units by {due:%d %b} (all the line time left), the other "
                                                   f"{i['qty'] - max(0, can):,}" + (f" by {dt.date.fromisoformat(i['earliest']):%d %b}" if i.get("earliest") else " later")})
    short = [m for m in mats if not m["enough"] and i["order_id"] in m["orders"]]
    if short:
        last = max(short, key=lambda m: m.get("lead_days") or 0)
        steps.append({"kind": "buy", "text": ("To make those units again, order " if i["result"] == "covered" else "Order ")
                                             + f"{len(short)} short material{'s' if len(short) != 1 else ''} today ("
                                             + ", ".join(m["material_id"] for m in short[:4]) + ("…" if len(short) > 4 else "")
                                             + f"); the last arrives {dt.date.fromisoformat(last['arrives']):%d %b}"})
    else:
        steps.append({"kind": "stock", "text": "Materials: enough in stock for this order, nothing to buy"})
    if i.get("overtime_h"):
        rates = T._rates()
        r = next(iter(rates.values()), {})
        cost = round(i["overtime_h"] * r.get("labour_inr_per_hour", 0) * r.get("overtime_multiplier", 1)) if r else None
        steps.append({"kind": "crew", "text": f"Cover {i['overtime_h']:g} h of crew overtime" + (f" (about ₹{cost:,})" if cost else "")
                                              + (" at " + ", ".join(CITY.get(p, p) for p in i.get("plants", [])) if i.get("plants") else "")})
    ok = [o for o in options if o["per_order"].get(i["order_id"], {}).get("state") == "on_time"]
    if ok:
        b = min(ok, key=lambda o: (o["cost_inr"] or 0) + o["at_risk_inr"])
        steps.append({"kind": "option", "option": b["key"],
                      "text": (f"It is on time in every option; the best overall is \u201c{b['title']}\u201d (fewest late orders for the money)"
                               if len(ok) == len(options) else f"Best way to have it on time: \u201c{b['title']}\u201d")})
    elif options:
        steps.append({"kind": "date", "text": "No option makes it on time: agree a new date with the customer"
                                             + (f" ({dt.date.fromisoformat(i['earliest']):%d %b})" if i.get("earliest") else "")})
    return steps


def _recommend(ids: list[str], items: list[dict], ctx: dict, plan: dict | None) -> dict:
    """The agent's recommendation: where production stands today, what the recommended plan achieves (all work and the new orders,
    in days), and the actions that get there. Every figure comes from the planning engine; nothing is estimated by hand."""
    x, before, ref = ctx["x"], ctx["before"], ctx["ref"]
    rec = (store.get(plan["schedule_id"]) if plan and plan.get("schedule_id") else None) or ref
    now, after = _stage(before, x.as_of), _stage(rec, x.as_of)
    # the new orders, from each order's own verdict: covered by open production orders (ready by its date), planned (its finish),
    # or waiting for parts (its earliest date once the parts are ordered)
    def ready(i):
        return i["ready"][:10] if i.get("ready") else i["due"] if i["result"] == "covered" else None
    dates = [ready(i) for i in items if ready(i)]
    fin = max(dates, default=None)
    new = {"orders": len(items), "units": sum(i["qty"] for i in items), "covered_units": sum(i["covered_units"] for i in items),
           "planned_units": sum(i["planned_units"] for i in items), "waiting_units": sum(i["short_units"] for i in items),
           "on_time": sum(i["result"] in ("covered", "on_time") for i in items), "late": sum(i["result"] == "late" for i in items),
           "waiting": sum(i["result"] == "blocked" for i in items), "finish": fin,
           "days": max(1, (dt.date.fromisoformat(fin) - x.as_of.date()).days) if fin else None}
    # work waiting for parts: order them today, and once the last part is in and the lines are free, final assembly makes the rest
    # at full rate (ponytail: one pooled estimate from lead times and final-assembly capacity, not a second plan)
    wait = defaultdict(int)
    for u in rec.unscheduled:
        if u.reason.startswith("MATERIAL"):
            wait[u.product_id] += u.qty
    back = {}
    for prod, q in wait.items():
        for m in T.buy_plan(ref, x, prod, q)[0]:
            b = back.setdefault(m["material_id"], {**m, "qty": 0})
            b["qty"] += m["qty"]
    back = sorted(back.values(), key=lambda m: -m["lead_days"])
    lead = max((m["lead_days"] for m in back), default=0)
    per_day = sum(l.rate_per_min for l in x.lines if l.kind == "FAL") * 3 * 450
    if wait and per_day:
        start = max(dt.date.fromisoformat(after["finish"]) if after["finish"] else x.as_of.date(), x.as_of.date() + dt.timedelta(days=lead))
        done = start + dt.timedelta(days=math.ceil(sum(wait.values()) / per_day))
        after.update({"all_finish": done.isoformat(), "all_days": (done - x.as_of.date()).days, "parts_lead_days": lead,
                      "make_days": math.ceil(sum(wait.values()) / per_day)})
    act = [i for i in items if i["needs_action"]]
    # parts to buy for the new orders, merged by material; they arrive after the supplier lead time
    buy = {}
    for i in act:
        for m in i.get("buy", []):
            b = buy.setdefault(m["material_id"], {**m, "qty": 0, "for": []})
            b["qty"] += m["qty"]; b["for"].append(i["order_id"])
    buy = sorted(buy.values(), key=lambda m: -m["lead_days"])
    for m in buy:
        m["arrives"] = (x.as_of + dt.timedelta(days=m["lead_days"])).date().isoformat()
    earliest = max((i["earliest"] for i in act if i.get("earliest")), default=None)
    actions = []
    if plan and plan.get("schedule_id"):
        actions.append({"kind": "approve", "owner": "Production manager", "title": "Approve the new production plan",
                        "detail": "It places the new orders with every open order. Nothing changes on the floor until it is approved and released.",
                        "schedule_id": plan["schedule_id"]})
    if buy:
        actions.append({"kind": "buy", "owner": "Purchasing", "title": f"Order {len(buy)} part{'s' if len(buy) != 1 else ''} today",
                        "detail": f"The last part arrives {dt.date.fromisoformat(buy[0]['arrives']):%d %b} ({buy[0]['lead_days']} days); "
                                  f"the waiting units can then be made" + (f", all done by {dt.date.fromisoformat(earliest):%d %b}" if earliest else ""),
                        "items": buy[:12]})
    for i in act:
        if i["result"] in ("late", "blocked") and i.get("earliest"):
            actions.append({"kind": "date", "owner": "Production manager", "title": f"Agree a new date for {i['order_id']}",
                            "detail": f"Earliest realistic date {dt.date.fromisoformat(i['earliest']):%d %b} (asked for {dt.date.fromisoformat(i['due']):%d %b})."})
    pushed = [i for i in act if i.get("pushes_back", {}).get("newly_late") or i.get("pushes_back", {}).get("left_short_orders")]
    if pushed:
        n = sum(i["pushes_back"]["newly_late"] + i["pushes_back"]["left_short_orders"] for i in pushed)
        actions.append({"kind": "protect", "owner": "Production manager", "title": f"Check {n} existing order{'s' if n != 1 else ''} the new orders push back",
                        "detail": "They become late or short of parts because the new orders take their line time or parts. Keep the new plan, or hold the new orders back."})
    if back:
        for m in back:
            m["arrives"] = (x.as_of + dt.timedelta(days=m["lead_days"])).date().isoformat()
        actions.append({"kind": "buy", "owner": "Purchasing", "title": f"Order parts for the {after['waiting_orders']:,} orders waiting for them",
                        "detail": f"{len(back)} parts are short for {after['waiting_units']:,} units already in the order book. Ordered today, the last "
                                  f"arrives in {lead} days; with {after.get('make_days', 0)} days of final assembly after that, everything is made by "
                                  f"{dt.date.fromisoformat(after['all_finish']):%d %b}." if after.get("all_finish") else "",
                        "items": back})
    ot = round(max((i.get("overtime_h", 0) for i in act), default=0), 1)          # one roster for all of them: counted once
    if ot:
        actions.append({"kind": "crew", "owner": "Production head", "title": f"Plan {ot:g} hours of overtime", "detail": "Some shifts in the plan are short of people."})
    plants = sorted({p for i in act for p in i.get("plants", [])})
    if act and plants:
        actions.append({"kind": "prepare", "owner": "Production head", "title": f"Prepare {', '.join(CITY.get(p, p) for p in plants)} for the new orders",
                        "detail": "Lines, crew and materials for the jobs in the plan."})
    if not actions:
        actions.append({"kind": "none", "owner": None, "title": "No action needed", "detail": "The current plan absorbs these orders on time."})
    d = lambda iso: dt.date.fromisoformat(iso).strftime("%d %b") if iso else None
    if after.get("all_finish"):
        hl = (f"With this plan and the parts ordered today, all production is done in {after['all_days']} days (by {d(after['all_finish'])}); "
              f"the work that has its parts in {after['days']} days (by {d(after['finish'])})")
    elif after["finish"]:
        hl = f"With this plan, all production is done in {after['days']} days (by {d(after['finish'])})"
    else:
        hl = "Nothing can be planned yet"
    if new["finish"] and new["on_time"] == new["orders"]:
        hl += f". All {new['orders']} new orders are on time: the last is ready by {d(new['finish'])}, in {new['days']} days"
    elif new["finish"] and not new["waiting"]:
        hl += f". The new orders are ready in {new['days']} days (by {d(new['finish'])}); {new['late']} late"
    elif new["waiting"]:
        hl += ". Some new orders wait for parts"
    if new["waiting_units"] and earliest:
        hl += f"; with the parts ordered today, the rest by {d(earliest)}"
    mats = _materials(items, x, before)
    machines = _machines(items, x, before)
    options = _options(items, x, plan.get("schedule_id") if plan else None)
    try:
        plants = _plants(items, x, before, plan.get("schedule_id") if plan else None)
    except Exception:                                               # the plant check never stops the recommendation
        log.exception("plant choice failed")
        plants = {}
    price = _prices(x)
    planned = next((o for o in options if o["key"] == "planned"), None)
    money = {"new_orders_inr": round(sum(i["qty"] * price.get(i["order_id"], 0) for i in items)),
             "parts_to_buy_inr": sum(m.get("buy_cost_inr") or 0 for m in mats),
             "overtime_inr": round(ot * next(iter(T._rates().values()), {}).get("labour_inr_per_hour", 0)
                                   * next(iter(T._rates().values()), {}).get("overtime_multiplier", 1)) if ot else 0,
             "late_penalty_inr": planned["late_penalty_inr"] if planned else None, "at_risk_inr": planned["at_risk_inr"] if planned else None,
             "covered_units": sum(i["covered_units"] for i in items)}
    for i in items:
        if i["needs_action"]:
            i["how"] = _how(i, mats, x, before, options)
    return {"headline": hl + ".", "as_of": x.as_of.date().isoformat(), "now": now, "with_plan": after, "new_orders": {**new, "earliest_all": earliest},
            "plan_id": plan.get("schedule_id") if plan else None, "actions": actions, "materials": mats, "machines": machines,
            "options": options, "money": money, "plants": plants}


# ------------------------------------------------------------------ the flow
def _facts(summary: str | None) -> list[str]:
    """A look's one-line summary as separate facts, in the words people use (findings, not signals)."""
    if not summary:
        return []
    words = [("signals", "findings"), ("signal", "finding"), ("lines watched", "production lines checked"), ("feed up to", "records up to"),
             ("Stock of ", "Stock count of "), ("under 3 days of cover", "materials with under 3 days of stock"),
             ("below safety", "materials below safety stock"), ("short for open orders", "materials short for open orders"),
             ("blocked for material in their plant", "waiting for parts in their own plant"),
             ("checked against the other plant", "checked against the other plant's stock, lines and crews"),
             ("moves worth making", "moves between plants worth making"), ("critical", "urgent")]
    out = []
    for x in summary.split("·"):
        x = x.strip()
        for a_, b_ in words:
            x = x.replace(a_, b_)
        if x:
            out.append(x[0].upper() + x[1:])
    return out

"""Plan options: each time the agent plans, it makes four complete plans from the same data, scores them the same way and
recommends one; the production manager chooses. The chosen plan goes on to the plant heads and gets its production plan document;
the other three are closed.

Every plan keeps the customers' dates (no re-dating): the point of planning is to deliver on time and avoid late penalties. They differ in
how hard they push to meet the dates:

  penalty    no extra spend: scarce parts and line time go to the orders due soonest that cost the most to miss
  at_risk    + express delivery only for parts whose normal lead time would miss the order's date
  shifts     + every shift of the pattern runs (days now off included) and through the break (labour at the overtime rate)
  outsource  + orders our plants would deliver more than two weeks late go to a contract manufacturer (extreme cases only)
  zero       every in-house lever at once (penalty first, express parts, every shift), then only the orders still late go to a contract
             manufacturer: what it costs to pay no late penalties at all

Every plan also checks the newest purchase orders before their dates are confirmed to the customer: which can be made by the date asked
and, for the rest, the earliest date that plan can deliver (promise).

Express and outsourcing terms are assumed (Policy.express_*: half the supplier lead time, at least 2 days, +20% on those parts;
Policy.outsource_*: 90% of our selling price, 7 days' notice) until real quotes are entered. Extra shifts use the labour rates. Every plan is scored the same way against the requested dates: on time, late orders, late penalties, express
premium, overtime, total cost; the agent recommends the lowest total cost including late penalties.
"""
import datetime as dt
import json
import logging
from collections import defaultdict

from . import planning, planning_tools as T, store, volume
from .config import Policy
from .db import AgentTask, PlanSet, SessionLocal, VolumePlan, _now

log = logging.getLogger("planner.plansets")
STRATEGIES = [   # key, title, what it does, policy changes (all: deadline first, customers' dates kept)
    ("penalty", "Penalty first", "No extra spend: scarce parts and line time go first to the orders due soonest that would cost the most to miss.",
     {"sequence": "penalty"}),
    ("at_risk", "Express where at risk", "Penalty first, plus express delivery for the parts whose normal lead time would miss the order's date.",
     {"sequence": "penalty", "express": "at_risk"}),
    ("shifts", "Extra shifts", "Penalty first, plus every shift runs (days now off included) and crews work through the break: more line time.",
     {"sequence": "penalty", "fill_shifts": True}),
    ("outsource", "Outsource the gap", "Last resort: orders our plants would deliver more than two weeks late go to a contract manufacturer; everything else stays in-house.",
     {"sequence": "penalty"}),
    ("zero", "Zero penalty", "Every in-house lever first (penalty first, express parts, every shift); only the orders that would still be late go to a contract manufacturer.",
     {"sequence": "penalty", "express": "at_risk", "fill_shifts": True}),
]
IN_HOUSE = ("penalty", "at_risk", "shifts")


def _plain(x):
    return json.loads(json.dumps(x, default=str))


def _metrics(s, x, requested: dict, overtime_h: float, overtime_inr: float, redated: int, premium_pct: float = 0.0,
             lever: dict | None = None) -> dict:
    """The same measures for every option, against the customers' requested dates."""
    rates = T._rates()
    R0 = next(iter(rates.values())) if rates else None
    price = {o.order_id: o.unit_price_inr or 0 for o in x.orders}
    # customer orders only (not the open production orders' own work, not forecast): the same measure as the plan document
    firm = {o.order_id for o in x.orders if o.order_id in requested and o.status not in ("PRODUCTION_ORDER", "FORECAST")}
    units = sum(o.qty for o in x.orders if o.order_id in firm)
    on_time, late, penalty = 0, set(), 0.0
    for a in s.assignments:
        oid = a.customer_order_id.split("#")[0]
        if oid not in firm:
            continue
        due = requested[oid]
        days = (a.fal_finish - (dt.datetime.combine(due, dt.time()) + dt.timedelta(days=1))).total_seconds() / 86400
        if days <= 0:
            on_time += a.qty
            continue
        late.add(oid)
        R = rates.get(a.plant_id, R0) if R0 else None
        if R:
            penalty += days * a.qty * price.get(oid, 0) * R["late_penalty_pct_per_day"] / 100
    end = x.horizon_end or (x.as_of + dt.timedelta(days=60))
    for u in s.unscheduled:                                          # not made within the planning period: late until its end
        oid = u.customer_order_id.split("#")[0]
        if oid in firm:
            late.add(oid)
            days = max(1.0, (end - (dt.datetime.combine(requested[oid], dt.time()) + dt.timedelta(days=1))).total_seconds() / 86400)
            if R0:
                penalty += days * u.qty * price.get(oid, 0) * R0["late_penalty_pct_per_day"] / 100
    lever = lever or {}
    on_time += s.summary.get("po_covered_qty") or 0                 # covered by production already under way
    on_time += lever.get("outsourced_units", 0)                     # a contract manufacturer delivers them on their date
    on_time = min(on_time, units)
    late_orders = len(late)
    c = T._cost(s, x)
    parts = dict(c.get("parts", {})) if c.get("available") else {}
    parts.pop("late_penalty", None)
    parts["overtime"] = round(overtime_inr)
    xp = [a for a in s.assignments if any(r.startswith("EXPEDITED") for r in a.reason_codes)]
    parts["express"] = round(sum(a.express_cost_inr for a in xp) * premium_pct / 100)     # premium on the parts bought by express only
    parts["extra_shifts"] = round(lever.get("extra_shift_inr", 0))
    parts["outsource"] = round(lever.get("outsource_inr", 0))
    running = sum(parts.values())
    m = s.summary
    last = max((a.fal_finish for a in s.assignments), default=None)
    return {"on_time_pct": round(100 * on_time / units, 1) if units else None, "late_orders": late_orders, "redated_orders": redated,
            "scheduled_units": m.get("scheduled_qty"), "forecast_units": m.get("forecast_qty", 0), "unscheduled_units": m.get("unscheduled_qty", 0),
            "overtime_h": round(overtime_h), "changeovers": m.get("changeovers"), "setup_h": round((m.get("changeover_min") or 0) / 60, 1),
            "utilisation_pct": round(100 * (m.get("avg_utilisation") or 0)), "bottleneck": m.get("bottleneck_line"),
            "purchase_jobs": m.get("purchase_wait_jobs", 0), "finish": last.date().isoformat() if last else None,
            "express_jobs": len(xp), "express_premium_inr": parts["express"], "overtime_inr": round(overtime_inr),
            "extra_shift_h": round(lever.get("extra_shift_h", 0)), "extra_shift_inr": parts["extra_shifts"],
            "outsourced_orders": lever.get("outsourced_orders", 0), "outsourced_units": lever.get("outsourced_units", 0),
            "outsource_inr": parts["outsource"],
            "running_cost_inr": round(running), "late_penalty_inr": round(penalty), "total_cost_inr": round(running + penalty)}


def _gantt(s, start: dt.datetime, days: int = 28) -> list[dict]:
    """Per line, the runs of each product over the first four weeks (consecutive jobs of the same product merged)."""
    end = start + dt.timedelta(days=days)
    span = (end - start).total_seconds()
    by = defaultdict(list)
    for a in s.assignments:
        for ln, a0, a1 in ((a.smt_line, a.smt_start, a.smt_finish), (a.fal_line, a.fal_start, a.fal_finish)):
            if a1 > start and a0 < end:
                by[ln].append((max(a0, start), min(a1, end), a.product_id))
    out = []
    for ln in sorted(by, key=lambda l: (l[:3] != "SMT", l)):
        blocks = []
        for a0, a1, p in sorted(by[ln]):
            if blocks and blocks[-1]["p"] == p and (a0 - blocks[-1]["_e"]).total_seconds() < 6 * 3600:
                blocks[-1]["_e"] = max(blocks[-1]["_e"], a1)
            else:
                blocks.append({"p": p, "_s": a0, "_e": a1})
        out.append({"line_id": ln, "blocks": [{"p": b["p"], "left": round(100 * (b["_s"] - start).total_seconds() / span, 2),
                                               "width": round(max(0.4, 100 * (b["_e"] - b["_s"]).total_seconds() / span), 2)} for b in blocks[:80]]})
    return out


def _shift_cost(x, x2) -> dict:
    """Extra line time from extra shifts (x2 vs x) and its labour cost: the crew of each line at the overtime rate."""
    rates, cr = T._rates(), T._gen("gen_crew")
    size = {"SMT": int(cr.crew_size_smt.iloc[0]), "FAL": int(cr.crew_size_fal.iloc[0])} if cr is not None else {"SMT": 6, "FAL": 9}
    before = {l.line_id: sum(s.minutes for s in l.slots) for l in x.lines}
    h = inr = 0.0
    for l in x2.lines:
        add = max(0.0, sum(s.minutes for s in l.slots) - before.get(l.line_id, 0))
        R = rates.get(l.plant_id) or next(iter(rates.values()), None)
        h += add / 60
        if R:
            inr += add / 60 * size[l.kind] * R["labour_inr_per_hour"] * R["overtime_multiplier"]
    return {"extra_shift_h": h, "extra_shift_inr": inr}


def _outsource(s, x, pol: Policy, min_late: float | None = None) -> dict:
    """Which orders a contract manufacturer makes: only the extreme cases. Orders the penalty-first plan would deliver more than
    Policy.outsource_min_late_days late, or cannot make at all, and that are due far enough ahead for its lead time. Everything
    else stays in our plants."""
    first = x.as_of.date() + dt.timedelta(days=pol.outsource_lead_days)
    orders = {o.order_id: o for o in x.orders if o.status not in ("PRODUCTION_ORDER", "FORECAST")}
    jobs = defaultdict(list)
    for a in s.assignments:
        jobs[a.customer_order_id].append(a)
    horizon_days = max(1, (x.horizon_end.date() - x.as_of.date()).days) if x.horizon_end else 60
    blocked = {u.customer_order_id for u in s.unscheduled}
    picks, pay = [], 0.0
    for oid, o in orders.items():
        js = jobs.get(oid, [])
        late = max((a.late_days for a in js), default=0.0) if js else (horizon_days if oid in blocked else 0.0)
        if late <= (pol.outsource_min_late_days if min_late is None else min_late) or o.due < first or not o.unit_price_inr:
            continue
        picks.append(oid)
        pay += o.qty * o.unit_price_inr * pol.outsource_price_pct / 100
    return {"orders": picks, "outsourced_orders": len(picks), "outsourced_units": sum(orders[i].qty for i in picks), "outsource_inr": pay}


def _new_ids() -> set[str]:
    from .final_plan import _new_orders
    return _new_orders()


def promise(s, x, requested: dict, new_ids: set, outsourced: set) -> dict:
    """The newest purchase orders checked before their dates are confirmed: how many this plan makes by the date asked, and for each of
    the others the earliest date it can deliver (None: not within the planning period). Outsourced orders arrive on their date."""
    fin = {}
    for a in s.assignments:
        oid = a.customer_order_id.split("#")[0]
        fin[oid] = max(fin.get(oid, a.fal_finish), a.fal_finish)
    blocked = {u.customer_order_id.split("#")[0] for u in s.unscheduled}
    new = [o for o in x.orders if o.order_id in new_ids and o.status not in ("PRODUCTION_ORDER", "FORECAST")]
    late = []
    for o in new:
        asked = requested.get(o.order_id, o.due)
        if o.order_id in outsourced:
            continue
        f = None if o.order_id in blocked else fin.get(o.order_id)
        if o.order_id in blocked or (f and f.date() > asked):
            late.append({"id": o.order_id, "customer": o.customer_id, "product_id": o.product_id, "qty": o.qty, "asked": asked.isoformat(),
                         "promise": f.date().isoformat() if f else None, "days_late": (f.date() - asked).days if f else None,
                         "value": round(o.qty * (o.unit_price_inr or 0))})
    late.sort(key=lambda r: (r["asked"], r["id"]))
    return {"new": len(new), "on_time": len(new) - len(late), "late": late[:300], "late_count": len(late),
            "until": x.horizon_end.date().isoformat() if x.horizon_end else None}


def pick(plans: list[dict], pol: Policy) -> dict:
    """The agent's recommendation: in-house first. Among the plans that keep production in our plants, the one with the most units
    on time (then the cheapest within 2 points of it). Outsourcing is recommended only in an extreme case: even the best in-house plan
    has fewer than outsource_trigger_on_time_pct of units on time AND outsourcing adds at least outsource_trigger_gain_pts."""
    def best_of(ps):
        top = max(p["metrics"]["on_time_pct"] or 0 for p in ps)
        return min((p for p in ps if (p["metrics"]["on_time_pct"] or 0) >= top - 2), key=lambda p: p["metrics"]["total_cost_inr"])
    inhouse = [p for p in plans if p["key"] in IN_HOUSE] or plans
    home = best_of(inhouse)
    zero = next((p for p in plans if p["key"] == "zero"), None)
    if zero and zero["metrics"]["total_cost_inr"] < home["metrics"]["total_cost_inr"]:
        return zero                                                    # paying no penalties costs no more than paying them
    out = next((p for p in plans if p["key"] == "outsource" and p["metrics"].get("outsourced_orders")), None)
    if out:
        h, o = home["metrics"]["on_time_pct"] or 0, out["metrics"]["on_time_pct"] or 0
        if h < pol.outsource_trigger_on_time_pct and o - h >= pol.outsource_trigger_gain_pts:
            return out
    return home


def gap(x, pol: Policy) -> dict:
    """What no plan can deliver on time: final-assembly demand due by each week against the capacity up to that week (cumulative).
    The largest shortfall is the work that must be added (line time, outsourcing) or agreed with customers."""
    f = volume.fill(x, pol)
    rate_h = sum(l.rate_per_min for l in x.lines if l.kind == "FAL") * 60 or 1.0
    dem = cap = 0.0
    worst = {"units": 0, "week": None, "load_pct": None}
    for r in f["load"]:
        dem, cap = dem + r["demand"], cap + r["capacity"]
        if dem - cap > worst["units"]:
            worst = {"units": round(dem - cap), "week": r["week"], "load_pct": r["load_pct"]}
    peak = max(f["load"], key=lambda r: r["load_pct"] or 0, default=None)
    return {**worst, "hours": round(worst["units"] / rate_h), "shifts": round(worst["units"] / rate_h / 8),
            "peak_week": peak and peak["week"], "peak_pct": peak and peak["load_pct"]}


def make(raw, pol: Policy, trigger: str, why: str, actor: str, name: str) -> dict:
    """Build the five plans from the same data, score them and save the set. Returns the set (recommended plan id included)."""
    vp = volume.agreed()
    x = volume.apply(raw, vp)                                       # dates already committed stay committed
    requested = {o.order_id: o.due for o in raw.orders}
    plans = []
    new_ids = _new_ids()
    first = None                                                      # the penalty-first plan: the outsourcing choice starts from it
    for key, title, text, upd in STRATEGIES:
        p2, x2, lever = pol.model_copy(update=upd), x, {}
        if vp:
            p2 = p2.model_copy(update={"volume_plan_id": vp.id})
        if key == "shifts":
            x2 = volume.levers(x, p2)
            lever = _shift_cost(x, x2)
        elif key == "zero":
            x_in = volume.levers(x, p2)                                # every shift; express is in the policy
            lever = _outsource(planning.plan(x_in, p2), x, pol, min_late=0)
            p2 = p2.model_copy(update={"outsource_orders": lever.pop("orders")})
            x2 = volume.levers(x, p2)
            lever.update(_shift_cost(x, x2))
        elif key == "outsource" and first is not None:
            lever = _outsource(first, x, pol)
            p2 = p2.model_copy(update={"outsource_orders": lever.pop("orders")})
            x2 = volume.levers(x, p2)
        s = planning.plan(x2, p2)
        if key == "penalty":
            first = s
        s.name = f"{title} · {name}"[:200]
        store.put(s, actor="planning agent", role="agent", inp=x2)
        out_list = [{"id": o.order_id, "product_id": o.product_id, "qty": o.qty, "due": o.due.isoformat(), "price": o.unit_price_inr,
                     "customer": o.customer_id}
                    for o in x.orders if o.order_id in set(p2.outsource_orders or [])]   # what the contract manufacturer makes (for the Gantt)
        plans.append({"key": key, "title": title, "text": text, "schedule_id": s.id, "volume_plan_id": None, "outsourced": out_list,
                      "metrics": _metrics(s, x, requested, 0.0, 0.0, 0, pol.express_premium_pct if p2.express != "off" else 0.0, lever),
                      "gantt": _gantt(s, dt.datetime.combine(x.as_of.date(), dt.time())),
                      "promise": promise(s, x, requested, new_ids, set(p2.outsource_orders or []))})
    best = pick(plans, pol)
    for p in plans:
        p["best"] = p is best
    stale = []
    with SessionLocal() as s:
        for old in s.query(PlanSet).filter_by(status="OPEN"):
            old.status = "SUPERSEDED"
            stale += [p["schedule_id"] for p in old.plans]
        for old in s.query(VolumePlan).filter_by(status="PROPOSED"):           # the options replace any capacity question still open
            old.status = "SUPERSEDED"
        g = gap(x, pol)
        for p in plans:
            p["gap"] = g                                              # the same for every plan: it is in the order book, not the plan
        moved = {oid: requested[oid].isoformat() for oid in ((vp.redates or {}) if vp else {}) if oid in requested}
        for p in plans:
            p["requested_dates"] = moved                                # the customers' own dates where a capacity decision moved them
        ps = PlanSet(trigger=trigger, reason=why[:2000], actor=actor, plans=_plain(plans), recommended=best["schedule_id"], status="OPEN",
                     as_of=x.as_of.isoformat(), products=sorted({o.product_id for o in x.orders}))
        s.add(ps)
        s.commit()
        s.expunge(ps)
    for sid in stale:                                                 # older options nobody chose: closed, so only the new four wait
        if store.set_status(sid, "PENDING_APPROVAL", "REJECTED", "planning agent", "Replaced by newer plans"):
            store.audit(sid, "planning agent", "agent", "rejected", {"note": "Replaced by newer plans", "plan_set": ps.id})
    log.info("plan options %s: %s (pick: %s)", ps.id, ", ".join(p["title"] for p in plans), best["title"])
    return out(ps)


def _inr(v) -> str:
    return "–" if v is None else f"₹{v / 1e7:.2f} Cr" if v >= 1e7 else f"₹{v / 1e5:.1f} L" if v >= 1e5 else f"₹{v:,.0f}"


def why(plans: list[dict]) -> list[str]:
    """Why the agent recommends its pick, in plain words, measured against the other three plans."""
    best = next((p for p in plans if p.get("best")), None)
    if not best:
        return []
    m, rest = best["metrics"], [p for p in plans if p is not best]
    out = []
    top = max(plans, key=lambda p: p["metrics"]["on_time_pct"] or 0)
    if top is best:
        out.append(f"Delivers the most on time: {m['on_time_pct']}% of ordered units by the date the customer asked for.")
    nxt = min(rest, key=lambda p: p["metrics"]["total_cost_inr"], default=None)
    if nxt and nxt["metrics"]["total_cost_inr"] > m["total_cost_inr"]:
        out.append(f"Lowest total cost once late deliveries are priced in: {_inr(m['total_cost_inr'])}, "
                   f"{_inr(nxt['metrics']['total_cost_inr'] - m['total_cost_inr'])} less than “{nxt['title']}”.")
    if top is not best and m["on_time_pct"] is not None and top["metrics"]["on_time_pct"] is not None:
        if top["key"] == "outsource" and best["key"] != "outsource":
            out.append(f"{m['on_time_pct']}% of units on time with our own plants. Outsourcing would reach {top['metrics']['on_time_pct']}% "
                       f"but costs {_inr(top['metrics']['total_cost_inr'] - m['total_cost_inr'])} more and moves production out of our plants.")
        else:
            out.append(f"On time {m['on_time_pct']}%, within {round(top['metrics']['on_time_pct'] - m['on_time_pct'], 1)} points of the best "
                       f"(“{top['title']}”), at a lower overall cost.")
    if m["late_orders"] <= min(p["metrics"]["late_orders"] for p in rest):
        out.append(f"Fewest late orders: {m['late_orders']:,}.")
    if m.get("late_penalty_inr") is not None and m["late_penalty_inr"] <= min(p["metrics"].get("late_penalty_inr", 0) for p in rest):
        out.append(f"Lowest late penalties: {_inr(m['late_penalty_inr'])}.")
    out.append("Keeps every customer's date: nothing is re-dated.")
    outp = next((p for p in plans if p["key"] == "outsource"), None)
    if best["key"] in IN_HOUSE and outp and outp["metrics"].get("outsourced_orders"):
        out.append(f"Keeps all production in our own plants. Outsourcing ({outp['metrics']['outsourced_orders']:,} very late orders) is held back "
                   "for extreme cases: it is only recommended when even the best in-house plan delivers under a quarter of units on time.")
    elif best["key"] == "outsource":
        home = max((p for p in rest if p["key"] != "outsource"), key=lambda p: p["metrics"]["on_time_pct"] or 0, default=None)
        if home:
            out.append(f"An extreme case: even the best in-house plan (“{home['title']}”) delivers only {home['metrics']['on_time_pct']}% on time, "
                       f"so only the orders that would be more than two weeks late go to a contract manufacturer.")
    zp = next((p for p in plans if p["key"] == "zero"), None)
    if zp and best is not zp:
        zm = zp["metrics"]
        out.append(f"Paying no late penalties (“Zero penalty”) would cost {_inr(zm['total_cost_inr'])} in total, "
                   f"{_inr(zm['total_cost_inr'] - m['total_cost_inr'])} more than this plan: our lines can't make every order by its date, "
                   f"so {zm['outsourced_orders']:,} orders would go to a contract manufacturer.")
    elif zp:
        out.append(f"Avoiding the late penalties costs less than paying them: {zm_txt(zp, plans)}")
    if m.get("express_jobs"):
        out.append(f"{m['express_jobs']:,} jobs get their parts by express delivery ({_inr(m['express_premium_inr'])} premium), so they start sooner.")
    if m.get("extra_shift_h"):
        out.append(f"Adds {m['extra_shift_h']:,} line hours by running every shift and through breaks ({_inr(m['extra_shift_inr'])} labour).")
    if m.get("outsourced_orders"):
        out.append(f"{m['outsourced_orders']:,} orders ({m['outsourced_units']:,} units) go to a contract manufacturer and arrive on their date "
                   f"({_inr(m['outsource_inr'])}), which frees our lines for the rest.")
    if not (m.get("express_jobs") or m.get("extra_shift_h") or m.get("outsourced_orders")):
        out.append("Needs no extra spend: no express parts, extra shifts or outsourcing.")
    if m["changeovers"] is not None and m["changeovers"] <= min(p["metrics"]["changeovers"] or 0 for p in rest):
        out.append(f"Fewest product changeovers: {m['changeovers']:,} ({m['setup_h']} h of setup).")
    if m.get("purchase_jobs"):
        out.append(f"{m['purchase_jobs']:,} jobs start once bought parts arrive; the order dates are in the plan.")
    if m["late_orders"]:
        out.append(f"{m['late_orders']:,} orders still miss their date in this plan: our lines and parts can't make them in time. "
                   "Talk to these customers now about partial or later delivery.")
    return out


def zm_txt(zp: dict, plans: list[dict]) -> str:
    home = min((p for p in plans if p["key"] in IN_HOUSE), key=lambda p: p["metrics"]["total_cost_inr"])
    return (f"{_inr(zp['metrics']['total_cost_inr'])} in total against {_inr(home['metrics']['total_cost_inr'])} for “{home['title']}”, "
            f"which would pay {_inr(home['metrics']['late_penalty_inr'])} in penalties.")


def out(ps: PlanSet | None) -> dict | None:
    if ps is None:
        return None
    d = _plain({k: getattr(ps, k) for k in ("id", "created_at", "trigger", "reason", "actor", "plans", "recommended", "status", "chosen", "chosen_by",
                                           "chosen_at", "as_of", "products")})
    d["why"] = why(d["plans"])
    return d


def history(limit: int = 20) -> list[dict]:
    """Cached until a plan is saved, decided or approved (same invalidation as the plan list)."""
    from . import cache
    return cache.get("plan_sets.history", (limit,), 60, lambda: _history(limit), ("plans", "planlist"))


def _history(limit: int = 20) -> list[dict]:
    """Every planning run, newest first: when and why, the agent's pick, the plan chosen and where it stands (approval status).
    The plans themselves are not loaded (they are large): names and statuses come from the plans table."""
    cols = (PlanSet.id, PlanSet.created_at, PlanSet.reason, PlanSet.actor, PlanSet.status, PlanSet.recommended, PlanSet.chosen, PlanSet.chosen_by)
    with SessionLocal() as s:
        sets = s.query(*cols).order_by(PlanSet.created_at.desc()).limit(limit).all()
    ids = [x for ps in sets for x in (ps.chosen, ps.recommended) if x]
    st, full = {}, set()
    if ids:
        got, _ = store._run(f"SELECT id,status,name FROM schedules WHERE id IN ({','.join('?' * len(ids))})", tuple(ids), "all")
        st, full = {r["id"]: r for r in got}, store.fully_approved_ids()
    title = lambda sid: (st.get(sid, {}).get("name") or "").split(" · ")[0] or None
    return [{"id": ps.id, "created_at": ps.created_at.isoformat() if ps.created_at else None, "reason": ps.reason, "actor": ps.actor,
             "status": ps.status, "recommended": title(ps.recommended), "chosen": ps.chosen and title(ps.chosen), "chosen_by": ps.chosen_by,
             "schedule_id": ps.chosen or ps.recommended, "plan_status": st.get(ps.chosen or ps.recommended, {}).get("status"),
             "fully_approved": (ps.chosen or ps.recommended) in full} for ps in sets]


def latest() -> dict | None:
    with SessionLocal() as s:
        ps = s.query(PlanSet).order_by(PlanSet.created_at.desc()).first()
        if ps:
            s.expunge(ps)
        return out(ps)


def get(pid: str) -> dict | None:
    with SessionLocal() as s:
        ps = s.get(PlanSet, pid)
        if ps:
            s.expunge(ps)
        return out(ps)


def choose(pid: str, sid: str, user: str, role: str, decide) -> dict:
    """The production manager chooses one plan: the agent's tasks move to it, it is approved (decide), the other three are closed, and
    if it commits new dates those become the agreed volume plan. decide(sid) is the normal approval (risk check, freezing orders)."""
    from . import planning_agent
    with SessionLocal() as s:
        ps = s.get(PlanSet, pid)
        if ps is None:
            raise LookupError(pid)
        if ps.status != "OPEN":
            raise ValueError("These plan options were already decided or replaced by newer ones")
        pick = next((p for p in ps.plans if p["schedule_id"] == sid), None)
        if pick is None:
            raise ValueError("That plan is not one of these options")
        ids = [p["schedule_id"] for p in ps.plans]
    # the hand-over tasks were made for the agent's pick (or moved by an earlier try that stopped at the risk check): move them here
    sched, inp = store.get(sid), store.inputs(sid)
    pk = {x["plant_id"]: x for x in planning_agent._packages(sched, inp)}
    with SessionLocal() as s:
        for t in s.query(AgentTask).filter(AgentTask.schedule_id.in_([i for i in ids if i != sid])):
            t.schedule_id = sid
            if t.role == "plant_head" and t.plant_id in pk:
                t.details = {**(t.details or {}), **pk[t.plant_id]}
        s.commit()
    result = decide(sid)                                              # raises when risks are not acknowledged: nothing else changes
    for p in ps.plans:
        if p["schedule_id"] != sid and store.set_status(p["schedule_id"], "PENDING_APPROVAL", "REJECTED", user, f"Another plan was chosen: {pick['title']}"):
            store.audit(p["schedule_id"], user, role, "rejected", {"note": f"Another plan was chosen: {pick['title']}", "plan_set": pid})
    with SessionLocal() as s:
        ps = s.get(PlanSet, pid)
        ps.status, ps.chosen, ps.chosen_by, ps.chosen_at = "CHOSEN", sid, user, _now()
        for p in ps.plans:                                            # options not chosen leave nothing behind
            if p.get("volume_plan_id") and p["schedule_id"] != sid:
                v = s.get(VolumePlan, p["volume_plan_id"])
                if v and v.status == "OPTION":
                    v.status = "SUPERSEDED"
        if pick.get("volume_plan_id"):                                # committed dates become the agreed volume plan
            v = s.get(VolumePlan, pick["volume_plan_id"])
            if v:
                for old in s.query(VolumePlan).filter(VolumePlan.status == "AGREED"):
                    old.status = "SUPERSEDED"
                v.status, v.chosen, v.agreed_by, v.agreed_at = "AGREED", pick["key"], user, _now()
                if pick["metrics"]["redated_orders"]:
                    from .db import TeamMember
                    pm = next((m.name for m in s.query(TeamMember).filter_by(role="production_manager")), None)
                    s.add(AgentTask(run_id=next((t.run_id for t in s.query(AgentTask).filter_by(schedule_id=sid)), None) or _run_id(s),
                                    schedule_id=sid, role="production_manager", assignee=pm, kind="alert", status="OPEN",
                                    title=f"Tell {pick['metrics']['redated_orders']} customers their committed delivery dates"[:200],
                                    details={"agent": "planning", "severity": "info", "type": "dates_committed"}))
        s.commit()
    from . import coord
    coord.changed("plans")
    store.audit(sid, user, role, "plan_option_chosen", {"plan_set": pid, "option": pick["key"], "title": pick["title"]})
    return {"chosen": sid, "title": pick["title"], "decision": result}


def _run_id(s) -> str:
    from .db import AgentRun
    r = AgentRun(trigger="plan_choice", outcome="CHOSEN", reason="Plan option chosen", open_orders=[], actor="planning agent", details={})
    s.add(r)
    s.flush()
    return r.id

"""Deterministic line-level planner. No LLM here. Proposes only; never edits BOM/routing/orders.

Model: each open order flows SMT line -> FAL line of ONE plant (SMT finish gates FAL start), finite shift capacity,
priority (urgent first, then HIGH>MEDIUM>LOW, then due date) drives who gets capacity AND material first.
Material is never fabricated: quantity is capped to what stock covers; the rest is reported as unscheduled/shortage.
Time fences: given a previous schedule, its jobs starting in the frozen zone (2 weeks) are re-placed first, same plant, same qty;
changes in the slushy zone (weeks 3-6) are flagged. Bottleneck levelling: of plants that finish on time, the less-loaded final assembly.
MRP: what stock can't cover starts after the missing parts arrive (ordered today, supplier lead time). Forecast demand beyond the
confirmed orders is planned last (make-to-stock), from stock left over.
"""
import math
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from .config import Policy
from .domain import Assignment, Change, Diagnostic, Order, PlanningInput, Schedule, Unscheduled

NOT_ENFORCED = [     # shown to planners: plain language
    "Staff skills and headcount (absence is only modelled as lost line time in what-if scenarios)",
    "Tooling availability",
    "Testing, quality-check and packing stations (only the SMT and final-assembly lines are planned)",
    "Purchases for forecast demand (forecast work only uses stock that is left over)",
    "Timing of open production orders' remaining work (what they still owe counts as supply now)",
    "Arrival dates of stock in transit (it is counted as available now)",
    "Time and cost of moving stock between plants",
    "Full changeover optimisation (orders are only grouped by product within each due week)",
    "Breaks within shifts",
]


class _Line:
    def __init__(self, spec):
        self.id, self.plant, self.kind, self.rate = spec.line_id, spec.plant_id, spec.kind, spec.rate_per_min
        self.slots = [{"start": s.start, "end": s.end, "cap": float(s.minutes), "used": 0.0, "cursor": s.start} for s in spec.slots]
        self.last, self.used, self.changeovers, self.setup_min = None, 0.0, 0, 0.0

    def alloc(self, minutes, not_before, commit=False):
        """Place `minutes` of work on free shift time, never before `not_before` and never before earlier work in the same shift.
        Returns (start, finish) or None if it doesn't fit the horizon.
        ponytail: idle gaps before a shift's cursor are not back-filled; add gap back-filling if utilisation under-reports."""
        need, plan, start, finish = minutes, [], None, None
        for s in self.slots:
            if s["end"] <= not_before:
                continue
            begin = max(s["cursor"], not_before)
            room = min(s["cap"] - s["used"], (s["end"] - begin).total_seconds() / 60)
            if room <= 1e-9:
                continue
            take = min(room, need)
            start = start or begin
            finish = begin + timedelta(minutes=take)
            plan.append((s, begin, take))
            need -= take
            if need <= 1e-9:
                break
        if need > 1e-9:
            return None
        if commit:
            for s, begin, take in plan:
                s["used"] += take
                s["cursor"] = begin + timedelta(minutes=take)
            self.used += minutes
        return start, finish


def plan(inp: PlanningInput, pol: Policy | None = None, previous: Schedule | None = None, name: str = "baseline") -> Schedule:
    pol = pol or Policy()
    lines = {l.line_id: _Line(l) for l in inp.lines}
    pair = {}
    for p in sorted({l.plant for l in lines.values()}):
        pair[p] = (next(l for l in lines.values() if l.plant == p and l.kind == "SMT"),
                   next(l for l in lines.values() if l.plant == p and l.kind == "FAL"))
    segs = defaultdict(set)
    setup = defaultdict(float)
    for r in inp.routing:
        segs[r.product_id].add(r.segment)
        setup[(r.product_id, r.segment)] += r.setup_min          # sum across ops = conservative
    bom = defaultdict(list)
    for b in inp.bom:
        bom[b.product_id].append(b)
    reserve = pol.respect_safety_stock
    stock = {(s.plant_id, s.material_id): max(0.0, s.available - (s.safety if reserve else 0)) for s in inp.stock}
    pooled = any(k[0] == "ALL" for k in stock)
    sk = (lambda p, m: ("ALL", m)) if pooled else (lambda p, m: (p, m))
    cost = {s.material_id: s.unit_cost_inr for s in inp.stock}
    orders = {o.order_id: o for o in inp.orders}
    diags: list[Diagnostic] = []
    out: list[Assignment] = []
    unsched: list[Unscheduled] = []
    for i in inp.data_issues:
        diags.append(Diagnostic(code="DATA_" + i["code"], severity="info" if i["severity"] == "info" else "warn", detail=f'{i["count"]} rows: {i["message"]}'))

    def su(product, stage, line):
        return setup.get((product, stage), 0) if pol.apply_setup_on_product_change and line.last != product else 0

    yl = inp.yields if pol.yield_aware else {}
    def speed(line, product):          # line rates are in good units at the line's average yield; a product with more rejects runs slower
        y, avg = yl.get(f"{line.id}|{product}"), yl.get(f"{line.id}|*")
        return line.rate * (y / avg if y and avg else 1.0)

    def mat_factor(plant, product):    # every unit made uses material, good or rejected: material per good unit = 1 / yield
        f = 1.0
        for ln in pair[plant]:
            y = yl.get(f"{ln.id}|{product}") or yl.get(f"{ln.id}|*")
            f /= y if y else 1.0
        return f

    def cover(plant, product, qty):
        """Max units the stock can cover; returns (qty, limiting material)."""
        c, lim, mf = qty, None, mat_factor(plant, product)
        for b in bom[product]:
            n = math.floor(stock.get(sk(plant, b.material_id), 0) / (b.qty_per * (1 + b.scrap) * mf) + 1e-9)
            if n < c:
                c, lim = n, b.material_id
        return max(c, 0), lim

    # ---- work list. With a previous plan: new expedited orders first (they may break the frozen fence: flagged for approval), then the
    # previous plan's jobs that start in the frozen zone (kept in their plant, same qty), then the rest by priority, then forecast demand.
    frozen_qty = defaultdict(int)
    cut = inp.as_of + timedelta(days=pol.frozen_days)
    kept = []
    if previous:
        for a in sorted(previous.assignments, key=lambda a: a.smt_start):
            o = orders.get(a.customer_order_id)
            if o and a.smt_start < cut:
                q = min(a.qty, o.qty - frozen_qty[o.order_id])
                if q > 0:
                    kept.append((o, q, a.plant_id, a))
                    frozen_qty[o.order_id] += q
    w = pol.batch_window_days
    def order_key(o):             # priority first; inside a priority, orders due in the same window are grouped by product
        bucket = (o.due - inp.as_of.date()).days // w if w > 0 else 0
        if pol.sequence == "penalty":  # deadline first: each week's orders, the costliest to miss (late penalty ~ order value) first
            return (not o.urgent, (o.due - inp.as_of.date()).days // 7, -(o.unit_price_inr or 0) * o.qty, o.due, o.order_id)
        return (not o.urgent, pol.priority_rank.get(o.priority, 99), bucket, o.product_id if w > 0 else "", o.due, o.order_id)
    rest = sorted(inp.orders, key=order_key)
    if reserve and any(s.safety for s in inp.stock):
        diags.append(Diagnostic(code="SAFETY_STOCK_RESERVED", severity="info",
                                detail=f"{round(sum(min(s.safety, s.available) for s in inp.stock))} units of stock kept as safety stock and not planned"))
    po = dict(inp.open_po_supply) if pol.net_open_po_supply else {}
    po_start, po_covered, netted = sum(po.values()), 0, {}
    early, normal = [], []
    for o in rest:
        q = o.qty - frozen_qty[o.order_id]
        if q <= 0:
            continue
        # an open production order's own work is never netted against itself; a sales enquiry (promise check) needs its own production
        own = o.kitted or o.status in ("PRODUCTION_ORDER", "ENQUIRY")
        c = 0 if own else min(po.get(o.product_id, 0), q)
        if c:
            po[o.product_id] -= c
            po_covered += c
            netted[o.order_id] = c
        if q - c > 0:
            (early if kept and o.urgent and not frozen_qty[o.order_id] else normal).append((o, q - c, None, None))
    for o, *_ in early:
        diags.append(Diagnostic(code="FROZEN_FENCE_BROKEN", severity="warn", order_id=o.order_id,
                                detail=f"expedited ahead of the frozen zone (first {pol.frozen_days} days): frozen jobs may move"))
    jobs = early + kept + normal
    if po_covered:
        diags.append(Diagnostic(code="OPEN_PO_SUPPLY_APPLIED", severity="warn",
                                detail=f"{po_covered} units covered by open production orders (of {po_start} open PO units; remaining qty unverified)"))
    # make-to-stock: each week's forecast, less the confirmed orders due that week, planned last (lowest priority, stock left over only)
    fc_ids = set()
    if pol.plan_forecast and inp.forecast:
        for prod, weeks in sorted(inp.forecast.items()):
            for f in weeks:
                w0 = datetime.fromisoformat(f["week"]).date()
                firm_q = sum(o.qty for o in inp.orders if o.product_id == prod and o.status != "PRODUCTION_ORDER" and w0 <= o.due <= w0 + timedelta(days=6))
                q = f["qty"] - firm_q
                if q > 0:
                    o = Order(order_id=f"FC-{prod}-{w0:%m%d}", product_id=prod, qty=q, due=w0 + timedelta(days=6), priority="LOW", status="FORECAST")
                    orders[o.order_id] = o
                    fc_ids.add(o.order_id)
                    jobs.append((o, q, None, None))
        # production budget: finished goods to hold at the end = target days of forecast demand, less what is in stock
        for prod, weeks in sorted(inp.forecast.items()):
            if not weeks:
                continue
            q = round(pol.fg_target_days * sum(f["qty"] for f in weeks) / (7 * len(weeks))) - inp.fg_stock.get(prod, 0)
            if q > 0:
                end = datetime.fromisoformat(weeks[-1]["week"]).date() + timedelta(days=6)
                o = Order(order_id=f"FC-{prod}-END", product_id=prod, qty=q, due=end, priority="LOW", status="FORECAST")
                orders[o.order_id] = o
                fc_ids.add(o.order_id)
                jobs.append((o, q, None, None))

    def lead(m):
        return inp.lead_days.get(m, pol.purchase_default_lead_days)

    def fast(m):                                   # express delivery (the plan options' assumed terms, Policy.express_*)
        return max(pol.express_min_days, math.ceil(lead(m) * pol.express_lead_factor))

    def lead_for(m, o):                            # the lead time a job's missing part gets: express when the plan says so
        if pol.express == "all" or (pol.express == "at_risk" and inp.as_of.date() + timedelta(days=lead(m)) >= o.due):
            return min(lead(m), fast(m))
        return lead(m)

    def short_of(plant, product, qty):             # materials stock can't cover for qty units
        mf = mat_factor(plant, product)
        return [b.material_id for b in bom[product] if stock.get(sk(plant, b.material_id), 0) + 1e-9 < qty * b.qty_per * (1 + b.scrap) * mf]

    line_cap = {l.id: sum(s["cap"] for s in l.slots) or 1.0 for l in lines.values()}
    was_at = defaultdict(set)
    for a in (previous.assignments if previous else []):
        was_at[a.customer_order_id].add(a.plant_id)

    def attempt(o, qty, forced, frozen, bought):
        """The best plant for qty units: most coverable qty, then on time, then (levelling) the less-loaded final assembly, then earliest finish.
        bought = the missing parts are ordered today: the job starts no earlier than the slowest of them arrives."""
        best, covers = None, []
        due_end = datetime.combine(o.due, datetime.min.time()) + timedelta(days=1)
        for p in ([forced] if forced else [o.plant_id] if o.plant_id in pair else pair):
            smt, fal = pair[p]
            nb, wait = inp.as_of, None
            if frozen:                                 # locked: same times and setup as planned; moves only if that line time is gone
                c, lim, nb = qty, None, max(inp.as_of, prev_of[o.order_id].smt_start)
            elif o.kitted:
                c, lim = qty, None
            elif bought:
                miss = short_of(p, o.product_id, qty)
                c, lim = qty, (max(miss, key=lambda m: lead_for(m, o)) if miss else None)
                if lim:
                    nb, wait = inp.as_of + timedelta(days=lead_for(lim, o)), lim
            else:
                c, lim = cover(p, o.product_id, qty)
            covers.append((c, lim))
            if c <= 0:
                continue
            pv = prev_of.get(o.order_id) if frozen else None
            s1 = pv.smt_setup_min if pv else su(o.product_id, "SMT", smt)
            m1 = c / speed(smt, o.product_id) + s1
            a = smt.alloc(m1, nb)
            if not a:
                continue
            s2 = pv.fal_setup_min if pv else su(o.product_id, "FAL", fal)
            m2 = c / speed(fal, o.product_id) + s2
            fnb = max(a[1], pv.fal_start) if pv else a[1]
            b = fal.alloc(m2, fnb)
            if not b:
                continue
            late = b[1] > due_end
            load = fal.used / line_cap[fal.id] if pol.level_bottleneck and not late else 0.0
            moved = bool(was_at) and p not in was_at.get(o.order_id, (p,))     # stability: keep an order in its previous plant when it is still on time
            key = (-c, late, moved, round(load, 3), b[1])
            if best is None or key < best[0]:
                best = (key, p, c, lim, s1, m1, a, s2, m2, b, nb, wait, fnb)
        return best, covers

    def commit(o, best, frozen, fc, prev, split):
        _, p, c, lim, s1, m1, a, s2, m2, b, nb, wait, fnb = best
        smt, fal = pair[p]
        smt.alloc(m1, nb, commit=True)
        fal.alloc(m2, fnb, commit=True)
        for ln, s in ((smt, s1), (fal, s2)):
            ln.last = o.product_id
            if s:
                ln.changeovers += 1
                ln.setup_min += s
        reasons = ["FROZEN"] if frozen else ["FORECAST_REPLENISHMENT"] if fc else ["PLANT_BEST_COVER_THEN_EARLIEST_FINISH"]
        if pol.level_bottleneck and not frozen:
            reasons.append("BOTTLENECK_LEVELLED")
        if o.order_id in netted:
            reasons.append(f"PO_NETTED:{netted[o.order_id]}")
        if s1 or s2:
            reasons.append("CHANGEOVER_ADDED")
        if wait:
            reasons.append(f"WAITS_FOR_PURCHASE:{wait}")
            x = lead_for(wait, o) < lead(wait)
            if x:
                reasons.append(f"EXPEDITED:{wait}")
            diags.append(Diagnostic(code="PLANNED_AFTER_PURCHASE", severity="warn", order_id=o.order_id, material_id=wait,
                                    detail=f"{c} units start after {wait} arrives ({lead_for(wait, o)}-day {'express ' if x else ''}lead time): order it today"))
        if split:
            reasons.append("SPLIT_FOR_PURCHASE")
        short, mcost, mf, xcost = False, 0.0, mat_factor(p, o.product_id), 0.0
        express = bool(wait) and lead_for(wait, o) < lead(wait)
        for bl in ([] if o.kitted else bom[o.product_id]):
            need = c * bl.qty_per * (1 + bl.scrap) * mf
            have = stock.get(sk(p, bl.material_id), 0)
            if express and have < need:                # the part bought for this job comes by express
                xcost += (need - have) * (cost.get(bl.material_id) or 0)
            if have + 1e-9 < need and frozen:          # a frozen job keeps its place even without the stock
                short = True
                diags.append(Diagnostic(code="MATERIAL_SHORT_FROZEN", severity="error", order_id=o.order_id, material_id=bl.material_id,
                                        detail=f"frozen job needs {need:.0f}, stock {have:.0f}"))
            stock[sk(p, bl.material_id)] = max(0.0, have - need)   # a purchased job uses what stock there is; the rest is bought for it
            mcost += need * (cost.get(bl.material_id) or 0)
        due_end = datetime.combine(o.due, datetime.min.time()) + timedelta(days=1)
        late = 0.0 if fc else max(0.0, (b[1] - due_end).total_seconds() / 86400)
        if late > 0:
            reasons.append("LATE_RISK")
            diags.append(Diagnostic(code="LATE_RISK", severity="warn", order_id=o.order_id, detail=f"finishes {late:.1f} d after due {o.due}"))
            if o.due < inp.as_of.date():
                reasons.append("PAST_DUE_AT_PLAN_START")
        out.append(Assignment(customer_order_id=o.order_id, product_id=o.product_id, plant_id=p, qty=c, priority=o.priority,
                              due=o.due.isoformat(), urgent=o.urgent, frozen=frozen, smt_line=smt.id, smt_start=a[0], smt_finish=a[1],
                              smt_setup_min=s1, fal_line=fal.id, fal_start=b[0], fal_finish=b[1], fal_setup_min=s2,
                              status="LATE_RISK" if late > 0 else "ON_TIME", late_days=round(late, 1), material_short=short,
                              reason_codes=reasons, material_cost_inr=round(mcost, 2), express_cost_inr=round(xcost, 2)))
        if frozen and (a[0], b[1]) != (prev.smt_start, prev.fal_finish):
            diags.append(Diagnostic(code="FROZEN_JOB_MOVED", severity="warn", order_id=o.order_id, detail="frozen job timing changed"))
        return c

    prev_of = {}
    for o, qty, forced, prev in jobs:
        frozen, fc = prev is not None, o.order_id in fc_ids
        if frozen:
            prev_of[o.order_id] = prev
        miss = ({"SMT", "FAL"} - segs[o.product_id])
        if miss or not bom[o.product_id]:
            code = "MISSING_ROUTING" if miss else "MISSING_BOM"
            unsched.append(Unscheduled(customer_order_id=o.order_id, product_id=o.product_id, qty=qty, priority=o.priority, reason=code,
                                       detail=f"no {'/'.join(sorted(miss))} routing" if miss else "no effective BOM"))
            diags.append(Diagnostic(code=code, severity="error", order_id=o.order_id, detail=f"{o.product_id} cannot be planned; nothing assumed"))
            continue
        best, covers = attempt(o, qty, forced, frozen, False)
        placed = commit(o, best, frozen, fc, prev, False) if best else 0
        first = len(out) - 1 if best else None
        left = qty - placed
        if left > 0 and pol.plan_purchases and not (frozen or fc or o.kitted) and (best or all(c <= 0 for c, _ in covers)):
            bought, _ = attempt(o, left, forced, False, True)       # MRP: order the missing parts today, make the rest when they arrive
            if bought:
                left -= commit(o, bought, False, False, None, placed > 0)
        if left <= 0:
            continue
        if fc:                                                       # forecast demand that doesn't fit: noted, never a risk
            diags.append(Diagnostic(code="FORECAST_NOT_PLANNED", severity="info", order_id=o.order_id, detail=f"{left} forecast units do not fit"))
        elif placed:
            lim = best[3]
            out[first].material_short = True
            out[first].reason_codes.append("PARTIAL_MATERIAL")
            unsched.append(Unscheduled(customer_order_id=o.order_id, product_id=o.product_id, qty=left, priority=o.priority,
                                       reason="MATERIAL_SHORT", detail=f"scheduled {placed}/{qty}; limiting {lim}"))
            diags.append(Diagnostic(code="MATERIAL_SHORT_PARTIAL", severity="warn", order_id=o.order_id, material_id=lim,
                                    detail=f"only {placed} of {qty} units covered; limiting {lim}"))
        elif covers and all(c <= 0 for c, _ in covers):
            lim = covers[0][1]
            unsched.append(Unscheduled(customer_order_id=o.order_id, product_id=o.product_id, qty=left, priority=o.priority,
                                       reason="MATERIAL_BLOCKED", detail=f"stock covers 0 units; limiting {lim}"))
            diags.append(Diagnostic(code="MATERIAL_BLOCKED", severity="error", order_id=o.order_id, material_id=lim,
                                    detail=f"{left} units cannot start: no stock of {lim}"))
        else:
            unsched.append(Unscheduled(customer_order_id=o.order_id, product_id=o.product_id, qty=left, priority=o.priority,
                                       reason="NO_CAPACITY_IN_HORIZON", detail=f"does not fit before {inp.horizon_end:%Y-%m-%d}"))
            diags.append(Diagnostic(code="NO_CAPACITY_IN_HORIZON", severity="error", order_id=o.order_id, detail=f"{left} units do not fit horizon"))
            if frozen:
                diags.append(Diagnostic(code="FROZEN_JOB_UNPLACEABLE", severity="error", order_id=o.order_id, detail="frozen job lost capacity (scenario)"))

    # job sequence per line (by start time)
    for kind, lf, sf in (("smt", "smt_line", "smt_start"), ("fal", "fal_line", "fal_start")):
        byl = defaultdict(list)
        for a in out:
            byl[getattr(a, lf)].append(a)
        for l in byl.values():
            for i, a in enumerate(sorted(l, key=lambda a: getattr(a, sf)), 1):
                setattr(a, kind + "_seq", i)

    # capacity table (line x ISO week) and utilisation
    cap = defaultdict(lambda: [0.0, 0.0])
    for l in lines.values():
        for s in l.slots:
            y, w, _ = s["start"].isocalendar()
            cap[(l.id, f"{y}-W{w:02d}")][0] += s["cap"]
            cap[(l.id, f"{y}-W{w:02d}")][1] += s["used"]
    capacity = [{"line_id": k[0], "week": k[1], "available_min": round(v[0]), "used_min": round(v[1]),
                 "utilisation": round(v[1] / v[0], 3) if v[0] else None} for k, v in sorted(cap.items())]
    tot = {l.id: sum(s["cap"] for s in l.slots) for l in lines.values()}
    util = {l.id: round(l.used / tot[l.id], 3) if tot[l.id] else 0.0 for l in lines.values()}

    # rough-cut capacity: each week's demand (orders due + forecast beyond them) against what final assembly can make that week
    rough = rough_cut(inp, lines, fc_ids, orders)
    for r in rough:
        if r["load_pct"] > 100:
            diags.append(Diagnostic(code="ROUGH_CUT_OVERLOAD", severity="warn",
                                    detail=f"week of {r['week']}: demand is {r['load_pct']}% of what final assembly can make ({r['demand']} of {r['capacity']} units)"))

    # metrics
    firm = [a for a in out if a.customer_order_id not in fc_ids]          # forecast work is not customer demand
    demand_qty = sum(o.qty for o in inp.orders)
    sched_qty = sum(a.qty for a in firm)
    on_time_qty = sum(a.qty for a in firm if a.status == "ON_TIME")
    price = {o.order_id: o.unit_price_inr or 0 for o in orders.values()}
    summary = {
        "open_orders": len(inp.orders), "scheduled_orders": len({a.customer_order_id for a in firm}), "unscheduled_entries": len(unsched),
        "demand_qty": demand_qty, "scheduled_qty": sched_qty, "unscheduled_qty": sum(u.qty for u in unsched), "po_covered_qty": po_covered,
        "late_jobs": sum(a.status == "LATE_RISK" for a in firm), "on_time_qty_pct": round(100 * on_time_qty / demand_qty, 1) if demand_qty else None,
        "material_short_jobs": sum(a.material_short for a in firm),
        "forecast_qty": sum(a.qty for a in out if a.customer_order_id in fc_ids),
        "purchase_wait_jobs": sum(any(r.startswith("WAITS_FOR_PURCHASE") for r in a.reason_codes) for a in firm),
        "changeovers": sum(l.changeovers for l in lines.values()), "changeover_min": round(sum(l.setup_min for l in lines.values())),
        "wip_unit_days_proxy": round(sum(a.qty * max(0, (a.fal_start - a.smt_finish).total_seconds()) / 86400 for a in out)),
        "material_cost_inr": round(sum(a.material_cost_inr or 0 for a in out)),
        "late_order_value_inr": round(sum(a.qty * price.get(a.customer_order_id, 0) for a in firm if a.status == "LATE_RISK")),
        "avg_utilisation": round(sum(util.values()) / len(util), 3) if util else None,
        "bottleneck_line": max(util, key=util.get) if util else None,
        "rough_cut": rough,
    }
    if not out and not unsched:
        feas = "NO_NEW_PRODUCTION_NEEDED"
    elif not out:
        feas = "NOTHING_SCHEDULABLE"
    elif unsched:
        feas = "INCOMPLETE"
    elif summary["late_jobs"]:
        feas = "COMPLETE_WITH_LATE_RISK"
    else:
        feas = "COMPLETE_ON_TIME"
    s = Schedule(id=uuid.uuid4().hex[:8], name=name, status="PENDING_APPROVAL", feasibility=feas, as_of=inp.as_of.isoformat(),
                 policy=pol.model_dump(), assignments=out, unscheduled=unsched, diagnostics=diags, capacity=capacity,
                 line_utilisation=util, summary=summary, constraints_not_enforced=NOT_ENFORCED)
    if previous:
        s.changes, stab = diff(previous, s)
        s.summary["stability_pct"] = stab
        # slushy zone: orders that started 2-6 weeks out in the previous plan and change now need their impact checked at approval
        start = {}
        for a in previous.assignments:
            start[a.customer_order_id] = min(start.get(a.customer_order_id, a.smt_start), a.smt_start)
        lo, hi = inp.as_of + timedelta(days=pol.frozen_days), inp.as_of + timedelta(days=pol.slushy_days)
        moved = [c for c in s.changes if c.customer_order_id in start and lo <= start[c.customer_order_id] < hi]
        if moved:
            s.diagnostics.append(Diagnostic(code="SLUSHY_ZONE_CHANGES", severity="warn",
                                            detail=f"{len(moved)} orders change in weeks {pol.frozen_days // 7 + 1}-{pol.slushy_days // 7}: check the material and capacity impact"))
        s.summary["slushy_changes"] = len(moved)
    return s


def rough_cut(inp: PlanningInput, lines: dict, fc_ids: set, orders: dict) -> list[dict]:
    """Per week (Monday): units due (customer orders + forecast/stock jobs) against final assembly's units that week (minutes x rate)."""
    def monday(d):
        return (d - timedelta(days=d.weekday())).isoformat()
    demand = defaultdict(int)
    for o in list(inp.orders) + [orders[i] for i in fc_ids]:
        demand[monday(max(o.due, inp.as_of.date()))] += o.qty
    cap = defaultdict(float)
    for l in lines.values():
        if l.kind == "FAL":
            for s in l.slots:
                cap[monday(s["start"].date())] += s["cap"] * l.rate
    weeks = sorted(w for w in cap if w in demand or cap[w])[:12]
    return [{"week": w, "demand": demand.get(w, 0), "capacity": round(cap[w]), "load_pct": round(100 * demand.get(w, 0) / cap[w], 1) if cap[w] else None}
            for w in weeks]


def diff(old: Schedule, new: Schedule):
    """Per-order schedule changes and stability = % of old orders unchanged (same plant, qty, SMT start)."""
    def key(s):
        d = {}
        for a in s.assignments:
            q, p, t = d.get(a.customer_order_id, (0, a.plant_id, a.smt_start))
            d[a.customer_order_id] = (q + a.qty, p, min(t, a.smt_start))
        return d
    o, n, ch, same = key(old), key(new), [], 0
    for k, (q, p, t) in o.items():
        if k not in n:
            ch.append(Change(customer_order_id=k, kind="DROPPED", detail="no longer scheduled"))
            continue
        q2, p2, t2 = n[k]
        if q2 != q:
            ch.append(Change(customer_order_id=k, kind="QTY_CHANGED", detail=f"{q} -> {q2}"))
        elif p2 != p:
            ch.append(Change(customer_order_id=k, kind="PLANT_CHANGED", detail=f"{p} -> {p2}"))
        elif t2 != t:
            ch.append(Change(customer_order_id=k, kind="RESCHEDULED", detail=f"start {t:%m-%d %H:%M} -> {t2:%m-%d %H:%M}"))
        else:
            same += 1
    ch += [Change(customer_order_id=k, kind="NEW", detail=f"{v[0]} units on {v[1]}") for k, v in n.items() if k not in o]
    return ch, round(100 * same / len(o), 1) if o else None


# ---------------------------------------------------------------- views
def dispatch_list(s: Schedule) -> list[dict]:
    """Per line, ordered jobs (SMT and FAL stages)."""
    rows = []
    for a in s.assignments:
        for st, line, seq, start, fin, setup in (("SMT", a.smt_line, a.smt_seq, a.smt_start, a.smt_finish, a.smt_setup_min),
                                                 ("FAL", a.fal_line, a.fal_seq, a.fal_start, a.fal_finish, a.fal_setup_min)):
            rows.append({"plant_id": a.plant_id, "line_id": line, "seq": seq, "stage": st, "customer_order_id": a.customer_order_id, "product_id": a.product_id,
                         "qty": a.qty, "start": start, "finish": fin, "setup_min": setup, "priority": a.priority, "urgent": a.urgent,
                         "frozen": a.frozen, "status": a.status, "reason_codes": a.reason_codes})
    return sorted(rows, key=lambda r: (r["line_id"], r["seq"]))


def material_calloff(inp: PlanningInput, s: Schedule) -> list[dict]:
    """Per material: requirement of ALL open demand vs usable stock (all plants), safety stock, and what the schedule consumes."""
    bom = defaultdict(list)
    for b in inp.bom:
        bom[b.product_id].append(b)
    need, cons = defaultdict(float), defaultdict(float)
    for o in inp.orders:
        for b in bom[o.product_id]:
            need[b.material_id] += o.qty * b.qty_per * (1 + b.scrap)
    bq = {(b.product_id, b.material_id): b for b in inp.bom}
    for a in s.assignments:
        for b in bom[a.product_id]:
            cons[b.material_id] += a.qty * b.qty_per * (1 + b.scrap)
    avail, safe, cost = defaultdict(float), defaultdict(float), {}
    for st in inp.stock:
        avail[st.material_id] += st.available
        safe[st.material_id] += st.safety
        cost[st.material_id] = st.unit_cost_inr
    rows = []
    for m in sorted(set(avail) | set(need)):
        short = max(0.0, need[m] - avail[m])
        rows.append({"material_id": m, "required_qty": round(need[m]), "available_qty": round(avail[m]), "scheduled_consumption": round(cons[m]),
                     "shortfall_qty": round(short), "safety_stock": round(safe[m]),
                     "shortfall_incl_safety_qty": round(max(0.0, need[m] + safe[m] - avail[m])),
                     "shortfall_cost_inr": round(short * cost[m]) if cost.get(m) is not None else None,
                     "in_bom": any(k[1] == m for k in bq)})
    return rows


def capacity_gaps(s: Schedule, inp: PlanningInput) -> dict:
    """Capacity by line/week, overloaded weeks, and minutes needed for orders that did not fit."""
    rate = {l.kind: l.rate_per_min for l in inp.lines}  # lines of a kind share one rate in this data
    gap = [u for u in s.unscheduled if u.reason == "NO_CAPACITY_IN_HORIZON"]
    return {"by_line_week": s.capacity,
            "overloaded_weeks": [c for c in s.capacity if c["utilisation"] and c["utilisation"] >= 0.95],
            "unfit_orders": [u.model_dump() for u in gap],
            "extra_minutes_needed": {k: round(sum(u.qty for u in gap) / r) for k, r in rate.items()},
            "late_orders": [{"customer_order_id": a.customer_order_id, "late_days": a.late_days, "qty": a.qty}
                            for a in sorted(s.assignments, key=lambda a: -a.late_days) if a.status == "LATE_RISK"][:50],
            "material_limited_qty": sum(u.qty for u in s.unscheduled if u.reason.startswith("MATERIAL"))}

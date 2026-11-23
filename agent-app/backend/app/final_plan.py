"""The final production plan for one plan, as one page: header and approvals, the key figures, then the twelve parts of a production
plan (strategy and targets, weekly MPS, daily schedule, MRP, machines and capacity, workforce and shifts, Gantt, maintenance, quality,
cost, risk and contingency, recommendations) and the final outcome. Everything is worked out from the plan and the plant's records;
a part that can't be worked out is left empty, never filled in by hand."""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import pandas as pd

from . import execution, overview, planning, planning_tools, signoff, store
from .config import Policy

PLANT = {"IN-TN-01": "Hosur", "IN-KA-01": "Bengaluru"}
STAGE = {"SMT": "Circuit boards", "FAL": "Final assembly"}
SHIFT = {"A": "Shift A (06:00–14:00)", "B": "Shift B (14:00–22:00)", "C": "Shift C (22:00–06:00)"}


def _safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception:
        return None


def _monday(d):
    return d - timedelta(days=d.weekday())


def _requested(sid: str) -> dict:
    """The customers' own dates for orders an agreed capacity decision moved (saved with the plan options); empty otherwise."""
    try:
        from .db import PlanSet, SessionLocal
        with SessionLocal() as db:
            for ps in db.query(PlanSet).order_by(PlanSet.created_at.desc()).limit(20):
                me = next((p for p in ps.plans or [] if p["schedule_id"] == sid), None)
                if me:
                    return {k: date.fromisoformat(v) for k, v in (me.get("requested_dates") or {}).items()}
    except Exception:
        pass
    return {}


def _outsourced(sid: str, inp, pol: Policy) -> list:
    """The orders this plan sends to a contract manufacturer. They left this plan's inputs, so they are read from the inputs of the
    penalty-first plan made from the same data (already loaded), else rebuilt."""
    want = set(pol.outsource_orders)
    try:
        from .db import PlanSet, SessionLocal
        with SessionLocal() as db:
            for ps in db.query(PlanSet).order_by(PlanSet.created_at.desc()).limit(20):
                me = next((p for p in ps.plans or [] if p["schedule_id"] == sid), None)
                if me:
                    if me.get("outsourced") is not None:                    # saved with the plan options: no rebuild needed
                        from types import SimpleNamespace
                        return [SimpleNamespace(order_id=o["id"], product_id=o["product_id"], qty=o["qty"], due=date.fromisoformat(o["due"]),
                                                unit_price_inr=o.get("price"), customer_id=o.get("customer"))
                                for o in me["outsourced"]]
                    base = next((p["schedule_id"] for p in ps.plans if p["key"] == "penalty"), None)
                    if base:
                        return [o for o in store.inputs(base).orders if o.order_id in want]
        from . import data
        full = data.to_inputs(store.D(), inp.as_of.isoformat(), pol.model_copy(update={"outsource_orders": [], "fill_shifts": False}))
        return [o for o in full.orders if o.order_id in want]
    except Exception:                                   # the chart still draws without the contract manufacturer rows
        return []


def _business(s, inp, pol: Policy, jobs, firm_orders, cost: dict) -> dict:
    """The plan in money and customers: order value, value delivered on time, value at risk, late-penalty exposure, estimated gross
    margin; value by delivery week (on time / late); customers ranked by value at risk; the most expensive late orders.
    On time = made on our lines by the date, or covered by production already under way, or delivered by the contract manufacturer."""
    rates = planning_tools._rates()
    pct = (next(iter(rates.values()))["late_penalty_pct_per_day"] if rates else 0.5) / 100
    end = inp.horizon_end or (inp.as_of + timedelta(days=60))
    by = defaultdict(list)
    for a in jobs:
        by[a.customer_order_id.split("#")[0]].append(a)
    blocked = defaultdict(int)
    for u in s.unscheduled:
        blocked[u.customer_order_id.split("#")[0]] += u.qty
    out_orders = _outsourced(s.id, inp, pol) if pol.outsource_orders else []
    out_ids = {o.order_id for o in out_orders}
    asked = _requested(s.id)                                              # the customer's own date where a decision moved it
    rows = []
    for o in list(firm_orders) + list(out_orders):
        price = getattr(o, "unit_price_inr", None) or 0
        due = asked.get(o.order_id, o.due)
        due_end = datetime.combine(due, datetime.min.time()) + timedelta(days=1)
        late_q, late_days_w, penalty = 0, 0.0, 0.0
        if o.order_id not in out_ids:
            for a in by.get(o.order_id, []):
                d = (a.fal_finish - due_end).total_seconds() / 86400
                if d > 0:
                    late_q += a.qty; late_days_w += d * a.qty; penalty += d * a.qty * price * pct
            if blocked.get(o.order_id):                                   # not made within the planning period
                q = min(blocked[o.order_id], o.qty)
                d = max(1.0, (end - due_end).total_seconds() / 86400)
                late_q += q; late_days_w += d * q; penalty += d * q * price * pct
        late_q = min(late_q, o.qty)
        rows.append({"order_id": o.order_id, "customer": getattr(o, "customer_id", None) or "–", "product_id": o.product_id, "due": due, "value": o.qty * price,
                     "late_value": late_q * price, "penalty": penalty, "days_late": round(late_days_w / late_q, 1) if late_q else 0.0,
                     "outsourced": o.order_id in out_ids})
    total = sum(r["value"] for r in rows)
    late = sum(r["late_value"] for r in rows)
    pen = sum(r["penalty"] for r in rows)
    parts = (cost or {}).get("parts") or {}
    production = sum(v for k, v in parts.items() if k != "late_penalty")
    outsourcing = sum(r["value"] for r in rows if r["outsourced"]) * pol.outsource_price_pct / 100
    margin = total - production - outsourcing - pen if parts else None
    # by delivery week (the plan's first day onward; earlier = overdue)
    g0 = inp.as_of.date()
    weeks = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        k = max(-1, min(8, (r["due"] - g0).days // 7))
        weeks[k][0] += r["value"] - r["late_value"]; weeks[k][1] += r["late_value"]
    week_rows = [{"label": "Overdue" if k < 0 else "Later" if k == 8 else f"W{k + 1}",
                  "sub": "" if k < 0 or k == 8 else (g0 + timedelta(days=7 * k)).strftime("%d %b"), "on_time": round(v[0]), "late": round(v[1])}
                 for k, v in sorted(weeks.items())]
    cust = defaultdict(lambda: {"value": 0.0, "late_value": 0.0, "penalty": 0.0, "late_orders": 0, "orders": 0})
    for r in rows:
        c = cust[r["customer"]]
        c["value"] += r["value"]; c["late_value"] += r["late_value"]; c["penalty"] += r["penalty"]; c["orders"] += 1
        c["late_orders"] += 1 if r["late_value"] else 0
    customers = sorted(({"customer": k, **{kk: round(vv) if isinstance(vv, float) else vv for kk, vv in v.items()}} for k, v in cust.items()),
                       key=lambda c: -(c["late_value"] + c["penalty"]))
    worst = sorted((r for r in rows if r["late_value"]), key=lambda r: -r["penalty"])[:12]
    return {"order_value": round(total), "on_time_value": round(total - late), "on_time_value_pct": round(100 * (total - late) / total, 1) if total else None,
            "at_risk_value": round(late), "penalty": round(pen), "margin": round(margin) if margin is not None else None,
            "margin_pct": round(100 * margin / total, 1) if margin is not None and total else None, "outsourcing": round(outsourcing),
            "weeks": week_rows, "customers": customers,
            "late_orders": [{**{k: v for k, v in r.items() if k not in ("due", "outsourced")}, "due": r["due"].isoformat(),
                             "value": round(r["value"]), "late_value": round(r["late_value"]), "penalty": round(r["penalty"])} for r in worst]}


def _ready(s, inp, pol: Policy, jobs, firm_orders) -> dict:
    """Units ready by the date each customer asked for: made on our lines by the due date, covered by production already under way,
    or delivered by the contract manufacturer on the date."""
    asked = _requested(s.id)                                              # the customer's own date where a decision moved it
    due = {o.order_id: datetime.combine(asked.get(o.order_id, o.due), datetime.min.time()) + timedelta(days=1) for o in firm_orders}
    ordered = sum(o.qty for o in firm_orders)
    on_lines = sum(a.qty for a in jobs if a.customer_order_id.split("#")[0] in due and a.fal_finish <= due[a.customer_order_id.split("#")[0]])
    covered = s.summary.get("po_covered_qty") or 0
    outsourced = sum(o.qty for o in (_outsourced(s.id, inp, pol) if pol.outsource_orders else []))
    ready = min(ordered + outsourced, on_lines + covered + outsourced)
    total = ordered + outsourced                                       # outsourced orders left this plan's inputs: count them back in
    return {"ordered_units": total, "ready_units": ready, "ready_pct": round(100 * ready / total, 1) if total else None}


def _new_orders() -> set[str]:
    """The orders of the latest purchase-order upload (highlighted in the Gantt chart)."""
    from .db import AgentJob, SessionLocal
    try:
        with SessionLocal() as db:
            j = db.query(AgentJob).filter_by(kind="upload").order_by(AgentJob.created_at.desc()).first()
            return {i.split("#")[0] for i in ((j.payload or {}).get("ids") or [])} if j else set()
    except Exception:                                   # the chart still draws without the highlight
        return set()


def build(sid: str) -> dict:
    s, inp = store.get(sid), store.inputs(sid)
    if s is None:
        raise KeyError(sid)
    pol, m, d = Policy(**s.policy), s.summary, store.D()
    t0 = inp.as_of
    end = max((a.fal_finish for a in s.assignments), default=t0)
    jobs = s.assignments
    firm_orders = [o for o in inp.orders if o.status not in ("PRODUCTION_ORDER", "FORECAST")]
    products = sorted({o.product_id for o in inp.orders} | {a.product_id for a in jobs})

    # header and approvals
    st = _safe(signoff.stages, s) or {"stages": [], "fully_approved": False}
    pm = next((x for x in st["stages"] if x["stage"] == "production_manager"), None) if st["stages"] else None
    approved_by = [f"{x['label']}: {x['by']}" for x in st["stages"] if x.get("done")]
    badge = "fully_approved" if st.get("fully_approved") else "approved" if s.status in ("APPROVED", "PUBLISHED") else "pending" if s.status == "PENDING_APPROVAL" else "rejected"

    # yields: good units out of all made, per line, from the production records (the plan's speeds already allow for them)
    ly = {k.split("|")[0]: v for k, v in inp.yields.items() if k.endswith("|*")}
    fal_yield = round(100 * sum(v for k, v in ly.items() if k.startswith("FAL")) / max(1, sum(1 for k in ly if k.startswith("FAL"))), 1) if ly else None
    planned = sum(a.qty for a in jobs)
    otd = (_safe(overview.heatmap, sid=sid) or {}).get("on_time_pct")

    # 1. strategy and targets, per product
    dem = defaultdict(int)
    prio = defaultdict(Counter)
    for o in firm_orders:
        dem[o.product_id] += o.qty
        prio[o.product_id][o.priority] += o.qty
    plan_by, fc_by = defaultdict(int), defaultdict(int)
    for a in jobs:
        (fc_by if a.customer_order_id.startswith("FC-") else plan_by)[a.product_id] += a.qty
    strategy = [{"product_id": p, "demand": dem[p], "planned": plan_by[p] + fc_by[p], "for_stock": fc_by[p],
                 "mix_pct": round(100 * (plan_by[p] + fc_by[p]) / planned, 1) if planned else 0,
                 "priority": prio[p].most_common(1)[0][0] if prio[p] else "–",
                 "strategy": "Make to order + stock" if fc_by[p] else "Make to order"} for p in products]

    # 2. master production schedule: units finishing per product per week (first 5 weeks, then later)
    wk0 = _monday(t0.date())
    weeks = [(wk0 + timedelta(days=7 * i)).isoformat() for i in range(5)]
    mps = defaultdict(lambda: defaultdict(int))
    for a in jobs:
        w = _monday(a.fal_finish.date()).isoformat()
        mps[a.product_id][w if w in weeks else "later"] += a.qty
    mps_rows = [{"product_id": p, **{w: mps[p].get(w, 0) for w in weeks}, "later": mps[p].get("later", 0), "total": sum(mps[p].values())} for p in products]

    # 3. detailed schedule, first working day (a sample: the first jobs to start)
    rows = sorted(planning.dispatch_list(s), key=lambda r: r["start"])
    day1 = rows[0]["start"].date() if rows else t0.date()
    daily = [{"order_id": r["customer_order_id"], "product_id": r["product_id"], "line_id": r["line_id"], "stage": STAGE.get(r["stage"], r["stage"]),
              "plant": PLANT.get(r["plant_id"], r["plant_id"]), "start": r["start"], "finish": r["finish"], "qty": r["qty"], "priority": r["priority"]}
             for r in rows if r["start"].date() == day1][:10]

    # 4. MRP: what the plan needs, what stock has, what to buy and when it can arrive
    waits = Counter(r.split(":", 1)[1] for a in jobs for r in a.reason_codes if r.startswith("WAITS_FOR_PURCHASE:"))
    mrp = []
    for r in planning.material_calloff(inp, s):
        if r["required_qty"] <= 0:
            continue
        lead = inp.lead_days.get(r["material_id"], pol.purchase_default_lead_days)
        short = r["shortfall_qty"]
        mrp.append({"material_id": r["material_id"], "required": r["required_qty"], "available": r["available_qty"], "shortage": short,
                    "lead_days": lead, "arrives": (t0 + timedelta(days=lead)).date().isoformat(), "jobs_waiting": waits.get(r["material_id"], 0),
                    "action": f"Procure {short:,} (order today)" if short > 0 else "No action"})
    mrp.sort(key=lambda r: -r["shortage"])

    # 5. machines and capacity: hours each line has and needs over the weeks with work
    used_weeks = sorted({c["week"] for c in s.capacity if c["used_min"]})
    cap = defaultdict(lambda: [0.0, 0.0])
    for c in s.capacity:
        if c["week"] in used_weeks:
            cap[c["line_id"]][0] += c["available_min"]
            cap[c["line_id"]][1] += c["used_min"]
    plant_of = {l.line_id: l.plant_id for l in inp.lines}
    machines = []
    for ln, (av, us) in sorted(cap.items()):
        u = round(100 * us / av, 1) if av else None
        machines.append({"line_id": ln, "stage": STAGE.get(ln[:3], ln[:3]), "plant": PLANT.get(plant_of.get(ln), plant_of.get(ln)), "available_h": round(av / 60),
                         "required_h": round(us / 60), "utilisation": u,
                         "status": "Overloaded" if u and u > 100 else "High load" if u and u >= 90 else "Available"})
    mtype = d["dim_machine"].groupby("line_id").machine_type.apply(lambda x: ", ".join(sorted(set(x)))).to_dict()
    for r in machines:
        r["machines"] = mtype.get(r["line_id"], "")

    # 6. workforce and shifts (crew plan for the first two weeks)
    crews = _safe(planning_tools.crews, 14, s, inp) or {}
    by = defaultdict(lambda: defaultdict(lambda: [0, 0, 0, 0]))           # shift -> date -> [needed, available, gap, overtime h]
    for r in crews.get("rows", []):
        x = by[r["shift"]][r["date"]]
        x[0] += r["needed"]; x[1] += r["available"]; x[2] += r["gap"]; x[3] += r["overtime_h"]
    cr = d.get("gen_crew")
    size = {(r.plant_id, r.shift_id): (int(r.crew_size_smt), int(r.crew_size_fal), int(r.headcount)) for r in cr.itertuples()} if cr is not None else {}
    workforce = []
    for sh in ("A", "B", "C"):
        days = by.get(sh, {})
        n = max(1, len(days))
        alloc = " · ".join(f"{PLANT[p]}: circuit boards {v[0]}, final assembly {v[1]}" for (p, s_), v in sorted(size.items()) if s_ == sh)
        workforce.append({"shift": SHIFT[sh], "needed": round(sum(v[0] for v in days.values()) / n), "available": round(sum(v[1] for v in days.values()) / n),
                          "short_days": sum(1 for v in days.values() if v[2] > 0), "overtime_h": sum(v[3] for v in days.values()), "allocation": alloc})

    # 7. Gantt: one row per line (the machine view): its runs back to back, each a product (jobs of the same product in a row merged);
    # idle stretches of a day or more shown with their reason; runs carrying orders from the latest upload marked new.
    g0 = t0.date()                                         # the chart starts the day the plan starts (not the Monday before it)
    w_start = datetime.combine(g0, datetime.min.time())
    n_weeks = max(4, min(10, -(-int((end - w_start).total_seconds() // 86400 + 1) // 7)))
    w_end = w_start + timedelta(days=7 * n_weeks)
    new_ids = _new_orders()
    pct = lambda t: max(0.0, min(100.0, 100 * (t - w_start).total_seconds() / (w_end - w_start).total_seconds()))
    bar = lambda a0, a1: {"left": round(pct(a0), 2), "width": round(max(0.4, pct(a1) - pct(a0)), 2)}
    per_line = defaultdict(list)
    for a in jobs:
        oid = a.customer_order_id.split("#")[0]
        wait = next((r.split(":")[1] for r in a.reason_codes if r.startswith("WAITS_FOR_PURCHASE:")), None)
        for ln, a0, a1 in ((a.smt_line, a.smt_start, a.smt_finish), (a.fal_line, a.fal_start, a.fal_finish)):
            if a1 > w_start and a0 < w_end:
                per_line[(a.plant_id, ln)].append((a0, a1, a.product_id, a.qty, oid in new_ids, oid, wait))
    gantt, idle_note = [], {}
    for (plant, ln), js in per_line.items():
        runs, idle = [], []
        for a0, a1, p, q, new, oid, wait in sorted(js):
            if runs and runs[-1]["product_id"] == p and (a0 - runs[-1]["_e"]).total_seconds() <= 3600:
                r = runs[-1]
                r["_e"], r["units"], r["new"] = max(r["_e"], a1), r["units"] + q, r["new"] or new
                r["_o"].add(oid)
                continue
            if runs and (a0 - runs[-1]["_e"]).total_seconds() >= 86400:          # a day or more with nothing on the line: a maintenance window
                why = (f"waiting for {wait} ({inp.lead_days.get(wait, pol.purchase_default_lead_days)}-day supplier lead time)" if wait
                       else "no work planned")
                nxt = next((w for w in sorted(inp.maintenance, key=lambda w: w["start"]) if w["line_id"] == ln and w["start"] >= runs[-1]["_e"]), None)
                idle.append({"start": runs[-1]["_e"], "finish": a0, "why": why, "days": round((a0 - runs[-1]["_e"]).total_seconds() / 86400, 1),
                             "next_service": nxt and {"start": nxt["start"], "hours": round(nxt["minutes"] / 60, 1)}, **bar(runs[-1]["_e"], a0)})
                if wait:
                    idle_note[wait] = max(idle_note.get(wait, 0), idle[-1]["days"])
            runs.append({"product_id": p, "_s": a0, "_e": a1, "units": q, "new": new, "_o": {oid}})
        services = [{"start": w["start"], "hours": round(w["minutes"] / 60, 1), **bar(w["start"], w["end"])}
                    for w in inp.maintenance if w["line_id"] == ln and w["end"] > w_start and w["start"] < w_end]
        gantt.append({"plant_id": plant, "plant": PLANT.get(plant, plant), "line_id": ln, "stage": STAGE.get(ln[:3], ln[:3]),
                      "units": sum(r["units"] for r in runs), "new": any(r["new"] for r in runs), "idle": idle, "services": services,
                      "runs": [{"product_id": r["product_id"], "start": r["_s"], "finish": r["_e"], "units": r["units"], "new": r["new"],
                                "orders": len(r["_o"]), **bar(r["_s"], r["_e"])} for r in runs]})
    gantt.sort(key=lambda r: (r["plant_id"], r["line_id"][:3] != "SMT", r["line_id"]))
    # orders a contract manufacturer makes: one row per product; each order from when it goes out (its lead time before the due date)
    # to its due date, overlapping orders merged into one bar
    if pol.outsource_orders:
        per_p = defaultdict(lambda: defaultdict(lambda: [0, 0, 0, 0]))                  # product -> delivery week -> units, orders, new orders, new units
        for o in _outsourced(sid, inp, pol):
            k = (o.due - g0).days // 7
            x = per_p[o.product_id][k]
            x[0] += o.qty; x[1] += 1
            if o.order_id in new_ids:
                x[2] += 1; x[3] += o.qty
        for p, by_week in sorted(per_p.items()):
            runs = []
            for k, (q, n, nn, nq) in sorted(by_week.items()):
                a0 = max(w_start, w_start + timedelta(days=7 * k) + timedelta(hours=6))
                a1 = min(w_end, w_start + timedelta(days=7 * k + 7) - timedelta(hours=6))
                if a1 <= a0:
                    continue
                runs.append({"product_id": p, "start": a0, "finish": a1, "units": q, "orders": n, "new": nn > 0, "new_orders": nn, "new_units": nq,
                             **bar(a0, a1)})
            if runs:
                gantt.append({"plant_id": "ZZ-EXT", "plant": "Contract manufacturer", "line_id": "Outsourced", "stage": f"{p} (made outside)",
                              "external": True, "units": sum(r["units"] for r in runs), "new": any(r["new"] for r in runs), "idle": [], "services": [],
                              "runs": runs})
    idle_txt = [f"{m} ({inp.lead_days.get(m, pol.purchase_default_lead_days)}-day supplier lead time)" for m in idle_note]
    # where each new order is in this plan: on the lines, covered by production already under way, outsourced, or not schedulable
    on_lines = {a.customer_order_id.split("#")[0] for a in jobs} & new_ids
    blocked = {u.customer_order_id.split("#")[0] for u in s.unscheduled} & new_ids
    outsourced = set(pol.outsource_orders or []) & new_ids
    in_plan = {o.order_id for o in inp.orders} | outsourced
    new_where = {"total": len(new_ids), "on_lines": len(on_lines), "outsourced": len(outsourced), "not_scheduled": len(blocked - on_lines),
                 "covered": len((new_ids & in_plan) - on_lines - blocked - outsourced), "not_in_plan": len(new_ids - in_plan)}
    gweeks = [{"label": f"W{i + 1}", "from": (g0 + timedelta(days=7 * i)).isoformat(), "to": (g0 + timedelta(days=7 * i + 6)).isoformat()}
              for i in range(n_weeks)]

    # 8. maintenance: services planned in the horizon (they block line time)
    services = sorted(inp.maintenance, key=lambda w: w["start"])
    maintenance = {"count": len(services), "hours": round(sum(w["minutes"] for w in services) / 60, 1),
                   "items": [{"line_id": w["line_id"], "machines": ", ".join(w.get("machine_ids") or [])[:80], "start": w["start"], "hours": round(w["minutes"] / 60, 1)}
                             for w in services[:6]]}

    # 9. quality: recorded yields, rejects and rework (last 30 days), inspection stations
    hp = d["fact_hourly_production"]
    last = pd.Timestamp(hp.event_date.max())
    q30 = hp[pd.to_datetime(hp.event_date) > last - pd.Timedelta(days=30)]
    made = float(q30.actual_qty.sum()) or 1.0
    reject_pct = round(100 * float(q30.reject_qty.sum()) / made, 2)
    rework_pct = round(100 * float(q30.rework_qty.sum()) / made, 2) if "rework_qty" in q30 else None
    inspect = d["dim_machine"][d["dim_machine"].machine_type.str.contains("AOI|ICT|SPI|Test|Inspection", case=False, na=False)]
    quality = {"fal_yield_pct": fal_yield, "line_yield": {k: round(100 * v, 1) for k, v in sorted(ly.items())}, "reject_pct": reject_pct, "rework_pct": rework_pct,
               "expected_rejects": round(planned * reject_pct / 100), "good_target": planned,
               "inspection": sorted(Counter(inspect.machine_type).items())}

    # 10. cost
    cost = _safe(planning_tools.cost, sid) or {"available": False}

    # 11. risks and 12. recommendations, worked out from the plan
    short = [r for r in mrp if r["shortage"] > 0]
    rough = [r for r in (m.get("rough_cut") or []) if (r.get("load_pct") or 0) > 100]
    over = [r for r in machines if r["status"] != "Available"]
    crew_short = sum(w["short_days"] for w in workforce)
    risks, recs = [], []
    if short:
        risks.append({"risk": "Material shortage", "impact": f"{sum(r['jobs_waiting'] for r in short)} jobs wait for {len(short)} bought materials",
                      "mitigation": "Order today; check the other plant's stock for a transfer; keep safety stock"})
        top = ", ".join(f"{r['shortage']:,} × {r['material_id']}" for r in short[:3])
        recs.append(f"Procure {len(short)} short materials now (largest: {top}) so jobs can start on their planned dates.")
    if rough:
        risks.append({"risk": "Demand above capacity", "impact": f"{len(rough)} weeks ask for more than final assembly can make",
                      "mitigation": "Agree the volume plan: commit achievable dates or work through breaks"})
        recs.append(f"Agree the volume plan for the {len(rough)} overloaded weeks before the plan is released.")
    if over:
        hi = max(over, key=lambda r: r["utilisation"] or 0)
        risks.append({"risk": "Bottleneck line", "impact": f"{hi['line_id']} at {hi['utilisation']}% of its hours",
                      "mitigation": "Balance work across plants; group products to cut changeovers"})
        recs.append(f"Relieve {hi['line_id']} ({hi['utilisation']}% used): move suitable jobs to a line with spare hours or add time.")
    if m.get("late_jobs"):
        risks.append({"risk": "Late deliveries", "impact": f"{m['late_jobs']} jobs finish after their date", "mitigation": "Expedite high-priority orders; tell customers committed dates"})
        recs.append(f"Review the {m['late_jobs']} late-risk jobs and confirm committed dates with customers.")
    if crew_short:
        ot = sum(w["overtime_h"] for w in workforce)
        risks.append({"risk": "Labour shortage", "impact": f"{crew_short} shift-days short of people", "mitigation": f"Overtime ({ot:,} h) or reassign certified operators"})
        recs.append(f"Cover {crew_short} short shift-days ({ot:,} overtime hours) or move certified people between shifts.")
    if services:
        risks.append({"risk": "Planned maintenance", "impact": f"{maintenance['hours']} line hours blocked", "mitigation": "Services placed in quieter shifts; spares checked before each window"})
    if reject_pct:
        risks.append({"risk": "Rejects", "impact": f"{reject_pct}% rejected (last 30 days), about {quality['expected_rejects']:,} units", "mitigation": "Plan with recorded yields; inspect at AOI/ICT"})
        recs.append(f"Keep rejects at or below {reject_pct}% so good output meets the {planned:,}-unit plan.")
    for n in (_safe(execution.notes, sid) or []):
        if n["kind"] == "risk" and n["status"] == "OPEN":
            risks.append({"risk": n["text"][:60], "impact": f"Logged · {n['severity']}", "mitigation": f"Owner: {n['owner'] or 'not set'}"})
    if otd is not None:
        recs.append(f"Hold on-time delivery at {otd}% or better; watch the delivery heatmap daily.")

    lines_used = sorted({a.smt_line for a in jobs} | {a.fal_line for a in jobs})
    outcome = {
        "what": " · ".join(f"{p}: {plan_by[p] + fc_by[p]:,}" for p in products if plan_by[p] + fc_by[p]),
        "when": f"{t0:%d %b} – {end:%d %b %Y}, week by week as in the MPS",
        "where": ", ".join(f"{PLANT.get(p, p)} ({', '.join(l for l in lines_used if plant_of.get(l) == p)})" for p in sorted({a.plant_id for a in jobs})),
        "resources": f"{len(short)} materials to buy · about {round(sum(w['needed'] for w in workforce) / 3) if workforce else '–'} people a shift · lines {round(100 * (m.get('avg_utilisation') or 0))}% used",
        "cost": cost.get("total_inr"), "delivery": otd,
    }
    return {
        "schedule_id": s.id, "name": s.name, "status": s.status, "badge": badge, "period": {"from": t0.date().isoformat(), "to": end.date().isoformat()},
        "prepared_by": s.created_by or "planning agent", "approved_by": approved_by, "manager": pm and pm.get("by"),
        "kpis": {"target": sum(dem.values()), "planned": planned, "fal_yield_pct": fal_yield, "utilisation_pct": round(100 * (m.get("avg_utilisation") or 0)),
                 "bottleneck": m.get("bottleneck_line"), "manpower_per_shift": round(sum(w["needed"] for w in workforce) / 3) if workforce else None,
                 "shifts": 3, "cost_inr": cost.get("total_inr"), "otd_pct": otd, **_ready(s, inp, pol, jobs, firm_orders),
                 "manpower_available": round(sum(w["available"] for w in workforce) / 3) if workforce else None},
        "strategy": strategy, "mps": {"weeks": weeks, "rows": mps_rows}, "daily": {"day": day1.isoformat(), "rows": daily},
        "mrp": mrp[:10], "mrp_count": len(mrp), "machines": machines, "workforce": workforce,
        "business": _safe(_business, s, inp, pol, jobs, firm_orders, cost),
        "gantt": {"weeks": gweeks, "rows": gantt, "new_orders": len(new_ids), "new_where": new_where, "idle_parts": idle_txt}, "maintenance": maintenance, "quality": quality, "cost": cost,
        "risks": risks, "recommendations": recs, "outcome": outcome,
    }

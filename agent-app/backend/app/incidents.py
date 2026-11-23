"""Planning agent, part 2: recovery from problems on the floor.

Triggers (all through the orchestrator)
  - detected: the floor agent finds a breakdown, a stopped line or late production orders.
  - reported: a plant head, production manager or planner reports one, or a maintenance/MES system posts it to the same API.
  - fixed: when someone records the fix and the problem had changed the plan, the agent re-plans with the line back in service.
The orchestrator passes the inventory agent's view of the materials the affected work uses; it shows up in the findings.

For each problem the agent
  1. Measures the impact against the plan everyone is working from (which orders and customers get later, by how much).
  2. Looks up how this was handled before: fixes people recorded for similar problems, plus what the production feed shows about
     past breakdowns on that line. It never invents a fix that nobody recorded.
  3. Tries recovery options on the planning engine: wait for the repair, move work to the other plant, split big orders across
     both plants, extend shifts, put the affected customers first, or all of these together.
  4. For orders that still miss their date: what can ship on the due date (partial delivery) and the earliest realistic new date.
  5. Saves the best option as a plan waiting for approval and tells the production manager (decide) and the plant heads (fix the
     problem, prepare the recovery plan once approved).
The agent never approves, changes orders or dates, or contacts customers; it says what people should decide and do.
"""
import json
import logging
import math
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
import pandas as pd
from . import data, planning, planning_agent as agent, scenarios, store
from .config import Policy
from .db import AgentRun, AgentTask, Incident, SessionLocal, TeamMember, _now
from .domain import Order
from .scenarios import ExtraTime, MachineLoss, Scenario

log = logging.getLogger("planner.agent.incidents")
ACTOR = agent.ACTOR
EFFORT = ["wait", "move", "split", "expedite", "overtime", "all"]   # when two options are as good, the one asking less of people wins


class NotFound(LookupError):
    pass


def _cid(oid: str) -> str:                       # split lots ("CO-1#2") belong to their order
    return oid.split("#")[0]


def _base():
    """The plan everyone is working from: the newest approved/published plan, else the newest plan (not a what-if). Makes one if none."""
    rows = [r for r in store.list_(50) if not store.is_what_if(r)]          # what-ifs added to the plan count as plans
    pick = next((r for r in rows if r["status"] in ("APPROVED", "PUBLISHED")), rows[0] if rows else None)
    if pick:
        return store.get(pick["id"]), store.inputs(pick["id"])
    pol = Policy()
    inp = data.to_inputs(store.D(), None, pol)
    s = planning.plan(inp, pol, name="Agent plan")
    store.put(s, actor=ACTOR, role="agent", inp=inp)
    return s, inp


def _late(s) -> dict:
    """order -> (late units, latest finish, unit-days late) for jobs at risk of missing their date."""
    out = {}
    for a in s.assignments:
        if a.status == "LATE_RISK":
            q, f, ud = out.get(_cid(a.customer_order_id), (0, a.fal_finish, 0.0))
            out[_cid(a.customer_order_id)] = (q + a.qty, max(f, a.fal_finish), ud + a.qty * a.late_days)
    return out


def _finish(s) -> dict:
    out = {}
    for a in s.assignments:
        k = _cid(a.customer_order_id)
        out[k] = max(out.get(k, a.fal_finish), a.fal_finish)
    return out


def _delays(s, ref) -> dict:
    """order -> hours later than in the reference plan (only orders that got later)."""
    f, r = _finish(s), _finish(ref)
    return {k: (f[k] - r[k]).total_seconds() / 3600 for k in f if k in r and f[k] > r[k] + timedelta(minutes=30)}


def _score(s, ref, affected) -> dict:
    late, dl = _late(s), _delays(s, ref)
    units = sum(q for q, _, _ in late.values())
    ud = sum(u for _, _, u in late.values())
    au = sum(late[k][0] for k in affected if k in late)
    qty = defaultdict(int)
    for a in s.assignments:
        qty[_cid(a.customer_order_id)] += a.qty
    # what the option is judged on: lateness of the orders the problem hits, plus any delay it pushes onto other orders
    # (helping unrelated orders earns nothing, so a broad lever like overtime only wins when it helps the affected orders)
    judged = sum(late[k][2] for k in affected if k in late) + sum(qty[k] * h / 24 for k, h in dl.items() if k not in affected)
    return {"judged_unit_days": round(judged),"late_orders": len(late), "late_units": units, "avg_days_late": round(ud / units, 1) if units else 0.0, "late_unit_days": round(ud),
            "affected_late": sum(k in late for k in affected),
            "affected_days_late": round(sum(late[k][2] for k in affected if k in late) / au, 1) if au else 0.0,
            "unscheduled_qty": s.summary["unscheduled_qty"], "on_time_qty_pct": s.summary["on_time_qty_pct"],
            "late_value_inr": s.summary["late_order_value_inr"], "moved_orders": sum(c.kind == "PLANT_CHANGED" for c in s.changes),
            "orders_delayed": len(dl), "avg_delay_h": round(sum(dl.values()) / len(dl), 1) if dl else 0.0}


# ------------------------------------------------------------------ what it is: impact, history, options
def _window(inc, inp):
    start = max(inc.started_at, inp.as_of)
    return start, max(start, inc.expected_end or start)


def _remaining() -> pd.DataFrame:
    """Open production orders with the units they still owe (data.open_po)."""
    return data.open_po(store.D())


def _po_rows(inc) -> pd.DataFrame:
    po = _remaining()
    return po[po.production_order_id.isin(inc.production_orders)]


def _floor() -> list[Order]:
    """The work on the floor: what open production orders still owe, in their plant. The plan counts these orders as supply and does
    not put them on the lines; a breakdown hits exactly this work, so the analysis does."""
    po = _remaining()
    return [Order(order_id=r.production_order_id, product_id=r.product_id, qty=int(r.remaining), due=date.fromisoformat(str(r.due_date)[:10]),
                  priority=r.priority, status="PRODUCTION_ORDER", customer_id=r.customer_order_id, plant_id=r.plant_id)
            for r in po.itertuples() if r.remaining > 0]


def _options(inc, ref, inp, floor, pol) -> tuple[list, set]:
    start, end = _window(inc, inp)
    loss = [MachineLoss(line_id=inc.line_id, start=start, end=end)] if inc.kind == "MACHINE_DOWN" and end > start else []
    common = {"machine_losses": loss, "floor_work": floor}
    if inc.kind == "MACHINE_DOWN":
        affected = {_cid(a.customer_order_id) for a in ref.assignments if inc.line_id in (a.smt_line, a.fal_line) and a.fal_finish > start}
    else:
        affected = set(inc.production_orders) & {o.order_id for o in floor}
    rel = sorted(affected)
    big = sorted(o.order_id for o in inp.orders + floor if o.order_id in affected and o.qty >= pol.agent_split_min_qty)
    ot = [ExtraTime(start=inp.as_of, end=inp.as_of + timedelta(days=pol.agent_overtime_days), minutes_per_shift=pol.agent_overtime_min)]
    keep = {_cid(a.customer_order_id): a.plant_id for a in ref.assignments}
    ot_txt = f"extend every shift by {pol.agent_overtime_min} minutes for {pol.agent_overtime_days} days"
    opts = [("wait", "Wait for the repair" if inc.kind == "MACHINE_DOWN" else "Leave it as it is",
             "Keep every order in the plant it was planned in; work resumes when the line is back" if inc.kind == "MACHINE_DOWN"
             else "Keep the plan; these production orders stay where they are", [], Scenario(name="", keep_plant=keep, **common)),
            ("move", "Move work to the other plant", "Affected orders go to whichever plant finishes them sooner",
             ["Move the kitted material with orders that change plant"], Scenario(name="", release_plant=rel, **common)),
            ("split", "Split big orders across both plants", f"Affected orders of {pol.agent_split_min_qty:,}+ units become two lots, one per plant, made at the same time",
             ["Send half the kitted material to the other plant", "Ship split orders when both lots are done"],
             Scenario(name="", split_orders=big, release_plant=rel, **common)),
            ("expedite", "Put the affected orders first", "They take the first free line time in their plant; other orders may move back",
             ["Tell the customers of orders that move back"], Scenario(name="", expedite=rel, **common)),
            ("overtime", "Move work and extend shifts", f"Affected orders may change plant, and {ot_txt}",
             ["Arrange overtime with the shift leads", "Move kitted material with orders that change plant"],
             Scenario(name="", release_plant=rel, extra_time=ot, **common)),
            ("all", "All of these together", f"Split big orders, put affected orders first, move work and {ot_txt}",
             ["Arrange overtime", "Move kitted material", "Tell customers of orders that move back"],
             Scenario(name="", split_orders=big, expedite=rel, release_plant=rel, extra_time=ot, **common))]
    if not big:
        opts = [o for o in opts if o[0] != "split"]
    if not affected:
        opts = [o for o in opts if o[0] == "wait"]
    return opts, affected


def _history(inc, pol) -> dict:
    """How this was handled before: fixes people recorded, and what the production feed shows."""
    with SessionLocal() as s:
        past = s.query(Incident).filter(Incident.status == "RESOLVED", Incident.kind == inc.kind, Incident.id != inc.id) \
                .order_by(Incident.resolved_at.desc()).limit(100).all()
    rank = lambda p: (0 if inc.machine_id and p.machine_id == inc.machine_id else 1 if p.line_id and p.line_id == inc.line_id
                      else 2 if p.plant_id == inc.plant_id else 3)
    recorded = [{"when": p.started_at, "where": " · ".join(filter(None, [p.plant_id, p.line_id, p.machine_id])), "what": p.description,
                 "match": ["same machine", "same line", "same plant", "elsewhere"][rank(p)], "option": (p.analysis or {}).get("option_names", {}).get(p.option),
                 "repair_hours": round((p.expected_end - p.started_at).total_seconds() / 3600, 1) if p.expected_end else None,
                 "fix": p.resolution, "by": p.resolved_by} for p in sorted(past, key=rank)[:5]]
    d = store.D()
    feed = {}
    if inc.kind == "MACHINE_DOWN" and inc.line_id:
        h = d["fact_hourly_production"]
        h = h[h.line_id == inc.line_id]
        f = h[h.downtime_reason == "EQUIPMENT_FAULT"].groupby("event_date").downtime_min.sum()
        bad = f[f >= pol.agent_fault_min]
        day = h.groupby("event_date")[["actual_qty", "target_qty"]].sum()
        nxt = [day.loc[x] for x in (str((pd.Timestamp(b) + pd.Timedelta(days=1)).date()) for b in bad.index) if x in day.index]
        feed = {"days": int(h.event_date.nunique()), "breakdown_days": int(len(bad)), "typical_min": int(bad.median()) if len(bad) else None,
                "worst_min": int(bad.max()) if len(bad) else None, "last": bad.index.max() if len(bad) else None,
                "next_day_output_pct": round(100 * sum(r.actual_qty for r in nxt) / sum(r.target_qty for r in nxt), 1) if nxt else None}
    elif inc.kind == "PO_DELAY":
        po = d["fact_production_order"]
        done = po[(po.plant_id == inc.plant_id) & po.order_status.isin(["CLOSED", "COMPLETED"])]
        over = (pd.to_datetime(done.planned_finish).dt.normalize() - pd.to_datetime(done.due_date)).dt.days
        feed = {"finished_orders": int(len(done)), "late_finished": int((over > 0).sum()), "typical_days_late": int(over[over > 0].median()) if (over > 0).any() else None}
    return {"recorded": recorded, "feed": feed}


def _still_late(s, ref, orders, affected) -> tuple[list, list]:
    """Orders that miss their date under the chosen option: what is ready on the due date, the earliest new date, what to do."""
    b = _late(ref)
    rows = defaultdict(lambda: {"qty": 0, "ready_by_due": 0, "finish": None})
    for a in s.assignments:
        r = rows[_cid(a.customer_order_id)]
        r["qty"] += a.qty
        r["finish"] = max(filter(None, [r["finish"], a.fal_finish]))
        due_end = datetime.fromisoformat(a.due) + timedelta(days=1)
        span = (a.fal_finish - a.fal_start).total_seconds()
        frac = 1.0 if a.fal_finish <= due_end else max(0.0, min(1.0, (due_end - a.fal_start).total_seconds() / span)) if span else 0.0
        r["ready_by_due"] += math.floor(a.qty * frac)
    late = []
    for k, r in rows.items():
        x = orders.get(k)
        if not x or r["ready_by_due"] >= r["qty"] or (k not in affected and k in b):
            continue                                  # on time, or late anyway and not touched by this problem
        new = (r["finish"] + timedelta(days=1)).date()
        part = r["ready_by_due"]
        act = (f"Ship {part:,} on {x.due:%d %b}, the other {r['qty'] - part:,} by {new:%d %b}" if part
               else f"Agree a new date with the customer: {new:%d %b}")
        late.append({"order_id": k, "production_order": x.kitted, "customer_id": x.customer_id, "priority": x.priority, "qty": r["qty"],
                     "due": x.due.isoformat(), "ready_by_due": part, "new_date": new.isoformat(), "days_late": (new - x.due).days,
                     "action": act, "was_late": k in b})
    rank = Policy().priority_rank
    late.sort(key=lambda r: (r["was_late"], rank.get(r["priority"], 9), -r["days_late"]))
    cust = defaultdict(lambda: {"orders": 0, "units": 0, "on_time_units": 0, "late_orders": 0})
    for k in affected:
        x = orders.get(k)
        if x and k in rows:
            c = cust[x.customer_id or "unknown"]
            c["orders"] += 1
            c["units"] += rows[k]["qty"]
            c["on_time_units"] += rows[k]["ready_by_due"]
    for r in late:
        if r["order_id"] in affected:
            cust[r["customer_id"] or "unknown"]["late_orders"] += 1
    return late[:40], sorted(({"customer_id": k, **v} for k, v in cust.items()), key=lambda c: -c["units"])


def analyse(inc) -> tuple[dict, dict]:
    """Returns (analysis for people, internals to save the chosen plan)."""
    pol = Policy()
    base, inp = _base()
    floor = _floor()
    ref = planning.plan(scenarios.apply(inp, Scenario(name="floor", floor_work=floor)), pol)      # the plan with floor work, without the problem
    start, end = _window(inc, inp)
    opts, affected = _options(inc, ref, inp, floor, pol)
    runs, out = {}, []
    for key, name, what, needs, sc in opts:
        sc.name = f"Recovery · {name}"
        x = scenarios.apply(inp, sc)
        s = planning.plan(x, pol, name=sc.name)                 # no frozen window: a breakdown is when frozen work has to move
        s.changes, s.summary["stability_pct"] = planning.diff(ref, s)
        runs[key] = (s, sc, x)
        out.append({"key": key, "name": name, "what": what, "needs": needs, **_score(s, ref, affected)})
    cost = lambda r: r["judged_unit_days"] + r["unscheduled_qty"] * pol.horizon_days   # not made within the horizon = late by the whole horizon
    wait, lo = out[0], min(cost(r) for r in out)
    tol = max(0.05 * (cost(wait) - lo), 0.01 * cost(wait)) + 1   # within 5% of the best gain, or 1% of the delay, counts as equally good ...
    best = next(r for r in sorted(out, key=lambda r: EFFORT.index(r["key"])) if cost(r) <= lo + tol)   # ... and the least effort wins
    for r in out:
        r["recommended"] = r is best
    rec_s, _, rec_x = runs[best["key"]]
    orders = {o.order_id: o for o in rec_x.orders}
    orders.update({_cid(o.order_id): o for o in rec_x.orders if "#" in o.order_id})
    late, customers = _still_late(rec_s, ref, orders, affected)
    ref_cost = cost(_score(ref, ref, affected))
    lost_min = sum(sl.minutes for l in inp.lines if l.line_id == inc.line_id for sl in l.slots if sl.start < end and sl.end > start) if inc.kind == "MACHINE_DOWN" else 0
    rate = next((l.rate_per_min for l in inp.lines if l.line_id == inc.line_id), 0)
    ref_late = _late(ref)
    aff_late = [k for k in affected if k in ref_late]
    impact = {"base_plan": base.id, "base_name": base.name, "window": [start, end], "lost_minutes": round(lost_min), "lost_units": round(lost_min * rate),
              "affected_orders": len(affected), "orders_delayed": wait["orders_delayed"], "avg_delay_h": wait["avg_delay_h"],
              "late_before": len(aff_late), "customers": len({orders[k].customer_id for k in affected if k in orders}),
              "floor_orders": len(floor), "floor_units": sum(o.qty for o in floor)}
    findings = []
    if floor:
        findings.append(f"Open production orders still owe {impact['floor_units']:,} units. The agent puts that work on the lines "
                        "(its material is already issued) to see what the problem really delays.")
    if inc.kind == "MACHINE_DOWN":
        if end <= start:
            findings.append("The stop is over before the plan starts, so no planned work is lost.")
        if inc.end_assumed:
            findings.append(f"Nobody has given a repair time yet; the agent assumed {pol.agent_default_outage_h:g} hours. The plant head should confirm it.")
        findings.append(f"Waiting for the repair delays {wait['orders_delayed']} order{'s' if wait['orders_delayed'] != 1 else ''} by about "
                        f"{wait['avg_delay_h']:g} hours on average." if wait["orders_delayed"] else "No order gets later: the work fits around the stop.")
    elif aff_late:
        findings.append(f"{len(aff_late)} of these production orders finish after their due date, {wait['affected_days_late']:g} days late on average.")
    if best["key"] != "wait":
        if inc.kind == "MACHINE_DOWN" and cost(wait) > ref_cost:
            findings.append(f"“{best['name']}” takes back {min(100, 100 * (cost(wait) - cost(best)) / (cost(wait) - ref_cost)):.0f}% of the delay the stop adds: "
                            f"{best['orders_delayed']} order{'s' if best['orders_delayed'] != 1 else ''} about {best['avg_delay_h']:g} hours later instead of "
                            f"{wait['orders_delayed']} about {wait['avg_delay_h']:g} hours later.")
        else:
            findings.append(f"“{best['name']}” brings these orders in {wait['affected_days_late'] - best['affected_days_late']:.1f} days sooner "
                            f"(from {wait['affected_days_late']:g} to {best['affected_days_late']:g} days late on average).")
    elif cost(wait) > lo:
        findings.append("The recovery options gain too little over waiting to be worth the effort, so the agent does not propose a change.")
    past_due = sum(1 for k in affected if k in orders and orders[k].due < inp.as_of.date())
    if past_due:
        findings.append(f"{past_due} of the affected orders were already past their due date before this problem; options are compared on how late they end up.")
    mat = sum(u.qty for u in ref.unscheduled if u.reason.startswith("MATERIAL"))
    if mat:
        findings.append(f"Material, not line time, holds back {mat:,} units of customer orders; no recovery option changes that.")
    if late:
        findings.append(f"{len(late)} order{'s' if len(late) != 1 else ''} still miss their date: ship what is ready on the due date and agree new dates (list below).")
    analysis = {"impact": impact, "history": _history(inc, pol), "options": out, "recommended": best["key"], "findings": findings,
                "still_late": late, "customers": customers[:20], "option_names": {r["key"]: r["name"] for r in out}}
    return json.loads(json.dumps(analysis, default=str)), {"base": base, "inp": inp, "runs": runs}     # stored as JSON


# ------------------------------------------------------------------ raise, choose, fix
def _team():
    with SessionLocal() as s:
        team = s.query(TeamMember).all()
    return next((t.name for t in team if t.role == "production_manager"), None), {t.plant_id: t.name for t in team if t.role == "plant_head"}


def _headline(inc) -> str:
    if inc.kind == "MACHINE_DOWN":
        what = inc.machine_id or inc.line_id
        return f"{what} stopped" + (f" on {inc.line_id}" if inc.machine_id else "") + f" at {inc.plant_id}"
    n = len(inc.production_orders)
    return f"{n} production order{'s' if n != 1 else ''} at {inc.plant_id} running late"


def _propose(inc, an, internals, key) -> list[AgentTask]:
    """Save option `key` as a plan waiting for approval and hand it over. Returns the new tasks (not yet added to the session)."""
    pm, head = _team()
    hl = _headline(inc)
    opt = next(o for o in an["options"] if o["key"] == key)
    sched, sc, sinp = internals["runs"][key]
    pid = None
    if key != "wait":
        sched.name = f"Recovery · {hl} · {opt['name']}"
        sc.name = sched.name
        store.put(sched, base_id=internals["base"].id, scenario=sc, actor=ACTOR, role="agent")
        pid = sched.id
    inc.proposal_id, inc.option = pid, key
    facts = {"incident_id": inc.id, "headline": hl, "option": opt["name"], "affected_orders": an["impact"]["affected_orders"],
             "orders_delayed": opt["orders_delayed"], "avg_delay_h": opt["avg_delay_h"], "still_late": len(an["still_late"]),
             "customers": an["impact"]["customers"], "findings": an["findings"][:3]}
    tasks = [AgentTask(run_id=None, schedule_id=pid, role="production_manager", assignee=pm, kind="approve" if pid else "review", status="OPEN", details=facts,
                       title=f"{hl}: approve the recovery plan ({opt['name'].lower()})" if pid else f"{hl}: review the agent's analysis")]
    if pid:
        for pk in agent._packages(sched, sinp):
            if pk["jobs"]:
                tasks.append(AgentTask(run_id=None, schedule_id=pid, role="plant_head", plant_id=pk["plant_id"], assignee=head.get(pk["plant_id"]),
                                       kind="prepare", status="WAITING_APPROVAL", details={**pk, "incident_id": inc.id},
                                       title=f"Recovery plan for {pk['plant_id']}: {pk['jobs']} job{'s' if pk['jobs'] != 1 else ''}, {pk['units']:,} units"))
    return tasks


def _with_context(an: dict, context: dict | None) -> None:
    """Add what the inventory agent says about the materials the affected work uses."""
    if not context:
        return
    an["context"] = json.loads(json.dumps(context, default=str))
    low = (context.get("inventory") or {}).get("low_cover", [])
    if low:
        names = ", ".join(f"{m['material_id']} ({m['cover_days']:g} d)" for m in low[:4]) + (f" and {len(low) - 4} more" if len(low) > 4 else "")
        moving = next((o for o in an["options"] if o["recommended"] and o["key"] in ("move", "split", "overtime", "all")), None)
        an["findings"].append(f"Inventory agent: materials these orders use are running low: {names}."
                              + (" Check stock at the receiving plant before moving work." if moving else ""))


def raise_(kind: str, *, plant_id: str, started_at: datetime, description: str, source: str, actor: str, key: str, line_id: str | None = None,
           machine_id: str | None = None, production_orders: list | None = None, expected_end: datetime | None = None,
           context: dict | None = None) -> dict:
    """The planning agent's recovery job. `context` is what the orchestrator gathered from the other agents."""
    pol = Policy()
    assumed = kind == "MACHINE_DOWN" and expected_end is None
    with agent._lock:
        inc = Incident(id=str(uuid.uuid4()), kind=kind, key=key, source=source, plant_id=plant_id, line_id=line_id, machine_id=machine_id,
                       production_orders=production_orders or [], started_at=started_at,
                       expected_end=expected_end or (started_at + timedelta(hours=pol.agent_default_outage_h) if kind == "MACHINE_DOWN" else None),
                       end_assumed=assumed, description=description, reported_by=actor)
        an, internals = analyse(inc)
        _with_context(an, context)
        inc.analysis = an
        tasks = _propose(inc, an, internals, an["recommended"])          # saves the proposal first (its own connection)
        pm, head = _team()
        with SessionLocal() as s:
            s.add(inc)
            fix = (f"Get {inc.machine_id or inc.line_id} running again, confirm the repair time and record the fix" if kind == "MACHINE_DOWN"
                   else f"Recover {', '.join(inc.production_orders[:3])}{'…' if len(inc.production_orders) > 3 else ''}: confirm the new finish and record what you did")
            tasks.append(AgentTask(run_id=None, role="plant_head", plant_id=plant_id, assignee=head.get(plant_id), kind="repair", status="OPEN",
                                   details={"incident_id": inc.id, "headline": _headline(inc)}, title=fix))
            run = AgentRun(trigger=kind.lower(), outcome="INCIDENT", reason=f"{_headline(inc)}. {' '.join(an['findings'][:2])}", schedule_id=inc.proposal_id,
                           details={"incident_id": inc.id}, open_orders=[], actor=actor)
            s.add(run)
            s.flush()
            for t in tasks:
                t.run_id = run.id
            s.add_all(tasks)
            s.commit()
    store.audit(inc.proposal_id, ACTOR, "agent", "incident_analysed", {"incident": inc.id, "kind": kind, "recommended": an["recommended"], "source": source})
    log.info("incident %s %s recommended=%s", kind, key, an["recommended"])
    return get(inc.id)


def choose(iid: str, key: str, actor: str) -> dict:
    """A person picks a different recovery option: the agent saves that one for approval instead and withdraws the old proposal's tasks."""
    with agent._lock:
        with SessionLocal() as s:
            inc = s.get(Incident, iid)
        if not inc:
            raise NotFound(iid)
        if inc.status != "OPEN":
            raise ValueError("This problem is already fixed")
        an, internals = analyse(inc)
        _with_context(an, (inc.analysis or {}).get("context"))
        if key not in {o["key"] for o in an["options"]}:
            raise ValueError(f"Unknown option '{key}'")
        old = inc.proposal_id
        if old and getattr(store.get(old), "status", "") != "PENDING_APPROVAL":
            raise ValueError("The recovery plan has already been decided on")
        if old:
            store.set_status(old, "PENDING_APPROVAL", "REJECTED", actor, "Replaced by another recovery option")
        inc.analysis = an
        tasks = _propose(inc, an, internals, key)
        with SessionLocal() as s:
            for t in s.query(AgentTask).filter(AgentTask.status.in_(["OPEN", "WAITING_APPROVAL", "ACKNOWLEDGED"])).all():
                if (t.details or {}).get("incident_id") == iid and t.kind != "repair":
                    t.status, t.note, t.updated_by = "CANCELLED", f"{actor} chose another recovery option", actor
            s.merge(inc)
            run = AgentRun(trigger="option_chosen", outcome="INCIDENT", reason=f"{_headline(inc)}: {actor} chose “{an['option_names'][key]}”",
                           schedule_id=inc.proposal_id, details={"incident_id": iid}, open_orders=[], actor=actor)
            s.add(run)
            s.flush()
            for t in tasks:
                t.run_id = run.id
            s.add_all(tasks)
            s.commit()
    store.audit(inc.proposal_id, actor, "", "recovery_option_chosen", {"incident": iid, "option": key})
    return get(iid)


def resolve(iid: str, fix: str, actor: str, repair_hours: float | None = None) -> dict:
    """Someone fixed it. The fix is kept for next time. If the problem had changed the plan, the agent re-plans with it gone."""
    with SessionLocal() as s:
        inc = s.get(Incident, iid)
        if not inc:
            raise NotFound(iid)
        if inc.status != "OPEN":
            raise ValueError("This problem is already marked as fixed")
        inc.status, inc.resolution, inc.resolved_by, inc.resolved_at = "RESOLVED", fix.strip(), actor, _now()
        if repair_hours is not None:
            inc.expected_end, inc.end_assumed = inc.started_at + timedelta(hours=repair_hours), False
        for t in s.query(AgentTask).filter(AgentTask.status.in_(["OPEN", "ACKNOWLEDGED"])).all():
            if (t.details or {}).get("incident_id") == iid and t.kind in ("repair", "review"):
                t.status, t.note, t.updated_by = "DONE", f"Fixed: {fix.strip()[:300]}", actor
        replan = inc.kind == "MACHINE_DOWN" and bool(inc.proposal_id)
        s.commit()
    store.audit(inc.proposal_id, actor, "", "incident_resolved", {"incident": iid, "fix": fix})
    out = get(iid)
    if replan:                                    # the recovery measures assumed the line was down; plan again with it back
        out["replan"] = agent.replan("repaired", f"{_headline(inc)} is fixed: re-planned with the line back in service", actor)
    return out


def get(iid: str) -> dict:
    with SessionLocal() as s:
        i = s.get(Incident, iid)
        if not i:
            raise NotFound(iid)
        return _out(i)


def list_(status: str | None = None, limit: int = 50, light: bool = False) -> list[dict]:
    """Problems, newest first. light=True leaves out what comes from the stored analysis (every recovery option and its plans, a
    large record): the agents only need what, where and when, and reading the analyses dominated an upload's time."""
    from sqlalchemy.orm import defer
    with SessionLocal() as s:
        if light:                                                     # only the option names from the analysis
            q = s.query(Incident, Incident.analysis["option_names"].label("names")).options(defer(Incident.analysis))
        else:
            q = s.query(Incident)
        if status:
            q = q.filter(Incident.status == status)
        rows = q.order_by(Incident.created_at.desc()).limit(limit)
        return [_light(i, names) for i, names in rows] if light else [_out(i, brief=True) for i in rows]


def _light(i, names=None) -> dict:
    return {"option": i.option, "option_name": (names or {}).get(i.option) if isinstance(names, dict) else None, "id": i.id, "kind": i.kind, "source": i.source, "status": i.status, "headline": _headline(i), "plant_id": i.plant_id, "line_id": i.line_id,
            "machine_id": i.machine_id, "production_orders": i.production_orders, "started_at": i.started_at, "expected_end": i.expected_end,
            "created_at": i.created_at}


def _out(i, brief=False) -> dict:
    an = i.analysis or {}
    o = {"id": i.id, "kind": i.kind, "source": i.source, "status": i.status, "headline": _headline(i), "plant_id": i.plant_id, "line_id": i.line_id,
         "machine_id": i.machine_id, "production_orders": i.production_orders, "started_at": i.started_at, "expected_end": i.expected_end,
         "end_assumed": i.end_assumed, "description": i.description, "reported_by": i.reported_by, "proposal_id": i.proposal_id, "option": i.option,
         "option_name": an.get("option_names", {}).get(i.option), "resolution": i.resolution, "resolved_by": i.resolved_by, "resolved_at": i.resolved_at,
         "created_at": i.created_at, "affected_orders": an.get("impact", {}).get("affected_orders"), "still_late": len(an.get("still_late", []))}
    if not brief:
        o["analysis"] = an
    return o

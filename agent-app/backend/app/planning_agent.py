"""Planning agent: makes the production plan when the orchestrator asks it to, using what the other agents found.

The orchestrator passes it a reason (demand changed, a machine is back, a planner asked) and context gathered by the other agents
(floor: open problems and alerts; inventory: materials at risk). It then
  1. Plans  - runs the deterministic planning engine (same as "Generate plan"); the plan is a PENDING_APPROVAL proposal.
  2. Organises - splits the plan per plant: jobs, units, lines, start/finish, late jobs, materials short for that plant's products.
  3. Assigns - production manager: review and approve (with the floor and inventory notes). Each plant head: prepare their plant
     (actionable once approved, withdrawn if rejected). Named people come from the team list; nothing is invented.
Recovery from problems on the floor is the same agent's other job (incidents.py).
Guardrails: it never approves, rejects, releases, cancels orders or changes master data; a person decides.
"""
import logging
import threading
from collections import defaultdict
from . import coord, order_watcher as watcher, planning, store
from .config import Policy
from .db import AgentTask, SessionLocal, TeamMember

log = logging.getLogger("planner.agent.planning")
ACTOR = "planning agent"
_lock = coord.Mutex("plan-change")            # one plan change at a time, across every process (orchestrator rounds, intake, manual runs, recoveries)


def plan(trigger: str, why: str, actor: str, context: dict | None = None) -> dict:
    """Make a plan now for `why`, with what the other agents found (`context`)."""
    with _lock:
        pol = Policy()
        return _plan(trigger, why, watcher.pending(pol=pol), pol, actor, context or {})


def run_now(actor: str, context: dict | None = None) -> dict:
    """A planner asked for a plan now, whatever the thresholds say."""
    p = watcher.pending()
    return plan("manual", f"Requested by {actor}" + (f"; {p['new_orders']} new orders since the last plan" if p["baseline"] else ""), actor, context)


def replan(trigger: str, why: str, actor: str) -> dict:
    """Plan again for a reason found elsewhere (e.g. a machine is back in service)."""
    from . import orchestrator                                       # the orchestrator gathers the floor and inventory context
    return plan(trigger, why, actor, orchestrator.context())


def _plan(trigger, why, p, pol, actor, context) -> dict:
    """Four complete plans from the same data (plan_sets.py); the agent hands over its pick and the production manager chooses one."""
    from . import plan_sets
    name = f"{p['new_orders']} new orders" if p["baseline"] else "agent plan"
    ps = plan_sets.make(p["_inp"], pol, trigger, why, actor, name)
    pick = next(x for x in ps["plans"] if x["best"])
    sched = store.get(pick["schedule_id"])
    run = _hand_over(sched, store.inputs(sched.id), trigger, why, p, actor, context, ps)
    with SessionLocal() as s:
        from .db import PlanSet
        s.get(PlanSet, ps["id"]).run_id = run["id"]
        s.commit()
    run["plan_set_id"] = ps["id"]
    return run


def adopt(sid: str, actor: str, role: str) -> dict:
    """Add a what-if's change to the plan. The agent re-plans every open order with that change on the latest working plan, saves the
    result as the plan waiting for approval, and hands the work over as for any plan it makes. People still decide: nothing is
    approved or released here."""
    from . import incidents, scenarios
    rows, _ = store._run("SELECT base_id, scenario_json FROM schedules WHERE id=?", (sid,), "one")
    if not rows:
        raise KeyError(sid)
    if not rows[0]["base_id"] or not rows[0]["scenario_json"]:
        raise ValueError("Only a what-if can be added to the plan")
    if sid in store.adopted_what_ifs():
        raise ValueError("This change is already in the plan")
    sc = scenarios.Scenario.model_validate_json(rows[0]["scenario_json"])
    with _lock:
        base, inp = incidents._base()
        pol = Policy(**base.policy)
        sched = scenarios.run(inp, base, sc, pol)
        sched.name = f"Agent plan · with change: {sc.name}"[:200]
        store.put(sched, base_id=base.id, scenario=sc, actor=ACTOR, role="agent")
        store.audit(sched.id, actor, role, "scenario_adopted", {"what_if": sid, "change": sc.name, "base": base.id})
        why = f"{actor} added a what-if to the plan: {sc.name}"
        run = _hand_over(sched, scenarios.apply(inp, sc), "what-if", why, watcher.pending(pol=pol), actor, {})
    return {"schedule_id": sched.id, "name": sched.name, "base_id": base.id, "comparison": scenarios.compare(base, [sched]),
            "tasks": len(run.get("tasks", []))}


def _hand_over(sched, inp, trigger, why, p, actor, context, plan_set: dict | None = None) -> dict:
    """Record the run and hand the plan over: the production manager approves it, each plant head prepares their plant."""
    packages = _packages(sched, inp)
    with SessionLocal() as s:
        team = s.query(TeamMember).all()
        pm = next((t.name for t in team if t.role == "production_manager"), None)
        head = {t.plant_id: t.name for t in team if t.role == "plant_head"}
    notes = _notes(context, packages)
    summary = {"feasibility": sched.feasibility, "scheduled_qty": sched.summary.get("scheduled_qty"), "late_jobs": sched.summary.get("late_jobs"),
               "unscheduled_qty": sched.summary.get("unscheduled_qty"), "po_covered_qty": sched.summary.get("po_covered_qty"),
               "new_orders": p["new_orders"], "new_units": p["new_units"], "urgent": p["urgent"], "by_product": p["by_product"], "plants": packages,
               "notes": notes}
    if plan_set:
        summary["plan_set_id"] = plan_set["id"]
        summary["options"] = [{"title": x["title"], "schedule_id": x["schedule_id"], "best": x["best"], **{k: x["metrics"][k] for k in
                               ("on_time_pct", "late_orders", "redated_orders", "overtime_h", "total_cost_inr")}} for x in plan_set["plans"]]
    run = watcher.record(trigger, "PLANNED", why, p, actor, sched.id, summary)
    approvable = bool(sched.assignments)
    tasks = [AgentTask(run_id=run["id"], schedule_id=sched.id, role="production_manager", assignee=pm, kind="approve" if approvable else "review",
                       title=(f"Choose one of the {len(plan_set['plans'])} plans (agent's pick: {sched.name.split(' · ')[0]})" if plan_set and approvable
                              else f"Review and approve “{sched.name}”" if approvable else f"Review “{sched.name}”: no new production needed"),
                       details={"why": why, **{k: v for k, v in summary.items() if k != "plants"},
                                "plants": [{k: pk[k] for k in ("plant_id", "jobs", "units", "late_jobs", "short_materials")} for pk in packages]},
                       status="OPEN")]
    for pk in packages:
        tasks.append(AgentTask(run_id=run["id"], schedule_id=sched.id, role="plant_head", plant_id=pk["plant_id"], assignee=head.get(pk["plant_id"]),
                               kind="prepare", title=(f"Prepare plant {pk['plant_id']}: {pk['jobs']} job{'s' if pk['jobs'] != 1 else ''}, {pk['units']:,} units" if pk["jobs"]
                                                      else f"Plant {pk['plant_id']}: no new jobs in this plan"),
                               details={**pk, "notes": [n for n in notes if n.get("plant_id") in (None, pk["plant_id"])]},
                               status="WAITING_APPROVAL" if approvable and pk["jobs"] else "OPEN"))
    with SessionLocal() as s:
        s.add_all(tasks)
        s.commit()
        run["tasks"] = [_task_out(t) for t in tasks]
    store.audit(sched.id, ACTOR, "agent", "agent_planned", {"trigger": trigger, "why": why, "tasks": len(tasks), "notes": len(notes)})
    return run


def _notes(context: dict, packages: list[dict]) -> list[dict]:
    """What the floor and inventory agents found that matters for this plan, in words for the people approving and preparing it."""
    notes = []
    for i in context.get("floor", {}).get("open_problems", []):
        notes.append({"from": "floor", "plant_id": i.get("plant_id"), "text": f"Open problem: {i['headline']}" + (f" (recovery: {i['option_name'].lower()})" if i.get("option_name") else "")})
    for a in context.get("floor", {}).get("alerts", [])[:5]:
        notes.append({"from": "floor", "plant_id": a.get("plant_id"), "text": a["title"]})
    used = {m["material_id"] for pk in packages for m in pk.get("short_materials", [])}
    inv = context.get("inventory", {})
    for m in inv.get("low_cover", [])[:8]:
        notes.append({"from": "inventory", "plant_id": None, "text": f"{m['material_id']} covers {m['cover_days']:g} days of production"
                      + (" and is short for this plan" if m["material_id"] in used else "")})
    for m in inv.get("below_safety", [])[:5]:
        notes.append({"from": "inventory", "plant_id": None, "text": f"{m['material_id']} is below safety stock ({m['free']:,} free, safety {m['safety']:,})"})
    return notes


def _packages(sched, inp) -> list[dict]:
    """Per plant: what the plant head needs to prepare."""
    rows = planning.dispatch_list(sched)
    callof = {r["material_id"]: r for r in planning.material_calloff(inp, sched) if r.get("shortfall_qty", 0) > 0}
    bom = defaultdict(set)
    for b in inp.bom:
        bom[b.product_id].add(b.material_id)
    plants = sorted({ln.plant_id for ln in inp.lines} | {r["plant_id"] for r in rows})
    out = []
    for p in plants:
        pr = [r for r in rows if r["plant_id"] == p]
        fal = [r for r in pr if r["stage"] == "FAL"]
        products = sorted({r["product_id"] for r in pr})
        mats = sorted({m for prod in products for m in bom[prod] if m in callof})
        lines = defaultdict(lambda: {"jobs": 0, "start": None, "finish": None})
        for r in pr:
            L = lines[r["line_id"]]
            L["jobs"] += 1
            L["start"] = min(filter(None, [L["start"], str(r["start"])]))
            L["finish"] = max(filter(None, [L["finish"], str(r["finish"])]))
        out.append({"plant_id": p, "jobs": len({r["customer_order_id"] for r in fal}), "units": int(sum(r["qty"] for r in fal)),
                    "late_jobs": len({r["customer_order_id"] for r in fal if r["status"] == "LATE_RISK"}),
                    "first_start": min((str(r["start"]) for r in pr), default=None), "last_finish": max((str(r["finish"]) for r in pr), default=None),
                    "products": products, "lines": [{"line_id": k, **v} for k, v in sorted(lines.items())],
                    "short_materials": [{"material_id": m, "shortfall_qty": int(callof[m]["shortfall_qty"])} for m in mats][:12],
                    "orders": sorted({r["customer_order_id"] for r in fal})[:50]})
    return out


def on_decision(schedule_id: str, status: str, user: str) -> None:
    """A person approved or rejected a plan: close the manager's task, release or withdraw the plant heads' tasks."""
    with SessionLocal() as s:
        for t in s.query(AgentTask).filter_by(schedule_id=schedule_id).all():
            if t.role == "production_manager" and t.status in ("OPEN", "ACKNOWLEDGED"):
                t.status, t.note, t.updated_by = "DONE", f"Plan {status.lower()} by {user}", user
            elif t.role == "plant_head" and t.status == "WAITING_APPROVAL":
                t.status, t.updated_by = ("OPEN" if status == "APPROVED" else "CANCELLED"), user
                t.note = f"Plan approved by {user}: approve it for your plant on Review & approve, then start preparing" if status == "APPROVED" else f"Plan rejected by {user}: nothing to prepare"
        s.commit()


_run_out = watcher.run_out


def _task_out(t: AgentTask) -> dict:
    return {"id": t.id, "run_id": t.run_id, "schedule_id": t.schedule_id, "role": t.role, "plant_id": t.plant_id, "assignee": t.assignee, "kind": t.kind,
            "title": t.title, "details": t.details, "status": t.status, "note": t.note, "updated_by": t.updated_by, "created_at": t.created_at,
            "updated_at": t.updated_at}

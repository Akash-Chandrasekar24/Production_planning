"""Orchestrator: the main agent. It runs the team, gathers what the sub-agents find, and decides who acts.

Sub-agents
  floor agent      - machines and floor operations: breakdowns, stopped lines, output, scrap, material stops, maintenance, late
                     production orders (floor_agent.py)
  inventory agent  - materials and supplies: days of cover, safety stock, shortfalls for open orders, expiring stock
                     (inventory_agent.py)
  order watcher    - the order book: tells when enough new demand arrived to re-plan (order_watcher.py)
  sourcing agent   - production orders blocked for parts in their plant: can the other plant make them, or send the parts?
                     Suggests a move only when material, line time, its own orders, lines, crew, yield and cost all allow it
                     (sourcing_agent.py)
  planning agent   - plans production with everything the others found: new plans and recovery plans (planning_agent.py,
                     incidents.py)

A round (every Policy.agent_interval_min minutes; also on demand, after order intake and when someone reports a problem):
  1. Ask the floor agent, the inventory agent and the order watcher what they see. Each finding is a signal with a severity.
  2. Record each new signal as an event (a signal with a key is raised only once).
  3. Route, most severe first:
       breakdown, stopped line, late production orders, reported problems -> planning agent: recovery plan, with the inventory
                                                                              agent's view of the materials those orders use
       demand changed                                                       -> planning agent: new plan, with the floor's open
                                                                              problems and the inventory agent's materials at risk
       everything else                                                      -> an alert task for the people who own it
  4. Record what happened to each event (which plan, recovery or task it led to).
Guardrails: no agent approves, releases, cancels or changes orders, dates, stock or master data. People decide.
"""
import json
import logging
from datetime import datetime
from . import coord, floor_agent, incidents, inventory_agent, order_watcher, planning_agent, sourcing_agent, store
from .config import Policy
from .db import AgentEvent, AgentLook, AgentRun, AgentTask, Incident, SessionLocal, TeamMember, _now

log = logging.getLogger("planner.agent.orchestrator")
NAME = "orchestrator"
RANK = {"critical": 0, "warn": 1, "info": 2}
RECOVER = {"floor.breakdown", "floor.line_stopped", "floor.po_late", "floor.reported"}
AGENTS = [("floor", "Floor agent", "Watches machines and floor operations and raises the alarm"),
          ("inventory", "Inventory agent", "Looks after materials and supplies"),
          ("orders", "Order watcher", "Watches the order book for new demand"),
          ("sourcing", "Sourcing agent", "Checks whether work blocked for parts in one plant can be made in the other"),
          ("planning", "Planning agent", "Plans production with what the others found")]
_round = coord.Mutex("orchestrator-round")    # one round at a time, across processes


def _seen(key: str | None) -> bool:
    if not key:
        return False
    with SessionLocal() as s:
        return s.query(AgentEvent.id).filter_by(key=key).first() is not None


def _note(agent: str, trigger: str, summary: str | None = None, signals: int = 0, error: str | None = None, details: dict | None = None):
    """Keep what the agent saw (in the database: the team's status survives restarts and keeps a history)."""
    with SessionLocal() as s:
        s.add(AgentLook(agent=agent, trigger=trigger[:20], summary=summary, signals=signals, error=error,
                        details=json.loads(json.dumps(details or {}, default=str))))
        s.commit()


def _look(agent: str, fn, trigger: str = "auto"):
    try:
        r = fn()
        sig = r.get("signals", []) + ([r["signal"]] if r.get("signal") else [])
        _note(agent, trigger, r.get("summary"), len(sig), details={"signals": [{"type": x["type"], "severity": x["severity"], "title": x["title"]} for x in sig][:30]})
        return r
    except Exception as e:                                           # one agent failing must not stop the others
        log.exception("%s failed", agent)
        _note(agent, trigger, None, 0, type(e).__name__)
        return {}


def _last_looks() -> dict:
    with SessionLocal() as s:
        out = {}
        for a in ("floor", "inventory", "orders", "sourcing"):
            x = s.query(AgentLook).filter_by(agent=a).order_by(AgentLook.at.desc()).first()
            if x:
                out[a] = {"at": x.at, "summary": x.summary, "signals": x.signals, "error": x.error, "trigger": x.trigger,
                          "looks": s.query(AgentLook).filter_by(agent=a).count()}
        return out


def run(trigger: str = "auto", actor: str = NAME, agents: list | None = None) -> dict:
    """One round. Returns what happened: events and what they led to, the order watcher's decision, new problems."""
    want = set(agents or ["floor", "inventory", "orders", "sourcing"])
    with _round:
        signals, out = [], {"events": [], "orders": None, "incidents": []}
        if "floor" in want:
            with SessionLocal() as s:
                seen_po = {p for (ps,) in s.query(Incident.production_orders).filter(Incident.kind == "PO_DELAY") for p in (ps or [])}
            signals += _look("floor", lambda: floor_agent.sense(seen_po), trigger).get("signals", [])
        if "inventory" in want:
            signals += _look("inventory", inventory_agent.sense, trigger).get("signals", [])
        if "sourcing" in want:
            signals += _look("sourcing", sourcing_agent.sense, trigger).get("signals", [])
        if "orders" in want:
            r = _look("orders", lambda: order_watcher.sense(trigger, actor), trigger)
            out["orders"] = r.get("watching")
            if r.get("signal"):
                signals.append(r["signal"])
        out["noticed"] = [{"title": x.get("title"), "severity": x.get("severity")} for x in sorted(signals, key=lambda x: RANK.get(x["severity"], 9))]
        for sig in sorted(signals, key=lambda x: RANK.get(x["severity"], 9)):
            if _seen(sig.get("key")):
                continue
            res = _handle(sig, trigger, actor)
            out["events"].append(res["event"])
            if res.get("incident"):
                out["incidents"].append(res["incident"])
            if res.get("plan"):
                out["orders"] = res["plan"]
        return out


def look(key: str, trigger: str = "auto", actor: str = NAME) -> dict:
    """One agent's look at the plant, without acting on what it finds (no round lock: an upload never waits for a background
    round). Returns {"summary", "signals"}; hand the signals over with handle()."""
    if key == "floor":
        with SessionLocal() as s:
            seen_po = {p for (ps,) in s.query(Incident.production_orders).filter(Incident.kind == "PO_DELAY") for p in (ps or [])}
        r = _look("floor", lambda: floor_agent.sense(seen_po), trigger)
    else:
        r = _look(key, {"inventory": inventory_agent.sense, "sourcing": sourcing_agent.sense}[key], trigger)
    sig = r.get("signals", []) + ([r["signal"]] if r.get("signal") else [])
    return {"summary": r.get("summary"), "signals": sorted(sig, key=lambda x: RANK.get(x["severity"], 9))}


def handle(signals: list, trigger: str, actor: str) -> list:
    """Raise and route signals found by look(): each new one becomes an event, a plan, a recovery or a task, as in a round."""
    with _round:
        keys = [k for k in (sig.get("key") for sig in signals) if k]
        with SessionLocal() as s:                                       # one round trip for all of them, not one per finding
            known = {k for (k,) in s.query(AgentEvent.key).filter(AgentEvent.key.in_(keys))} if keys else set()
        out = []
        for sig in sorted(signals, key=lambda x: RANK.get(x["severity"], 9)):
            if sig.get("key") in known:
                continue
            out.append(_handle(sig, trigger, actor)["event"])
            if sig.get("key"):
                known.add(sig["key"])
        return out


def submit(sig: dict, actor: str) -> dict:
    """A signal from outside a round (a person or a maintenance system reports a problem). Handled straight away."""
    with _round:
        return _handle(sig, "reported", actor)


def context(products: list | None = None) -> dict:
    """What the planning agent should know: open problems on the floor, recent floor alerts, materials at risk."""
    with SessionLocal() as s:
        alerts = s.query(AgentEvent).filter(AgentEvent.agent == floor_agent.NAME, AgentEvent.status == "ALERTED") \
                  .order_by(AgentEvent.created_at.desc()).limit(10).all()
        floor = {"open_problems": incidents.list_("OPEN", light=True), "alerts": [{"title": a.title, "plant_id": a.plant_id, "severity": a.severity} for a in alerts]}
    try:
        inv = inventory_agent.context(products)
    except Exception:
        log.exception("inventory context failed")
        inv = {}
    return {"floor": floor, "inventory": inv}


def _handle(sig: dict, trigger: str, actor: str) -> dict:
    with SessionLocal() as s:
        ev = AgentEvent(type=sig["type"], agent=sig["agent"][:40], key=sig.get("key"), severity=sig["severity"], title=sig["title"],
                        detail=json.loads(json.dumps(sig.get("detail", {}), default=str)), plant_id=sig.get("plant_id"), line_id=sig.get("line_id"))
        s.add(ev)
        s.commit()
    res = {}
    try:
        if sig["type"] in RECOVER:
            inc = _recover(sig, actor)
            res["incident"] = inc
            _close(ev.id, "RECOVERY", "planning agent", f"Recovery analysed: {inc['option_name'] or 'no change'}"
                   + (" · recovery plan sent for approval" if inc["proposal_id"] else ""), {"incident_id": inc["id"], "schedule_id": inc["proposal_id"]})
        elif sig["type"] == "orders.replan":
            plan = planning_agent.plan(sig["detail"].get("trigger", trigger), sig["detail"]["why"], actor, context())
            res["plan"] = plan
            if plan.get("outcome") == "WAITING_VOLUME_DECISION":
                _close(ev.id, "WAITING", "production manager", "Demand doesn't fit capacity: the production manager decides how to go on before the plan is made",
                       {"run_id": plan["id"], "volume_plan_id": plan.get("volume_plan_id")})
            else:
                _close(ev.id, "PLANNED", "planning agent", f"New plan sent for approval · {len(plan.get('tasks', []))} tasks",
                       {"run_id": plan["id"], "schedule_id": plan["schedule_id"]})
        else:
            who, run_id = _alert(ev.id, sig)
            _close(ev.id, "ALERTED", who, f"Alert sent to the {who}", {"run_id": run_id})
    except Exception as e:
        log.exception("handling %s failed", sig["type"])
        _close(ev.id, "FAILED", None, f"Could not be handled ({type(e).__name__}); it will be retried at the next round", {})
        with SessionLocal() as s:                                    # forget the key so the next round raises it again
            x = s.get(AgentEvent, ev.id)
            x.key = None
            s.commit()
    with SessionLocal() as s:
        res["event"] = event_out(s.get(AgentEvent, ev.id))
    return res


def _recover(sig: dict, actor: str) -> dict:
    d = sig.get("detail", {})
    start = datetime.fromisoformat(str(d["started_at"])[:19]) if d.get("started_at") else _now()
    if sig["type"] == "floor.po_late":
        po = store.D()["fact_production_order"]
        prods = sorted(set(po[po.production_order_id.isin(d["production_orders"])].product_id))
        return incidents.raise_("PO_DELAY", plant_id=sig["plant_id"], production_orders=d["production_orders"], started_at=start, description=sig["title"],
                                source="detected", actor=actor if actor != NAME else floor_agent.NAME, key=sig["key"], context=context(prods))
    if sig["type"] == "floor.reported" and d.get("kind") == "PO_DELAY":
        po = store.D()["fact_production_order"]
        prods = sorted(set(po[po.production_order_id.isin(d["production_orders"])].product_id))
        return incidents.raise_("PO_DELAY", plant_id=sig["plant_id"], production_orders=d["production_orders"], started_at=start,
                                description=sig["title"], source="reported", actor=actor, key=sig["key"], context=context(prods))
    hp = store.D()["fact_hourly_production"]
    prods = sorted(set(hp[hp.line_id == sig["line_id"]].product_id))
    return incidents.raise_("MACHINE_DOWN", plant_id=sig["plant_id"], line_id=sig["line_id"], machine_id=d.get("machine_id"), started_at=start,
                            expected_end=datetime.fromisoformat(d["expected_end"][:19]) if d.get("expected_end") else None, description=sig["title"], source="reported" if sig["type"] == "floor.reported" else "detected",
                            actor=actor if sig["type"] == "floor.reported" else floor_agent.NAME, key=sig["key"], context=context(prods))


def _alert(eid: str, sig: dict) -> tuple[str, str]:
    """Floor alerts go to the production head of that plant (and the production manager when critical); material alerts to the
    production manager. Returns (who, run id)."""
    plant = sig.get("plant_id")
    roles = []
    if sig["agent"] == floor_agent.NAME and plant:
        roles.append(("plant_head", plant))
    if sig["agent"] == sourcing_agent.NAME:                      # a move between plants: the planner decides, both plant heads prepare
        d = sig.get("detail", {})
        roles += [("planner", None)] + [("plant_head", p) for p in (d.get("other"), d.get("home")) if p]
    if sig["severity"] == "critical" or not roles:
        roles.append(("production_manager", None))
    with SessionLocal() as s:
        team = s.query(TeamMember).all()
        run = AgentRun(trigger="alert", outcome="ALERT", reason=sig["title"], open_orders=[], details={"event_id": eid, "agent": sig["agent"], "type": sig["type"]},
                       actor=sig["agent"])
        s.add(run)
        s.flush()
        for role, p in roles:
            name = next((t.name for t in team if t.role == role and (t.plant_id or None) == p), None)
            title = sig["title"] if len(sig["title"]) <= 200 else sig["title"][:197].rsplit(" ", 1)[0] + "…"   # task titles hold 200; the event keeps it all
            s.add(AgentTask(run_id=run.id, role=role, plant_id=p, assignee=name, kind="alert", status="OPEN", title=title,
                            details={"event_id": eid, "agent": sig["agent"], "severity": sig["severity"], "type": sig["type"],
                                     **json.loads(json.dumps(sig.get("detail", {}), default=str))}))
        s.commit()
        who = " and ".join(("production head of " + p) if r == "plant_head" else "production manager" for r, p in roles)
        return who, run.id


def _close(eid, status, routed_to, outcome, refs):
    with SessionLocal() as s:
        e = s.get(AgentEvent, eid)
        e.status, e.routed_to, e.outcome, e.refs, e.handled_at = status, routed_to, outcome, refs, _now()
        s.commit()


def event_out(e: AgentEvent) -> dict:
    return {"id": e.id, "type": e.type, "agent": e.agent, "severity": e.severity, "title": e.title, "plant_id": e.plant_id, "line_id": e.line_id,
            "status": e.status, "routed_to": e.routed_to, "outcome": e.outcome, "refs": e.refs, "created_at": e.created_at, "handled_at": e.handled_at}


def status(limit: int = 40) -> dict:
    with SessionLocal() as s:
        evs = [event_out(e) for e in s.query(AgentEvent).order_by(AgentEvent.created_at.desc()).limit(limit)]
        counts = {a: s.query(AgentEvent).filter(AgentEvent.agent == n).count()
                  for a, n in (("floor", floor_agent.NAME), ("inventory", inventory_agent.NAME), ("orders", order_watcher.NAME),
                               ("sourcing", sourcing_agent.NAME))}
        last_plan = s.query(AgentRun).filter_by(outcome="PLANNED").order_by(AgentRun.started_at.desc()).first()
        open_rec = s.query(Incident).filter_by(status="OPEN").count()
    plan_state = {"at": last_plan.started_at if last_plan else None,
                  "summary": (f"Last plan {last_plan.started_at:%d %b %H:%M}" if last_plan else "No plan yet") + f" · {open_rec} open recover{'ies' if open_rec != 1 else 'y'}"}
    looks = _last_looks()
    agents = [{"key": k, "name": n, "job": j, "last": plan_state if k == "planning" else looks.get(k), "raised": counts.get(k)} for k, n, j in AGENTS]
    return {"agents": agents, "events": evs, "interval_min": Policy().agent_interval_min}


# ---- background loop -------------------------------------------------------------------------------------------------------


def start() -> None:
    """A round now and then every interval, in the one process that holds the lease (others take over if it stops)."""
    coord.leader_loop("orchestrator", Policy().agent_interval_min * 60, lambda: run("auto"), first=True)

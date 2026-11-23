"""Order watcher agent: watches the open order book and tells the orchestrator when enough has changed to re-plan.

It compares the open orders with the order book at the last plan (or, before any plan, when it started watching) and signals a
re-plan when Policy thresholds are met: new orders, new units, or a new high-priority order due soon. Below the thresholds it
records that it is watching, and why. It never plans; the planning agent does, when the orchestrator asks it to.
"""
import logging
from collections import defaultdict
from datetime import timedelta
from . import data, store
from .config import Policy
from .db import AgentRun, SessionLocal

log = logging.getLogger("planner.agent.orders")
NAME = "order watcher"


def _open_orders(d, pol):
    inp = data.to_inputs(d, None, pol)
    return inp, {o.order_id: o for o in inp.orders}


def _last_plan(s):
    return s.query(AgentRun).filter_by(outcome="PLANNED").order_by(AgentRun.started_at.desc()).first()


def _base(s):
    """What new orders are counted against: the order book at the last plan, or (before any plan) when watching started."""
    return _last_plan(s) or s.query(AgentRun).filter_by(outcome="WATCHING").order_by(AgentRun.started_at).first()


def pending(d=None, pol=None) -> dict:
    """What arrived since the last plan (or since watching started)."""
    pol, d = pol or Policy(), d if d is not None else store.D()
    inp, now = _open_orders(d, pol)
    with SessionLocal() as s:
        base = _base(s)
        seen = set(base.open_orders) if base else None
        since = base.started_at if base else None
    new = [] if seen is None else [o for k, o in now.items() if k not in seen]
    horizon = inp.as_of.date() + timedelta(days=pol.agent_urgent_days)
    urgent = [o for o in new if o.priority == "HIGH" and o.due <= horizon]
    by_product = defaultdict(int)
    for o in new:
        by_product[o.product_id] += o.qty
    return {"baseline": seen is not None, "since": since, "open_orders": len(now), "new_orders": len(new), "new_units": sum(o.qty for o in new),
            "urgent": [{"order_id": o.order_id, "product_id": o.product_id, "qty": o.qty, "due": o.due.isoformat()} for o in urgent[:20]],
            "urgent_count": len(urgent), "by_product": dict(by_product), "new_ids": [o.order_id for o in new][:200], "_now": now, "_inp": inp}


def reason(p, pol) -> tuple[bool, str]:
    hits = []
    if p["new_orders"] >= pol.agent_min_new_orders:
        hits.append(f"{p['new_orders']} new orders (threshold {pol.agent_min_new_orders})")
    if p["new_units"] >= pol.agent_min_new_units:
        hits.append(f"{p['new_units']:,} new units (threshold {pol.agent_min_new_units:,})")
    if p["urgent_count"]:
        hits.append(f"{p['urgent_count']} new high-priority order(s) due within {pol.agent_urgent_days} days")
    if hits:
        return True, "; ".join(hits)
    return False, (f"{p['new_orders']} new orders and {p['new_units']:,} new units since the last plan; below the thresholds "
                   f"({pol.agent_min_new_orders} orders / {pol.agent_min_new_units:,} units / urgent high-priority)")


def record(trigger, outcome, why, p, actor, schedule_id=None, details=None) -> dict:
    """Log a decision. The open order book is kept on each record: the next look compares against it."""
    with SessionLocal() as s:
        r = AgentRun(trigger=trigger, outcome=outcome, reason=why, new_orders=p["new_orders"], new_units=p["new_units"],
                     open_orders=sorted(p["_now"]), schedule_id=schedule_id, details=details or {}, actor=actor)
        if outcome in ("WATCHING", "WAITING_VOLUME_DECISION"):     # keep the comparison point, so arrivals add up until the next plan
            base = _base(s)
            if base:
                r.open_orders = base.open_orders
        s.add(r)
        s.commit()
        log.info("%s trigger=%s new_orders=%s", outcome, trigger, p["new_orders"])
        return run_out(r)


def run_out(r: AgentRun) -> dict:
    return {"id": r.id, "trigger": r.trigger, "outcome": r.outcome, "reason": r.reason, "new_orders": r.new_orders, "new_units": r.new_units,
            "schedule_id": r.schedule_id, "details": r.details, "actor": r.actor, "started_at": r.started_at}


def sense(trigger: str, actor: str) -> dict:
    """Look at the order book. Returns {"signal": ...} when a re-plan is due, else {"watching": <logged decision>}."""
    pol = Policy()
    p = pending(pol=pol)
    if not p["baseline"]:                                          # first look: remember the order book, nothing to compare with yet
        return {"watching": record(trigger, "WATCHING", "Started watching the order book", p, actor), "summary": f"{p['open_orders']:,} open orders; started watching"}
    go, why = reason(p, pol)
    summary = f"{p['new_orders']:,} new orders · {p['new_units']:,} new units since the last plan"
    if not go:
        return {"watching": record(trigger, "WATCHING", why, p, actor), "summary": summary}
    return {"signal": {"type": "orders.replan", "agent": NAME, "severity": "warn",
                       "title": f"Demand changed: {why}", "detail": {"why": why, "trigger": trigger,
                       **{k: v for k, v in p.items() if not k.startswith("_") and k not in ("new_ids",)}}, "_pending": p},
            "summary": summary}

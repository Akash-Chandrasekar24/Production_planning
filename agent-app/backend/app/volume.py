"""Demand and capacity gate: the step before the agent schedules (sales and operations planning, in small).

Before making a plan the agent checks, week by week, whether final assembly (the bottleneck) can make every order by its date: orders
fill each week's capacity in planning priority order, after netting what open production orders still make. If they all fit, it plans.
If not, it does not schedule. It proposes a volume plan instead, with options the production manager chooses from:
  commit   - orders that don't fit get an achievable committed date (the week they can be finished); customers are told
  overtime - work through the breaks in the overloaded weeks, then commit dates for what still doesn't fit
  keep     - keep the requested dates and accept that these orders will be late
Once a volume plan is agreed, the agent plans with it (committed dates, extra minutes) and it carries into later plans: committed dates
stay committed, accepted late orders stay accepted. New orders that don't fit raise a new proposal on top of it.
"""
import hashlib
import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta

from .config import Policy, Settings
from .db import AgentTask, SessionLocal, TeamMember, VolumePlan, _now

log = logging.getLogger("planner.volume")
LABEL = {"commit": "Commit achievable dates", "overtime": "Work through breaks, then commit dates", "keep": "Keep the requested dates"}


def enabled(pol: Policy) -> bool:
    return pol.volume_gate and Settings().volume_gate


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _capacity(inp, extra: float, weeks: set | None) -> dict[date, float]:
    """Final-assembly units per week (minutes x rate), with `extra` minutes per shift in `weeks` (never beyond the shift's length)."""
    cap = defaultdict(float)
    for l in inp.lines:
        if l.kind != "FAL":
            continue
        for s in l.slots:
            w = _monday(s.start.date())
            m = s.minutes + (extra if extra and (weeks is None or w.isoformat() in weeks) else 0)
            cap[w] += min(m, (s.end - s.start).total_seconds() / 60) * l.rate_per_min
    return cap


def fill(inp, pol: Policy, extra: float = 0.0, weeks: set | None = None, seq: dict | None = None) -> dict:
    """Bucket-fill each week's final-assembly capacity in the planner's priority order. Returns each order's finishing week and the load.
    seq = the requested dates to sequence by (a committed date changes when an order is due, not its place in the queue)."""
    cap = _capacity(inp, extra, weeks)
    order = sorted(cap)
    left = {w: cap[w] for w in order}
    po = dict(inp.open_po_supply) if pol.net_open_po_supply else {}
    done, need_w = {}, defaultdict(float)
    i = 0
    seq = seq or {}
    for o in sorted(inp.orders, key=lambda o: (not o.urgent, pol.priority_rank.get(o.priority, 99), seq.get(o.order_id, o.due), o.order_id)):
        q = o.qty
        if not (o.kitted or o.status in ("PRODUCTION_ORDER", "ENQUIRY")):
            c = min(po.get(o.product_id, 0), q)
            po[o.product_id] = po.get(o.product_id, 0) - c
            q -= c
        if q <= 0:
            continue
        need_w[_monday(max(o.due, inp.as_of.date()))] += q
        while q > 1e-9 and i < len(order):
            take = min(q, left[order[i]])
            left[order[i]] -= take
            q -= take
            if q > 1e-9:
                i += 1
        done[o.order_id] = order[i].isoformat() if q <= 1e-9 and i < len(order) else None    # None: doesn't fit the planning horizon
    load = [{"week": w.isoformat(), "demand": round(need_w.get(w, 0)), "capacity": round(cap[w]),
             "load_pct": round(100 * need_w.get(w, 0) / cap[w], 1) if cap[w] else None} for w in order[:12]]
    return {"finish_week": done, "load": load}


def late_orders(inp, f: dict) -> list[dict]:
    """Customer orders that can't be finished by their date at this capacity (production orders are internal, forecast isn't an order)."""
    out = []
    for o in inp.orders:
        if o.status in ("PRODUCTION_ORDER", "FORECAST"):
            continue
        w = f["finish_week"].get(o.order_id, "skip")
        if w == "skip":
            continue
        fits_by = date.fromisoformat(w) + timedelta(days=6) if w else None
        if fits_by is None or fits_by > o.due:
            out.append({"order_id": o.order_id, "product_id": o.product_id, "qty": o.qty, "priority": o.priority, "due": o.due.isoformat(),
                        "can_finish": fits_by.isoformat() if fits_by else None, "days_later": (fits_by - o.due).days if fits_by else None})
    return out


def _overtime_cost(inp, extra: float, weeks: set) -> tuple[float, float]:
    """Hours and cost of `extra` minutes a shift on every line in `weeks` (crew size x labour rate x overtime multiplier)."""
    from . import planning_tools as T
    rates, cr = T._rates(), T._gen("gen_crew")
    size = {"SMT": int(cr.crew_size_smt.iloc[0]), "FAL": int(cr.crew_size_fal.iloc[0])} if cr is not None else {"SMT": 6, "FAL": 9}
    hours = cost = 0.0
    for l in inp.lines:
        R = rates.get(l.plant_id) or (next(iter(rates.values())) if rates else None)
        for s in l.slots:
            if _monday(s.start.date()).isoformat() in weeks:
                m = min(s.minutes + extra, (s.end - s.start).total_seconds() / 60) - s.minutes
                hours += m / 60
                if R:
                    cost += m / 60 * size.get(l.kind, 8) * R["labour_inr_per_hour"] * R["overtime_multiplier"]
    return round(hours), round(cost)


def _requested(inp) -> dict:
    return {o.order_id: o.due for o in inp.orders}


def options(inp, pol: Policy, seq: dict | None = None) -> dict:
    """The three ways forward, each with what it means for customers and cost; the agent's pick is the one that keeps most dates."""
    base = fill(inp, pol, seq=seq)
    late = late_orders(inp, base)
    over = {r["week"] for r in base["load"] if (r["load_pct"] or 0) > 100}
    ot_f = fill(inp, pol, pol.volume_break_minutes, over, seq)
    ot_late = late_orders(inp, ot_f)
    hours, cost = _overtime_cost(inp, pol.volume_break_minutes, over)

    def opt(key, lt, extra, weeks, h=0, c=0):
        redates = {r["order_id"]: r["can_finish"] for r in lt if r["can_finish"]} if key != "keep" else {}
        later = [r["days_later"] for r in lt if r["days_later"] is not None]
        return {"key": key, "title": LABEL[key], "redates": redates, "accept_late": [r["order_id"] for r in lt] if key == "keep" else
                [r["order_id"] for r in lt if not r["can_finish"]], "extra_min": extra, "extra_weeks": sorted(weeks),
                "orders_affected": len(lt), "units_affected": sum(r["qty"] for r in lt), "high_affected": sum(r["priority"] == "HIGH" for r in lt),
                "avg_days_later": round(sum(later) / len(later), 1) if later else 0, "max_days_later": max(later, default=0),
                "beyond_horizon": sum(1 for r in lt if not r["can_finish"]), "overtime_h": h, "overtime_cost_inr": c,
                "orders": sorted(lt, key=lambda r: -(r["days_later"] or 999))[:40]}
    out = [opt("commit", late, 0.0, set()), opt("overtime", ot_late, pol.volume_break_minutes, over, hours, cost), opt("keep", late, 0.0, set())]
    best = "overtime" if out[1]["orders_affected"] <= 0.9 * out[0]["orders_affected"] else "commit"
    for o in out:
        o["best"] = o["key"] == best
    return {"late": late, "load": base["load"], "overloaded_weeks": sorted(over), "options": out, "recommended": best}


def agreed() -> VolumePlan | None:
    with SessionLocal() as s:
        v = s.query(VolumePlan).filter_by(status="AGREED").order_by(VolumePlan.agreed_at.desc()).first()
        if v:
            s.expunge(v)
        return v


def apply(inp, vp: VolumePlan | None):
    """The inputs with an agreed volume plan in force: committed dates and the extra minutes in its weeks."""
    if vp is None:
        return inp
    x = inp.model_copy(deep=True)
    for o in x.orders:
        if o.order_id in (vp.redates or {}):
            o.due = date.fromisoformat(vp.redates[o.order_id])
    if vp.extra_min:
        weeks = set(vp.extra_weeks or [])
        for l in x.lines:
            for s in l.slots:
                if _monday(s.start.date()).isoformat() in weeks:
                    s.minutes = min(s.minutes + vp.extra_min, (s.end - s.start).total_seconds() / 60)
    return x


def levers(inp, pol: Policy):
    """Capacity levers of a plan option. fill_shifts: every shift of the line's pattern runs on every day of the horizon (days now off
    included) and works through the break. outsource_orders: those orders leave our lines (a contract manufacturer delivers them)."""
    from .domain import Slot
    x = inp.model_copy(deep=True)
    if pol.outsource_orders:
        out = set(pol.outsource_orders)
        x.orders = [o for o in x.orders if o.order_id not in out]
    if pol.fill_shifts:
        for l in x.lines:
            if not l.slots:
                continue
            length = max((s.end - s.start) for s in l.slots)
            starts = sorted({(s.start.hour, s.start.minute) for s in l.slots})
            have = {s.start for s in l.slots}
            for s in l.slots:                                          # work through the break
                s.minutes = min(s.minutes + pol.volume_break_minutes, (s.end - s.start).total_seconds() / 60)
            day, last = x.as_of.date() - timedelta(days=1), x.horizon_end.date() if x.horizon_end else max(s.end for s in l.slots).date()
            while day <= last:
                for h, m in starts:
                    st = datetime.combine(day, datetime.min.time()).replace(hour=h, minute=m)
                    en = st + length
                    if st in have or en <= x.as_of or (x.horizon_end and st >= x.horizon_end):
                        continue
                    mins = (en - max(st, x.as_of)).total_seconds() / 60
                    for w in x.maintenance:                            # a service still stops the line
                        if w["line_id"] == l.line_id:
                            mins -= max(0.0, (min(en, w["end"]) - max(st, w["start"])).total_seconds() / 60)
                    if mins > 0:
                        l.slots.append(Slot(start=max(st, x.as_of), end=en, minutes=mins))
                day += timedelta(days=1)
            l.slots.sort(key=lambda s: s.start)
    return x


def load(vid: str | None) -> VolumePlan | None:
    if not vid:
        return None
    with SessionLocal() as s:
        v = s.get(VolumePlan, vid)
        if v:
            s.expunge(v)
        return v


def check(inp, pol: Policy) -> tuple[object, VolumePlan | None, dict | None]:
    """(inputs to plan with, the agreed volume plan in force, a proposal if a decision is needed first)."""
    vp = agreed()
    x = apply(inp, vp)
    if not enabled(pol):
        return x, vp, None
    lt = late_orders(x, fill(x, pol, vp.extra_min if vp else 0.0, set(vp.extra_weeks or []) if vp else None, _requested(inp)))
    lt = [r for r in lt if r["order_id"] not in set((vp.accept_late or []) if vp else [])]
    if not lt:
        return x, vp, None
    return x, vp, options(x, pol, _requested(inp))


def propose(x, vp: VolumePlan | None, prop: dict, trigger: str, why: str) -> tuple[VolumePlan, bool]:
    """Save the proposal (or keep the open one when nothing changed). Returns (proposal, is_new)."""
    sig = hashlib.sha1(json.dumps(sorted(r["order_id"] for r in prop["late"])).encode()).hexdigest()[:16]
    with SessionLocal() as s:
        same = s.query(VolumePlan).filter_by(status="PROPOSED", signature=sig).first()
        if same:
            s.expunge(same)
            return same, False
        for old in s.query(VolumePlan).filter_by(status="PROPOSED"):
            old.status = "SUPERSEDED"
        v = VolumePlan(status="PROPOSED", signature=sig, trigger=trigger, reason=why[:2000], base_id=vp.id if vp else None,
                       load=prop["load"], options=prop["options"], recommended=prop["recommended"], late_count=len(prop["late"]),
                       as_of=x.as_of.isoformat())
        s.add(v)
        s.commit()
        s.expunge(v)
    from . import coord
    coord.changed("planlist")                                    # the decision card appears at once
    return v, True


def agree(vid: str, key: str, user: str) -> VolumePlan:
    """The production manager's decision: the chosen option, merged with the volume plan already in force."""
    with SessionLocal() as s:
        v = s.get(VolumePlan, vid)
        if v is None:
            raise LookupError(vid)
        if v.status != "PROPOSED":
            raise ValueError("This volume plan was already decided or replaced by a newer one")
        o = next((o for o in v.options if o["key"] == key), None)
        if o is None:
            raise ValueError("Choose commit, overtime or keep")
        prev = s.get(VolumePlan, v.base_id) if v.base_id else None
        v.chosen, v.agreed_by, v.agreed_at, v.status = key, user, _now(), "AGREED"
        v.redates = {**((prev.redates or {}) if prev else {}), **o["redates"]}
        v.accept_late = sorted(set((prev.accept_late or []) if prev else []) | set(o["accept_late"]))
        v.extra_min = max(o["extra_min"], prev.extra_min if prev else 0.0)
        v.extra_weeks = sorted(set(o["extra_weeks"]) | set((prev.extra_weeks or []) if prev else []))
        for old in s.query(VolumePlan).filter(VolumePlan.status == "AGREED", VolumePlan.id != v.id):
            old.status = "SUPERSEDED"
        for t in s.query(AgentTask).filter(AgentTask.kind == "volume", AgentTask.status == "OPEN"):
            t.status, t.updated_by, t.note = "DONE", user, f"Agreed: {LABEL[key]}"
        if o["redates"]:                                             # customers whose date moved must be told
            pm = next((m.name for m in s.query(TeamMember).filter_by(role="production_manager")), None)
            s.add(AgentTask(run_id=v.run_id, role="production_manager", assignee=pm, kind="alert", status="OPEN",
                            title=f"Tell {len(o['redates'])} customers their committed delivery dates"[:200],
                            details={"agent": "planning", "severity": "info", "type": "dates_committed", "orders": list(o["redates"])[:200]}))
        s.commit()
        s.expunge(v)
    from . import coord
    coord.changed("plans")                                       # pages showing the decision or committed dates refresh at once
    return v


def out(v: VolumePlan | None) -> dict | None:
    if v is None:
        return None
    return {k: getattr(v, k) for k in ("id", "status", "trigger", "reason", "load", "options", "recommended", "chosen", "agreed_by", "agreed_at",
                                       "created_at", "late_count", "redates", "accept_late", "extra_min", "extra_weeks", "as_of")}


def state() -> dict:
    from . import cache
    return cache.get("volume.state", (), 60, _state, ("plans", "planlist", "data"))


def _state() -> dict:
    """The proposal waiting for a decision (if any) and the volume plan in force."""
    with SessionLocal() as s:
        p = s.query(VolumePlan).filter_by(status="PROPOSED").order_by(VolumePlan.created_at.desc()).first()
        if p:
            s.expunge(p)
    a = agreed()
    return {"proposal": out(p), "agreed": out(a), "enabled": enabled(Policy()),
            "committed_orders": len((a.redates or {}) if a else {}), "accepted_late": len((a.accept_late or []) if a else [])}

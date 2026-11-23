"""What-if scenarios: transform PlanningInput, replan against a baseline (frozen jobs stay put), compare metrics."""
from datetime import date, datetime
from pydantic import BaseModel
from .config import Policy
from .domain import Order, PlanningInput, Schedule
from .planning import plan


class MachineLoss(BaseModel):
    line_id: str | None = None           # give a line, or a machine_id (a machine outage stops its line: lines are serial)
    machine_id: str | None = None
    start: datetime
    end: datetime


class DemandSpike(BaseModel):
    product_id: str | None = None        # None = all products
    multiplier: float


class UrgentOrder(BaseModel):
    product_id: str
    qty: int
    due: date
    order_id: str | None = None


class ExtraTime(BaseModel):
    line_id: str | None = None           # None = every line
    start: datetime
    end: datetime
    minutes_per_shift: float             # added to each shift in the window, never beyond the shift's length


class Scenario(BaseModel):
    name: str
    demand_spikes: list[DemandSpike] = []
    absenteeism_pct: float = 0.0         # capacity loss on all lines (no skills data -> modelled as % of minutes)
    machine_losses: list[MachineLoss] = []
    material_factor: dict[str, float] = {}   # material_id or "*" -> multiplier on available stock
    urgent_orders: list[UrgentOrder] = []
    # recovery levers used by the planning agent when something goes wrong on the floor
    extra_time: list[ExtraTime] = []     # overtime / extended shifts
    keep_plant: dict[str, str] = {}      # order_id -> plant: make it where it was planned
    split_orders: list[str] = []         # make these orders as two lots that can run in different plants at the same time
    expedite: list[str] = []             # these existing orders go first
    floor_work: list[Order] = []         # remaining work of open production orders, made on the lines (material already issued)
    release_plant: list[str] = []        # these orders may move to another plant


def apply(inp: PlanningInput, sc: Scenario) -> PlanningInput:
    x = inp.model_copy(deep=True)
    have = {o.order_id for o in x.orders}                 # plans may already carry the production orders' work: never twice
    x.orders += [o.model_copy(update={"kitted": True}) for o in sc.floor_work if o.order_id not in have]
    rel = set(sc.release_plant)
    for o in x.orders:
        o.plant_id = None if o.order_id in rel else sc.keep_plant.get(o.order_id, o.plant_id)
        o.urgent = o.urgent or o.order_id in sc.expedite
    if sc.split_orders:
        split, out = set(sc.split_orders), []
        for o in x.orders:
            if o.order_id in split and o.qty >= 2:
                h = o.qty // 2
                out += [o.model_copy(update={"order_id": f"{o.order_id}#1", "qty": o.qty - h}), o.model_copy(update={"order_id": f"{o.order_id}#2", "qty": h})]
            else:
                out.append(o)
        x.orders = out
    for et in sc.extra_time:
        for l in x.lines:
            if et.line_id in (None, l.line_id):
                for s in l.slots:
                    if s.start < et.end and s.end > et.start:
                        s.minutes = min(s.minutes + et.minutes_per_shift, (s.end - s.start).total_seconds() / 60)
    for sp in sc.demand_spikes:
        for o in x.orders:
            if sp.product_id in (None, o.product_id):
                o.qty = max(1, round(o.qty * sp.multiplier))
    for l in x.lines:
        if sc.absenteeism_pct:
            for s in l.slots:
                s.minutes *= 1 - sc.absenteeism_pct / 100
    for ml in sc.machine_losses:
        line = ml.line_id or x.machine_line.get(ml.machine_id)
        if not line:
            raise ValueError(f"unknown machine/line in machine_loss: {ml.machine_id or ml.line_id}")
        for l in x.lines:
            if l.line_id == line:
                for s in l.slots:
                    if s.start < ml.end and s.end > ml.start:
                        s.minutes = 0
    for st in x.stock:
        st.available *= sc.material_factor.get(st.material_id, sc.material_factor.get("*", 1.0))
    for i, u in enumerate(sc.urgent_orders, 1):
        x.orders.append(Order(order_id=u.order_id or f"URGENT-{i:03d}", product_id=u.product_id, qty=u.qty, due=u.due,
                              priority="HIGH", status="URGENT", urgent=True))
    return x


def run(inp: PlanningInput, baseline: Schedule, sc: Scenario, pol: Policy | None = None) -> Schedule:
    return plan(apply(inp, sc), pol, previous=baseline, name=sc.name)


def compare(baseline: Schedule, others: list[Schedule]) -> list[dict]:
    """Metric rows; deltas vs baseline for numeric metrics. Cost/labour metrics only where data exists (material cost, order value)."""
    rows = []
    for s in [baseline] + others:
        r = {"schedule_id": s.id, "name": s.name, "feasibility": s.feasibility, **{k: v for k, v in s.summary.items() if k != "rough_cut"},   # a table, not a measure
             "frozen_moved": sum(d.code == "FROZEN_JOB_MOVED" for d in s.diagnostics)}
        for k, v in s.summary.items():
            b = baseline.summary.get(k)
            if s is not baseline and isinstance(v, (int, float)) and isinstance(b, (int, float)):
                r[f"d_{k}"] = round(v - b, 3)
        rows.append(r)
    return rows

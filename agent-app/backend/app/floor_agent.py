"""Floor agent: watches the production floor and raises the alarm.

Reads the hourly production feed (MES), the machine list and the production orders, looking at the last 24 hours of the feed:
  critical (the orchestrator asks the planning agent for a recovery plan)
    floor.breakdown      equipment faults on a line add up to Policy.agent_fault_min minutes or more
    floor.line_stopped   a line made nothing for Policy.floor_stopped_hours hours in a row
    floor.po_late        open production orders planned to finish after their due date
  warnings and notices (the orchestrator alerts the right people)
    floor.output_drop    a line made less than Policy.floor_output_min of its target over the last 4 hours
    floor.scrap_spike    a line's reject rate is far above its normal (mean + 4 standard deviations of its daily rate)
    floor.material_stops a line stopped for missing material Policy.floor_material_stop_hours hours or more
    floor.micro_stops    many short stops on a line (Policy.floor_micro_stops hours or more)
    floor.maintenance    a critical (A) machine reaches its planned maintenance interval within 2 days
    floor.po_behind      open production orders behind their planned progress (production confirmations vs plan)
Each signal has a key, so the same thing is raised once. The agent only reports; it never changes plans or orders.
"""
import pandas as pd
from . import data, store
from .config import Policy

NAME = "floor agent"


def _sig(type_, severity, key, title, plant=None, line=None, **detail) -> dict:
    return {"type": type_, "agent": NAME, "severity": severity, "key": key, "title": title, "plant_id": plant, "line_id": line, "detail": detail}


def sense(seen_po: set | None = None) -> dict:
    pol, d = Policy(), store.D()
    h = d["fact_hourly_production"].copy()
    h["ts"] = pd.to_datetime(h.event_ts)
    end = h.ts.max()
    day = str(end.date())
    last = h[h.ts > end - pd.Timedelta(hours=24)]
    plant_of = h.groupby("line_id").plant_id.first().to_dict()
    out = []
    for line, g in last.sort_values("ts").groupby("line_id"):
        plant = plant_of[line]
        f = g[g.downtime_reason == "EQUIPMENT_FAULT"]
        if f.downtime_min.sum() >= pol.agent_fault_min:
            out.append(_sig("floor.breakdown", "critical", f"MACHINE_DOWN:{line}:{f.event_date.min()}",
                            f"{line} broke down: {int(f.downtime_min.sum())} minutes of equipment faults in the last 24 hours", plant, line,
                            started_at=str(f.ts.min()), minutes=int(f.downtime_min.sum())))
        zero = (g.actual_qty == 0).astype(int)
        run = zero.groupby((zero != zero.shift()).cumsum()).cumsum().max()
        if run >= pol.floor_stopped_hours:
            first = g.ts[zero.astype(bool)].min()
            out.append(_sig("floor.line_stopped", "critical", f"MACHINE_DOWN:{line}:stopped:{first}",
                            f"{line} made nothing for {int(run)} hours in a row", plant, line, started_at=str(first), hours=int(run)))
        g4 = g[g.ts > end - pd.Timedelta(hours=4)]
        if g4.target_qty.sum() and g4.actual_qty.sum() / g4.target_qty.sum() < pol.floor_output_min:
            pct = round(100 * g4.actual_qty.sum() / g4.target_qty.sum())
            out.append(_sig("floor.output_drop", "warn", f"floor.output_drop:{line}:{end}", f"{line} made only {pct}% of its target in the last 4 hours",
                            plant, line, pct=pct))
        hist = h[h.line_id == line].groupby("event_date")[["reject_qty", "actual_qty"]].sum()
        rate = hist.reject_qty / hist.actual_qty
        now = g.reject_qty.sum() / g.actual_qty.sum() if g.actual_qty.sum() else 0
        if len(rate) > 7 and now > rate.mean() + 4 * rate.std():
            out.append(_sig("floor.scrap_spike", "warn", f"floor.scrap_spike:{line}:{day}",
                            f"{line} rejects {100 * now:.1f}% of what it makes (normally {100 * rate.mean():.1f}%)", plant, line, rate=round(now, 4)))
        nm = int((g.downtime_reason == "NO_MATERIAL").sum())
        if nm >= pol.floor_material_stop_hours:
            out.append(_sig("floor.material_stops", "warn", f"floor.material_stops:{line}:{day}",
                            f"{line} stopped for missing material in {nm} of the last 24 hours", plant, line, hours=nm,
                            products=sorted(g[g.downtime_reason == "NO_MATERIAL"].product_id.unique())))
        ms = int((g.downtime_reason == "MICRO_STOP").sum())
        if ms >= pol.floor_micro_stops:
            out.append(_sig("floor.micro_stops", "info", f"floor.micro_stops:{line}:{day}", f"{line} had short stops in {ms} of the last 24 hours",
                            plant, line, hours=ms))
    due_by_plant = {}                                                # one notice per plant, listing its machines
    from .twin import _service                                       # maintenance records when there are any, else the interval
    for m in d["dim_machine"].itertuples():
        if m.criticality != "A":
            continue
        due = _service(d, m.machine_id, int(m.planned_maintenance_interval_days), m.commissioned_date, end)["days_to_next"]
        if due <= 2:
            when = (end.normalize() + pd.Timedelta(days=due)).date()
            due_by_plant.setdefault(plant_of.get(m.line_id), []).append({"machine_id": m.machine_id, "type": m.machine_type, "line_id": m.line_id, "due": str(when)})
    for plant, ms in due_by_plant.items():
        first = min(x["due"] for x in ms)
        out.append(_sig("floor.maintenance", "info", f"floor.maintenance:{plant}:{first}",
                        f"{len(ms)} critical machine{'s' if len(ms) != 1 else ''} at {plant} due for planned maintenance from {pd.Timestamp(first):%d %b}: "
                        + ", ".join(x["machine_id"] for x in ms), plant, machines=ms))
    # production orders planned to finish after their due date (still owing units)
    po = data.open_po(d, pol)
    po = po[(po.planned_finish.str[:10] > po.due_date) & ~po.production_order_id.isin(seen_po or set()) & (po.remaining > 0)]
    for plant, g in po.groupby("plant_id"):
        ids = sorted(g.production_order_id)
        out.append(_sig("floor.po_late", "critical", "PO_DELAY:" + ",".join(ids)[:180],
                        f"{len(ids)} open production order{'s' if len(ids) != 1 else ''} at {plant} are planned to finish after their due date",
                        plant, production_orders=ids, started_at=str(g.due_date.min())))
    # production orders falling behind their planned progress (plan vs actual), before they are late
    from . import planning_tools
    behind = pd.DataFrame(planning_tools.adherence().get("behind", []))
    for plant, g in (behind.groupby("plant_id") if not behind.empty else []):
        out.append(_sig("floor.po_behind", "warn", f"floor.po_behind:{plant}:{end:%Y-%m-%d}",
                        f"{len(g)} open production order{'s' if len(g) != 1 else ''} at {plant} are behind their planned progress "
                        f"({int(g.gap.sum()):,} units short of plan); worst {g.iloc[0].production_order_id}",
                        plant, production_orders=list(g.production_order_id)))
    crit = sum(s["severity"] == "critical" for s in out)
    return {"signals": out, "summary": f"{h.line_id.nunique()} lines watched · feed up to {end:%d %b %H:%M} · "
                                       + (f"{len(out)} signal{'s' if len(out) != 1 else ''} ({crit} critical)" if out else "all normal")}

"""The production plan document for one plan: the seven parts a production plan is made of, in one place.
1 sales forecast  2 production budget  3 inventory plan (materials)  4 resource plan  5 cost estimate  6 schedule  7 control plan."""
from collections import defaultdict
from datetime import timedelta

from . import budget, execution, planning, planning_tools, plant_brief, signoff, store
from .config import Policy

PLANT = {"IN-TN-01": "Hosur", "IN-KA-01": "Bengaluru"}
TARGETS = {"adherence": 90, "attainment": 95, "on_time": 95, "utilisation": 85, "stability": 80}     # what each control measure is held to


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:                                    # a part that can't be worked out is shown as not available, the rest still renders
        return None


def build(sid: str) -> dict:
    s, inp = store.get(sid), store.inputs(sid)
    if s is None:
        raise KeyError(sid)
    pol = Policy(**s.policy)
    m = s.summary

    # 3 inventory plan: materials the plan needs, what stock covers, and what to buy (parts ordered today, supplier lead time)
    waits = defaultdict(int)
    for a in s.assignments:
        for r in a.reason_codes:
            if r.startswith("WAITS_FOR_PURCHASE:"):
                waits[r.split(":", 1)[1]] += 1
    mats = []
    for r in planning.material_calloff(inp, s):
        if r["scheduled_consumption"] <= 0 and r["shortfall_qty"] <= 0:
            continue
        lead = inp.lead_days.get(r["material_id"], pol.purchase_default_lead_days)
        mats.append({**{k: r[k] for k in ("material_id", "required_qty", "available_qty", "scheduled_consumption", "safety_stock", "shortfall_qty")},
                     "lead_days": lead, "arrives_if_ordered_today": (inp.as_of + timedelta(days=lead)).date().isoformat(), "jobs_waiting": waits.get(r["material_id"], 0)})
    mats.sort(key=lambda r: -r["shortfall_qty"])

    # 4 resource plan: line hours, planned services, crews
    lines = []
    for l in inp.lines:
        avail = sum(sl.minutes for sl in l.slots)
        lines.append({"line_id": l.line_id, "plant_id": l.plant_id, "stage": l.kind, "available_h": round(avail / 60),
                      "utilisation_pct": round(100 * s.line_utilisation.get(l.line_id, 0), 1),
                      "services": sum(1 for w in inp.maintenance if w["line_id"] == l.line_id),
                      "service_h": round(sum(w["minutes"] for w in inp.maintenance if w["line_id"] == l.line_id) / 60, 1)})
    crews = _safe(planning_tools.crews, 14, s, inp) or {}
    crew_cut = next((i["count"] for i in inp.data_issues if i["code"] == "CREW_SHORT"), 0)

    plants = {p: _safe(plant_brief.build, sid, p) for p in sorted({a.plant_id for a in s.assignments})}
    kpi = _safe(planning_tools.kpis, sid) or {"items": []}
    st = _safe(signoff.stages, s) or {}
    notes = execution.notes(sid)
    return {
        "schedule_id": s.id, "name": s.name, "status": s.status, "fully_approved": st.get("fully_approved", False), "stages": st.get("stages", []),
        "as_of": s.as_of, "horizon_days": pol.horizon_days,
        "forecast": _safe(budget.forecast_review, sid),
        "budget": _safe(budget.production_budget, sid),
        "rough_cut": _safe(budget.rough_cut, sid),
        "inventory": {"materials": mats[:25], "count": len(mats), "short": sum(1 for r in mats if r["shortfall_qty"] > 0),
                      "safety_stock_kept": pol.respect_safety_stock, "fg_stock": inp.fg_stock},
        "resources": {"lines": lines, "crews": {"short_shifts": len(crews.get("short_shifts", [])), "overtime_h": crews.get("overtime_h"),
                                                 "rows": (crews.get("short_shifts") or [])[:12]}, "crew_hours_removed": crew_cut},
        "cost": _safe(planning_tools.cost, sid),
        "schedule": {"summary": {k: m.get(k) for k in ("demand_qty", "scheduled_qty", "unscheduled_qty", "late_jobs", "on_time_qty_pct", "forecast_qty",
                                                       "purchase_wait_jobs", "changeovers", "changeover_min", "bottleneck_line", "avg_utilisation", "stability_pct")},
                     "plants": {p: ({k: b[k] for k in ("units", "jobs", "plan_days", "otd_pct", "late_jobs", "bottleneck", "lead_time_h", "mps")} if b and b.get("available") else None)
                                for p, b in plants.items()},
                     "fences": {"frozen_days": pol.frozen_days, "slushy_days": pol.slushy_days}},
        "control": {"kpis": [{**k, "target": TARGETS.get(k["key"])} for k in kpi.get("items", [])],
                    "risks": [n for n in notes if n["kind"] == "risk"], "comments": [n for n in notes if n["kind"] == "comment"][:10],
                    "plan_risks": [{"code": d.code, "detail": d.detail} for d in s.diagnostics if d.severity in ("warn", "error")][:15],
                    "reviews": [f"Daily plant brief to each production head at {pol.report_daily_hour:02d}:00",
                                "Weekly report to the production managers on " + ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][pol.report_weekly_day],
                                "Execution board updated by each plant during the shift; blocked jobs alert the production manager",
                                "Re-plan on new orders, line stops or low stock; the first 2 weeks stay frozen"] if pol.reports_enabled else []},
    }

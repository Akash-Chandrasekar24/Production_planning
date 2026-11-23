"""Planner API (under /api). Generation, approval and publication are separate actions with separate permissions.
The engine/agent only create PENDING_APPROVAL schedules; only a human with the 'approve' action approves; publication needs a
verified ERP/MES connector (none exists) and is never claimed. Auth is dev-header only: see app/auth.py."""
import threading
import csv
import io
from datetime import datetime, timedelta
from collections import Counter
from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from . import cache, data, insights, inventory_agent, nemoguard, planning, planning_agent, publish, scenarios, store, twin
from .db import AgentTask, Incident, SessionLocal
from .auth import Principal, require
from .config import Policy, Settings, database_url, readonly_database_url
from .domain import Schedule
from .errors import ApiError
from .integrations import IntegrationUnavailable
from .schemas import ApprovalRequest, AuditEvent, DecisionResult, DQIssue, GenerateRequest, ScenarioResult, ScheduleSummary

router = APIRouter(prefix="/api")


def _sched(sid: str) -> Schedule:
    s = store.get(sid)
    if not s:
        raise ApiError(404, "SCHEDULE_NOT_FOUND", f"No schedule '{sid}'")
    return s


def _summary(s: Schedule, **extra) -> ScheduleSummary:
    return ScheduleSummary(id=s.id, name=s.name, status=s.status, feasibility=s.feasibility, as_of=s.as_of, created_by=s.created_by,
                           decided_by=s.decided_by, summary=s.summary, line_utilisation=s.line_utilisation, **extra)


def risks(s: Schedule) -> list[dict]:
    """Conditions a planner must explicitly acknowledge before approving."""
    r = []
    if s.unscheduled:
        r.append({"code": "UNSCHEDULED_DEMAND", "detail": f"{sum(u.qty for u in s.unscheduled)} units not scheduled",
                  "reasons": dict(Counter(u.reason for u in s.unscheduled))})
    if s.summary.get("late_jobs"):
        r.append({"code": "LATE_RISK_JOBS", "detail": f"{s.summary['late_jobs']} jobs finish after their due date"})
    if s.summary.get("material_short_jobs"):
        r.append({"code": "MATERIAL_SHORT_JOBS", "detail": f"{s.summary['material_short_jobs']} jobs are short of material"})
    ack = ("FROZEN_JOB_MOVED", "FROZEN_FENCE_BROKEN", "SLUSHY_ZONE_CHANGES", "PLANNED_AFTER_PURCHASE")   # time fences and MRP purchases
    for code, n in Counter(d.code for d in s.diagnostics if d.severity == "error" or d.code in ack).items():
        if code not in ("MATERIAL_BLOCKED", "NO_CAPACITY_IN_HORIZON"):          # already covered by UNSCHEDULED_DEMAND
            r.append({"code": code, "detail": f"{n} diagnostics"})
    return r


# ------------------------------------------------------------------ status / data
def _freshness() -> dict:
    d = store.D()
    return {"inventory_snapshot": d["fact_inventory_daily_snapshot"].snapshot_date.max(), "calendar_ends": d["dim_shift_calendar"].calendar_date.max(),
            "last_order_date": d["fact_customer_order"].order_date.max(), "hourly_actuals_end": d["fact_hourly_production"].event_date.max()}


@router.get("/status")
def status(_: Principal = Depends(require("read"))):
    s = Settings()
    llm = "CONFIGURED" if s.domo_subdomain and s.domo_token and s.domo_token.get_secret_value() else "NOT_CONFIGURED"
    try:
        store.D()
        data_ok = True
    except FileNotFoundError:
        data_ok = False
    return {"auth": {"mode": s.auth_mode, "authenticated": False, "warning": "roles come from headers; no identity provider is verified"},
            "persistence": {**store.backend(), "schedules": store.count()},
            "data": {"loaded": data_ok, "source": data.SOURCE["name"], "latest": _freshness() if data_ok else None},
            "integrations": {"domo_llm": llm, "guardrails": nemoguard.status(), "erp_mes_publish": publish.status(),
                             "agent_sql": "ENABLED_READONLY_ROLE" if readonly_database_url() else "DISABLED_NO_READONLY_ROLE"},
            "database": "postgresql" if database_url() else "sqlite"}


@router.get("/data-quality", response_model=list[DQIssue])
def dq(_: Principal = Depends(require("read"))):
    return data.dq_report(store.D())


@router.get("/demand")
def demand(_: Principal = Depends(require("read"))):
    inp = data.to_inputs(store.D())
    by_p, by_pr = Counter(), Counter()
    for o in inp.orders:
        by_p[o.product_id] += o.qty
        by_pr[o.priority] += o.qty
    return {"open_orders": len(inp.orders), "open_qty": sum(by_p.values()), "by_product": by_p, "by_priority": by_pr,
            "open_po_supply_by_product": inp.open_po_supply}


def filters(plant: str | None = Query(None, max_length=40), line: str | None = Query(None, max_length=40),
            product: str | None = Query(None, max_length=40), priority: str | None = Query(None, max_length=20),
            channel: str | None = Query(None, max_length=40)) -> dict:
    """Shared dashboard filters (?plant=&line=&product=&priority=&channel=). Unknown values are a 422, never silently ignored."""
    try:
        return insights.validate(store.D(), {"plant": plant, "line": line, "product": product, "priority": priority, "channel": channel})
    except ValueError as e:
        raise ApiError(422, "UNKNOWN_FILTER_VALUE", str(e), {"options": insights.options(store.D())})


@router.get("/insights")
@cache.route(120, ("data",))
def data_insights(days: int = Query(30, ge=7, le=60), f: dict = Depends(filters), _: Principal = Depends(require("read"))):
    """Demand, supply, stock and line-performance facts from the data (and period-over-period trends), independent of any plan."""
    return insights.build(store.D(), days=days, f=f)


@router.get("/twin")
@cache.route(60, ("data",))
def digital_twin(at: str | None = Query(None, max_length=25, description="Hour to show (ISO, e.g. 2026-10-31T14:00); default = latest record"),
                 _: Principal = Depends(require("read"))):
    """The production floor at one hour: line and machine states, OEE, stoppages. Replays recorded data (no live feed exists)."""
    return insights._clean(twin.snapshot(store.D(), _hour(at)))


def _hour(at: str | None) -> str | None:
    if at:
        try:
            datetime.fromisoformat(at)
        except ValueError:
            raise ApiError(422, "INVALID_TIME", f"'{at}' is not a date and time like 2026-10-31T14:00")
    return at


@router.get("/twin/sites")
@cache.route(30, ("data", "plans"))
def twin_sites(at: str | None = Query(None, max_length=25), _: Principal = Depends(require("read"))):
    """The plants on the map, with how each is running at that hour and its open problems and alerts."""
    out = twin.sites(store.D(), _hour(at))
    with SessionLocal() as s:
        probs = s.query(Incident.plant_id).filter_by(status="OPEN").all()
        alerts = s.query(AgentTask.plant_id).filter(AgentTask.kind == "alert", AgentTask.status.in_(["OPEN", "ACKNOWLEDGED"])).all()
    for p in out:
        p["open_problems"] = sum(x == p["plant_id"] for (x,) in probs)
        p["open_alerts"] = sum(x == p["plant_id"] for (x,) in alerts)
    return insights._clean(out)


@router.get("/twin/vehicle/{vehicle_id}")
def twin_vehicle(vehicle_id: str, at: str | None = Query(None, max_length=25), _: Principal = Depends(require("read"))):
    """A forklift or AGV: what it is doing, condition, trips and loads per day / week / month / year, service and repairs."""
    v = twin.vehicle(store.D(), vehicle_id, _hour(at))
    if not v:
        raise ApiError(404, "VEHICLE_NOT_FOUND", f"No records for vehicle '{vehicle_id}'")
    return insights._clean(v)


@router.get("/twin/warehouse/{plant}/{warehouse}")
def twin_warehouse(plant: str, warehouse: str, at: str | None = Query(None, max_length=25), _: Principal = Depends(require("read"))):
    """A warehouse on the day's stock count: materials, lots, holds, stock in transit, value and the last 30 counts."""
    d = store.D()
    w = twin.warehouse(d, plant, warehouse, _hour(at))
    cover = {x["material_id"]: x for x in inventory_agent._view()["materials"]}
    for m in w["materials"]:
        c = cover.get(m["material_id"], {})
        m.update({"cover_days": c.get("cover_days"), "daily_use": c.get("daily_use")})
    return insights._clean(w)


@router.get("/twin/area/{plant}/{kind}")
def twin_area(plant: str, kind: str, at: str | None = Query(None, max_length=25), _: Principal = Depends(require("read"))):
    """Receiving dock, shipping dock, packing, gate or office of a plant."""
    a = twin.area(store.D(), plant, kind, _hour(at))
    if not a:
        raise ApiError(404, "AREA_NOT_FOUND", "Area must be receiving, shipping, packing, gate or office")
    if kind == "office":
        with SessionLocal() as s:
            a["open_problems"] = s.query(Incident).filter_by(status="OPEN", plant_id=plant).count()
            a["open_alerts"] = s.query(AgentTask).filter(AgentTask.kind == "alert", AgentTask.plant_id == plant, AgentTask.status.in_(["OPEN", "ACKNOWLEDGED"])).count()
    return insights._clean(a)


@router.get("/twin/crew/{line_id}")
def twin_crew(line_id: str, at: str | None = Query(None, max_length=25), _: Principal = Depends(require("read"))):
    """The crew working a line in that hour and how its hours compare with the line's normal."""
    c = twin.crew(store.D(), line_id, _hour(at))
    if not c:
        raise ApiError(404, "NO_CREW", f"No crew recorded on '{line_id}' in that hour")
    return insights._clean(c)


@router.get("/twin/machine/{machine_id}")
def twin_machine(machine_id: str, at: str | None = Query(None, max_length=25), _: Principal = Depends(require("read"))):
    """Everything about one machine: condition, current supply, output per day/week/month/year, service dates, repairs, problems."""
    d = store.D()
    m = twin.machine(d, machine_id, _hour(at))
    if not m:
        raise ApiError(404, "MACHINE_NOT_FOUND", f"No machine '{machine_id}'")
    if m["supply"]:                                     # the inventory agent's view of the parts feeding it
        cover = {x["material_id"]: x for x in inventory_agent._view()["materials"]}
        for p in m["supply"]["parts"]:
            c = cover.get(p["material_id"], {})
            p.update({"free": c.get("free"), "cover_days": c.get("cover_days")})
    with SessionLocal() as s:
        inc = s.query(Incident).filter(Incident.line_id == m["line_id"]).order_by(Incident.started_at.desc()).limit(20).all()
        mine = [i for i in inc if i.machine_id in (None, machine_id)]
        fixed = next((i for i in mine if i.status == "RESOLVED"), None)
        m["last_repair"] = {"date": fixed.resolved_at, "fix": fixed.resolution, "by": fixed.resolved_by, "problem": fixed.description} if fixed else None
        m["open_problems"] = [{"id": i.id, "description": i.description, "started_at": i.started_at} for i in mine if i.status == "OPEN"]
    return insights._clean(m)


@router.get("/orders")
def list_orders(f: dict = Depends(filters), status: str | None = Query(None, max_length=30), only_open: bool = False, only_overdue: bool = False,
                q: str | None = Query(None, max_length=40), limit: int = Query(100, ge=1, le=500), _: Principal = Depends(require("read"))):
    """Customer orders for the order browser, narrowed by the same filters (plant/line do not apply to customer orders)."""
    d = store.D()
    co = insights.scope(d, f)["fact_customer_order"]
    start = (datetime.fromisoformat(d["fact_inventory_daily_snapshot"].snapshot_date.max()) + timedelta(days=1)).date().isoformat()
    x = co.assign(open_qty=co.order_qty - co.shipped_qty)
    x = x.assign(is_open=x.order_status.isin(Policy().open_statuses) & (x.open_qty > 0))
    if status:
        x = x[x.order_status == status]
    if only_open:
        x = x[x.is_open]
    if only_overdue:
        x = x[x.is_open & (x.confirmed_delivery_date < start)]
    if q:
        x = x[x.customer_order_id.str.contains(q.strip(), case=False, regex=False) | x.customer_id.str.contains(q.strip(), case=False, regex=False)]
    x = x.sort_values(["is_open", "confirmed_delivery_date"], ascending=[False, True])
    cols = ["customer_order_id", "customer_id", "product_id", "channel", "priority", "order_status", "order_date", "confirmed_delivery_date", "order_qty", "open_qty", "is_open"]
    return {"total": int(len(x)), "plan_start": start, "statuses": sorted(co.order_status.unique()),
            "rows": insights._clean(x[cols].head(limit).to_dict("records"))}


@router.get("/insights/detail/{key}")
def data_insight_detail(key: str, days: int = Query(30, ge=7, le=60), f: dict = Depends(filters), _: Principal = Depends(require("read"))):
    """Drill-through behind a dashboard card: sections of stats, series, bars and tables."""
    out = insights.detail(store.D(), key, days, f=f)
    if out is None:
        raise ApiError(404, "UNKNOWN_DETAIL", f"No detailed view called '{key}'", {"available": insights.DRILLS})
    return out


@router.get("/orders/{oid}")
def order_detail(oid: str, _: Principal = Depends(require("read"))):
    """One customer order from the data, with open quantity and days to/after its due date (relative to the plan start)."""
    d = store.D()
    co, inv = d["fact_customer_order"], d["fact_inventory_daily_snapshot"]
    r = co[co.customer_order_id == oid.strip().upper()]
    if r.empty:
        raise ApiError(404, "ORDER_NOT_FOUND", f"No order '{oid}'")
    x = r.iloc[0].to_dict()
    start = (datetime.fromisoformat(inv.snapshot_date.max()) + timedelta(days=1)).date()
    open_qty = int(x["order_qty"] - x["shipped_qty"])
    due = datetime.fromisoformat(x["confirmed_delivery_date"]).date()
    return insights._clean({**x, "open_qty": open_qty, "value_inr": x["order_qty"] * x["unit_price_inr"], "plan_start": start.isoformat(),
                            "days_to_due": (due - start).days, "is_open": x["order_status"] in Policy().open_statuses and open_qty > 0,
                            "confirmed_vs_requested_days": (due - datetime.fromisoformat(x["requested_delivery_date"]).date()).days})


@router.get("/reference")
def reference(_: Principal = Depends(require("read"))):
    """Lists the scenario form can offer (from the source data, not hard-coded in the browser)."""
    d = store.D()
    return {"products": sorted(d["dim_product_routing"].product_id.unique()), "lines": sorted(d["dim_machine"].line_id.unique()),
            "machines": d["dim_machine"][["machine_id", "machine_name", "line_id"]].to_dict("records"),
            "materials": sorted(d["fact_bom_component"].material_id.unique())}


# ------------------------------------------------------------------ generate / read
@router.post("/schedules", response_model=ScheduleSummary, status_code=201)
def generate(req: GenerateRequest, p: Principal = Depends(require("generate"))):
    pol = Policy(horizon_days=req.horizon_days)
    inp = data.to_inputs(store.D(), req.as_of, pol)
    s = planning.plan(inp, pol)
    store.put(s, actor=p.user, role=p.role, inp=inp)
    return _summary(s)


@router.get("/schedules", response_model=list[ScheduleSummary])
def list_schedules(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), _: Principal = Depends(require("read"))):
    return [ScheduleSummary(**{k: v for k, v in r.items() if k != "summary_json"}) for r in store.list_(limit, offset)]


@router.get("/schedules/{sid}", response_model=Schedule)
def get_schedule(sid: str, _: Principal = Depends(require("read"))):
    return _sched(sid)


@router.get("/schedules/{sid}/dispatch")
def dispatch(sid: str, _: Principal = Depends(require("read"))):
    return planning.dispatch_list(_sched(sid))


@router.get("/schedules/{sid}/material-calloff")
def calloff(sid: str, _: Principal = Depends(require("read"))):
    s = _sched(sid)
    return planning.material_calloff(store.inputs(s.id), s)


@router.get("/schedules/{sid}/capacity-gaps")
def gaps(sid: str, _: Principal = Depends(require("read"))):
    s = _sched(sid)
    return planning.capacity_gaps(s, store.inputs(s.id))


@router.get("/schedules/{sid}/audit", response_model=list[AuditEvent])
def audit_log(sid: str, _: Principal = Depends(require("read"))):
    _sched(sid)
    return store.events(sid)


# ------------------------------------------------------------------ scenarios
@router.post("/schedules/{sid}/scenarios", response_model=ScenarioResult, status_code=201)
def scenario(sid: str, sc: scenarios.Scenario, p: Principal = Depends(require("scenario"))):
    base = _sched(sid)
    try:
        s = scenarios.run(store.inputs(sid), base, sc, Policy(**base.policy))
    except ValueError as e:
        raise ApiError(422, "INVALID_SCENARIO", str(e))
    store.put(s, sid, sc, actor=p.user, role=p.role)
    return ScenarioResult(id=s.id, comparison=scenarios.compare(base, [s]))


@router.get("/compare")
def compare(base: str, ids: str = Query(..., description="comma-separated schedule ids"), _: Principal = Depends(require("read"))):
    return scenarios.compare(_sched(base), [_sched(i) for i in ids.split(",") if i])


@router.post("/schedules/{sid}/adopt", status_code=201)
def adopt_what_if(sid: str, p: Principal = Depends(require("scenario"))):
    """Add a what-if's change to the plan: the planning agent re-plans with it. The new plan waits for the production manager's approval."""
    from . import planning_agent
    try:
        return _plain(planning_agent.adopt(sid, p.user, p.role))
    except KeyError:
        raise ApiError(404, "SCHEDULE_NOT_FOUND", f"No schedule {sid}")
    except ValueError as e:
        raise ApiError(409, "NOT_ADOPTABLE", str(e))


# ------------------------------------------------------------------ approval (human only)
def _decide(sid: str, to: str, body: ApprovalRequest, p: Principal) -> DecisionResult:
    s = _sched(sid)
    if s.status != "PENDING_APPROVAL":
        raise ApiError(409, "INVALID_STATE", f"Schedule is {s.status}; only PENDING_APPROVAL schedules can be decided")
    rk = []
    if to == "APPROVED":
        if not s.assignments:
            raise ApiError(409, "NOTHING_TO_APPROVE", f"Schedule has no assignments (feasibility {s.feasibility}); there is nothing to release",
                           {"feasibility": s.feasibility, "summary": s.summary})
        rk = risks(s)
        if rk and not body.acknowledge_risks:
            raise ApiError(409, "RISK_ACK_REQUIRED", "Schedule has risks; review them and resubmit with acknowledge_risks=true", rk)
    if not store.set_status(sid, "PENDING_APPROVAL", to, p.user, body.note):
        raise ApiError(409, "INVALID_STATE", "Schedule was decided by someone else just now")
    store.audit(sid, p.user, p.role, to.lower(), {"note": body.note, "acknowledged_risks": rk})
    planning_agent.on_decision(sid, to, p.user)               # close / release / withdraw the agent's tasks for this plan
    if to == "APPROVED":                                        # its received orders are now frozen: a new upload can't change them
        from . import intake_flow, signoff
        intake_flow.freeze(sid, p.user)
        threading.Thread(target=signoff.email_plan, args=(sid,), name="plan-email", daemon=True).start()   # production heads and senior officials
    return DecisionResult(id=sid, status=to, decided_by=p.user, acknowledged_risks=rk)


@router.post("/schedules/{sid}/approve", response_model=DecisionResult)
def approve(sid: str, body: ApprovalRequest = ApprovalRequest(), p: Principal = Depends(require("approve"))):
    return _decide(sid, "APPROVED", body, p)


class OptionChoice(BaseModel):
    scenario: scenarios.Scenario
    note: str | None = Field(None, max_length=500)
    acknowledge_risks: bool = False


@router.post("/schedules/{sid}/approve-option", response_model=DecisionResult)
def approve_option(sid: str, body: OptionChoice, p: Principal = Depends(require("approve"))):
    """Approve one of the agent's other options instead of its plan: the option is planned again from the same inputs and saved as
    a what-if of the agent's plan, the agent's tasks move to it, it is approved (its orders freeze) and the agent's plan is closed."""
    base = _sched(sid)
    if base.status != "PENDING_APPROVAL":
        raise ApiError(409, "INVALID_STATE", f"Schedule is {base.status}; only PENDING_APPROVAL schedules can be decided")
    try:
        s = scenarios.run(store.inputs(sid), base, body.scenario, Policy(**base.policy))
    except ValueError as e:
        raise ApiError(422, "INVALID_SCENARIO", str(e))
    store.put(s, sid, body.scenario, actor=p.user, role=p.role)
    with SessionLocal() as db:
        for t in db.query(AgentTask).filter_by(schedule_id=sid):
            t.schedule_id = s.id
        db.commit()
    out = _decide(s.id, "APPROVED", ApprovalRequest(note=body.note, acknowledge_risks=body.acknowledge_risks), p)
    if store.set_status(sid, "PENDING_APPROVAL", "REJECTED", p.user, f"Option approved instead: {body.scenario.name}"):
        store.audit(sid, p.user, p.role, "rejected", {"note": f"Option approved instead: {body.scenario.name}", "option": s.id})
    return out


@router.post("/schedules/{sid}/reject", response_model=DecisionResult)
def reject(sid: str, body: ApprovalRequest = ApprovalRequest(), p: Principal = Depends(require("approve"))):
    return _decide(sid, "REJECTED", body, p)


# ------------------------------------------------------------------ approval stages: production manager, then each plant's production head
@router.get("/approved-plans")
def approved_plans(_: Principal = Depends(require("read"))):
    """Approved plans with their approval stages; fully approved ones have every stage done."""
    from . import signoff
    return _plain(signoff.approved())


@router.delete("/approvals/{sid}")
def delete_approval(sid: str, p: Principal = Depends(require("approve"))):
    """The production manager deletes a plan's approval from the Approvals history (Review & approve)."""
    from . import signoff
    try:
        return signoff.remove_approval(sid, p.user)
    except KeyError:
        raise ApiError(404, "SCHEDULE_NOT_FOUND", f"No schedule {sid}")
    except ValueError as e:
        raise ApiError(409, "NOT_APPROVED", str(e))


@router.get("/schedules/{sid}/stages")
def plan_stages(sid: str, _: Principal = Depends(require("read"))):
    from . import signoff
    return _plain(signoff.stages(_sched(sid)))


@router.post("/schedules/{sid}/plant-approve")
def plant_approve(sid: str, p: Principal = Depends(require("plant_approve"))):
    """The production head approves an approved plan for their plant. The last stage makes it fully approved and emails its details."""
    from . import signoff
    try:
        return _plain(signoff.approve_plant(sid, p.user, p.role, p.plant))
    except KeyError:
        raise ApiError(404, "SCHEDULE_NOT_FOUND", f"No schedule {sid}")
    except ValueError as e:
        raise ApiError(409, "STAGE_NOT_OPEN", str(e))


@router.post("/schedules/{sid}/email-plan")
def email_plan_again(sid: str, p: Principal = Depends(require("approve"))):
    """Send the approved plan's email again (for example once a mail server is set up). Production managers only."""
    from . import signoff
    if _sched(sid).status not in ("APPROVED", "PUBLISHED"):
        raise ApiError(409, "NOT_APPROVED", "The production manager must approve the plan before it is emailed")
    return signoff.email_plan(sid)


# ------------------------------------------------------------------ publication / export
@router.post("/schedules/{sid}/publish", response_model=DecisionResult)
def publish_schedule(sid: str, p: Principal = Depends(require("publish"))):
    s = _sched(sid)
    if s.status != "APPROVED":
        raise ApiError(409, "NOT_APPROVED", f"Schedule is {s.status}; only APPROVED schedules can be published")
    try:
        receipt = publish.get_publisher().publish(s, planning.dispatch_list(s))
    except IntegrationUnavailable as e:
        store.audit(sid, p.user, p.role, "publish_unavailable", {"code": e.code})
        raise ApiError(501, "PUBLISH_CONNECTOR_UNAVAILABLE", "No verified ERP/MES connector is available; the schedule was NOT published",
                       {"integration_code": e.code, "alternative": f"GET /api/schedules/{sid}/export"})
    if not store.set_status(sid, "APPROVED", "PUBLISHED", p.user):
        raise ApiError(409, "INVALID_STATE", "Schedule changed state during publication")
    store.audit(sid, p.user, p.role, "published", {"receipt": receipt})
    return DecisionResult(id=sid, status="PUBLISHED", decided_by=p.user)


@router.get("/schedules/{sid}/export")
def export(sid: str, p: Principal = Depends(require("export"))):
    """Dispatch list as CSV. Labelled export only: this is NOT publication to ERP/MES."""
    s = _sched(sid)
    rows = planning.dispatch_list(s)
    buf = io.StringIO()
    cols = ["approval_status", "plant_id", "line_id", "seq", "stage", "customer_order_id", "product_id", "qty", "start", "finish", "setup_min",
            "priority", "urgent", "frozen", "status", "reason_codes"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({**r, "approval_status": s.status, "reason_codes": "|".join(r["reason_codes"])})
    store.audit(sid, p.user, p.role, "exported", {"rows": len(rows), "approval_status": s.status})
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"X-Export-Label": "EXPORT-ONLY-NOT-PUBLISHED-TO-ERP-MES", "X-Approval-Status": s.status,
                             "Content-Disposition": f'attachment; filename="schedule-{sid}.csv"'})


# ------------------------------------------------------------------ planning tools (read-only; see planning_tools.py)
def _plain(x):
    """numpy numbers and timestamps -> JSON-safe values."""
    import json
    import numpy as np
    def conv(v):
        if isinstance(v, np.integer):
            return int(v)
        if isinstance(v, np.floating):
            return None if np.isnan(v) else float(v)
        if isinstance(v, np.bool_):
            return bool(v)
        return str(v)
    return json.loads(json.dumps(x, default=conv))


def _tool(fn, *a, **k):
    from . import planning_tools
    try:
        return _plain(getattr(planning_tools, fn)(*a, **k))
    except ValueError as e:
        raise ApiError(400, "BAD_REQUEST", str(e))
    except KeyError as e:
        raise ApiError(404, "SCHEDULE_NOT_FOUND", f"No schedule {e}")


@router.get("/plan-tools/promise")
def tool_promise(product_id: str = Query(..., max_length=40), qty: int = Query(..., gt=0, le=1_000_000),
                 due: str | None = Query(None, max_length=10), priority: str = Query("HIGH", pattern="^(HIGH|MEDIUM|LOW)$"),
                 _: Principal = Depends(require("read"))):
    from datetime import date
    try:
        d = date.fromisoformat(due) if due else None
    except ValueError:
        raise ApiError(400, "BAD_DATE", "due must be YYYY-MM-DD")
    return _tool("promise", product_id, qty, d, priority)


@router.get("/plan-tools/adherence")
def tool_adherence(_: Principal = Depends(require("read"))):
    return _tool("adherence")


@router.get("/plan-tools/maintenance")
def tool_maintenance(_: Principal = Depends(require("read"))):
    return _tool("maintenance")


@router.get("/plan-tools/changeovers")
def tool_changeovers(_: Principal = Depends(require("read"))):
    return _tool("changeovers")


@router.get("/plan-tools/materials")
def tool_mrp(_: Principal = Depends(require("read"))):
    return _tool("mrp")


@router.get("/plan-tools/forecast")
def tool_forecast(weeks: int = Query(12, ge=4, le=26), _: Principal = Depends(require("read"))):
    return _tool("forecast", weeks)


@router.get("/plan-tools/forecast-series")
def tool_forecast_series(section: str = Query("demand", pattern="^(demand|production|inventory|machinery|workforce)$"), weeks: int = Query(8, ge=4, le=16),
                         plant: str | None = Query(None, pattern="^(IN-TN-01|IN-KA-01)$"), _: Principal = Depends(require("read"))):
    """One section's weekly history and forecast, for the dashboard's forecast card."""
    return _tool("forecast_series", section, weeks, plant)


@router.get("/plan-tools/kpis")
def tool_kpis(schedule_id: str | None = Query(None, max_length=40), _: Principal = Depends(require("read"))):
    """The use case's seven success measures for one plan (default: the plan everyone works from)."""
    return _tool("kpis", schedule_id)


@router.get("/plan-tools/plant-brief")
@cache.route(60, ("data", "plans"), scoped=True)
def tool_plant_brief(schedule_id: str = Query(..., max_length=40), plant: str = Query(..., pattern="^(IN-TN-01|IN-KA-01)$"),
                     p: Principal = Depends(require("read"))):
    """One plant's plan in standard manufacturing terms (MPS, time fences, takt, utilisation, OEE, OTD, MRP, turns). A production head sees only their plant."""
    from . import plant_brief
    if p.role == "plant_head" and p.plant and plant != p.plant:
        raise ApiError(403, "OTHER_PLANT", "You can see your own plant's plan only")
    try:
        return _plain(plant_brief.build(schedule_id, plant))
    except KeyError:
        raise ApiError(404, "SCHEDULE_NOT_FOUND", f"No schedule {schedule_id}")


@router.get("/plan-tools/readiness")
def tool_readiness(schedule_id: str | None = Query(None, max_length=40), _: Principal = Depends(require("read"))):
    """Can the plan be built with the stock there is: room to build more, orders held back, daily kits, jobs at risk."""
    return _tool("readiness", schedule_id)


@router.get("/plan-tools/forecast-agent")
def tool_forecast_agent(section: str = Query("demand", pattern="^(demand|production|inventory|machinery|workforce)$"), weeks: int = Query(8, ge=4, le=16),
                        plant: str | None = Query(None, pattern="^(IN-TN-01|IN-KA-01)$"), _: Principal = Depends(require("read"))):
    """The forecast agent's forecast for one section: the method it chose by back-testing, its reasoning and what it means."""
    from . import forecast_agent
    return _plain(forecast_agent.run(section, weeks, plant))


@router.get("/plan-tools/crews")
@cache.route(60, ("data", "plans"))
def tool_crews(days: int = Query(14, ge=1, le=60), _: Principal = Depends(require("read"))):
    return _tool("crews", days)


@router.get("/plan-tools/yields")
def tool_yields(_: Principal = Depends(require("read"))):
    return _tool("yields")


@router.get("/plan-tools/costs")
def tool_costs(base: str | None = Query(None, max_length=40), _: Principal = Depends(require("read"))):
    """Cost of a plan and of its what-ifs (the newest plan when no base is given)."""
    if not base:
        rows = [r for r in store.list_(50) if not r["base_id"]]
        if not rows:
            return []
        base = rows[0]["id"]
    return _tool("costs", base)


@router.get("/plan-tools/sourcing")
def tool_sourcing(_: Principal = Depends(require("read"))):
    from . import sourcing_agent
    return _plain(sourcing_agent.analyse())


@router.get("/plan-tools/outlook")
def tool_outlook(weeks: int = Query(12, ge=4, le=26), _: Principal = Depends(require("read"))):
    """The weeks ahead: demand forecast against capacity, materials, and a dated list of what to do."""
    return _tool("outlook", weeks)

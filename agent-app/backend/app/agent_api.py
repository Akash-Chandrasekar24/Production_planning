"""Planning agent API (/api/agent) and order intake (/api/orders/intake).

Order intake is how new orders arrive (an ERP or web shop calls it, or a planner uploads a CSV). Each accepted batch joins the order
book and the agent checks right away whether to re-plan. Tasks are the agent's hand-over: the production manager reviews and
approves (on Review & approve), plant heads prepare their plant. Only the person in the task's role (and plant) can update it.
"""
import datetime as dt
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_
from . import incidents, intake_flow, notify, orchestrator, order_watcher as watcher, planning_agent as agent, store
from .auth import Principal, require
from .config import Policy
from .db import AgentRun, AgentTask, Incident, IntakeOrder, Notification, SessionLocal, TeamMember, _now
from .errors import ApiError

router = APIRouter(prefix="/api")
ROLES = {"production_manager": "Production manager", "plant_head": "Production head", "engineer": "Engineer", "planner": "Planner"}


# ------------------------------------------------------------------ order intake
class OrderIn(BaseModel):
    customer_order_id: str | None = Field(None, max_length=40)
    customer_id: str = Field(min_length=1, max_length=40)
    product_id: str = Field(min_length=1, max_length=40)
    order_qty: int = Field(gt=0, le=1_000_000)
    requested_delivery_date: dt.date
    confirmed_delivery_date: dt.date | None = None
    priority: str = "MEDIUM"
    channel: str = "B2B"
    unit_price_inr: float | None = Field(None, ge=0)

    @field_validator("priority")
    @classmethod
    def _prio(cls, v):
        v = v.strip().upper()
        if v not in ("HIGH", "MEDIUM", "LOW"):
            raise ValueError("priority must be HIGH, MEDIUM or LOW")
        return v


class IntakeIn(BaseModel):
    orders: list[OrderIn] = Field(min_length=1, max_length=2000)
    source: str = Field("api", max_length=40)


@router.post("/orders/intake", status_code=201)
def intake(body: IntakeIn, p: Principal = Depends(require("intake"))):
    """Receive new customer orders (an ERP or web shop calls this). All or nothing; then the agent flow runs on them straight away."""
    return _received([o.model_dump(mode="json") for o in body.orders], body.source, p, "API")


class UploadIn(BaseModel):
    filename: str = Field("orders.csv", max_length=200)
    csv: str = Field(min_length=1, max_length=2_000_000)


class SheetIn(BaseModel):
    filename: str = Field("orders.xlsx", max_length=200)
    data: str = Field(min_length=1, max_length=4_000_000)          # base64 of the .xlsx file


@router.post("/orders/sheet")
def sheet_to_csv(body: SheetIn, _: Principal = Depends(require("intake"))):
    """An Excel sheet (first sheet, header row first) as CSV text, so it can be previewed and uploaded like a CSV."""
    import base64, io
    import pandas as pd
    try:
        df = pd.read_excel(io.BytesIO(base64.b64decode(body.data)), sheet_name=0, dtype=str)
    except Exception:
        raise ApiError(422, "UNREADABLE_FILE", "The file could not be read as an Excel sheet (.xlsx)")
    df = df.dropna(how="all")
    for c in df.columns:                                           # Excel dates come back as '2026-12-01 00:00:00'
        df[c] = df[c].str.replace(r"^(\d{4}-\d{2}-\d{2}) 00:00:00$", r"\1", regex=True)
    return {"csv": df.to_csv(index=False), "rows": len(df)}


@router.post("/orders/upload", status_code=201)
def upload(body: UploadIn, p: Principal = Depends(require("intake"))):
    """A planner uploads a CSV of purchase orders (columns: po_number, customer_id, product_id, qty, due_date, priority, channel)."""
    try:
        orders = intake_flow.parse_csv(body.csv)
    except (intake_flow.Invalid, Exception) as e:                   # not a CSV the app can read
        msg = str(e) if isinstance(e, intake_flow.Invalid) else "The file could not be read as CSV"
        intake_flow.log_upload(p.user, p.role, body.filename, "UNREADABLE", message=msg)
        raise ApiError(422, "UNREADABLE_FILE", msg)
    if len(orders) > 2000:
        intake_flow.log_upload(p.user, p.role, body.filename, "REJECTED", orders, message="more than 2,000 orders")
        raise ApiError(422, "TOO_MANY_ORDERS", "Upload at most 2,000 orders at a time")
    try:
        ids, updated = intake_flow.receive(orders, "upload", p.user)
    except intake_flow.Invalid as e:
        intake_flow.log_upload(p.user, p.role, body.filename, "REJECTED", orders, problems=e.detail, message=str(e))
        raise ApiError(422, "INVALID_ORDERS", str(e), e.detail)
    store.audit(None, p.user, p.role, "orders_received", {"count": len(ids), "updated": len(updated), "source": "upload"})
    units = sum(int(float(o.get("order_qty", 0))) for o in orders)
    facts = [f"{len(ids)} order{'s' if len(ids) != 1 else ''} · {units:,} units" + (f" · {len(updated)} updated from an earlier upload" if updated else ""),
             "Products: " + ", ".join(sorted({o['product_id'] for o in orders})), "Every product, date, priority and channel checked"]
    jid = intake_flow.start(ids, p.user, body.filename, facts)        # the page follows the checks step by step
    intake_flow.log_upload(p.user, p.role, body.filename, "RECEIVED", orders, [i for i in ids if i not in updated], updated, job_id=jid)
    return {"received": len(ids), "updated": updated, "order_ids": ids, "job_id": jid}


@router.get("/orders/upload-log")
def upload_log(limit: int = Query(50, ge=1, le=500), _: Principal = Depends(require("read"))):
    """Who uploaded what and when, including refused uploads."""
    from .db import UploadLog
    with SessionLocal() as s:
        q = s.query(UploadLog).order_by(UploadLog.uploaded_at.desc()).limit(limit)
        return [{c: getattr(r, c) for c in ("id", "uploaded_at", "username", "role", "filename", "status", "orders", "units", "new_orders",
                                            "updated_orders", "message", "job_id")} for r in q]


@router.get("/orders/agent-ran")
def agent_ran(_: Principal = Depends(require("read"))):
    """Whether the planning agent has run since the app started (pages hide plans and warnings until it has)."""
    return intake_flow.ran()


@router.get("/orders/jobs/latest")
def latest_upload_job(_: Principal = Depends(require("read"))):
    """The latest upload run, for the review page (anyone can follow it and the production manager approves its plan)."""
    j = intake_flow.latest_job()
    if not j:
        raise ApiError(404, "JOB_NOT_FOUND", "No upload has been run yet")
    return j


@router.get("/orders/jobs/{jid}")
def upload_job(jid: str, _: Principal = Depends(require("read"))):
    j = intake_flow.job(jid)
    if not j:
        raise ApiError(404, "JOB_NOT_FOUND", "No such upload in progress (the server may have restarted)")
    return j


def _received(orders: list[dict], source: str, p: Principal, filename: str) -> dict:
    try:
        ids, _ = intake_flow.receive(orders, source, p.user)
    except intake_flow.Invalid as e:
        raise ApiError(422, "INVALID_ORDERS", str(e), e.detail)
    store.audit(None, p.user, p.role, "orders_received", {"count": len(ids), "source": source})
    flow = intake_flow.run(ids, p.user, filename)                    # every agent looks, each order is assessed, action only where needed
    return {"received": len(ids), "order_ids": ids, "agent": flow["decision"], "flow": flow}


@router.get("/orders/samples")
def samples(_: Principal = Depends(require("read"))):
    """Four test purchase orders from today's figures: two the plant absorbs, two that need the agents to act."""
    from fastapi.responses import Response
    return Response(intake_flow.samples(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="sample-purchase-orders.csv"'})


@router.get("/orders/uploads")
def upload_history(limit: int = Query(20, ge=1, le=100), _: Principal = Depends(require("read"))):
    return intake_flow.uploads(limit)


@router.get("/orders/intake")
def intake_list(limit: int = Query(100, ge=1, le=500), _: Principal = Depends(require("read"))):
    with SessionLocal() as s:
        q = s.query(IntakeOrder).order_by(IntakeOrder.received_at.desc()).limit(limit)
        return [{c: getattr(r, c) for c in ("customer_order_id", "customer_id", "product_id", "order_qty", "requested_delivery_date", "priority",
                                            "channel", "source", "received_by", "received_at")} for r in q]


# ------------------------------------------------------------------ agent
@router.get("/agent/status")
def status(_: Principal = Depends(require("read"))):
    pol = Policy()
    p = watcher.pending(pol=pol)
    with SessionLocal() as s:
        last = s.query(AgentRun).filter(AgentRun.outcome.in_(["PLANNED", "WATCHING"])).order_by(AgentRun.started_at.desc()).first()   # the order watcher's own decisions
        open_tasks = s.query(AgentTask).filter(AgentTask.status.in_(["OPEN", "ACKNOWLEDGED", "WAITING_APPROVAL"])).count()
        open_problems = s.query(Incident).filter_by(status="OPEN").count()
    go, why = watcher.reason(p, pol) if p["baseline"] else (False, "Not watching yet: the first check records the order book")
    return {"rules": {"min_new_orders": pol.agent_min_new_orders, "min_new_units": pol.agent_min_new_units, "urgent_days": pol.agent_urgent_days,
                      "interval_min": pol.agent_interval_min},
            "pending": {k: v for k, v in p.items() if not k.startswith("_")}, "would_plan": go, "why": why,
            "last_run": agent._run_out(last) if last else None, "open_tasks": open_tasks, "open_problems": open_problems,
            "problem_rules": {"fault_min": pol.agent_fault_min, "default_outage_h": pol.agent_default_outage_h,
                              "overtime_min": pol.agent_overtime_min, "overtime_days": pol.agent_overtime_days, "split_min_qty": pol.agent_split_min_qty}}


@router.post("/agent/check")
def check_now(agents: list[str] | None = Query(None), p: Principal = Depends(require("agent"))):
    """Run an orchestrator round now: the floor agent, inventory agent and order watcher look, the orchestrator routes what they
    find (re-plans only when the order thresholds are met). `agents` limits the round, e.g. ?agents=orders."""
    if agents and not set(agents) <= {"floor", "inventory", "orders", "sourcing"}:
        raise ApiError(422, "UNKNOWN_AGENT", "agents must be floor, inventory, orders or sourcing")
    return orchestrator.run("manual", p.user, agents)


@router.get("/agent/orchestrator")
def orchestrator_status(limit: int = Query(40, ge=1, le=200), _: Principal = Depends(require("read"))):
    """The agent team: what each agent last saw, and recent events with where the orchestrator sent them."""
    return orchestrator.status(limit)


@router.post("/agent/run", status_code=201)
def run_now(p: Principal = Depends(require("agent"))):
    """Ask the agent to plan now, whatever the thresholds say."""
    return agent.run_now(p.user, orchestrator.context())


@router.get("/agent/runs")
def runs(limit: int = Query(30, ge=1, le=200), _: Principal = Depends(require("read"))):
    with SessionLocal() as s:
        rs = s.query(AgentRun).order_by(AgentRun.started_at.desc()).limit(limit).all()
        counts = {r.id: s.query(AgentTask).filter_by(run_id=r.id).count() for r in rs}
        return [{**agent._run_out(r), "tasks": counts[r.id]} for r in rs]


@router.get("/agent/tasks")
def tasks(role: str | None = None, plant: str | None = None, status: str | None = None, mine: bool = False,
          p: Principal = Depends(require("read"))):
    """All tasks, or with mine=true the caller's inbox (their role, and for plant heads their plant)."""
    with SessionLocal() as s:
        q = s.query(AgentTask)
        if mine:
            q = q.filter(AgentTask.role == p.role)
            if p.role == "plant_head":
                q = q.filter(AgentTask.plant_id == p.plant)
        if role:
            q = q.filter(AgentTask.role == role)
        if plant:
            q = q.filter(or_(AgentTask.plant_id == plant, AgentTask.plant_id.is_(None)))
        if status:
            q = q.filter(AgentTask.status == status)
        return [agent._task_out(t) for t in q.order_by(AgentTask.created_at.desc()).limit(300)]


class TaskUpdate(BaseModel):
    note: str | None = Field(None, max_length=500)


def _update(tid: str, to: str, body: TaskUpdate, p: Principal):
    with SessionLocal() as s:
        t = s.get(AgentTask, tid)
        if not t:
            raise ApiError(404, "TASK_NOT_FOUND", f"No task '{tid}'")
        if t.role != p.role or (t.role == "plant_head" and t.plant_id != p.plant):
            raise ApiError(403, "NOT_YOUR_TASK", f"This task is for the {ROLES.get(t.role, t.role)}" + (f" of {t.plant_id}" if t.plant_id else ""))
        if t.kind == "approve" and to == "DONE":
            raise ApiError(409, "DECIDE_ON_REVIEW_PAGE", "Approve or reject the plan on Review & approve; this task closes itself")
        if t.kind == "repair" and to == "DONE":
            raise ApiError(409, "RECORD_THE_FIX", "Record how it was fixed on the problem; this task closes itself")
        allowed = {"ACKNOWLEDGED": ("OPEN",), "DONE": ("OPEN", "ACKNOWLEDGED")}[to]
        if t.status not in allowed:
            raise ApiError(409, "INVALID_STATE", f"Task is {t.status.lower().replace('_', ' ')}")
        t.status, t.note, t.updated_by = to, body.note or t.note, p.user
        s.commit()
        store.audit(t.schedule_id, p.user, p.role, f"task_{to.lower()}", {"task": t.id, "title": t.title, "note": body.note})
        return agent._task_out(t)


@router.post("/agent/tasks/{tid}/acknowledge")
def acknowledge(tid: str, body: TaskUpdate = TaskUpdate(), p: Principal = Depends(require("task"))):
    return _update(tid, "ACKNOWLEDGED", body, p)


@router.post("/agent/tasks/{tid}/done")
def done(tid: str, body: TaskUpdate = TaskUpdate(), p: Principal = Depends(require("task"))):
    return _update(tid, "DONE", body, p)


# ------------------------------------------------------------------ problems on the floor (machine stops, late production orders)
class IncidentIn(BaseModel):
    kind: str                                            # MACHINE_DOWN | PO_DELAY
    line_id: str | None = Field(None, max_length=20)
    machine_id: str | None = Field(None, max_length=20)
    started_at: dt.datetime | None = None                # default: now
    repair_hours: float | None = Field(None, gt=0, le=24 * 30)
    production_orders: list[str] = Field([], max_length=200)
    note: str | None = Field(None, max_length=500)


def _naive(t: dt.datetime | None) -> dt.datetime:
    t = t or dt.datetime.now(dt.timezone.utc)
    return t.astimezone(dt.timezone.utc).replace(tzinfo=None) if t.tzinfo else t


@router.get("/agent/incidents")
def incident_list(status: str | None = None, _: Principal = Depends(require("read"))):
    d = store.D()
    lines = d["fact_hourly_production"].groupby("line_id").plant_id.first().to_dict()
    m = d["dim_machine"]
    po = d["fact_production_order"]
    return {"incidents": incidents.list_(status),
            "lines": [{"line_id": k, "plant_id": v} for k, v in sorted(lines.items())],
            "machines": [{"machine_id": r.machine_id, "name": f"{r.machine_name} ({r.machine_type})", "line_id": r.line_id} for r in m.itertuples()],
            "open_production_orders": sorted(po[po.order_status.isin(Policy().open_po_statuses)].production_order_id)}


@router.get("/agent/incidents/{iid}")
def incident_get(iid: str, _: Principal = Depends(require("read"))):
    try:
        return incidents.get(iid)
    except incidents.NotFound:
        raise ApiError(404, "INCIDENT_NOT_FOUND", f"No problem '{iid}'")


@router.post("/agent/incidents", status_code=201)
def incident_report(body: IncidentIn, p: Principal = Depends(require("incident"))):
    """Report a problem (people, or a maintenance/MES system). The agent analyses it straight away and hands over the work."""
    d = store.D()
    lines = d["fact_hourly_production"].groupby("line_id").plant_id.first().to_dict()
    start = _naive(body.started_at)
    note = (body.note or "").strip()
    if body.kind == "MACHINE_DOWN":
        mline = d["dim_machine"].set_index("machine_id").line_id.to_dict()
        if body.machine_id and body.machine_id not in mline:
            raise ApiError(422, "UNKNOWN_MACHINE", f"No machine '{body.machine_id}'")
        line = mline.get(body.machine_id) if body.machine_id else body.line_id
        if line not in lines:
            raise ApiError(422, "UNKNOWN_LINE", f"Give a machine or one of the lines: {', '.join(sorted(lines))}")
        plant = lines[line]
        if p.role == "plant_head" and plant != p.plant:
            raise ApiError(403, "NOT_YOUR_PLANT", f"{line} is in {plant}")
        what = body.machine_id or line
        desc = f"{p.user} reported {what} stopped" + (f": {note}" if note else "")
        end = (start + dt.timedelta(hours=body.repair_hours)).isoformat() if body.repair_hours else None
        return _reported({"type": "floor.reported", "severity": "critical", "key": f"MACHINE_DOWN:{line}:{start.isoformat()}", "title": desc,
                          "plant_id": plant, "line_id": line, "detail": {"kind": "MACHINE_DOWN", "machine_id": body.machine_id,
                                                                          "started_at": start.isoformat(), "expected_end": end}}, p)
    if body.kind == "PO_DELAY":
        po = d["fact_production_order"].set_index("production_order_id")
        ids = sorted({x.strip().upper() for x in body.production_orders if x.strip()})
        bad = [x for x in ids if x not in po.index]
        if not ids or bad:
            raise ApiError(422, "UNKNOWN_PRODUCTION_ORDER", f"Unknown production orders: {', '.join(bad)}" if bad else "Give at least one production order")
        plants = set(po.loc[ids].plant_id)
        if len(plants) > 1:
            raise ApiError(422, "SEVERAL_PLANTS", "Report production orders of one plant at a time")
        plant = plants.pop()
        if p.role == "plant_head" and plant != p.plant:
            raise ApiError(403, "NOT_YOUR_PLANT", f"These production orders are in {plant}")
        desc = f"{p.user} reported {len(ids)} production order{'s' if len(ids) != 1 else ''} running late" + (f": {note}" if note else "")
        return _reported({"type": "floor.reported", "severity": "critical", "key": f"PO_DELAY:{','.join(ids)}"[:200], "title": desc, "plant_id": plant,
                          "detail": {"kind": "PO_DELAY", "production_orders": ids, "started_at": start.isoformat()}}, p)
    raise ApiError(422, "UNKNOWN_KIND", "kind must be MACHINE_DOWN or PO_DELAY")


def _reported(sig: dict, p: Principal) -> dict:
    """A person's report goes to the orchestrator like any other signal; it sends it to the planning agent for a recovery plan."""
    res = orchestrator.submit({**sig, "agent": f"reported by {p.user}"}, p.user)
    if not res.get("incident"):
        raise ApiError(500, "NOT_ANALYSED", res["event"]["outcome"] or "The problem could not be analysed")
    return res["incident"]


class ChooseIn(BaseModel):
    option: str = Field(max_length=20)


@router.post("/agent/incidents/{iid}/choose")
def incident_choose(iid: str, body: ChooseIn, p: Principal = Depends(require("agent"))):
    """Use another recovery option: it replaces the agent's proposal and goes to approval."""
    try:
        return incidents.choose(iid, body.option, p.user)
    except incidents.NotFound:
        raise ApiError(404, "INCIDENT_NOT_FOUND", f"No problem '{iid}'")
    except ValueError as e:
        raise ApiError(409, "CANNOT_CHOOSE", str(e))


class ResolveIn(BaseModel):
    fix: str = Field(min_length=3, max_length=1000)
    repair_hours: float | None = Field(None, gt=0, le=24 * 30)


@router.post("/agent/incidents/{iid}/resolve")
def incident_resolve(iid: str, body: ResolveIn, p: Principal = Depends(require("incident"))):
    """Record how it was fixed. The fix is shown the next time a similar problem comes up."""
    try:
        cur = incidents.get(iid)
        if p.role == "plant_head" and cur["plant_id"] != p.plant:
            raise ApiError(403, "NOT_YOUR_PLANT", f"This problem is in {cur['plant_id']}")
        return incidents.resolve(iid, body.fix, p.user, body.repair_hours)
    except incidents.NotFound:
        raise ApiError(404, "INCIDENT_NOT_FOUND", f"No problem '{iid}'")
    except ValueError as e:
        raise ApiError(409, "ALREADY_FIXED", str(e))


@router.post("/agent/detect")
def detect_now(p: Principal = Depends(require("agent"))):
    """Look at the production feed and production orders now."""
    return {"raised": orchestrator.run("manual", p.user, ["floor"])["incidents"]}


# ------------------------------------------------------------------ notifications (the bell) and email
INBOX = {"engineer": ("engineer", "planner")}          # engineers do the planner's work, so they get the planner's notifications too


_named_cache: dict = {}


def _named(s, role: str, user: str) -> bool:
    """Is this person on the team list under this role? Asked once a minute, not on every bell refresh."""
    import time
    k, now = (role, user), time.monotonic()
    hit = _named_cache.get(k)
    if hit and hit[0] > now:
        return hit[1]
    v = bool(s.query(TeamMember.id).filter_by(role=role, name=user).first())
    _named_cache[k] = (now + 60, v)
    return v


def _mine(q, p: Principal, s):
    q = q.filter(Notification.role.in_(INBOX.get(p.role, (p.role,))))
    if p.role == "plant_head":
        q = q.filter(Notification.plant_id == p.plant)
    if _named(s, p.role, p.user):                                                  # a named person sees their own copies
        q = q.filter(or_(Notification.member == p.user, Notification.member.is_(None)))
    return q


@router.get("/notifications")
def notifications(limit: int = Query(30, ge=1, le=200), p: Principal = Depends(require("read"))):
    """My notifications (my role, and plant for plant heads), newest first, with the unread count."""
    with SessionLocal() as s:
        rows = _mine(s.query(Notification), p, s).order_by(Notification.created_at.desc()).limit(limit * 3).all()
        seen, out = set(), []
        for n in rows:                                                # one per task when the role has several copies
            k = n.task_id or n.id
            if k not in seen:
                seen.add(k)
                out.append(notify.out(n))
        return {"items": out[:limit], "unread": sum(not x["read"] for x in out), "email": notify.configured()}


@router.post("/notifications/{nid}/read")
def notification_read(nid: str, p: Principal = Depends(require("read"))):
    with SessionLocal() as s:
        n = s.get(Notification, nid)
        if not n or n.role not in INBOX.get(p.role, (p.role,)):
            raise ApiError(404, "NOTIFICATION_NOT_FOUND", "No such notification")
        for x in s.query(Notification).filter(Notification.task_id == n.task_id, Notification.role.in_(INBOX.get(p.role, (p.role,)))) if n.task_id else [n]:
            x.read_at = x.read_at or _now()
        s.commit()
        return {"ok": True}


@router.post("/notifications/read-all")
def notifications_read_all(p: Principal = Depends(require("read"))):
    with SessionLocal() as s:
        n = 0
        for x in _mine(s.query(Notification).filter(Notification.read_at.is_(None)), p, s):
            x.read_at, n = _now(), n + 1
        s.commit()
        return {"read": n}


@router.post("/agent/team/{mid}/test-email")
def test_email(mid: str, p: Principal = Depends(require("team"))):
    """Send a test email to a team member (admins). Says why when it cannot."""
    with SessionLocal() as s:
        m = s.get(TeamMember, mid)
        if not m:
            raise ApiError(404, "MEMBER_NOT_FOUND", "No such team member")
        if not m.email:
            raise ApiError(422, "NO_EMAIL", f"{m.name} has no email address")
        name, to = m.name, m.email
    r = notify.test_email(to, name)
    if r == "NOT_CONFIGURED":
        raise ApiError(409, "EMAIL_NOT_CONFIGURED", "Email is not set up on the server yet (mail server settings)")
    if r != "SENT":
        raise ApiError(502, "EMAIL_FAILED", f"The mail server did not accept it ({r})")
    store.audit(None, p.user, p.role, "test_email_sent", {"member": name})
    return {"sent": True}


# ------------------------------------------------------------------ team: who holds each role
class MemberIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    role: str
    plant_id: str | None = Field(None, max_length=20)
    email: str | None = Field(None, max_length=200)


@router.get("/agent/team")
def team(_: Principal = Depends(require("read"))):
    with SessionLocal() as s:
        return [{"id": t.id, "name": t.name, "role": t.role, "role_label": ROLES.get(t.role, t.role), "plant_id": t.plant_id, "email": t.email}
                for t in s.query(TeamMember).order_by(TeamMember.role, TeamMember.plant_id)]


@router.post("/agent/team", status_code=201)
def add_member(body: MemberIn, p: Principal = Depends(require("team"))):
    if body.role not in ROLES:
        raise ApiError(422, "UNKNOWN_ROLE", f"Role must be one of {', '.join(ROLES)}")
    plants = set(store.D()["fact_hourly_production"].plant_id)
    if body.role == "plant_head" and body.plant_id not in plants:
        raise ApiError(422, "UNKNOWN_PLANT", f"A plant head needs one of the plants: {', '.join(sorted(plants))}")
    with SessionLocal() as s:
        m = TeamMember(name=body.name.strip(), role=body.role, plant_id=body.plant_id if body.role == "plant_head" else None, email=body.email)
        s.add(m)
        # open tasks for that role (and plant) without a named person get this person
        q = s.query(AgentTask).filter(AgentTask.role == m.role, AgentTask.assignee.is_(None), AgentTask.status.in_(["OPEN", "WAITING_APPROVAL", "ACKNOWLEDGED"]))
        if m.plant_id:
            q = q.filter(AgentTask.plant_id == m.plant_id)
        for t in q:
            t.assignee = m.name
        s.commit()
        store.audit(None, p.user, p.role, "team_member_added", {"name": m.name, "role": m.role, "plant": m.plant_id})
        return {"id": m.id, "name": m.name, "role": m.role, "plant_id": m.plant_id}


@router.delete("/agent/team/{mid}", status_code=204)
def remove_member(mid: str, p: Principal = Depends(require("team"))):
    with SessionLocal() as s:
        m = s.get(TeamMember, mid)
        if m:
            s.delete(m)
            s.commit()
            store.audit(None, p.user, p.role, "team_member_removed", {"name": m.name, "role": m.role})

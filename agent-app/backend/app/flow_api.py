"""Endpoints for the planning stages around the plan itself: demand (forecast review, production budget, rough-cut capacity), the plan
document, execution (board, comments and risk log) and the scheduled reports."""
import asyncio
import json
import time
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from . import cache, budget, execution, plan_document, reports
from .auth import Principal, require
from .db import Notification, ReportLog, ScheduleJob, SessionLocal
from .errors import ApiError
from .planning_api import _plain

router = APIRouter(prefix="/api")
PLANT_RE = "^(IN-TN-01|IN-KA-01)$"


def _plan(fn, sid):
    try:
        return _plain(fn(sid))
    except KeyError:
        raise ApiError(404, "SCHEDULE_NOT_FOUND", f"No schedule {sid}")


# ---------------------------------------------------------------- 1 demand
@router.get("/demand/forecast")
@cache.route(60, ("data", "plans"))
def get_forecast(schedule_id: str = Query(..., max_length=40), _: Principal = Depends(require("read"))):
    return _plan(budget.forecast_review, schedule_id)


class ForecastSet(BaseModel):
    product_id: str = Field(max_length=40)
    week: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    qty: int | None = Field(None, ge=0, le=10_000_000)            # None = back to the model's forecast
    note: str | None = Field(None, max_length=300)


@router.put("/demand/forecast")
def put_forecast(body: ForecastSet, p: Principal = Depends(require("forecast"))):
    budget.set_forecast(body.product_id, body.week, body.qty, body.note, p.user)
    return {"ok": True}


@router.get("/demand/budget")
@cache.route(60, ("data", "plans"))
def get_budget(schedule_id: str = Query(..., max_length=40), _: Principal = Depends(require("read"))):
    return _plan(budget.production_budget, schedule_id)


@router.get("/demand/rough-cut")
@cache.route(60, ("data", "plans"))
def get_rough_cut(schedule_id: str = Query(..., max_length=40), _: Principal = Depends(require("read"))):
    return _plan(budget.rough_cut, schedule_id)


@router.get("/plan-cost/{sid}")
def get_cost(sid: str, _: Principal = Depends(require("read"))):
    """The plan's cost estimate: material, labour, line time, changeovers, overtime, late penalties and carrying cost."""
    from . import planning_tools
    return _plan(planning_tools.cost, sid)


# ---------------------------------------------------------------- 2 the demand and capacity gate
@router.get("/volume")
def get_volume(_: Principal = Depends(require("read"))):
    from . import volume
    return _plain(volume.state())


class VolumeAgree(BaseModel):
    option: str = Field(pattern="^(commit|overtime|keep)$")


@router.post("/volume/{vid}/agree")
def agree_volume(vid: str, body: VolumeAgree, p: Principal = Depends(require("approve"))):
    """The production manager's decision; the agent then makes the plan with it."""
    from . import planning_agent, volume
    try:
        v = volume.agree(vid, body.option, p.user)
    except LookupError:
        raise ApiError(404, "VOLUME_PLAN_NOT_FOUND", "No such volume plan")
    except ValueError as e:
        raise ApiError(409, "VOLUME_NOT_OPEN", str(e))
    run = planning_agent.replan("volume_agreed", f"{p.user} agreed the volume plan: {volume.LABEL[body.option]}", p.user)
    return _plain({"volume": volume.out(v), "plan": {k: run.get(k) for k in ("id", "outcome", "schedule_id", "volume_plan_id")}})


# ---------------------------------------------------------------- a page that failed to draw in someone's browser: keep the error in the log
class ClientError(BaseModel):
    page: str | None = Field(None, max_length=80)
    message: str = Field(max_length=500)
    stack: str | None = Field(None, max_length=4000)


@router.post("/client-error", status_code=204)
def client_error(body: ClientError, p: Principal = Depends(require("read"))):
    import logging
    logging.getLogger("planner.ui").warning("page %r failed to draw for %s (%s): %s\n%s", body.page, p.user, p.role, body.message, body.stack or "")


# ---------------------------------------------------------------- 4 plan options: the agent's five plans, the production manager chooses one
@router.get("/plan-sets/latest")
def plan_set_latest(_: Principal = Depends(require("read"))):
    from . import plan_sets
    return plan_sets.latest()


@router.get("/plan-sets")
def plan_set_history(limit: int = Query(20, ge=1, le=100), _: Principal = Depends(require("read"))):
    from . import plan_sets
    return plan_sets.history(limit)


@router.get("/plan-sets/{pid}")
def plan_set(pid: str, _: Principal = Depends(require("read"))):
    from . import plan_sets
    ps = plan_sets.get(pid)
    if ps is None:
        raise ApiError(404, "PLAN_SET_NOT_FOUND", "No such set of plans")
    return ps


class PlanChoice(BaseModel):
    schedule_id: str
    note: str | None = Field(None, max_length=500)
    acknowledge_risks: bool = False


@router.post("/plan-sets/{pid}/choose")
def plan_set_choose(pid: str, body: PlanChoice, p: Principal = Depends(require("approve"))):
    """Choose one of the five plans: it is approved (as on Review & approve), the other four are closed."""
    from . import plan_sets
    from .planning_api import _decide
    from .schemas import ApprovalRequest
    try:
        out = plan_sets.choose(pid, body.schedule_id, p.user, p.role,
                               lambda sid: _decide(sid, "APPROVED", ApprovalRequest(note=body.note, acknowledge_risks=body.acknowledge_risks), p))
    except LookupError:
        raise ApiError(404, "PLAN_SET_NOT_FOUND", "No such set of plans")
    except ValueError as e:
        raise ApiError(409, "PLAN_SET_NOT_OPEN", str(e))
    return _plain(out)


# ---------------------------------------------------------------- 6 the plan document
@router.get("/plan-document/{sid}")
@cache.route(60, ("data", "plans"))
def get_document(sid: str, _: Principal = Depends(require("read"))):
    return _plan(plan_document.build, sid)


# ---------------------------------------------------------------- 5 execute and control
@router.get("/execution")
@cache.route(30, ("data", "plans", "exec"), scoped=True)
def get_board(plant: str = Query(..., pattern=PLANT_RE), day: str | None = Query(None, max_length=10), p: Principal = Depends(require("read"))):
    if p.role == "plant_head" and p.plant and plant != p.plant:
        raise ApiError(403, "OTHER_PLANT", "You can see your own plant only")
    return _plain(execution.board(plant, day))


class JobUpdate(BaseModel):
    order_id: str = Field(max_length=60)
    stage: str = Field(pattern="^(SMT|FAL)$")
    status: str = Field(pattern="^(TO_START|RUNNING|DONE|BLOCKED)$")
    note: str | None = Field(None, max_length=500)


@router.post("/execution/jobs")
def post_job(body: JobUpdate, p: Principal = Depends(require("task"))):
    try:
        return execution.update(body.order_id, body.stage, body.status, body.note, p.user, p.role, p.plant)
    except PermissionError as e:
        raise ApiError(403, "OTHER_PLANT", str(e))
    except LookupError as e:
        raise ApiError(404, "JOB_NOT_FOUND", str(e))
    except ValueError as e:
        raise ApiError(400, "BAD_REQUEST", str(e))


@router.get("/schedules/{sid}/notes")
def get_notes(sid: str, _: Principal = Depends(require("read"))):
    return _plain(execution.notes(sid))


class NoteIn(BaseModel):
    kind: str = Field(pattern="^(comment|risk)$")
    text: str = Field(min_length=1, max_length=2000)
    severity: str | None = Field(None, pattern="^(low|medium|high)$")
    owner: str | None = Field(None, max_length=100)


@router.post("/schedules/{sid}/notes")
def post_note(sid: str, body: NoteIn, p: Principal = Depends(require("read"))):       # everyone signed in may comment or log a risk
    try:
        return _plain(execution.add_note(sid, body.kind, body.text, body.severity, body.owner, p.user))
    except LookupError:
        raise ApiError(404, "SCHEDULE_NOT_FOUND", f"No schedule {sid}")
    except ValueError as e:
        raise ApiError(400, "BAD_REQUEST", str(e))


@router.post("/notes/{nid}/close")
def close_note(nid: str, p: Principal = Depends(require("read"))):
    try:
        return _plain(execution.close_risk(nid, p.user))
    except LookupError:
        raise ApiError(404, "NOTE_NOT_FOUND", "No such risk")


# ---------------------------------------------------------------- reports
def _report(kind: str, plant: str | None):
    if kind == "daily":
        if not plant:
            raise ApiError(400, "PLANT_REQUIRED", "A daily brief is for one plant")
        return reports.daily(plant)
    return reports.weekly()


@router.get("/reports/preview")
def preview(kind: str = Query(..., pattern="^(daily|weekly)$"), plant: str | None = Query(None, pattern=PLANT_RE), _: Principal = Depends(require("read"))):
    r = _report(kind, plant)
    return _plain({**r, "subject": reports.html_of(r)[0]})


class ReportSend(BaseModel):
    kind: str = Field(pattern="^(daily|weekly)$")
    plant: str | None = Field(None, pattern=PLANT_RE)
    to: str | None = Field(None, max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")   # set = a preview to this address only


@router.post("/reports/send")
def send_report(body: ReportSend, _: Principal = Depends(require("report"))):
    return reports.send(_report(body.kind, body.plant), body.to)


@router.get("/reports/log")
def report_log(_: Principal = Depends(require("read"))):
    with SessionLocal() as db:
        return _plain([{"key": r.key, "kind": r.kind, "detail": r.detail, "sent_at": r.sent_at} for r in db.query(ReportLog).order_by(ReportLog.sent_at.desc()).limit(30)])


# ---------------------------------------------------------------- work across plans (schedule_jobs: one row per job of every plan)
@router.get("/plan-jobs")
def plan_jobs(line_id: str | None = Query(None, max_length=20), plant: str | None = Query(None, pattern=PLANT_RE),
              start: datetime | None = None, end: datetime | None = None, schedule_id: str | None = Query(None, max_length=40),
              status: str | None = Query(None, pattern="^(ON_TIME|LATE_RISK)$"), limit: int = Query(500, ge=1, le=5000),
              p: Principal = Depends(require("read"))):
    """Jobs by line, plant, time window or plan, across every plan (indexed rows, not plan documents). A production head sees their plant."""
    if p.role == "plant_head" and p.plant:
        if plant and plant != p.plant:
            raise ApiError(403, "OTHER_PLANT", "You can see your own plant only")
        plant = p.plant
    with SessionLocal() as s:
        q = s.query(ScheduleJob)
        for col, v in ((ScheduleJob.line_id, line_id), (ScheduleJob.plant_id, plant), (ScheduleJob.schedule_id, schedule_id), (ScheduleJob.status, status)):
            if v:
                q = q.filter(col == v)
        if start:
            q = q.filter(ScheduleJob.finish > start)
        if end:
            q = q.filter(ScheduleJob.start < end)
        rows = q.order_by(ScheduleJob.start).limit(limit).all()
        cols = ("schedule_id", "order_id", "stage", "plant_id", "line_id", "product_id", "qty", "start", "finish", "priority", "status", "frozen")
        return _plain([{c: getattr(r, c) for c in cols} for r in rows])


# ---------------------------------------------------------------- live updates: one connection instead of the page polling
def _snapshot(p: Principal, job_id: str | None) -> dict:
    from . import intake_flow, jobs
    from .agent_api import _mine
    with SessionLocal() as s:
        rows = _mine(s.query(Notification.id, Notification.task_id, Notification.read_at), p, s).order_by(Notification.created_at.desc()).limit(90).all()
    seen, unread, newest = set(), 0, rows[0][0] if rows else None
    for nid, tid, read in rows:
        k = tid or nid
        if k not in seen:
            seen.add(k)
            unread += read is None
    out = {"unread": unread, "newest": newest, "ran": intake_flow.ran()["ran"]}
    if job_id:
        j = jobs.get(job_id)
        out["job"] = {"status": j.get("status"), "steps": {k: v.get("state") for k, v in (j.get("steps") or {}).items()},
                      "thoughts": sum(len(v.get("thoughts") or []) for v in (j.get("steps") or {}).values())} if j else None
    return out


EOM = chr(10) * 2                                          # an event ends with a blank line


@router.get("/stream")
async def stream(request: Request, job: str | None = Query(None, max_length=20), p: Principal = Depends(require("read"))):
    """Server-sent events: a message whenever my unread count, the agent's status or the followed job changes (checked every 2 s),
    a heartbeat every 15 s. The connection ends after 10 minutes; the page reconnects."""
    async def gen():
        last, beat, t_end = None, time.monotonic(), time.monotonic() + 600
        yield "retry: 3000" + EOM
        while time.monotonic() < t_end and not await request.is_disconnected():
            try:
                snap = await run_in_threadpool(_snapshot, p, job)
            except Exception:
                snap = None
            if snap is not None and snap != last:
                last = snap
                yield "data: " + json.dumps(snap, default=str) + EOM
            elif time.monotonic() - beat > 15:
                yield ": keep-alive" + EOM
                beat = time.monotonic()
            await asyncio.sleep(2)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- Overview: what the agentic flow solves, delivery heatmap
@router.get("/overview/solutions")
@cache.route(60, ("data", "plans"))
def overview_solutions(_: Principal = Depends(require("read"))):
    from . import overview
    return _plain(overview.solutions())


@router.get("/overview/line-load")
@cache.route(60, ("data", "plans"))
def overview_line_load(_: Principal = Depends(require("read"))):
    from . import overview
    return _plain(overview.line_load())


@router.get("/overview/delivery-heatmap")
@cache.route(60, ("data", "plans"))
def overview_heatmap(_: Principal = Depends(require("read"))):
    from . import overview
    return _plain(overview.heatmap())


# ---------------------------------------------------------------- the final production plan (one page)
@router.get("/final-plan/{sid}")
@cache.route(60, ("data", "plans", "notes"))
def get_final_plan(sid: str, _: Principal = Depends(require("read"))):
    from . import final_plan
    return _plan(final_plan.build, sid)

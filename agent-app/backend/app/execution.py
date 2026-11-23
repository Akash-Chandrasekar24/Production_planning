"""Execute and control: the execution board (each job of the plan in use, by plant and day: to start, running, done, blocked) and the
comments and risk log on a plan. A blocked job tells the production manager and the plant's production head straight away."""
from datetime import date

from . import incidents, planning, store
from .db import AgentRun, AgentTask, JobProgress, PlanNote, SessionLocal

STATUSES = ("TO_START", "RUNNING", "DONE", "BLOCKED")


def board(plant: str, day: str | None = None) -> dict:
    s, _ = incidents._base()                                      # the plan everyone works from
    rows = [r for r in planning.dispatch_list(s) if r["plant_id"] == plant]
    days = sorted({r["start"].date().isoformat() for r in rows})
    if not days:
        return {"schedule_id": s.id, "schedule_name": s.name, "plant_id": plant, "days": [], "day": None, "jobs": [], "counts": {}}
    today = date.today().isoformat()
    day = day if day in days else next((d for d in days if d >= today), days[0])
    with SessionLocal() as db:
        prog = {(p.order_id, p.stage): p for p in db.query(JobProgress).filter_by(schedule_id=s.id, plant_id=plant)}
    jobs = []
    for r in rows:
        if r["start"].date().isoformat() != day:
            continue
        p = prog.get((r["customer_order_id"], r["stage"]))
        jobs.append({**{k: r[k] for k in ("line_id", "seq", "stage", "customer_order_id", "product_id", "qty", "start", "finish", "priority", "status")},
                     "waits_for_parts": any(c.startswith("WAITS_FOR_PURCHASE") for c in r["reason_codes"]),
                     "forecast": r["customer_order_id"].startswith("FC-"),
                     "progress": p.status if p else "TO_START", "note": p.note if p else None, "by": p.updated_by if p else None,
                     "at": p.updated_at if p else None})
    jobs.sort(key=lambda j: (j["line_id"], j["start"]))
    counts = {k: sum(j["progress"] == k for j in jobs) for k in STATUSES}
    blocked_all = sum(p.status == "BLOCKED" for p in prog.values())
    return {"schedule_id": s.id, "schedule_name": s.name, "status": s.status, "plant_id": plant, "days": days, "day": day,
            "jobs": jobs, "counts": counts, "blocked_in_plan": blocked_all}


def update(order_id: str, stage: str, status: str, note: str | None, user: str, role: str, plant: str | None) -> dict:
    """Move a job on the board. Only the plan in use; a production head only for their plant. BLOCKED needs a reason and raises a task."""
    if status not in STATUSES:
        raise ValueError("Status must be to start, running, done or blocked")
    s, _ = incidents._base()
    job = next((a for a in s.assignments if a.customer_order_id == order_id), None)
    if job is None or stage not in ("SMT", "FAL"):
        raise LookupError(f"{order_id} is not in the plan in use")
    if role == "plant_head" and plant and job.plant_id != plant:
        raise PermissionError("That job belongs to the other plant")
    if status == "BLOCKED" and not (note or "").strip():
        raise ValueError("Say what is blocking the job")
    with SessionLocal() as db:
        p = db.get(JobProgress, (s.id, order_id, stage))
        was = p.status if p else "TO_START"
        if p:
            p.status, p.note, p.updated_by = status, note, user
        else:
            db.add(JobProgress(schedule_id=s.id, order_id=order_id, stage=stage, plant_id=job.plant_id, status=status, note=note, updated_by=user))
        if status == "BLOCKED" and was != "BLOCKED":            # the people who can unblock it hear about it now (bell, and email if set up)
            run = AgentRun(trigger="job_blocked", outcome="ALERT", reason=f"{order_id} ({stage}) blocked at {job.plant_id}: {note}",
                           schedule_id=s.id, details={}, open_orders=[], actor=user)
            db.add(run)
            db.flush()
            line = job.smt_line if stage == "SMT" else job.fal_line
            for r, pl in (("production_manager", None), ("plant_head", job.plant_id)):
                db.add(AgentTask(run_id=run.id, schedule_id=s.id, role=r, plant_id=pl, kind="alert", status="OPEN",
                                 title=f"Job blocked: {order_id} on {line}"[:200],
                                 details={"agent": "execution", "order": order_id, "severity": "warn", "type": "job_blocked", "reason": note}))
        db.commit()
    store.audit(s.id, user, role, "job_progress", {"order": order_id, "stage": stage, "status": status, "note": note})
    from . import coord
    coord.changed("exec")                                          # every process shows the move at once
    return {"order_id": order_id, "stage": stage, "status": status}


# ---------------------------------------------------------------- comments and risk log
def _out(n: PlanNote) -> dict:
    return {k: getattr(n, k) for k in ("id", "schedule_id", "kind", "text", "severity", "owner", "status", "created_by", "created_at", "closed_by")}


def notes(sid: str) -> list[dict]:
    from . import cache
    return cache.get("execution.notes", (sid,), 120, lambda: _notes(sid), ("notes",))


def _notes(sid: str) -> list[dict]:
    with SessionLocal() as db:
        return [_out(n) for n in db.query(PlanNote).filter_by(schedule_id=sid).order_by(PlanNote.created_at.desc())]


def add_note(sid: str, kind: str, text: str, severity: str | None, owner: str | None, user: str) -> dict:
    if store.get(sid) is None:
        raise LookupError(sid)
    if kind not in ("comment", "risk") or not text.strip():
        raise ValueError("Write a comment or a risk")
    if kind == "risk" and severity not in ("low", "medium", "high"):
        raise ValueError("A risk needs a severity: low, medium or high")
    with SessionLocal() as db:
        n = PlanNote(schedule_id=sid, kind=kind, text=text.strip()[:2000], severity=severity if kind == "risk" else None,
                     owner=(owner or None) if kind == "risk" else None, created_by=user)
        db.add(n)
        db.commit()
        out = _out(n)
    from . import coord
    coord.changed("notes")
    return out


def close_risk(nid: str, user: str) -> dict:
    with SessionLocal() as db:
        n = db.get(PlanNote, nid)
        if n is None or n.kind != "risk":
            raise LookupError(nid)
        n.status, n.closed_by = "CLOSED", user
        db.commit()
        out = _out(n)
    from . import coord
    coord.changed("notes")
    return out

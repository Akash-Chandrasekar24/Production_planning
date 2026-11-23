"""Durable background jobs (agent_jobs table). The API queues a job; a worker claims it (one claimer, by a conditional update), runs it,
and saves its progress as it goes, so any API worker can show the page the job's steps. A job whose worker died is marked failed."""
import datetime as dt
import json
import logging
import threading
import time
import uuid

from sqlalchemy import text

from . import coord
from .db import AgentJob, SessionLocal, _now

log = logging.getLogger("planner.jobs")
RUNNERS: dict = {}                    # kind -> fn(payload, say, think) -> result
STALE_MIN = 15                        # a running job not updated for this long lost its worker


def runner(kind: str):
    def reg(fn):
        RUNNERS[kind] = fn
        return fn
    return reg


def _plain(x):
    return json.loads(json.dumps(x, default=str))


def queue(kind: str, payload: dict, state: dict) -> str:
    jid = uuid.uuid4().hex[:12]
    with SessionLocal() as s:
        s.add(AgentJob(id=jid, kind=kind, status="queued", payload=_plain(payload), state=_plain({**state, "id": jid, "status": "running"})))
        s.commit()
    if coord.run_mode() == "all":                     # one process does everything: start it now rather than at the next poll
        threading.Thread(target=claim_and_run, args=(jid,), name=f"job-{jid}", daemon=True).start()
    return jid


def claim_and_run(jid: str) -> bool:
    with SessionLocal() as s:
        if s.execute(text("UPDATE agent_jobs SET status='running', worker=:w, updated_at=:now WHERE id=:id AND status='queued'"),
                     {"w": coord.PROCESS, "now": _now(), "id": jid}).rowcount != 1:
            s.commit()
            return False                              # someone else has it
        s.commit()
        j = s.get(AgentJob, jid)
        kind, payload, state = j.kind, dict(j.payload), dict(j.state)
    log.info("job %s (%s) claimed", jid, kind)
    t0 = time.monotonic()
    lock, last = threading.Lock(), [0.0]

    def save(force=False):
        if not force and time.monotonic() - last[0] < 0.4:   # progress is saved a few times a second at most
            return
        last[0] = time.monotonic()
        with lock, SessionLocal() as s:
            x = s.get(AgentJob, jid)
            x.state, x.updated_at = _plain(state), _now()
            s.commit()

    def say(step, st, detail=None, facts=None):
        state["steps"][step] = {**state["steps"][step], "state": st, "detail": detail, "facts": facts or []}
        save(True)

    def think(step, txt):
        state["steps"][step]["thoughts"].append(txt)
        save()

    try:
        state["result"] = RUNNERS[kind](payload, say, think)
        state["status"], status = "done", "done"
    except Exception as e:                                    # never hide a failure: the page shows it
        log.exception("job %s failed", jid)
        state["status"], state["error"], status = "failed", type(e).__name__, "failed"
    with lock, SessionLocal() as s:
        x = s.get(AgentJob, jid)
        x.state, x.status, x.updated_at = _plain(state), status, _now()
        s.commit()
    log.info("job %s (%s) %s in %.1f s", jid, kind, status, time.monotonic() - t0)
    return True


def get(jid: str) -> dict | None:
    with SessionLocal() as s:
        j = s.get(AgentJob, jid)
        return j.state if j else None


def latest(kind: str) -> dict | None:
    with SessionLocal() as s:
        j = s.query(AgentJob).filter_by(kind=kind).order_by(AgentJob.created_at.desc()).first()
        return j.state if j else None


def done_since(kind: str, since: dt.datetime) -> dt.datetime | None:
    """When the newest job of `kind` finished, if one finished after `since`."""
    with SessionLocal() as s:
        j = s.query(AgentJob).filter(AgentJob.kind == kind, AgentJob.status == "done", AgentJob.created_at >= since) \
            .order_by(AgentJob.created_at.desc()).first()
        return j.updated_at if j else None


def work_once() -> int:
    """A worker's poll: fail jobs whose worker died, then run what is queued (oldest first). Returns how many it ran."""
    cut = _now() - dt.timedelta(minutes=STALE_MIN)
    with SessionLocal() as s:
        for j in s.query(AgentJob).filter(AgentJob.status == "running", AgentJob.updated_at < cut):
            j.status, j.state = "failed", {**j.state, "status": "failed", "error": "WorkerStopped"}
        s.commit()
        ids = [j.id for j in s.query(AgentJob.id).filter_by(status="queued").order_by(AgentJob.created_at).limit(5)]
    return sum(claim_and_run(i) for i in ids)


def start_polling(every: float = 1.0) -> None:
    def loop():
        while not coord._stop.wait(every):
            try:
                work_once()
            except Exception:
                log.exception("job poll failed")
    threading.Thread(target=loop, name="jobs", daemon=True).start()

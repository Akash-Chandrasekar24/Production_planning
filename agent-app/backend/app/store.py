"""Persistence for schedules, scenarios, approval state and audit events, on the shared SQLAlchemy engine (app.db).

Backend: Postgres (Neon) when DATABASE_URL is set, otherwise a local SQLite file. Scenario inputs are NOT stored: they are
rebuilt deterministically from the data + stored policy + scenario (see inputs()). Credentials never leave app.config.
ponytail: no migrations (CREATE IF NOT EXISTS); add Alembic when the schema changes (guide section 11).
"""
import json
import re
import threading
from collections import defaultdict
from datetime import datetime, timezone
import logging
from functools import lru_cache
from sqlalchemy import text
from . import data, scenarios
from .config import Policy
from .db import engine
from .domain import PlanningInput, Schedule

_COLS = """id TEXT PRIMARY KEY, base_id TEXT, name TEXT, status TEXT, feasibility TEXT, as_of TEXT,
  created_by TEXT, created_at TEXT, decided_by TEXT, decided_at TEXT, decision_note TEXT,
  scenario_json TEXT, summary_json TEXT, body_json TEXT"""
_EV = "ts TEXT, schedule_id TEXT, actor TEXT, role TEXT, action TEXT, detail_json TEXT"
log = logging.getLogger("planner.store")
_inputs_cache: dict[str, PlanningInput] = {}
_ready: set[str] = set()


def backend() -> dict:
    """Safe to show: never includes host, user or password."""
    return {"kind": engine.dialect.name}


def _ensure() -> None:
    key = str(engine.url.render_as_string(hide_password=True))
    if key in _ready:
        return
    ev_id = "BIGSERIAL PRIMARY KEY" if engine.dialect.name == "postgresql" else "INTEGER PRIMARY KEY AUTOINCREMENT"
    with engine.begin() as c:
        c.execute(text(f"CREATE TABLE IF NOT EXISTS schedules({_COLS})"))
        c.execute(text(f"CREATE TABLE IF NOT EXISTS events(id {ev_id}, {_EV})"))
    _ready.add(key)


def _run(sql: str, params: tuple = (), fetch: str = "none"):
    """One statement, own transaction. '?' placeholders -> bound parameters. Returns (rows as dicts, rowcount)."""
    _ensure()
    n = iter(range(len(params)))
    stmt = re.sub(r"\?", lambda _: f":p{next(n)}", sql)
    with engine.begin() as c:
        r = c.execute(text(stmt), {f"p{i}": v for i, v in enumerate(params)})
        rows = [dict(x._mapping) for x in r.fetchall()] if fetch == "all" else ([dict(x._mapping) for x in [r.fetchone()] if x] if fetch == "one" else [])
        return rows, r.rowcount


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@lru_cache
def D():
    return data.load()          # raises FileNotFoundError if the data cannot be loaded (API maps this to DATA_UNAVAILABLE)


def _clear():
    D.cache_clear()
    _inputs_cache.clear()


def _job_rows(s: Schedule) -> list[dict]:
    out, n = [], {}
    for a in s.assignments:
        for st, line, t0, t1 in (("SMT", a.smt_line, a.smt_start, a.smt_finish), ("FAL", a.fal_line, a.fal_start, a.fal_finish)):
            k = (a.customer_order_id, st)
            n[k] = n.get(k, -1) + 1
            out.append({"schedule_id": s.id, "order_id": a.customer_order_id, "stage": st, "seq_key": n[k], "plant_id": a.plant_id, "line_id": line,
                        "product_id": a.product_id, "qty": a.qty, "start": t0, "finish": t1, "priority": a.priority, "status": a.status, "frozen": a.frozen})
    return out


def index_jobs(s: Schedule) -> None:
    """The plan's jobs as rows (schedule_jobs), so work can be queried across plans by line, plant and time."""
    from .db import ScheduleJob, SessionLocal
    rows = _job_rows(s)
    with SessionLocal() as db:
        db.query(ScheduleJob).filter_by(schedule_id=s.id).delete()
        if rows:
            db.bulk_insert_mappings(ScheduleJob, rows)
        db.commit()


def backfill_jobs() -> int:
    """Index plans saved before schedule_jobs existed."""
    from .db import ScheduleJob, SessionLocal
    with SessionLocal() as db:
        have = {i for (i,) in db.query(ScheduleJob.schedule_id).distinct()}
    rows, _ = _run("SELECT id FROM schedules", (), "all")
    todo = [r["id"] for r in rows if r["id"] not in have]
    for sid in todo:
        s = get(sid)
        if s:
            index_jobs(s)
    return len(todo)


def put(s: Schedule, base_id: str | None = None, scenario: scenarios.Scenario | None = None, actor: str = "system", role: str = "",
        inp: PlanningInput | None = None) -> None:
    s.created_by = actor
    if inp is not None:
        _inputs_cache[s.id] = inp
    _run("INSERT INTO schedules(id,base_id,name,status,feasibility,as_of,created_by,created_at,scenario_json,summary_json,body_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
         (s.id, base_id, s.name, s.status, s.feasibility, s.as_of, actor, _now(),
          scenario.model_dump_json() if scenario else None, json.dumps(s.summary), s.model_dump_json()))
    audit(s.id, actor, role, "scenario_created" if base_id else "schedule_generated", {"feasibility": s.feasibility, "base_id": base_id})
    try:
        index_jobs(s)
    except Exception:                                  # the plan is saved either way; the index is rebuilt at the next start
        log.exception("indexing plan jobs failed")
    _coord.changed("planlist")                         # plan lists everywhere show it at once (saved plans stay cached)


# A saved plan's body never changes; only status/decided_by do, and only through set_status below. Opening a plan page makes five
# requests that each needed the full body (a remote round trip plus parsing a large JSON), so parsed plans are kept in memory.
# Each process keeps its own copy; a status change anywhere bumps the "plans" version and every process drops its copies (coord.sync).
_plans: dict[str, Schedule] = {}


def get(sid: str) -> Schedule | None:
    if sid not in _plans:
        rows, _ = _run("SELECT status,decided_by,body_json FROM schedules WHERE id=?", (sid,), "one")
        if not rows:
            return None
        s = Schedule.model_validate_json(rows[0]["body_json"])
        s.status, s.decided_by = rows[0]["status"], rows[0]["decided_by"]
        _plans[sid] = s
    return _plans[sid].model_copy()           # callers may set fields on their copy; the cached plan stays as stored


def list_(limit: int = 50, offset: int = 0) -> list[dict]:
    """Plans, newest first (cached until a plan is saved, decided or approved for a plant, in any process)."""
    from . import cache
    return [dict(r) for r in cache.get("store.list_", (limit, offset), 60, lambda: _list(limit, offset), ("plans", "planlist"))]


def _list(limit: int = 50, offset: int = 0) -> list[dict]:
    """Plans, newest first. `adopted`: a what-if that someone added to the plan; it counts as a real plan, not a what-if."""
    rows, _ = _run("SELECT id,base_id,name,status,feasibility,as_of,created_by,created_at,decided_by,decided_at,summary_json "
                   "FROM schedules ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset), "all")
    took, full = adopted_ids(), fully_approved_ids()
    return [{**r, "summary": json.loads(r["summary_json"]), "summary_json": None, "adopted": r["id"] in took, "fully_approved": r["id"] in full}
            for r in rows]


def fully_approved_ids() -> set:
    """Plans every approval stage has approved (app/signoff.py)."""
    rows, _ = _run("SELECT DISTINCT schedule_id FROM events WHERE action=?", ("fully_approved",), "all")
    return {r["schedule_id"] for r in rows}


def adopted_ids() -> set:
    """Plans made by adding a what-if's change to the plan (they keep their what-if link so their inputs rebuild with the change)."""
    rows, _ = _run("SELECT DISTINCT schedule_id FROM events WHERE action=?", ("scenario_adopted",), "all")
    return {r["schedule_id"] for r in rows}


def adopted_what_ifs() -> set:
    """The what-ifs whose change has already been added to the plan."""
    rows, _ = _run("SELECT detail_json FROM events WHERE action=?", ("scenario_adopted",), "all")
    return {json.loads(r["detail_json"] or "{}").get("what_if") for r in rows} - {None}


def is_what_if(r: dict) -> bool:
    return bool(r.get("base_id")) and not r.get("adopted")


def count() -> int:
    rows, _ = _run("SELECT COUNT(*) AS n FROM schedules", (), "one")
    return int(rows[0]["n"])


def base_of(sid: str) -> str | None:
    rows, _ = _run("SELECT base_id FROM schedules WHERE id=?", (sid,), "one")
    return rows[0]["base_id"] if rows else None


def set_status(sid: str, from_: str, to: str, actor: str, note: str | None = None) -> bool:
    """Atomic compare-and-set; False if the schedule is not in `from_` (e.g. two planners racing)."""
    _, n = _run("UPDATE schedules SET status=?, decided_by=?, decided_at=?, decision_note=? WHERE id=? AND status=?",
                (to, actor, _now(), note, sid, from_))
    if n == 1:
        _coord.changed("plans")                        # every process (this one now, the others within 2 s) drops its cached copies
    return n == 1


_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)


def inputs(sid: str) -> PlanningInput:
    """The exact PlanningInput a schedule was planned from, rebuilt deterministically (cached). One build per plan: a plan page makes
    several requests at once, and without the lock each would rebuild the same inputs in parallel."""
    if sid in _inputs_cache:
        return _inputs_cache[sid]
    with _locks[sid]:
        if sid not in _inputs_cache:
            _inputs_cache[sid] = _build_inputs(sid)
    return _inputs_cache[sid]


def warm(n: int = 3) -> None:
    """Load the newest plans and their inputs ahead of the first visitor (run in a background thread at startup)."""
    for r in list_(n):
        try:
            get(r["id"]); inputs(r["id"])
        except Exception:                      # warming is best-effort; a real request reports any error properly
            pass


def _build_inputs(sid: str) -> PlanningInput:
    rows, _ = _run("SELECT base_id,as_of,scenario_json,body_json FROM schedules WHERE id=?", (sid,), "one")
    if not rows:
        raise KeyError(sid)
    r = rows[0]
    if r["base_id"]:
        inp = scenarios.apply(inputs(r["base_id"]), scenarios.Scenario.model_validate_json(r["scenario_json"]))
    else:
        inp = data.to_inputs(D(), r["as_of"], Policy(**json.loads(r["body_json"])["policy"]))
    return inp


def audit(sid: str | None, actor: str, role: str, action: str, detail: dict | None = None) -> None:
    _run("INSERT INTO events(ts,schedule_id,actor,role,action,detail_json) VALUES(?,?,?,?,?,?)",
         (_now(), sid, actor, role, action, json.dumps(detail or {}, default=str)))


def events(sid: str | None) -> list[dict]:
    """Events of one schedule; sid=None -> events not tied to a schedule (connectors, data sources)."""
    rows, _ = _run("SELECT ts,actor,role,action,detail_json FROM events WHERE " + ("schedule_id=?" if sid else "schedule_id IS NULL") + " ORDER BY id",
                   (sid,) if sid else (), "all")
    return [{"ts": r["ts"], "actor": r["actor"], "role": r["role"], "action": r["action"], "detail": json.loads(r["detail_json"])} for r in rows]


from . import coord as _coord  # noqa: E402  (registered last: store is imported by coord's users)
_coord.on_change(_clear)
_coord.on_change(_plans.clear, "plans")

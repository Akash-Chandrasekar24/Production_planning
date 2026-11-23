"""Agent API (guide section 6.7) + planner API (app/planning_api.py), all under /api.
Run from agent-app/backend:  uvicorn app.main:app --reload --port 8000
Sign-in: app accounts with hashed passwords and session tokens (app/users.py). ponytail: in-process state; add SSO if the company needs it."""
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from . import agent_api, coord, data, flow_api, jobs, users, errors, integrations_api, notify, orchestrator, planning, planning_api, reports, store   # notify: hooks tasks -> notifications
from .agent import run_agent
from .auth import Principal, require
from .config import Policy, Settings
from .db import Conversation, Message, SessionLocal, init_db
from .domo_llm import DomoError
from .errors import ApiError
from .integrations import IntegrationUnavailable

log = logging.getLogger("planner.api")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def start_workers() -> None:
    """The agents' side: background loops (one leader across processes) and the durable job queue."""
    jobs.start_polling()
    if Settings().agent_enabled:                       # planning agent: a look at the order book now, then every interval
        orchestrator.start()
        reports.start()                                # the daily plant briefs and the weekly report, once each


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    users.ensure_admin()
    store._ensure()
    try:
        store.D()                                     # load the data now so the first page load is not slow
    except Exception as e:
        log.warning("data not preloaded (%s)", type(e).__name__)
    if Settings().auto_baseline:
        try:
            if store.count() == 0:                       # first start: give the dashboard a plan to show (a proposal, still needs approval)
                pol = Policy()
                inp = data.to_inputs(store.D(), None, pol)
                store.put(planning.plan(inp, pol), actor="system", role="system", inp=inp)
                log.info("created initial baseline plan")
        except Exception as e:
            log.warning("initial baseline not created (%s)", type(e).__name__)
    def _warm():                                                                     # newest plans and the success measures ready before the first visit
        store.warm()
        try:
            store.backfill_jobs()
        except Exception:
            log.exception("indexing older plans failed")
        try:
            from . import planning_tools
            planning_tools.kpis()
        except Exception:
            pass
        from .auth import Principal                                                  # the home page's heavy parts, ready before the first visit
        me = Principal(user="startup", role="admin")
        for fn in (lambda: flow_api.overview_solutions(_=me), lambda: flow_api.overview_heatmap(_=me), lambda: planning_api.digital_twin(at=None, _=me),
                   lambda: planning_api.data_insights(days=30, f={}, _=me), lambda: planning_api.tool_crews(days=14, _=me)):
            try:
                fn()
            except Exception:
                log.exception("warming the home page failed")
    mode = coord.run_mode()
    coord.start_sync()                                 # every 2 s, in the background: drop caches another process made stale
    if mode != "worker":
        threading.Thread(target=_warm, name="warm-plans", daemon=True).start()
    if mode == "all":                                  # one process does everything (the default); "api" leaves the agents to `python -m app.worker`
        start_workers()
    if Settings().auth_mode == "dev_headers":
        log.warning("AUTH_MODE=dev_headers: roles are taken from request headers and are NOT authenticated")
    yield
    coord._stop.set()                                  # shutting down: background loops stop and hand their leases over


app = FastAPI(title="Agent API", version="0.5.0", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)       # plans and tables are large JSON: ~5-10x smaller on the wire
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in Settings().cors_origins.split(",") if o.strip()],
                   allow_methods=["*"], allow_headers=["*"])
errors.install(app)
app.include_router(users.router)
app.include_router(agent_api.router)                   # first: /api/orders/intake must win over /api/orders/{oid}
app.include_router(planning_api.router)
app.include_router(integrations_api.router)
app.include_router(flow_api.router)


@app.middleware("http")
async def request_log(request: Request, call_next):
    request.state.request_id = rid = uuid.uuid4().hex[:12]
    t = time.perf_counter()
    if request.url.path.startswith("/api/"):
        await run_in_threadpool(coord.sync)            # at most every 2 s: drop caches another process made stale
    resp = await call_next(request)
    resp.headers["X-Request-ID"] = rid
    # method/path/status/role only: no headers, bodies, tokens or chat text
    log.info("%s %s -> %s %dms rid=%s role=%s", request.method, request.url.path, resp.status_code,
             (time.perf_counter() - t) * 1000, rid, request.headers.get("x-role", "-"))
    return resp


@app.get("/health")
def health():
    return {"ok": True}


class ChatIn(BaseModel):
    conversation_id: str | None = None
    message: str = Field(min_length=1, max_length=4000)


@app.get("/api/conversations")
def list_convs(p: Principal = Depends(require("read"))):
    with SessionLocal() as s:
        rows = s.query(Conversation).filter_by(owner=p.user).order_by(Conversation.created_at.desc()).all()
        return [{"id": c.id, "title": c.title} for c in rows]


@app.get("/api/conversations/{cid}/messages")
def get_msgs(cid: str, p: Principal = Depends(require("read"))):
    with SessionLocal() as s:
        conv = s.get(Conversation, cid)
        if not conv or conv.owner != p.user:
            raise ApiError(404, "CONVERSATION_NOT_FOUND", "No such conversation")
        q = s.query(Message).filter_by(conversation_id=cid).order_by(Message.id)
        return [{"role": m.role, "content": m.content, "steps": m.steps, "warnings": m.warnings} for m in q]


@app.post("/api/chat")
def chat(body: ChatIn, p: Principal = Depends(require("read"))):
    # The agent runs OUTSIDE any open database transaction (its tools write to the database themselves, and a model call can take
    # many seconds). The two messages are saved together afterwards; if the agent fails, nothing is saved (as in the guide).
    with SessionLocal() as s:
        conv = s.get(Conversation, body.conversation_id) if body.conversation_id else None
        if body.conversation_id and (not conv or conv.owner != p.user):
            raise ApiError(404, "CONVERSATION_NOT_FOUND", "No such conversation")
        history = [{"role": m.role, "content": m.content} for m in conv.messages] if conv else []
    cid = conv.id if conv else str(uuid.uuid4())
    try:
        answer, steps, warnings = run_agent(body.message, history, role=p.role, user=p.user, conversation_id=cid)
    except DomoError as e:
        raise ApiError(503 if e.code == "LLM_NOT_CONFIGURED" else 502, e.code, str(e))
    except IntegrationUnavailable as e:
        raise ApiError(503, f"GUARDRAIL_{e.code}", "The assistant's guardrails are unavailable, so the request was not sent to the model")
    with SessionLocal() as s:
        if not conv:
            s.add(Conversation(id=cid, title=body.message[:60], owner=p.user))
            s.flush()
        s.add(Message(conversation_id=cid, role="user", content=body.message))
        s.add(Message(conversation_id=cid, role="assistant", content=answer, steps=steps, warnings=warnings or None))
        s.commit()
    return {"conversation_id": cid, "answer": answer, "steps": steps, "warnings": warnings}


# Optional: serve the built UI (npm run build -> frontend/dist) from the same origin. Mounted last so /api and /health win.
_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="ui")

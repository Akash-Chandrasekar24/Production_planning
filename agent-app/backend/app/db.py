"""Database: SQLAlchemy engines + chat models (guide section 6.3).

engine     : application read/write role (DATABASE_URL, Postgres/Neon; local SQLite file if unset).
ro_engine  : SELECT-only role for the agent's SQL tool (READONLY_DATABASE_URL). None when unset: the SQL tool is then disabled
             (it never falls back to the read/write role).
"""
import datetime as dt
import uuid
from pathlib import Path
import socket
from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from .config import Settings, database_url, readonly_database_url


def _local_sqlite() -> str:
    p = Path(Settings().planning_db or Path(__file__).resolve().parent.parent / "var" / "planning.db")
    p.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{p.as_posix()}"


# Each round trip to Neon costs ~225 ms, so a ping before every use (pool_pre_ping) doubled most queries. Instead: TCP keep-alives hold
# connections open, connections are replaced after 4 minutes, and only one that sat idle over a minute is pinged before use.
_PG = dict(pool_pre_ping=False, pool_recycle=240, pool_size=10, max_overflow=20)
_KEEPALIVE = {"keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 3,
              "connect_timeout": 20}              # a slow route to the database: wait up to 20 s for a connection, not the short default


def _pg(url: str) -> dict:
    """Pool settings; TCP keep-alives only where the driver has them (Postgres, not the SQLite used in tests)."""
    return {**_PG, "connect_args": _KEEPALIVE} if url.startswith("postgresql") else dict(_PG)
engine = create_engine(database_url(), **_pg(database_url())) if database_url() else create_engine(_local_sqlite(), pool_pre_ping=True,
                                                                                   connect_args={"timeout": 30, "check_same_thread": False})
if engine.dialect.name == "sqlite":                     # local and test runs: readers don't wait for writers, one writer at a time
    @event.listens_for(engine, "connect")
    def _sqlite_wal(conn, _):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()
ro_engine = create_engine(readonly_database_url(), **_pg(readonly_database_url())) if readonly_database_url() else None
_addr: dict = {}


def _keep_address(eng) -> None:
    """Connect by the address the last name lookup gave when this machine's lookup fails for a moment (the host name still goes
    with the connection, so the secure connection and Neon's routing are unchanged)."""
    if eng is None or eng.dialect.name != "postgresql":
        return

    @event.listens_for(eng, "do_connect")
    def _dns(dialect, rec, cargs, cparams):
        host = cparams.get("host")
        if not host or host.startswith("/") or cparams.get("hostaddr"):
            return
        try:
            _addr[host] = socket.getaddrinfo(host, cparams.get("port") or 5432, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
        except OSError:
            pass                                                   # use the address from last time, if there is one
        if host in _addr:
            cparams["hostaddr"] = _addr[host]


_keep_address(engine); _keep_address(ro_engine)


def _ping_if_idle(eng, idle_s: float = 60.0) -> None:
    """Ping a pooled connection before use only if it sat idle longer than idle_s (a dropped one is replaced, not handed out)."""
    if eng is None or eng.dialect.name != "postgresql":
        return
    import time as _t
    from sqlalchemy import exc

    @event.listens_for(eng, "checkin")
    def _in(dbapi_con, rec):
        rec.info["idle_since"] = _t.monotonic()

    @event.listens_for(eng, "checkout")
    def _out(dbapi_con, rec, proxy):
        since = rec.info.get("idle_since")
        if since is not None and _t.monotonic() - since > idle_s:
            try:
                cur = dbapi_con.cursor()
                cur.execute("SELECT 1")
                cur.close()
            except Exception:
                raise exc.DisconnectionError()      # the pool drops it and opens a fresh connection


_ping_if_idle(engine); _ping_if_idle(ro_engine)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title: Mapped[str] = mapped_column(String(200), default="New chat")
    owner: Mapped[str] = mapped_column(String(64), default="unknown")        # X-User; conversations are only listed for their owner
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    messages = relationship("Message", back_populates="conversation", order_by="Message.id", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(20))                             # user | assistant
    content: Mapped[str] = mapped_column(Text)
    steps: Mapped[list | None] = mapped_column(JSON, nullable=True)           # tool trace
    warnings: Mapped[list | None] = mapped_column(JSON, nullable=True)        # e.g. numbers not found in tool results
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    conversation = relationship("Conversation", back_populates="messages")


class Connector(Base):
    """An external system the app can read from. Credentials are encrypted (app/connectors.py) and never returned by the API."""
    __tablename__ = "connectors"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(20))                             # sap_odata | postgresql | mysql | mssql | oracle
    config: Mapped[dict] = mapped_column(JSON, default=dict)                  # non-secret settings (host, port, database, ...)
    secret: Mapped[str | None] = mapped_column(Text, nullable=True)           # encrypted password
    status: Mapped[str] = mapped_column(String(20), default="NOT_TESTED")     # NOT_TESTED | CONNECTED | FAILED
    last_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), default="unknown")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class ConnectorSync(Base):
    """One import of a source table/entity into a staging table of the app database."""
    __tablename__ = "connector_syncs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    connector_id: Mapped[str] = mapped_column(ForeignKey("connectors.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(200))                          # table / entity set in the external system
    dataset: Mapped[str] = mapped_column(String(60))                          # planning dataset it feeds (data.TABLES)
    table_name: Mapped[str | None] = mapped_column(String(80), nullable=True) # staging table in the app database
    rows: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(20), default="RUNNING")        # RUNNING | SUCCEEDED | FAILED
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String(64), default="unknown")
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class DatasetSource(Base):
    """Planning dataset -> staging table that replaces the built-in source table. No row = built-in data."""
    __tablename__ = "dataset_sources"
    dataset: Mapped[str] = mapped_column(String(60), primary_key=True)
    sync_id: Mapped[str] = mapped_column(ForeignKey("connector_syncs.id", ondelete="CASCADE"))
    table_name: Mapped[str] = mapped_column(String(80))
    set_by: Mapped[str] = mapped_column(String(64), default="unknown")
    set_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


class IntakeOrder(Base):
    """A customer order received through the intake API or an upload. Planning reads these together with the order book."""
    __tablename__ = "order_intake"
    customer_order_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(40))
    product_id: Mapped[str] = mapped_column(String(40))
    order_date: Mapped[str] = mapped_column(String(10))
    requested_delivery_date: Mapped[str] = mapped_column(String(10))
    confirmed_delivery_date: Mapped[str] = mapped_column(String(10))
    order_qty: Mapped[int] = mapped_column()
    unit_price_inr: Mapped[float] = mapped_column()
    priority: Mapped[str] = mapped_column(String(10))
    channel: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(40), default="api")
    received_by: Mapped[str] = mapped_column(String(64), default="unknown")
    received_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


class AgentRun(Base):
    """One decision of the planning agent: checked the order book, and either kept watching or planned and assigned work."""
    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    trigger: Mapped[str] = mapped_column(String(20))                         # auto | manual | intake | baseline
    outcome: Mapped[str] = mapped_column(String(20))                         # WATCHING | PLANNED | FAILED
    reason: Mapped[str] = mapped_column(Text)
    new_orders: Mapped[int] = mapped_column(default=0)
    new_units: Mapped[int] = mapped_column(default=0)
    open_orders: Mapped[list] = mapped_column(JSON, default=list)            # open order ids at this run: the next run compares against it
    schedule_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    actor: Mapped[str] = mapped_column(String(64), default="planning agent")
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


class AgentTask(Base):
    """Work the agent hands to a person: the production manager reviews and approves, each plant head prepares their plant."""
    __tablename__ = "agent_tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"))
    schedule_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    role: Mapped[str] = mapped_column(String(30))                            # production_manager | plant_head
    plant_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    assignee: Mapped[str | None] = mapped_column(String(100), nullable=True) # from the team list, if someone is set up for the role
    kind: Mapped[str] = mapped_column(String(20))                            # approve | review | prepare
    title: Mapped[str] = mapped_column(String(200))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20))                          # OPEN | WAITING_APPROVAL | ACKNOWLEDGED | DONE | CANCELLED
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Incident(Base):
    """Something that went wrong on the floor (a machine stopped, production orders running late), what the agent worked out, and
    how people fixed it. Fixed incidents are the agent's memory: the next similar problem shows how it was handled before."""
    __tablename__ = "agent_incidents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    kind: Mapped[str] = mapped_column(String(20))                            # MACHINE_DOWN | PO_DELAY
    key: Mapped[str] = mapped_column(String(200), index=True)                # de-duplicates automatic detection
    source: Mapped[str] = mapped_column(String(20))                          # detected | reported
    status: Mapped[str] = mapped_column(String(20), default="OPEN")          # OPEN | RESOLVED
    plant_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    line_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    machine_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    production_orders: Mapped[list] = mapped_column(JSON, default=list)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime)
    expected_end: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    end_assumed: Mapped[bool] = mapped_column(default=False)                 # repair time is the agent's assumption, not given by a person
    description: Mapped[str] = mapped_column(Text)
    reported_by: Mapped[str] = mapped_column(String(64))
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)
    proposal_id: Mapped[str | None] = mapped_column(String(36), nullable=True)   # the recovery plan waiting for approval
    option: Mapped[str | None] = mapped_column(String(20), nullable=True)        # the recovery option in that plan
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)          # how it was fixed, in the words of the person who fixed it
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


class AgentEvent(Base):
    """A signal one of the agents raised and what the orchestrator did with it (sent to the planning agent, or alerted people)."""
    __tablename__ = "agent_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    type: Mapped[str] = mapped_column(String(40))                            # floor.breakdown | inventory.low_cover | orders.replan | ...
    agent: Mapped[str] = mapped_column(String(40))                           # which agent raised it
    key: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)   # the same key is raised once
    severity: Mapped[str] = mapped_column(String(10))                        # critical | warn | info
    title: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    plant_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    line_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="NEW")           # NEW | PLANNED | RECOVERY | ALERTED | FAILED
    routed_to: Mapped[str | None] = mapped_column(String(60), nullable=True) # planning agent | production manager | plant head ...
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    refs: Mapped[dict] = mapped_column(JSON, default=dict)                   # run / incident / plan it led to
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    handled_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class AgentLook(Base):
    """What one agent saw in one orchestrator round (kept, so the team's status survives restarts and has a history)."""
    __tablename__ = "agent_looks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent: Mapped[str] = mapped_column(String(20), index=True)             # floor | inventory | orders
    trigger: Mapped[str] = mapped_column(String(20))
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    signals: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(String(60), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, index=True)


class Notification(Base):
    """A message to a person (in the app, and by email when an address and a mail server are set up)."""
    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(30), index=True)
    plant_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    member: Mapped[str | None] = mapped_column(String(100), nullable=True)    # the named person, if the role has one
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    severity: Mapped[str] = mapped_column(String(10), default="info")       # critical | warn | info
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str] = mapped_column(String(60), default="Review & approve")  # the page it opens
    email_status: Mapped[str] = mapped_column(String(20), default="NOT_SENT")  # SENT | FAILED | PENDING | NO_EMAIL | NOT_CONFIGURED
    email_error: Mapped[str | None] = mapped_column(String(120), nullable=True)
    emailed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    read_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, index=True)


class TeamMember(Base):
    """Who holds each planning role (and, for plant heads, which plant). Tasks are assigned to these people."""
    __tablename__ = "team_members"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(30))
    plant_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


class OrderFreeze(Base):
    """A received order that is part of an approved plan: it can no longer be changed by uploading it again."""
    __tablename__ = "order_freeze"
    customer_order_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    schedule_id: Mapped[str] = mapped_column(String(40))
    frozen_by: Mapped[str] = mapped_column(String(64))
    frozen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


class UploadLog(Base):
    """Every order upload: who, when, which file and orders, what was new or updated, and what was refused."""
    __tablename__ = "upload_log"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    uploaded_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, index=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(30))
    filename: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20))                      # RECEIVED | REJECTED | UNREADABLE
    orders: Mapped[int] = mapped_column(default=0)
    units: Mapped[int] = mapped_column(default=0)
    new_orders: Mapped[list] = mapped_column(JSON, default=list)
    updated_orders: Mapped[list] = mapped_column(JSON, default=list)
    problems: Mapped[list | None] = mapped_column(JSON, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(20), nullable=True)


class User(Base):
    """A person who signs in. Password is stored as a salted scrypt hash only (app/users.py)."""
    __tablename__ = "app_users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(30))
    plant_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    last_login: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class UserSession(Base):
    """A signed-in browser. Only the SHA-256 of the token is stored; signing out deletes the row."""
    __tablename__ = "app_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("app_users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)


class ForecastOverride(Base):
    """Forecast review: the number agreed for a product and week, replacing the model's forecast in planning."""
    __tablename__ = "forecast_overrides"
    product_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    week: Mapped[str] = mapped_column(String(10), primary_key=True)        # Monday, YYYY-MM-DD
    qty: Mapped[int] = mapped_column()
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    set_by: Mapped[str] = mapped_column(String(64))
    set_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class JobProgress(Base):
    """Execution board: where each job of the plan in use stands on the floor."""
    __tablename__ = "job_progress"
    schedule_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(60), primary_key=True)
    stage: Mapped[str] = mapped_column(String(5), primary_key=True)          # SMT | FAL
    plant_id: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))                          # TO_START | RUNNING | DONE | BLOCKED
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class PlanNote(Base):
    """Comments and the risk log on a plan."""
    __tablename__ = "plan_notes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    schedule_id: Mapped[str] = mapped_column(String(40), index=True)
    kind: Mapped[str] = mapped_column(String(10))                             # comment | risk
    text: Mapped[str] = mapped_column(Text)
    severity: Mapped[str | None] = mapped_column(String(10), nullable=True)   # risk: low | medium | high
    owner: Mapped[str | None] = mapped_column(String(100), nullable=True)     # risk: who handles it
    status: Mapped[str] = mapped_column(String(10), default="OPEN")           # risk: OPEN | CLOSED
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    closed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ReportLog(Base):
    """Scheduled reports sent (one row per report and day), so each goes out once."""
    __tablename__ = "report_log"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)            # daily:2026-11-19:IN-TN-01 | weekly:2026-11-16
    kind: Mapped[str] = mapped_column(String(10))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    sent_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


class VolumePlan(Base):
    """Demand and capacity gate: the agent's proposal when demand doesn't fit capacity, and the production manager's decision."""
    __tablename__ = "volume_plans"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    status: Mapped[str] = mapped_column(String(12))                  # PROPOSED | OPTION | AGREED | SUPERSEDED
    signature: Mapped[str] = mapped_column(String(20))               # the set of orders that don't fit: same set = same proposal
    trigger: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    as_of: Mapped[str | None] = mapped_column(String(25), nullable=True)
    base_id: Mapped[str | None] = mapped_column(String(36), nullable=True)   # the volume plan in force when this was proposed
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    load: Mapped[list] = mapped_column(JSON, default=list)
    options: Mapped[list] = mapped_column(JSON, default=list)
    recommended: Mapped[str | None] = mapped_column(String(10), nullable=True)
    late_count: Mapped[int] = mapped_column(default=0)
    chosen: Mapped[str | None] = mapped_column(String(10), nullable=True)
    agreed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agreed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    redates: Mapped[dict] = mapped_column(JSON, default=dict)        # agreed: order -> committed date (cumulative)
    accept_late: Mapped[list] = mapped_column(JSON, default=list)    # agreed: orders accepted as late (cumulative)
    extra_min: Mapped[float] = mapped_column(default=0.0)
    extra_weeks: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


class PlanSet(Base):
    """The five plans the agent made from the same data in one run (plan_sets.py); the production manager chooses one."""
    __tablename__ = "plan_sets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    status: Mapped[str] = mapped_column(String(12))                  # OPEN | CHOSEN | SUPERSEDED
    trigger: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    as_of: Mapped[str | None] = mapped_column(String(32), nullable=True)
    products: Mapped[list] = mapped_column(JSON, default=list)
    plans: Mapped[list] = mapped_column(JSON, default=list)
    recommended: Mapped[str | None] = mapped_column(String(36), nullable=True)
    chosen: Mapped[str | None] = mapped_column(String(36), nullable=True)
    chosen_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chosen_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


class Lease(Base):
    """A named lock any process can hold for a while (coord.py): one plan change at a time, one leader per background loop."""
    __tablename__ = "leases"
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    holder: Mapped[str] = mapped_column(String(160))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)


class AppState(Base):
    """Small shared values, e.g. the data version that tells every process to clear its caches."""
    __tablename__ = "app_state"
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


class AgentJob(Base):
    """Durable background work (an upload's checks): queued by the API, claimed and run by a worker, followed by the page."""
    __tablename__ = "agent_jobs"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(10), index=True)               # queued | running | done | failed
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[dict] = mapped_column(JSON, default=dict)                   # what the page shows: steps, result, error
    worker: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


class ScheduleJob(Base):
    """Every job of every plan as a row (plans themselves are stored whole): query work across plans by line, plant and time."""
    __tablename__ = "schedule_jobs"
    schedule_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(60), primary_key=True)
    stage: Mapped[str] = mapped_column(String(5), primary_key=True)
    seq_key: Mapped[int] = mapped_column(primary_key=True, default=0)          # an order split into lots has one row per lot
    plant_id: Mapped[str] = mapped_column(String(20), index=True)
    line_id: Mapped[str] = mapped_column(String(20), index=True)
    product_id: Mapped[str] = mapped_column(String(40))
    qty: Mapped[int] = mapped_column()
    start: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    finish: Mapped[dt.datetime] = mapped_column(DateTime)
    priority: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(12))                           # ON_TIME | LATE_RISK
    frozen: Mapped[bool] = mapped_column(default=False)


def init_db() -> None:
    """Create any missing tables. The database can be slow or briefly unreachable at start-up: try again a few times (waiting
    longer each time) instead of failing the whole service on the first timeout."""
    import logging
    import time
    from sqlalchemy.exc import OperationalError, ProgrammingError
    for attempt in range(1, 7):
        try:
            try:
                Base.metadata.create_all(engine)
            except ProgrammingError:          # the API and the worker started together and the other made a new table first
                Base.metadata.create_all(engine)
            return
        except OperationalError as e:
            if attempt == 6:
                raise
            logging.getLogger("planner.db").warning("database not reachable at start-up (%s); trying again (%d of 6)", type(e.orig).__name__, attempt + 1)
            time.sleep(5 * attempt)

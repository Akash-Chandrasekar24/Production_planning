"""Agent tools (guide sections 6.5 and 10).

SQL tools use the SELECT-only role (READONLY_DATABASE_URL); when it is not configured they are disabled, never falling back to
the read/write role. Planner tools call the deterministic planning service: numbers come from it, not from the model.
There is deliberately NO tool to approve, reject, publish, cancel orders, or edit BOMs/routings. generate_schedule and run_scenario
only create schedules that wait for planner approval, and they check the caller's role (set per request in CTX).
"""
import json
import re
from collections import Counter
from contextvars import ContextVar
from langchain_core.tools import tool
from sqlalchemy import inspect, text
from . import data, planning, scenarios, store
from .config import Policy
from . import neon_mcp
from .db import ro_engine

CTX: ContextVar[dict] = ContextVar("agent_ctx", default={"role": "viewer", "user": "unknown"})

FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|copy|call|do)\b", re.I)
HIDDEN_TABLES = {"conversations", "messages", "schedules", "events",       # app internals are not for the model to browse:
                 "connectors", "connector_syncs", "dataset_sources",        # connection settings (sealed credentials)
                 "agent_runs", "agent_tasks", "agent_incidents", "agent_events", "team_members"}   # agents' work, people's contacts
NO_SQL = "ERROR: SQL tools are disabled: READONLY_DATABASE_URL (a SELECT-only database role) is not configured."


def _allowed(action: str) -> bool:
    return action in Policy().role_actions.get(CTX.get()["role"], ())


def _j(o) -> str:
    return json.dumps(o, default=str, separators=(",", ":"))


@tool
def get_schema() -> str:
    """List all business tables and their columns in the database (read-only)."""
    if ro_engine is None:                       # no local read-only role: use the Neon MCP server if it is configured
        try:
            return neon_mcp.run_sql_via_mcp(
                "select table_name, string_agg(column_name || ':' || data_type, ', ' order by ordinal_position) as columns "
                "from information_schema.columns where table_schema = 'public' and table_name like 'src%' group by table_name order by table_name")[:4000]
        except neon_mcp.NeonMcpError:
            return NO_SQL
    try:
        insp = inspect(ro_engine)
        return "\n".join(f"{t}({', '.join(c['name'] + ':' + str(c['type']) for c in insp.get_columns(t))})"
                         for t in insp.get_table_names() if t not in HIDDEN_TABLES) or "No tables visible to the read-only role."
    except Exception as e:
        return f"ERROR: {type(e).__name__}"


@tool
def run_sql(query: str) -> str:
    """Run ONE read-only PostgreSQL SELECT query and return up to 50 rows."""
    if ro_engine is None:
        return NO_SQL
    q = query.strip().rstrip(";")
    if ";" in q or not re.match(r"^(select|with)\b", q, re.I) or FORBIDDEN.search(q):
        return "ERROR: only a single read-only SELECT is allowed."
    try:
        with ro_engine.connect() as conn:
            if conn.dialect.name == "postgresql":
                conn.execute(text("SET TRANSACTION READ ONLY"))
                conn.execute(text("SET LOCAL statement_timeout = 8000"))
            res = conn.execute(text(f"SELECT * FROM ({q}) AS t LIMIT 50"))
            cols = list(res.keys())
            rows = [dict(zip(cols, r)) for r in res.fetchall()]
            return str(rows) if rows else "No rows."
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {str(e).splitlines()[0][:200]}"


@tool
def neon_run_sql(query: str) -> str:
    """Run ONE read-only SELECT on the business tables (src_*) through the Neon MCP server (server-side read-only)."""
    q = query.strip().rstrip(";")
    if ";" in q or not re.match(r"^(select|with)\b", q, re.I) or FORBIDDEN.search(q):
        return "ERROR: only a single read-only SELECT is allowed."
    try:
        return neon_mcp.run_sql_via_mcp(q)[:4000]
    except neon_mcp.NeonMcpError as e:
        return f"ERROR: {e}"


# ---------------------------------------------------------------- planner tools (deterministic service)
@tool
def list_schedules() -> str:
    """List the most recent planning schedules (id, name, approval status, feasibility)."""
    return _j([{k: r[k] for k in ("id", "name", "status", "feasibility", "created_by")} for r in store.list_(10)])


def _metrics(s) -> dict:
    """The plan's figures for the model: the rough-cut table is summed up as its overloaded weeks (keeps the answer short)."""
    m = {k: v for k, v in s.summary.items() if k != "rough_cut"}
    m["overloaded_weeks"] = [r["week"] for r in s.summary.get("rough_cut") or [] if (r.get("load_pct") or 0) > 100]
    return m


@tool
def get_schedule_summary(schedule_id: str) -> str:
    """Key facts of a schedule from the planning engine: feasibility, approval status, metrics, unscheduled units by reason, diagnostics."""
    s = store.get(schedule_id)
    if not s:
        return "ERROR: schedule not found."
    unr = Counter()
    for u in s.unscheduled:
        unr[u.reason] += u.qty
    return _j({"id": s.id, "name": s.name, "approval_status": s.status, "feasibility": s.feasibility, "metrics": _metrics(s),
               "unscheduled_units_by_reason": dict(unr), "diagnostic_counts": dict(Counter(d.code for d in s.diagnostics)),
               "constraints_not_enforced": s.constraints_not_enforced})


@tool
def get_material_shortfalls(schedule_id: str) -> str:
    """Top material shortfalls (required vs available units) for a schedule's open demand."""
    s = store.get(schedule_id)
    if not s:
        return "ERROR: schedule not found."
    rows = sorted(planning.material_calloff(store.inputs(s.id), s), key=lambda r: -r["shortfall_qty"])[:8]
    return _j([r for r in rows if r["shortfall_qty"] > 0] or "No material shortfall.")


@tool
def get_capacity_gaps(schedule_id: str) -> str:
    """Capacity view of a schedule: overloaded weeks, orders that did not fit, late orders, units limited by material."""
    s = store.get(schedule_id)
    if not s:
        return "ERROR: schedule not found."
    g = planning.capacity_gaps(s, store.inputs(s.id))
    return _j({"overloaded_weeks": g["overloaded_weeks"][:10], "unfit_orders": len(g["unfit_orders"]), "extra_minutes_needed": g["extra_minutes_needed"],
               "late_orders_top10": g["late_orders"][:10], "material_limited_qty": g["material_limited_qty"]})


@tool
def generate_schedule(horizon_days: int = 60) -> str:
    """Generate a new baseline schedule with the planning engine. It is only a proposal: a planner must approve it in the app."""
    if not _allowed("generate"):
        return "ERROR: your role may not generate schedules."
    c = CTX.get()
    pol = Policy(horizon_days=max(1, min(int(horizon_days), 365)))
    inp = data.to_inputs(store.D(), None, pol)
    s = planning.plan(inp, pol)
    store.put(s, actor=f"{c['user']} (assistant)", role=c["role"], inp=inp)
    return _j({"id": s.id, "approval_status": s.status, "feasibility": s.feasibility, "metrics": _metrics(s)})


@tool
def run_scenario(schedule_id: str, scenario: dict) -> str:
    """Run a what-if on a schedule and compare it with that baseline. scenario example: {"name":"spike","demand_spikes":[{"multiplier":3}],
    "absenteeism_pct":10,"machine_losses":[{"machine_id":"M-FAL-01","start":"2026-11-02T00:00:00","end":"2026-11-05T00:00:00"}],
    "material_factor":{"*":0.5},"urgent_orders":[{"product_id":"SPH-A15-128","qty":500,"due":"2026-11-10"}]}. Result is a proposal only."""
    if not _allowed("scenario"):
        return "ERROR: your role may not run scenarios."
    base = store.get(schedule_id)
    if not base:
        return "ERROR: schedule not found."
    try:
        sc = scenarios.Scenario(**scenario)
        s = scenarios.run(store.inputs(base.id), base, sc, Policy(**base.policy))
    except Exception as e:
        return f"ERROR: invalid scenario ({type(e).__name__}: {str(e)[:200]})"
    c = CTX.get()
    store.put(s, base.id, sc, actor=f"{c['user']} (assistant)", role=c["role"])
    return _j({"new_schedule_id": s.id, "comparison": scenarios.compare(base, [s])})


TOOLS = [get_schema, run_sql, neon_run_sql, list_schedules, get_schedule_summary, get_material_shortfalls, get_capacity_gaps, generate_schedule, run_scenario]

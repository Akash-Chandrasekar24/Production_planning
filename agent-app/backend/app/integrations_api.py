"""Integrations API (under /api/integrations): connect external systems, import their data, and choose what planning uses.

Flow: create a connector -> test it -> pick a source table/entity -> sync it into a staging table (app database) for one planning
dataset -> switch the dataset to that import (validated by building planning inputs first) -> switch back to built-in data any time.
Reading/listing needs 'read'; everything that connects, imports or switches needs 'integrate' (admin role). Every change is audited.
"""
import datetime as dt
import pandas as pd
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from . import cache, connectors as cx
from . import data, store
from .auth import Principal, require
from .config import Policy
from .db import Connector, ConnectorSync, DatasetSource, SessionLocal, engine
from .errors import ApiError
from .integrations import IntegrationUnavailable

router = APIRouter(prefix="/api/integrations")
LABELS = {"fact_customer_order": "Customer orders", "fact_production_order": "Production orders", "fact_bom_component": "Bills of materials",
          "fact_inventory_daily_snapshot": "Inventory", "dim_product_routing": "Routings", "dim_machine": "Machines",
          "dim_shift_calendar": "Shift calendar", "fact_hourly_production": "Production actuals"}


class ConnectorIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: str
    config: dict = {}
    secrets: dict = {}           # on update, a blank value keeps the saved one


class SyncIn(BaseModel):
    source: str = Field(min_length=1, max_length=200)
    dataset: str
    mapping: dict[str, str] = {}  # planning column -> source column (optional; same names match automatically)


class SourceIn(BaseModel):
    sync_id: str


def _now():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _out(c: Connector) -> dict:
    return {"id": c.id, "name": c.name, "kind": c.kind, "label": cx.KINDS[c.kind]["label"], "config": c.config, "status": c.status,
            "has_secret": bool(c.secret), "last_checked_at": c.last_checked_at, "last_error": c.last_error,
            "created_by": c.created_by, "created_at": c.created_at}


def _get(s, cid: str) -> Connector:
    c = s.get(Connector, cid)
    if not c:
        raise ApiError(404, "CONNECTOR_NOT_FOUND", f"No connector '{cid}'")
    return c


def _err(e: Exception):
    if isinstance(e, cx.ConnectorError):
        return ApiError(422 if e.missing else 502, "MISSING_COLUMNS" if e.missing else "CONNECTOR_ERROR", str(e),
                        {"missing": e.missing} if e.missing else None)
    return ApiError(503, f"CONNECTOR_{e.code}", str(e))


def _refresh():
    """The data planning uses changed (a dataset switched): every process and every cached page drops what it had."""
    from . import coord
    coord.changed()


def _conform(df: pd.DataFrame, ref: pd.DataFrame) -> pd.DataFrame:
    """Give imported columns the types and date formats of the built-in data the planner was built and tested on."""
    out = {}
    for col in ref.columns:
        s, r = df[col], ref[col].dropna()
        first = r.iloc[0] if len(r) else None
        if pd.api.types.is_bool_dtype(ref[col]):
            out[col] = s.map(lambda v: str(v).strip().lower() in ("true", "1", "yes", "x"))
        elif pd.api.types.is_numeric_dtype(ref[col]):
            out[col] = pd.to_numeric(s)
        elif isinstance(first, str) and len(first) in (10, 19) and first[4:5] == "-" and first[7:8] == "-":
            fmt = "%Y-%m-%d" if len(first) == 10 else "%Y-%m-%dT%H:%M:%S"
            out[col] = pd.to_datetime(s).dt.strftime(fmt).where(s.notna(), None)
        else:
            out[col] = s.map(lambda v: None if pd.isna(v) else str(v))
    return pd.DataFrame(out)


# ------------------------------------------------------------------ catalog / connectors
@router.get("/catalog")
def catalog(_: Principal = Depends(require("read"))):
    return cx.catalog()


@router.get("/connectors")
def list_connectors(_: Principal = Depends(require("read"))):
    with SessionLocal() as s:
        return [_out(c) for c in s.query(Connector).order_by(Connector.created_at)]


@router.post("/connectors", status_code=201)
def create(body: ConnectorIn, p: Principal = Depends(require("integrate"))):
    try:
        cx.validate(body.kind, body.config, body.secrets)
        sealed = cx.seal({k: body.secrets.get(k) for k in cx.secret_keys(body.kind)})
    except cx.ConnectorError as e:
        raise ApiError(422, "INVALID_CONNECTOR", str(e))
    except IntegrationUnavailable as e:
        raise _err(e)
    keys = {f["key"] for f in next(k for k in cx.catalog() if k["kind"] == body.kind)["fields"]} - set(cx.secret_keys(body.kind))
    with SessionLocal() as s:
        c = Connector(name=body.name.strip(), kind=body.kind, config={k: v for k, v in body.config.items() if k in keys and v not in (None, "")},
                      secret=sealed, created_by=p.user)
        s.add(c)
        s.commit()
        store.audit(None, p.user, p.role, "connector_created", {"connector": c.id, "kind": c.kind, "name": c.name})
        return _out(c)


@router.put("/connectors/{cid}")
def update(cid: str, body: ConnectorIn, p: Principal = Depends(require("integrate"))):
    with SessionLocal() as s:
        c = _get(s, cid)
        if body.kind != c.kind:
            raise ApiError(422, "INVALID_CONNECTOR", "The connector type cannot be changed; create a new connector instead")
        try:
            secrets = {**cx.unseal(c.secret), **{k: v for k, v in body.secrets.items() if v not in (None, "")}}
            cx.validate(c.kind, body.config, secrets)
            c.secret = cx.seal({k: secrets.get(k) for k in cx.secret_keys(c.kind)})
        except cx.ConnectorError as e:
            raise ApiError(422, "INVALID_CONNECTOR", str(e))
        except IntegrationUnavailable as e:
            raise _err(e)
        keys = {f["key"] for f in next(k for k in cx.catalog() if k["kind"] == c.kind)["fields"]} - set(cx.secret_keys(c.kind))
        c.name, c.config = body.name.strip(), {k: v for k, v in body.config.items() if k in keys and v not in (None, "")}
        c.status, c.last_error = "NOT_TESTED", None
        s.commit()
        store.audit(None, p.user, p.role, "connector_updated", {"connector": c.id})
        return _out(c)


@router.delete("/connectors/{cid}", status_code=204)
def delete(cid: str, p: Principal = Depends(require("integrate"))):
    with SessionLocal() as s:
        c = _get(s, cid)
        syncs = s.query(ConnectorSync).filter_by(connector_id=cid).all()
        ids, tables = [x.id for x in syncs], {x.table_name for x in syncs if x.table_name}
        switched = s.query(DatasetSource).filter(DatasetSource.sync_id.in_(ids)).all() if ids else []
        for x in switched + syncs:
            s.delete(x)
        s.flush()
        s.delete(c)
        s.commit()
    with engine.begin() as cn:                       # staging tables belong to this connector only
        for t in tables:
            cn.execute(text(f'DROP TABLE IF EXISTS "{t}"'))
    if switched:
        _refresh()
    store.audit(None, p.user, p.role, "connector_deleted", {"connector": cid, "datasets_reset": [x.dataset for x in switched]})


def _creds(c: Connector) -> dict:
    try:
        return cx.unseal(c.secret)
    except IntegrationUnavailable as e:
        raise _err(e)


@router.post("/connectors/{cid}/test")
def test(cid: str, p: Principal = Depends(require("integrate"))):
    with SessionLocal() as s:
        c = _get(s, cid)
        try:
            msg, c.status, c.last_error = cx.test(c.kind, c.config, _creds(c)), "CONNECTED", None
        except cx.ConnectorError as e:
            msg, c.status, c.last_error = str(e), "FAILED", str(e)
        c.last_checked_at = _now()
        s.commit()
        store.audit(None, p.user, p.role, "connector_tested", {"connector": c.id, "status": c.status})
        return {"ok": c.status == "CONNECTED", "message": msg, "connector": _out(c)}


@router.get("/connectors/{cid}/objects")
def list_objects(cid: str, _: Principal = Depends(require("integrate"))):
    with SessionLocal() as s:
        c = _get(s, cid)
    try:
        return cx.objects(c.kind, c.config, _creds(c))
    except (cx.ConnectorError, IntegrationUnavailable) as e:
        raise _err(e)


@router.get("/connectors/{cid}/preview")
def preview(cid: str, source: str = Query(min_length=1), _: Principal = Depends(require("integrate"))):
    with SessionLocal() as s:
        c = _get(s, cid)
    try:
        df = cx.fetch(c.kind, c.config, _creds(c), source, limit=20)
    except (cx.ConnectorError, IntegrationUnavailable) as e:
        raise _err(e)
    return {"columns": list(df.columns), "rows": df.astype(object).where(df.notna(), None).to_dict("records")}


# ------------------------------------------------------------------ syncs
@router.post("/connectors/{cid}/sync", status_code=201)
def sync(cid: str, body: SyncIn, p: Principal = Depends(require("integrate"))):
    if body.dataset not in data.TABLES:
        raise ApiError(422, "UNKNOWN_DATASET", f"Unknown dataset '{body.dataset}'", {"datasets": data.TABLES})
    ref = store.D()[body.dataset]                    # the shape planning expects
    with SessionLocal() as s:
        c = _get(s, cid)
        creds = _creds(c)
        run = ConnectorSync(connector_id=cid, source=body.source, dataset=body.dataset, actor=p.user)
        s.add(run)
        s.commit()
        try:
            df = cx.fetch(c.kind, c.config, creds, body.source)
            if df.empty:
                raise cx.ConnectorError("The source returned no rows")
            df = _conform(cx.map_columns(df, list(ref.columns), body.mapping), ref)
            run.table_name = f"ext_{cid.replace('-', '')[:8]}_{run.id.replace('-', '')[:8]}_{body.dataset}"[:63]
            df.to_sql(run.table_name, engine, if_exists="replace", index=True, index_label="_row", method="multi", chunksize=1000)
            run.rows, run.status = len(df), "SUCCEEDED"
        except Exception as e:                       # recorded on the run; the API answer says what went wrong
            run.status, run.error = "FAILED", str(e)[:500] if isinstance(e, (cx.ConnectorError, IntegrationUnavailable)) else f"{type(e).__name__}: {str(e)[:300]}"
            run.finished_at = _now()
            s.commit()
            store.audit(None, p.user, p.role, "connector_sync_failed", {"connector": cid, "source": body.source, "dataset": body.dataset})
            if isinstance(e, (cx.ConnectorError, IntegrationUnavailable)):
                raise _err(e)
            raise ApiError(422, "INVALID_DATA", f"The imported data could not be converted: {run.error}")
        run.finished_at = _now()
        s.commit()
        store.audit(None, p.user, p.role, "connector_synced", {"connector": cid, "source": body.source, "dataset": body.dataset, "rows": run.rows})
        return _sync_out(run)


def _sync_out(r: ConnectorSync) -> dict:
    return {"id": r.id, "connector_id": r.connector_id, "source": r.source, "dataset": r.dataset, "dataset_label": LABELS.get(r.dataset, r.dataset),
            "rows": r.rows, "status": r.status, "error": r.error, "actor": r.actor, "started_at": r.started_at, "finished_at": r.finished_at}


@router.get("/syncs")
def syncs(connector_id: str | None = None, _: Principal = Depends(require("read"))):
    with SessionLocal() as s:
        q = s.query(ConnectorSync)
        if connector_id:
            q = q.filter_by(connector_id=connector_id)
        return [_sync_out(r) for r in q.order_by(ConnectorSync.started_at.desc()).limit(100)]


@router.get("/activity")
def activity(_: Principal = Depends(require("read"))):
    return store.events(None)[-50:][::-1]


# ------------------------------------------------------------------ datasets: what planning reads
@router.get("/datasets")
@cache.route(60, ("data",))
def datasets(_: Principal = Depends(require("read"))):
    d = store.D()
    with SessionLocal() as s:
        src = {x.dataset: x for x in s.query(DatasetSource)}
        runs = {r.id: r for r in s.query(ConnectorSync).filter(ConnectorSync.id.in_([x.sync_id for x in src.values()]))} if src else {}
        names = {c.id: c.name for c in s.query(Connector)}
    out = []
    for t in data.TABLES:
        x = src.get(t)
        r = runs.get(x.sync_id) if x else None
        out.append({"dataset": t, "label": LABELS[t], "rows": len(d[t]), "columns": list(d[t].columns),
                    "source": {"kind": "connector", "connector": names.get(r.connector_id), "connector_id": r.connector_id, "object": r.source,
                               "sync_id": r.id, "synced_at": r.finished_at, "set_by": x.set_by, "set_at": x.set_at} if r
                    else {"kind": "built_in"}})
    return out


@router.put("/datasets/{dataset}/source")
def use_import(dataset: str, body: SourceIn, p: Principal = Depends(require("integrate"))):
    with SessionLocal() as s:
        r = s.get(ConnectorSync, body.sync_id)
        if not r or r.dataset != dataset or r.status != "SUCCEEDED":
            raise ApiError(422, "INVALID_SYNC", "Choose a successful import made for this dataset")
        with engine.connect() as cn:
            staged = pd.read_sql(text(f'SELECT * FROM "{r.table_name}" ORDER BY _row'), cn).drop(columns="_row")
        candidate = {**store.D(), dataset: staged}
        try:                                          # the planner must be able to read it before planning switches over
            data.to_inputs(candidate, None, Policy())
        except Exception as e:
            raise ApiError(422, "INVALID_DATA", f"Planning cannot use this import: {type(e).__name__}: {str(e)[:200]}")
        x = s.get(DatasetSource, dataset) or DatasetSource(dataset=dataset)
        x.sync_id, x.table_name, x.set_by, x.set_at = r.id, r.table_name, p.user, _now()
        s.merge(x)
        s.commit()
    _refresh()
    store.audit(None, p.user, p.role, "dataset_source_switched", {"dataset": dataset, "sync": body.sync_id, "rows": len(staged)})
    return {"dataset": dataset, "rows": len(staged), "source": "connector"}


@router.delete("/datasets/{dataset}/source")
def use_built_in(dataset: str, p: Principal = Depends(require("integrate"))):
    with SessionLocal() as s:
        x = s.get(DatasetSource, dataset)
        if x:
            s.delete(x)
            s.commit()
    _refresh()
    store.audit(None, p.user, p.role, "dataset_source_reset", {"dataset": dataset})
    return {"dataset": dataset, "source": "built_in"}


# ------------------------------------------------------------------ Neon: the app's own database (built-in, always connected)
NEON_LABELS = {**LABELS, "gen_twin_machine": "Digital twin machines", "gen_twin_line_hourly": "Line hours (digital twin)", "gen_vehicle_hourly": "Vehicle hours",
               "gen_material_supplier": "Material suppliers", "gen_crew": "Crews", "gen_crew_leave": "Crew leave", "gen_cost_rate": "Cost rates",
               "gen_shipment": "Shipments", "gen_po_confirmation": "Supplier PO confirmations", "gen_machine_hourly": "Machine hours",
               "gen_maintenance_event": "Maintenance events", "gen_vehicle": "Vehicles",
               "schedules": "Plans", "events": "Plan history", "order_intake": "Uploaded orders", "upload_log": "Upload log", "order_freeze": "Frozen orders",
               "team_members": "Team", "app_users": "User accounts", "app_sessions": "Sign-in sessions", "conversations": "Assistant chats",
               "messages": "Assistant messages", "agent_runs": "Agent runs", "agent_tasks": "Agent tasks", "agent_looks": "Agent checks",
               "agent_events": "Agent events", "agent_incidents": "Incidents", "notifications": "Notifications", "connectors": "Connections",
               "connector_syncs": "Imports", "dataset_sources": "Dataset choices"}
NEON_PREVIEW = {"order_intake", "upload_log", "order_freeze"}     # app records safe to show; never users, sessions, connections or chats


def _neon_tables(cn) -> dict[str, list[str]]:
    cols: dict[str, list[str]] = {}
    for t, c in cn.execute(text("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public' "
                                "ORDER BY table_name, ordinal_position")):
        if c != "_row":
            cols.setdefault(t, []).append(c)
    return cols


@router.get("/neon")
@cache.route(120, ("data",))
def neon(_: Principal = Depends(require("read"))):
    """The Neon database the app runs on: is it reachable, and which tables (with row counts and columns) it supplies."""
    if engine.dialect.name != "postgresql":
        return {"connected": False, "message": "This copy of the app runs on a local file, not Neon."}
    try:
        with engine.connect() as cn:
            t0 = dt.datetime.now()
            cn.execute(text("SELECT 1"))
            ms = round((dt.datetime.now() - t0).total_seconds() * 1000)
            name = cn.execute(text("SELECT current_database()")).scalar()
            cols = _neon_tables(cn)
            q = " UNION ALL ".join(f"SELECT '{t}' AS t, count(*) AS n FROM \"{t}\"" for t in cols if t.replace("_", "").isalnum())
            rows = dict(cn.execute(text(q)).all()) if q else {}
    except Exception:
        return {"connected": False, "message": "Neon could not be reached right now."}
    tables = []
    for t, c in sorted(cols.items()):
        key = t[4:] if t.startswith("src_") else t
        tables.append({"name": t, "label": NEON_LABELS.get(key, key.replace("_", " ").capitalize()), "rows": rows.get(t, 0), "columns": c,
                       "group": ("planning" if key in LABELS else "plant") if t.startswith("src_") else "app",
                       "preview": t.startswith("src_") or t in NEON_PREVIEW})
    return {"connected": True, "database": name, "checked_at": _now(), "response_ms": ms, "tables": tables,
            "rows": sum(x["rows"] for x in tables), "used_for_planning": data_source_is_db()}


def data_source_is_db() -> bool:
    try:
        with engine.connect() as cn:
            return bool(cn.execute(text("SELECT 1 FROM information_schema.tables WHERE table_name = 'src_fact_customer_order'")).first())
    except Exception:
        return False


@router.get("/neon/tables/{name}")
def neon_table(name: str, limit: int = Query(25, ge=1, le=200), _: Principal = Depends(require("read"))):
    """First rows of one Neon table (planning data and a few safe app records only)."""
    if engine.dialect.name != "postgresql":
        raise ApiError(404, "NOT_NEON", "This copy of the app doesn't run on Neon")
    with engine.connect() as cn:
        cols = _neon_tables(cn)
        if name not in cols or not (name.startswith("src_") or name in NEON_PREVIEW):
            raise ApiError(404, "TABLE_NOT_SHOWN", "This table can't be previewed")
        order = "ORDER BY _row" if name.startswith("src_") else ""
        df = pd.read_sql(text(f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols[name])} FROM "{name}" {order} LIMIT {limit}'), cn)
    df = df.astype(object).where(df.notna(), None)
    return {"name": name, "columns": list(df.columns), "rows": df.to_dict("records")}

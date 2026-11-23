"""Data connectors: SAP S/4HANA (OData v2/v4) and SQL databases (PostgreSQL, MySQL, SQL Server, Oracle).

Every connector is READ-ONLY: it tests the connection, lists tables/entity sets and reads rows. Nothing is ever written to the
external system. Imported rows land in a staging table of the app database (Neon) and only feed planning after an admin switches
a dataset to that table (app/integrations_api.py), which can be undone.

Credentials are encrypted with CONNECTOR_KEY (Fernet) before they are stored, and never returned by the API.
"""
import datetime as dt
import json
import xml.etree.ElementTree as ET
import pandas as pd
import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool
from .config import Settings
from .integrations import IntegrationUnavailable, NotConfigured

TIMEOUT = 15
MAX_ROWS = 200_000          # ponytail: one request/query per sync; add paging when sources outgrow this

_DB = [("host", "Host", "text", True), ("port", "Port", "number", True), ("database", "Database", "text", True),
       ("schema", "Schema (optional)", "text", False), ("username", "Username", "text", True), ("password", "Password", "secret", True)]
KINDS = {
    "sap_odata": {"label": "SAP S/4HANA", "group": "ERP", "description": "Sales orders, production orders, materials and stock through SAP OData services.",
                  "fields": [("base_url", "OData service URL", "url", True), ("sap_client", "SAP client (optional)", "text", False),
                             ("username", "Username (optional)", "text", False), ("password", "Password (optional)", "secret", False),
                             ("api_key", "API key (SAP sandbox, optional)", "secret", False)]},
    "postgresql": {"label": "PostgreSQL", "group": "Database", "default_port": 5432, "description": "MES, WMS or data-warehouse tables in PostgreSQL.",
                   "fields": _DB + [("sslmode", "SSL", "select:require,prefer,disable", False)]},
    "mysql": {"label": "MySQL", "group": "Database", "default_port": 3306, "description": "Tables in a MySQL or MariaDB database.", "fields": _DB},
    "mssql": {"label": "Microsoft SQL Server", "group": "Database", "default_port": 1433, "description": "Tables in SQL Server (for example an MES database).", "fields": _DB},
    "oracle": {"label": "Oracle Database", "group": "Database", "default_port": 1521, "description": "Tables in an Oracle database (service name).",
               "fields": [f if f[0] != "database" else ("database", "Service name", "text", True) for f in _DB]},
}


class ConnectorError(Exception):
    """Connection or read failed. The message is safe to show (it never contains credentials)."""
    def __init__(self, msg: str, missing: list[str] | None = None):
        super().__init__(msg)
        self.missing = missing or []


def catalog() -> list[dict]:
    return [{"kind": k, "label": v["label"], "group": v["group"], "description": v["description"], "default_port": v.get("default_port"),
             "fields": [{"key": f[0], "label": f[1], "type": f[2].split(":")[0], "required": f[3],
                         **({"options": f[2].split(":")[1].split(",")} if ":" in f[2] else {})} for f in v["fields"]]} for k, v in KINDS.items()]


def secret_keys(kind: str) -> list[str]:
    return [f[0] for f in KINDS[kind]["fields"] if f[2] == "secret"]


def validate(kind: str, config: dict, secrets: dict) -> None:
    if kind not in KINDS:
        raise ConnectorError(f"Unknown connector type '{kind}'")
    for key, label, typ, req in KINDS[kind]["fields"]:
        v = (secrets if typ == "secret" else config).get(key)
        if req and v in (None, ""):
            raise ConnectorError(f"{label} is required")
        if typ == "url" and v and not str(v).startswith(("https://", "http://")):
            raise ConnectorError(f"{label} must start with https://")
        if typ == "number" and v not in (None, "") and not str(v).isdigit():
            raise ConnectorError(f"{label} must be a number")


# ------------------------------------------------------------------ credentials
def _fernet() -> Fernet:
    k = Settings().connector_key
    if not (k and k.get_secret_value()):
        raise NotConfigured("CONNECTOR_KEY is not set, so connector credentials cannot be stored safely")
    return Fernet(k.get_secret_value().encode())


def seal(secrets: dict) -> str | None:
    clean = {k: v for k, v in secrets.items() if v not in (None, "")}
    return _fernet().encrypt(json.dumps(clean).encode()).decode() if clean else None


def unseal(token: str | None) -> dict:
    if not token:
        return {}
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except InvalidToken:
        raise IntegrationUnavailable("saved credentials cannot be decrypted (CONNECTOR_KEY changed); enter the password again") from None


# ------------------------------------------------------------------ SQL databases
def _url(kind: str, c: dict, s: dict) -> tuple[URL, dict]:
    port = int(c.get("port") or KINDS[kind]["default_port"])
    common = dict(username=c["username"], password=s.get("password"), host=c["host"], port=port)
    if kind == "postgresql":
        return URL.create("postgresql+psycopg", database=c["database"], query={"sslmode": c.get("sslmode") or "require"}, **common), \
            {"connect_timeout": TIMEOUT}
    if kind == "mysql":
        return URL.create("mysql+pymysql", database=c["database"], **common), {"connect_timeout": TIMEOUT}
    if kind == "mssql":
        return URL.create("mssql+pymssql", database=c["database"], **common), {"login_timeout": TIMEOUT, "timeout": 120}
    return URL.create("oracle+oracledb", query={"service_name": c["database"]}, **common), {"tcp_connect_timeout": TIMEOUT}


def _engine(kind: str, c: dict, s: dict):
    url, args = _url(kind, c, s)
    return create_engine(url, connect_args=args, poolclass=NullPool)        # one-off connections: nothing stays open


def _safe(e: Exception) -> str:
    """First line of a driver error, without anything that could echo the password."""
    msg = (str(getattr(e, "orig", None) or e).splitlines() or [type(e).__name__])[0][:300]
    return f"{type(e).__name__}: {msg}"


# ------------------------------------------------------------------ SAP OData
def _sap(c: dict, s: dict, path: str, params: dict | None = None) -> httpx.Response:
    p = dict(params or {})
    if c.get("sap_client"):
        p["sap-client"] = c["sap_client"]
    auth = (c["username"], s.get("password", "")) if c.get("username") else None
    accept = "application/xml" if path.endswith("$metadata") else "application/json"        # $metadata is always XML (CSDL)
    headers = {"Accept": accept} | ({"APIKey": s["api_key"]} if s.get("api_key") else {})
    try:
        r = httpx.get(c["base_url"].rstrip("/") + path, params=p, auth=auth, headers=headers, timeout=TIMEOUT * 2, follow_redirects=False)
    except httpx.HTTPError as e:
        raise ConnectorError(f"SAP service unreachable ({type(e).__name__})") from None
    if r.status_code in (401, 403):
        raise ConnectorError(f"SAP rejected the credentials (HTTP {r.status_code})")
    if r.status_code != 200:
        raise ConnectorError(f"SAP returned HTTP {r.status_code}")
    return r


def _entity_sets(xml: str) -> list[str]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise ConnectorError("The service did not return OData metadata; check the service URL") from None
    return sorted({e.get("Name") for e in root.iter() if e.tag.endswith("}EntitySet") and e.get("Name")})


def _odata_value(v):
    """OData v2 (SAP Gateway) sends Edm.DateTime as "/Date(1660000000000)/" or "/Date(1660000000000+0000)/" -> ISO timestamp (UTC)."""
    if isinstance(v, str) and v.startswith("/Date(") and v.endswith(")/"):
        body = v[6:-2]
        cut = next((i for i, ch in enumerate(body) if i and ch in "+-"), len(body))      # drop a trailing offset
        try:
            return dt.datetime.fromtimestamp(int(body[:cut]) / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return v
    return v


def _rows(j: dict) -> list[dict]:
    d = j.get("d", j)
    rows = d.get("results", d.get("value")) if isinstance(d, dict) else d
    if not isinstance(rows, list):
        raise ConnectorError("Unexpected OData response")
    return [{k: _odata_value(v) for k, v in r.items() if not isinstance(v, (dict, list))} for r in rows]   # drop __metadata and links


# ------------------------------------------------------------------ operations
def test(kind: str, c: dict, s: dict) -> str:
    """Opens a real connection. Returns a short description of what was reached; raises ConnectorError."""
    if kind == "sap_odata":
        n = len(_entity_sets(_sap(c, s, "/$metadata", {}).text))
        return f"Connected to the SAP service: {n} data sets available"
    eng = _engine(kind, c, s)
    try:
        with eng.connect() as cn:
            cn.execute(text("SELECT 1 FROM DUAL" if kind == "oracle" else "SELECT 1"))
            ver = ".".join(str(x) for x in (eng.dialect.server_version_info or ())[:2])
        return f"Connected to {KINDS[kind]['label']}" + (f" {ver}" if ver else "")
    except Exception as e:
        raise ConnectorError(_safe(e)) from None
    finally:
        eng.dispose()


def objects(kind: str, c: dict, s: dict) -> list[str]:
    if kind == "sap_odata":
        return _entity_sets(_sap(c, s, "/$metadata", {}).text)
    eng = _engine(kind, c, s)
    try:
        ins = inspect(eng)
        sch = c.get("schema") or None
        return sorted(set(ins.get_table_names(schema=sch)) | set(ins.get_view_names(schema=sch)))
    except Exception as e:
        raise ConnectorError(_safe(e)) from None
    finally:
        eng.dispose()


def fetch(kind: str, c: dict, s: dict, obj: str, limit: int = MAX_ROWS) -> pd.DataFrame:
    """Reads up to `limit` rows of one table/entity set. `obj` must be one of objects() (no free-form SQL)."""
    if obj not in objects(kind, c, s):
        raise ConnectorError(f"'{obj}' was not found in this source")
    if kind == "sap_odata":
        return pd.DataFrame(_rows(_sap(c, s, f"/{obj}", {"$top": str(limit), "$format": "json"}).json()))
    eng = _engine(kind, c, s)
    try:
        t = Table(obj, MetaData(), autoload_with=eng, schema=c.get("schema") or None)
        with eng.connect() as cn:
            return pd.read_sql(select(t).limit(limit), cn)
    except Exception as e:
        raise ConnectorError(_safe(e)) from None
    finally:
        eng.dispose()


def map_columns(df: pd.DataFrame, required: list[str], mapping: dict | None = None) -> pd.DataFrame:
    """Planning column <- source column: explicit mapping first, then the same name ignoring case. Raises when any is missing."""
    lower = {c.lower(): c for c in df.columns}
    pick, missing = {}, []
    for col in required:
        src = (mapping or {}).get(col) or lower.get(col.lower())
        if src in df.columns:
            pick[col] = src
        else:
            missing.append(col)
    if missing:
        raise ConnectorError("Missing columns: " + ", ".join(missing), missing)
    return pd.DataFrame({col: df[src] for col, src in pick.items()})

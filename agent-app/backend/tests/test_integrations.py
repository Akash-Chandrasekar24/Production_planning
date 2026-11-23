"""Connectors end to end: create -> test -> browse -> preview -> sync -> switch planning to the import -> back -> delete.
A local SQLite file stands in for the external database (the SQL path is the same SQLAlchemy code); SAP OData is mocked."""
import os
import tempfile
import httpx
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from app import connectors, data, store
from app.db import Connector, SessionLocal, engine
from app.main import app

c = TestClient(app)
AD = {"x-role": "admin", "x-user": "ada"}
PL = {"x-role": "planner", "x-user": "pat"}
VW = {"x-role": "viewer", "x-user": "vic"}
PG = {"name": "MES database", "kind": "postgresql",
      "config": {"host": "mes.example", "port": 5432, "database": "mes", "username": "reader", "sslmode": "require"},
      "secrets": {"password": "s3cret-pw"}}


@pytest.fixture(scope="module")
def source_db():
    """External 'MES' database: customer orders with upper-case columns and real DATE types, plus an unrelated table."""
    path = os.path.join(tempfile.mkdtemp(), "mes.db").replace("\\", "/")
    eng = create_engine(f"sqlite:///{path}")
    co = data.load_csv()["fact_customer_order"].copy()
    for col in ("order_date", "requested_delivery_date", "confirmed_delivery_date"):
        co[col] = pd.to_datetime(co[col])
    co.columns = [x.upper() for x in co.columns]
    co.to_sql("SALES_ORDERS", eng, index=False)
    pd.DataFrame({"x": [1, 2]}).to_sql("misc", eng, index=False)
    eng.dispose()
    return path


@pytest.fixture
def sqlite_as_postgres(monkeypatch, source_db):
    seen = {}

    def fake_engine(kind, cfg, secrets):
        seen.update(kind=kind, password=secrets.get("password"))
        return create_engine(f"sqlite:///{source_db}")
    monkeypatch.setattr(connectors, "_engine", fake_engine)
    return seen


def test_catalog_lists_sap_and_databases():
    kinds = {k["kind"]: k for k in c.get("/api/integrations/catalog", headers=VW).json()}
    assert set(kinds) == {"sap_odata", "postgresql", "mysql", "mssql", "oracle"}
    assert any(f["key"] == "password" and f["type"] == "secret" for f in kinds["postgresql"]["fields"])


def test_only_admins_manage_connectors():
    for h in (VW, PL):
        assert c.post("/api/integrations/connectors", json=PG, headers=h).status_code == 403
    assert c.post("/api/integrations/connectors", json={**PG, "config": {}}, headers=AD).status_code == 422     # required fields
    assert c.post("/api/integrations/connectors", json={**PG, "kind": "ftp"}, headers=AD).status_code == 422


def test_password_is_encrypted_and_never_returned():
    r = c.post("/api/integrations/connectors", json=PG, headers=AD)
    j = r.json()
    assert r.status_code == 201 and j["has_secret"] and "s3cret-pw" not in r.text and "password" not in j["config"]
    with SessionLocal() as s:
        row = s.get(Connector, j["id"])
        assert "s3cret-pw" not in row.secret and connectors.unseal(row.secret) == {"password": "s3cret-pw"}
    assert "s3cret-pw" not in c.get("/api/integrations/connectors", headers=VW).text
    # update with a blank password keeps the saved one
    r = c.put(f"/api/integrations/connectors/{j['id']}", json={**PG, "name": "MES (renamed)", "secrets": {"password": ""}}, headers=AD)
    assert r.status_code == 200 and r.json()["name"] == "MES (renamed)"
    with SessionLocal() as s:
        assert connectors.unseal(s.get(Connector, j["id"]).secret) == {"password": "s3cret-pw"}


def test_full_flow_from_database_to_planning(sqlite_as_postgres):
    cid = c.post("/api/integrations/connectors", json=PG, headers=AD).json()["id"]
    t = c.post(f"/api/integrations/connectors/{cid}/test", headers=AD).json()
    assert t["ok"] and t["connector"]["status"] == "CONNECTED" and sqlite_as_postgres["password"] == "s3cret-pw"
    assert c.get(f"/api/integrations/connectors/{cid}/objects", headers=AD).json() == ["SALES_ORDERS", "misc"]
    pv = c.get(f"/api/integrations/connectors/{cid}/preview?source=SALES_ORDERS", headers=AD).json()
    assert len(pv["rows"]) == 20 and "CUSTOMER_ORDER_ID" in pv["columns"]
    assert c.get(f"/api/integrations/connectors/{cid}/preview?source=nope", headers=AD).status_code == 502      # only listed objects

    bad = c.post(f"/api/integrations/connectors/{cid}/sync", json={"source": "misc", "dataset": "fact_customer_order"}, headers=AD)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "MISSING_COLUMNS" and "order_qty" in bad.json()["error"]["detail"]["missing"]

    r = c.post(f"/api/integrations/connectors/{cid}/sync", json={"source": "SALES_ORDERS", "dataset": "fact_customer_order"}, headers=AD)
    sync = r.json()
    built_in = store.D()["fact_customer_order"]
    assert r.status_code == 201 and sync["status"] == "SUCCEEDED" and sync["rows"] == len(built_in)
    staged = [x for x in inspect(engine).get_table_names() if x.startswith("ext_")]
    assert staged
    try:
        assert c.put("/api/integrations/datasets/fact_customer_order/source", json={"sync_id": sync["id"]}, headers=PL).status_code == 403
        r = c.put("/api/integrations/datasets/fact_customer_order/source", json={"sync_id": sync["id"]}, headers=AD)
        assert r.status_code == 200
        ds = {x["dataset"]: x for x in c.get("/api/integrations/datasets", headers=VW).json()}
        assert ds["fact_customer_order"]["source"]["kind"] == "connector" and ds["fact_customer_order"]["source"]["object"] == "SALES_ORDERS"
        now = store.D()["fact_customer_order"]
        pd.testing.assert_frame_equal(now.reset_index(drop=True), built_in.reset_index(drop=True), check_dtype=False)  # same data, same formats
        assert now.order_date.iloc[0] == built_in.order_date.iloc[0]                   # dates converted back to YYYY-MM-DD text
        assert c.post("/api/schedules", json={}, headers=PL).status_code < 300        # planning runs on the imported data
        assert c.get("/api/insights", headers=VW).status_code == 200
    finally:
        assert c.delete("/api/integrations/datasets/fact_customer_order/source", headers=AD).status_code == 200
    ds = {x["dataset"]: x for x in c.get("/api/integrations/datasets", headers=VW).json()}
    assert ds["fact_customer_order"]["source"]["kind"] == "built_in"
    acts = [e["action"] for e in c.get("/api/integrations/activity", headers=VW).json()]
    assert {"connector_created", "connector_tested", "connector_synced", "dataset_source_switched", "dataset_source_reset"} <= set(acts)

    assert c.delete(f"/api/integrations/connectors/{cid}", headers=AD).status_code == 204
    with engine.connect() as cn:
        left = [x for x in inspect(cn).get_table_names() if x.startswith(f"ext_{cid.replace('-', '')[:8]}")]
    assert left == []


def test_failed_connection_is_recorded_without_the_password(monkeypatch):
    def boom(kind, cfg, secrets):
        return create_engine("sqlite:////nonexistent-dir/x/y.db")
    monkeypatch.setattr(connectors, "_engine", boom)
    cid = c.post("/api/integrations/connectors", json=PG, headers=AD).json()["id"]
    t = c.post(f"/api/integrations/connectors/{cid}/test", headers=AD)
    assert t.status_code == 200 and not t.json()["ok"] and t.json()["connector"]["status"] == "FAILED" and "s3cret-pw" not in t.text


SAP = {"name": "SAP S/4HANA", "kind": "sap_odata",
       "config": {"base_url": "https://sap.example/sap/opu/odata/sap/API_SALES_ORDER_SRV", "sap_client": "100", "username": "RFC_USER"},
       "secrets": {"password": "sap-pw"}}
META = """<?xml version="1.0"?><edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx"><edmx:DataServices>
<Schema xmlns="http://schemas.microsoft.com/ado/2008/09/edm"><EntityContainer Name="C">
<EntitySet Name="A_SalesOrder" EntityType="x"/><EntitySet Name="A_SalesOrderItem" EntityType="y"/></EntityContainer></Schema>
</edmx:DataServices></edmx:Edmx>"""


class Resp:
    def __init__(self, code=200, text="", body=None):
        self.status_code, self.text, self._b = code, text, body

    def json(self):
        return self._b


def test_sap_odata_connector(monkeypatch):
    calls = []

    def get(url, params=None, auth=None, headers=None, timeout=None, follow_redirects=None):
        calls.append({"url": url, "params": params, "auth": auth})
        if auth != ("RFC_USER", "sap-pw"):
            return Resp(401)
        if url.endswith("/$metadata"):
            return Resp(text=META) if headers["Accept"] == "application/xml" else Resp(415)    # as real OData services do
        assert headers["Accept"] == "application/json"
        return Resp(body={"d": {"results": [{"__metadata": {"uri": "x"}, "SalesOrder": "1", "TotalNetAmount": "10.5",
                                             "CreationDate": "/Date(1785542400000)/", "to_Item": {"__deferred": {}}}]}})
    monkeypatch.setattr(httpx, "get", get)
    cid = c.post("/api/integrations/connectors", json=SAP, headers=AD).json()["id"]
    t = c.post(f"/api/integrations/connectors/{cid}/test", headers=AD).json()
    assert t["ok"] and "2 data sets" in t["message"]
    assert calls[0]["params"] == {"sap-client": "100"} and calls[0]["url"].endswith("API_SALES_ORDER_SRV/$metadata")
    assert c.get(f"/api/integrations/connectors/{cid}/objects", headers=AD).json() == ["A_SalesOrder", "A_SalesOrderItem"]
    pv = c.get(f"/api/integrations/connectors/{cid}/preview?source=A_SalesOrder", headers=AD).json()
    assert pv["columns"] == ["SalesOrder", "TotalNetAmount", "CreationDate"] and calls[-1]["params"]["$top"] == "20"   # metadata/links dropped
    assert pv["rows"][0]["CreationDate"] == "2026-08-01T00:00:00"                                       # OData v2 date -> ISO
    c.put(f"/api/integrations/connectors/{cid}", json={**SAP, "secrets": {"password": "wrong"}}, headers=AD)
    t = c.post(f"/api/integrations/connectors/{cid}/test", headers=AD).json()
    assert not t["ok"] and "credentials" in t["message"]

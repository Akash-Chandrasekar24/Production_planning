"""Orchestrator and its agents: floor, inventory and order watcher look; the orchestrator routes what they find."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from app import db, inventory_agent, notify, orchestrator, store
from app.main import app

c = TestClient(app)
PL = {"x-role": "planner", "x-user": "pat"}
PM = {"x-role": "production_manager", "x-user": "maya"}
TN = {"x-role": "plant_head", "x-user": "tanu", "x-plant": "IN-TN-01"}
VW = {"x-role": "viewer", "x-user": "vic"}


@pytest.fixture(scope="module", autouse=True)
def clean():
    def wipe():
        with db.engine.begin() as cn:
            for t in ("agent_tasks", "agent_runs", "agent_events", "agent_incidents", "agent_looks", "notifications", "order_intake", "team_members"):
                cn.execute(text(f"DELETE FROM {t}"))
        store.D.cache_clear(); store._inputs_cache.clear()
    wipe()
    yield
    wipe()


def test_a_round_asks_every_agent_and_routes_each_finding():
    r = c.post("/api/agent/check", headers=PL).json()
    assert r["orders"]["outcome"] == "WATCHING"                                   # first look at the order book
    evs = r["events"]
    assert evs and all(e["status"] in ("ALERTED", "RECOVERY", "PLANNED") for e in evs)
    agents = {e["agent"] for e in evs}
    assert "inventory agent" in agents                                             # the data's materials cover only a few days
    low = next(e for e in evs if e["type"] == "inventory.low_cover")
    assert low["routed_to"] == "production manager" and "cover" in low["title"]
    for e in evs:                                                                  # floor alerts go to that plant's head
        if e["agent"] == "floor agent" and e["status"] == "ALERTED":
            assert "production head of " + e["plant_id"] in e["routed_to"]
    alerts = [t for t in c.get("/api/agent/tasks", headers=VW).json() if t["kind"] == "alert"]
    assert alerts and {t["details"]["event_id"] for t in alerts} <= {e["id"] for e in evs}
    st = c.get("/api/agent/orchestrator", headers=VW).json()
    assert [a["key"] for a in st["agents"]] == ["floor", "inventory", "orders", "sourcing", "planning"]
    assert all(a["last"] and not a["last"]["error"] for a in st["agents"][:4]) and len(st["events"]) == len(evs)


def test_the_same_finding_is_raised_once():
    assert c.post("/api/agent/check", headers=PL).json()["events"] == []


def test_new_plans_carry_what_the_floor_and_inventory_agents_found():
    orders = {"source": "test", "orders": [{"customer_order_id": f"CO-OR{i:04d}", "customer_id": "CUS-001", "product_id": "SPH-A15-128", "order_qty": 2000,
                                            "requested_delivery_date": "2026-12-20", "priority": "MEDIUM", "channel": "B2B"} for i in range(25)]}
    r = c.post("/api/orders/intake", json=orders, headers=PL).json()
    run = r["agent"]
    assert run["outcome"] == "PLANNED"
    notes = run["details"]["notes"]
    assert any(n["from"] == "inventory" and "days of production" in n["text"] for n in notes)
    pm = next(t for t in run["tasks"] if t["role"] == "production_manager")
    assert pm["details"]["notes"] == notes
    ev = c.get("/api/agent/orchestrator", headers=VW).json()["events"][0]
    assert ev["type"] == "orders.replan" and ev["status"] == "PLANNED" and ev["refs"]["schedule_id"] == run["schedule_id"]


def test_a_reported_breakdown_goes_through_the_orchestrator_with_inventory_context():
    i = c.post("/api/agent/incidents", json={"kind": "MACHINE_DOWN", "line_id": "FAL-L1", "started_at": "2026-11-01T06:00:00", "repair_hours": 24}, headers=TN).json()
    assert i["analysis"]["context"]["inventory"]["low_cover"]                         # what the inventory agent said about these products
    assert any(f.startswith("Inventory agent:") for f in i["analysis"]["findings"])
    ev = c.get("/api/agent/orchestrator", headers=VW).json()["events"][0]
    assert ev["type"] == "floor.reported" and ev["status"] == "RECOVERY" and ev["refs"]["incident_id"] == i["id"] and ev["agent"] == "reported by tanu"


def test_one_agent_failing_does_not_stop_the_round(monkeypatch):
    def broken():
        raise RuntimeError("feed down")
    monkeypatch.setattr(inventory_agent, "sense", broken)
    r = c.post("/api/agent/check", headers=PL).json()
    assert r["orders"] is not None                                                 # the order watcher still looked
    st = {a["key"]: a for a in c.get("/api/agent/orchestrator", headers=VW).json()["agents"]}
    assert st["inventory"]["last"]["error"] == "RuntimeError" and not st["floor"]["last"]["error"]
    assert c.post("/api/agent/check?agents=nope", headers=PL).status_code == 422


def test_agent_status_is_kept_in_the_database():
    with db.SessionLocal() as s:
        looks = s.query(db.AgentLook).all()
    assert {x.agent for x in looks} == {"floor", "inventory", "orders", "sourcing"} and all(x.at for x in looks)
    st = {a["key"]: a for a in c.get("/api/agent/orchestrator", headers=VW).json()["agents"]}
    assert st["floor"]["last"]["looks"] >= 2 and st["floor"]["last"]["summary"]


def test_tasks_become_notifications_in_the_app(monkeypatch):
    pm = c.get("/api/notifications", headers=PM).json()
    assert pm["items"] and pm["unread"] == len(pm["items"]) and all(n["email_status"] == "IN_APP" for n in pm["items"])   # never emailed
    assert any(n["title"].startswith("Alert:") for n in pm["items"])
    tn = c.get("/api/notifications", headers=TN).json()["items"]
    assert tn and all(n["plant_id"] == "IN-TN-01" for n in tn)                    # plant heads see their own plant only
    first = pm["items"][0]
    assert c.post(f"/api/notifications/{first['id']}/read", headers=PM).status_code == 200
    assert c.get("/api/notifications", headers=PM).json()["unread"] == pm["unread"] - 1
    assert c.post("/api/notifications/read-all", headers=PM).json()["read"] >= 1 and c.get("/api/notifications", headers=PM).json()["unread"] == 0


def test_email_goes_to_the_named_person(monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def starttls(self): pass
        def login(self, u, pw): sent.append(("login", u))
        def send_message(self, m): sent.append((m["To"], m["Subject"], m.get_body(("plain",)).get_content()))
        def quit(self): pass
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_FROM", "planning@test")
    monkeypatch.setenv("SMTP_USER", "bot")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    AD = {"x-role": "admin", "x-user": "ada"}
    mid = c.post("/api/agent/team", json={"name": "Tanu R", "role": "plant_head", "plant_id": "IN-TN-01", "email": "tanu@test"}, headers=AD).json()["id"]
    assert c.post(f"/api/agent/team/{mid}/test-email", headers=AD).json() == {"sent": True}
    c.post("/api/agent/incidents", json={"kind": "MACHINE_DOWN", "line_id": "SMT-L1", "started_at": "2026-11-01T06:00:00", "repair_hours": 6}, headers=TN)
    notify.send_pending()
    mails = [x for x in sent if x[0] == "tanu@test"]
    assert len(mails) == 1 and "Open it in the app" in mails[0][2]                      # only the test email: alerts stay in the app
    with db.SessionLocal() as s:
        n = s.query(db.Notification).filter_by(member="Tanu R").all()
    assert n and all(x.email_status == "IN_APP" for x in n)
    assert all("secret" not in str(m) for m in sent)
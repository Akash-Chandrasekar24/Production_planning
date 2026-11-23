"""Planning agent: orders come in -> it decides -> plans -> hands work to the production manager and each plant head."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from app import db, store
from app.main import app

c = TestClient(app)
PL = {"x-role": "planner", "x-user": "pat"}
PM = {"x-role": "production_manager", "x-user": "maya"}
TN = {"x-role": "plant_head", "x-user": "tanu", "x-plant": "IN-TN-01"}
KA = {"x-role": "plant_head", "x-user": "kiran", "x-plant": "IN-KA-01"}
AD = {"x-role": "admin", "x-user": "ada"}
VW = {"x-role": "viewer", "x-user": "vic"}


def orders(n, qty=15000, prio="MEDIUM", due="2026-12-20", prefix="T"):
    return {"source": "test", "orders": [{"customer_order_id": f"CO-{prefix}{i:04d}", "customer_id": "CUS-001", "product_id": ["SPH-A15-128", "SPH-A15-256", "SPH-PRO-256"][i % 3],
                                          "order_qty": qty, "requested_delivery_date": due, "priority": prio, "channel": "B2B"} for i in range(n)]}


@pytest.fixture(scope="module", autouse=True)
def clean():
    def wipe():
        with db.engine.begin() as cn:
            for t in ("agent_tasks", "agent_runs", "agent_events", "agent_incidents", "order_intake", "team_members"):
                cn.execute(text(f"DELETE FROM {t}"))
        store.D.cache_clear(); store._inputs_cache.clear()
    wipe()
    yield
    wipe()                                              # leave the order book as the other tests expect it


def test_intake_is_validated_and_all_or_nothing():
    bad = orders(3)
    bad["orders"][1]["product_id"] = "NOPE"
    r = c.post("/api/orders/intake", json=bad, headers=PL)
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_ORDERS" and r.json()["error"]["detail"][0]["row"] == 2
    assert c.get("/api/orders/intake", headers=VW).json() == []                       # nothing stored
    assert c.post("/api/orders/intake", json=orders(1), headers=VW).status_code == 403
    assert c.post("/api/orders/intake", json=orders(1, due="2020-01-01"), headers=PL).status_code == 422


def test_agent_watches_until_enough_orders_arrive_then_plans_and_assigns():
    first = c.post("/api/agent/check?agents=orders", headers=PL).json()["orders"]
    assert first["outcome"] == "WATCHING" and "Started watching" in first["reason"]
    few = c.post("/api/orders/intake", json=orders(5, qty=100, prefix="A"), headers=PL).json()
    assert few["received"] == 5 and few["agent"]["outcome"] == "WATCHING" and few["agent"]["new_orders"] == 5 and "below the thresholds" in few["agent"]["reason"]
    many = c.post("/api/orders/intake", json=orders(20, prefix="B"), headers=PL).json()
    run = many["agent"]
    assert run["outcome"] == "PLANNED" and run["new_orders"] == 25 and "25 new orders" in run["reason"]      # arrivals add up since the last plan
    sched = store.get(run["schedule_id"])
    assert sched.status == "PENDING_APPROVAL" and sched.created_by == "planning agent" and sched.assignments        # a proposal, never approved by the agent
    roles = sorted((t["role"], t["plant_id"]) for t in run["tasks"])
    assert roles == [("plant_head", "IN-KA-01"), ("plant_head", "IN-TN-01"), ("production_manager", None)]
    pm = next(t for t in run["tasks"] if t["role"] == "production_manager")
    assert pm["kind"] == "approve" and pm["status"] == "OPEN" and "IN-TN-01" in str(pm["details"]["plants"])
    heads = [t for t in run["tasks"] if t["role"] == "plant_head" and t["details"]["jobs"]]
    assert heads and all(t["status"] == "WAITING_APPROVAL" and t["details"]["lines"] for t in heads)


def test_inboxes_are_per_role_and_plant_and_only_the_owner_can_act():
    tn = c.get("/api/agent/tasks?mine=true", headers=TN).json()
    assert tn and all(t["role"] == "plant_head" and t["plant_id"] == "IN-TN-01" for t in tn)
    assert all(t["role"] == "production_manager" for t in c.get("/api/agent/tasks?mine=true", headers=PM).json())
    ka_task = c.get("/api/agent/tasks?mine=true", headers=KA).json()[0]
    assert c.post(f"/api/agent/tasks/{ka_task['id']}/acknowledge", headers=TN).json()["error"]["code"] == "NOT_YOUR_TASK"
    pm_task = next(t for t in c.get("/api/agent/tasks?mine=true", headers=PM).json() if t["kind"] == "approve")
    assert c.post(f"/api/agent/tasks/{pm_task['id']}/done", headers=PM).json()["error"]["code"] == "DECIDE_ON_REVIEW_PAGE"


def test_approval_by_the_production_manager_releases_the_plant_tasks():
    pm_task = next(t for t in c.get("/api/agent/tasks?mine=true", headers=PM).json() if t["kind"] == "approve")
    r = c.post(f"/api/schedules/{pm_task['schedule_id']}/approve", json={"note": "ok", "acknowledge_risks": True}, headers=PM)
    assert r.status_code == 200 and r.json()["status"] == "APPROVED"
    assert c.get(f"/api/agent/tasks?role=production_manager", headers=VW).json()[0]["status"] == "DONE"
    heads = [t for t in c.get("/api/agent/tasks?role=plant_head", headers=VW).json() if t["schedule_id"] == pm_task["schedule_id"]]
    heads = [t for t in heads if t["kind"] != "alert"]                   # alerts about new orders' impact are separate from the jobs
    busy = [t for t in heads if t["details"]["jobs"]]
    assert busy and all(t["status"] == "OPEN" and "approved by maya" in t["note"] for t in busy)        # released to start preparing
    assert all(t["status"] == "OPEN" and t["note"] is None for t in heads if not t["details"]["jobs"])  # "no new jobs" notices were never held
    t = busy[0]
    H = TN if t["plant_id"] == "IN-TN-01" else KA
    assert c.post(f"/api/agent/tasks/{t['id']}/acknowledge", json={"note": "lines briefed"}, headers=H).json()["status"] == "ACKNOWLEDGED"
    assert c.post(f"/api/agent/tasks/{t['id']}/done", headers=H).json()["status"] == "DONE"
    assert c.post(f"/api/agent/tasks/{t['id']}/done", headers=H).status_code == 409


def test_an_urgent_high_priority_order_triggers_a_plan_on_its_own():
    r = c.post("/api/orders/intake", json=orders(1, qty=300, prio="HIGH", due="2026-11-04", prefix="U"), headers=PL).json()
    assert r["agent"]["outcome"] == "PLANNED" and "high-priority" in r["agent"]["reason"]


def test_team_members_get_the_open_tasks_and_status_reports_the_rules():
    assert c.post("/api/agent/team", json={"name": "Tanu R", "role": "plant_head"}, headers=AD).json()["error"]["code"] == "UNKNOWN_PLANT"
    assert c.post("/api/agent/team", json={"name": "Maya S", "role": "production_manager"}, headers=PL).status_code == 403
    assert c.post("/api/agent/team", json={"name": "Maya S", "role": "production_manager"}, headers=AD).status_code == 201
    open_pm = [t for t in c.get("/api/agent/tasks?role=production_manager&status=OPEN", headers=VW).json()]
    assert open_pm and all(t["assignee"] == "Maya S" for t in open_pm)
    st = c.get("/api/agent/status", headers=VW).json()
    assert st["rules"]["min_new_orders"] == 20 and st["pending"]["new_orders"] == 0 and st["last_run"]["outcome"] == "PLANNED"
    assert [r["outcome"] for r in c.get("/api/agent/runs", headers=VW).json() if r["outcome"] in ("PLANNED", "WATCHING")][:2] == ["PLANNED", "PLANNED"]

"""Uploading purchase orders: every agent looks, each order is assessed, and the agents act only on the orders that need it."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from app import db, store
from app.main import app

c = TestClient(app)
PL = {"x-role": "planner", "x-user": "pat"}
VW = {"x-role": "viewer", "x-user": "vic"}


def follow(r):
    """Wait for an upload's checks to finish; returns (job, the flow it produced)."""
    import time
    jid = r.json()["job_id"]
    for _ in range(600):
        j = c.get(f"/api/orders/jobs/{jid}", headers=VW).json()
        if j["status"] != "running":
            if j["status"] == "done":                          # the agent's reasoning comes with every step, and the review page can find the run
                assert all(st["thoughts"] for st in j["steps"].values()), {k: st["thoughts"] for k, st in j["steps"].items()}
                assert c.get("/api/orders/jobs/latest", headers=VW).json()["id"] == jid
                assert c.get("/api/orders/agent-ran", headers=VW).json()["ran"] is True      # pages may now show the plan
                rc = j["result"]["recommendation"]                     # the planning outcome: materials, machines, money, options
                assert rc["materials"] and all({"needed", "in_stock", "free", "enough"} <= set(m) for m in rc["materials"])
                assert all(("buy" in m and m["arrives"]) for m in rc["materials"] if not m["enough"])
                assert {k["kind"] for k in rc["machines"]["kinds"]} == {"SMT", "FAL"} and rc["money"]["new_orders_inr"] > 0
                assert all(i.get("how") for i in j["result"]["orders"] if i["needs_action"])
                ch = {x["key"]: x for x in rc["plants"]["choices"]}                 # both plants, Hosur only, Bengaluru only
                assert set(ch) == {"both", "only_IN-TN-01", "only_IN-KA-01"} and ch["both"]["to_transfer"] == 0
                for pc in ch.values():                                             # every unit of every material has a source
                    assert all(m["use"] + m["transfer"] + m["order"] >= m["need"] - 1 for m in pc["materials"])
                    assert pc["materials_days"] == max(m["days"] for m in pc["materials"])
                if rc["options"]:
                    assert sum(bool(o.get("best")) for o in rc["options"]) == 1
            return j, j["result"]
        time.sleep(0.1)
    raise AssertionError("upload checks did not finish")


@pytest.fixture(scope="module", autouse=True)
def clean():
    def wipe():
        with db.engine.begin() as cn:
            for t in ("agent_tasks", "agent_runs", "agent_events", "agent_incidents", "order_intake", "notifications", "order_freeze", "upload_log"):
                cn.execute(text(f"DELETE FROM {t}"))
        store.D.cache_clear(); store._inputs_cache.clear()
    wipe()
    yield
    wipe()


def test_two_sample_orders_fit_and_two_make_the_agents_act():
    c.post("/api/agent/check?agents=orders", headers=PL)                       # the watcher's starting point
    csv = c.get("/api/orders/samples", headers=VW).text
    assert len(csv.strip().splitlines()) == 5
    assert c.post("/api/orders/upload", json={"filename": "s.csv", "csv": csv}, headers=VW).status_code == 403
    r = c.post("/api/orders/upload", json={"filename": "s.csv", "csv": csv}, headers=PL)
    assert r.status_code == 201 and r.json()["received"] == 4
    job, f = follow(r)
    assert job["status"] == "done" and all(v["state"] == "done" and v["facts"] for v in job["steps"].values())       # every step reported
    assert [s["step"] for s in f["steps"]] == ["receive", "look", "assess", "act"]
    assert {a["agent"] for a in f["steps"][1]["agents"]} == {"floor", "inventory", "sourcing"} and all(a["summary"] for a in f["steps"][1]["agents"])
    light, heavy = f["orders"][:2], f["orders"][2:]
    assert not any(o["needs_action"] for o in light) and all(o["needs_action"] and o["why"] for o in heavy)
    assert all(o.get("buy") is not None and o.get("pushes_back") for o in heavy)
    assert f["decision"]["outcome"] == "PLANNED" and f["schedule_id"] and len(f["tasks"]) >= 2
    rc = f["recommendation"]                                                     # today vs the recommended plan, in days
    assert rc["plan_id"] == f["schedule_id"] and rc["headline"].startswith("With this plan")
    assert rc["now"]["orders"] and rc["with_plan"]["days"] and rc["new_orders"]["orders"] == 4
    assert rc["actions"][0]["kind"] == "approve" and all(a["title"] and a["detail"] for a in rc["actions"])
    tasks = [t for t in c.get("/api/agent/tasks", headers=VW).json() if t["title"].startswith("New order")]
    assert {t["details"]["order"]["order_id"] for t in tasks} == {o["order_id"] for o in heavy}       # no task for the orders that fit
    assert c.get("/api/orders/uploads", headers=VW).json()[0]["outcome"] == "ACTED"
    again = c.post("/api/orders/upload", json={"filename": "s.csv", "csv": csv}, headers=PL)     # the same POs again: updated, not refused
    assert again.status_code == 201 and sorted(again.json()["updated"]) == sorted(o["order_id"] for o in f["orders"])
    follow(again)
    log = c.get("/api/orders/upload-log", headers=VW).json()
    assert log[0]["status"] == "RECEIVED" and log[0]["username"] == "pat" and len(log[0]["updated_orders"]) == 4 and log[0]["job_id"]
    # once the production manager approves a plan with these orders, they are frozen: a new upload of them is refused
    sid = c.get("/api/schedules", headers=VW).json()[0]["id"]
    ok = c.post(f"/api/schedules/{sid}/approve", json={"acknowledge_risks": True}, headers={"x-role": "production_manager", "x-user": "mia"})
    assert ok.status_code == 200, ok.text
    frozen = c.post("/api/orders/upload", json={"filename": "s.csv", "csv": csv}, headers=PL)
    assert frozen.status_code == 422 and "frozen" in frozen.json()["error"]["message"]
    assert "approved by mia" in str(frozen.json()["error"]["detail"])
    assert c.get("/api/orders/upload-log", headers=VW).json()[0]["status"] == "REJECTED"


def test_orders_that_fit_need_nobody_but_still_get_the_five_plans():
    lines = c.get("/api/orders/samples", headers=VW).text.strip().splitlines()
    _, f = follow(c.post("/api/orders/upload", json={"filename": "light.csv", "csv": "\n".join(lines[:3])}, headers=PL))
    assert f["needs_action"] == 0 and not f["tasks"]                                       # no order needs a person
    assert f["decision"]["outcome"] == "PLANNED" and f["decision"]["plan_set_id"]          # an upload always ends with the four plans
    ps = c.get("/api/plan-sets/latest", headers=VW).json()
    assert ps["id"] == f["decision"]["plan_set_id"] and len(ps["plans"]) == 5 and ps["why"]
    assert all(p["promise"]["new"] > 0 for p in ps["plans"])                               # the uploaded orders are date-checked
    assert c.get("/api/orders/jobs/nope", headers=VW).status_code == 404
    assert c.get("/api/orders/uploads", headers=VW).json()[0]["outcome"] == "ABSORBED"
    bad = c.post("/api/orders/upload", json={"filename": "x.csv", "csv": "po_number,product_id,qty,due_date\nX1,NOPE,5,2026-12-01\n"}, headers=PL)
    assert bad.status_code == 422 and "unknown product" in str(bad.json()["error"]["detail"])

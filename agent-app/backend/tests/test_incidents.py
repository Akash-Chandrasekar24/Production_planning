"""Problems on the floor: a machine stops or production orders run late -> the agent measures the impact, looks up how it was handled
before, tries recovery options, and hands the decision and the work to the production manager and plant heads."""
from datetime import datetime, timedelta
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from app import db, store
from app.domain import BomLine, LineSpec, Order, PlanningInput, RoutingOp, Slot, Stock
from app.main import app
from app.planning import plan
from app.scenarios import ExtraTime, MachineLoss, Scenario, apply

c = TestClient(app)
PL = {"x-role": "planner", "x-user": "pat"}
PM = {"x-role": "production_manager", "x-user": "maya"}
TN = {"x-role": "plant_head", "x-user": "tanu", "x-plant": "IN-TN-01"}
KA = {"x-role": "plant_head", "x-user": "kiran", "x-plant": "IN-KA-01"}
VW = {"x-role": "viewer", "x-user": "vic"}


@pytest.fixture(scope="module", autouse=True)
def clean():
    def wipe():
        with db.engine.begin() as cn:
            for t in ("agent_tasks", "agent_runs", "agent_events", "agent_incidents", "order_intake", "team_members"):
                cn.execute(text(f"DELETE FROM {t}"))
        store.D.cache_clear(); store._inputs_cache.clear()
    wipe()
    yield
    wipe()


# ---- the recovery levers on a small, capacity-bound factory (two plants, one SMT + one FAL line each)
T0 = datetime(2026, 11, 2)


def _factory():
    slots = [Slot(start=T0 + timedelta(hours=8 * i), end=T0 + timedelta(hours=8 * i + 8), minutes=420) for i in range(3 * 20)]
    lines = [LineSpec(line_id=f"{k}-L{n}", plant_id=p, kind=k, rate_per_min=2.0, slots=[s.model_copy() for s in slots]) for n, p in ((1, "P1"), (2, "P2")) for k in ("SMT", "FAL")]
    orders = [Order(order_id=f"O{i}", product_id="X", qty=2400, due=(T0 + timedelta(days=2 + i)).date(), priority="HIGH" if i == 0 else "MEDIUM",
                    customer_id=f"C{i % 2}") for i in range(4)]
    return PlanningInput(as_of=T0, horizon_end=T0 + timedelta(days=20), orders=orders, lines=lines,
                         routing=[RoutingOp(product_id="X", seq=1, station_id="ST-SMT-1", segment="SMT", cycle_sec=1, setup_min=0),
                                  RoutingOp(product_id="X", seq=2, station_id="ST-FAL-1", segment="FAL", cycle_sec=1, setup_min=0)],
                         bom=[BomLine(product_id="X", material_id="M", qty_per=1, scrap=0, effective_from="2026-01-01", effective_to="2027-12-31")],
                         stock=[Stock(plant_id="ALL", material_id="M", available=1e9, safety=0, unit_cost_inr=1)])


def _late_units(s):
    return sum(a.qty for a in s.assignments if a.status == "LATE_RISK")


def test_levers_keep_plant_split_and_overtime_change_the_plan():
    inp = _factory()
    base = plan(inp)
    assert _late_units(base) == 0 and {a.plant_id for a in base.assignments} == {"P1", "P2"}
    down = [MachineLoss(line_id="SMT-L1", start=T0, end=T0 + timedelta(days=3))]
    keep = {a.customer_order_id: a.plant_id for a in base.assignments}
    wait = plan(apply(inp, Scenario(name="w", machine_losses=down, keep_plant=keep)))
    move = plan(apply(inp, Scenario(name="m", machine_losses=down)))
    assert _late_units(wait) > 0                                                     # waiting for the repair makes P1's orders late
    assert all(a.plant_id == keep[a.customer_order_id] for a in wait.assignments)     # and keeps every order in its plant
    assert _late_units(move) < _late_units(wait)                                     # moving work to P2 helps
    split = apply(inp, Scenario(name="s", split_orders=["O1"]))
    assert sorted(o.order_id for o in split.orders if o.order_id.startswith("O1")) == ["O1#1", "O1#2"]
    assert sum(o.qty for o in split.orders if o.order_id.startswith("O1")) == 2400
    ot = apply(inp, Scenario(name="o", extra_time=[ExtraTime(start=T0, end=T0 + timedelta(days=1), minutes_per_shift=120)]))
    assert ot.lines[0].slots[0].minutes == 480 and ot.lines[0].slots[3].minutes == 420  # never beyond the shift, only inside the window


# ---- end to end on the order book
def test_reported_breakdown_is_analysed_and_handed_over():
    r = c.post("/api/agent/incidents", json={"kind": "MACHINE_DOWN", "machine_id": "M-SMT-01", "repair_hours": 48, "note": "nozzle head",
                                                "started_at": "2026-11-01T06:00:00"}, headers=TN)       # the test data's plan starts 1 Nov
    assert r.status_code == 201, r.text
    i = r.json()
    an = i["analysis"]
    assert i["line_id"] == "SMT-L1" and i["plant_id"] == "IN-TN-01" and i["source"] == "reported" and not i["end_assumed"]
    assert an["options"][0]["key"] == "wait" and sum(o["recommended"] for o in an["options"]) == 1
    assert {"move", "overtime", "all"} <= {o["key"] for o in an["options"]}
    assert an["findings"] and an["history"]["recorded"] == [] and an["history"]["feed"]["days"] > 0     # nothing fixed before; feed history shown
    assert an["impact"]["floor_orders"] > 0 and an["impact"]["orders_delayed"] > 0                  # open production orders are the work it delays
    if an["recommended"] != "wait":                                                                 # a change is proposed for approval
        assert i["proposal_id"] and store.get(i["proposal_id"]).status == "PENDING_APPROVAL"
    tasks = [t for t in c.get("/api/agent/tasks", headers=VW).json() if t["details"].get("incident_id") == i["id"]]
    kinds = sorted((t["role"], t["kind"]) for t in tasks)
    assert ("plant_head", "repair") in kinds and any(k[0] == "production_manager" for k in kinds)
    repair = next(t for t in tasks if t["kind"] == "repair")
    assert repair["plant_id"] == "IN-TN-01"
    assert c.post(f"/api/agent/tasks/{repair['id']}/done", headers=TN).json()["error"]["code"] == "RECORD_THE_FIX"
    run = c.get("/api/agent/runs", headers=VW).json()[0]
    assert run["outcome"] == "INCIDENT" and "M-SMT-01 stopped" in run["reason"]


def test_plant_heads_report_only_for_their_plant_and_inputs_are_checked():
    assert c.post("/api/agent/incidents", json={"kind": "MACHINE_DOWN", "line_id": "SMT-L1"}, headers=KA).json()["error"]["code"] == "NOT_YOUR_PLANT"
    assert c.post("/api/agent/incidents", json={"kind": "MACHINE_DOWN", "machine_id": "NOPE"}, headers=PM).json()["error"]["code"] == "UNKNOWN_MACHINE"
    assert c.post("/api/agent/incidents", json={"kind": "PO_DELAY", "production_orders": ["PO-X"]}, headers=PM).json()["error"]["code"] == "UNKNOWN_PRODUCTION_ORDER"
    assert c.post("/api/agent/incidents", json={"kind": "MACHINE_DOWN", "line_id": "SMT-L1"}, headers=VW).status_code == 403


def test_a_person_can_pick_another_option_and_the_fix_is_remembered():
    i = c.get("/api/agent/incidents", headers=VW).json()["incidents"][0]
    full = c.get(f"/api/agent/incidents/{i['id']}", headers=VW).json()
    other = next(o["key"] for o in full["analysis"]["options"] if not o["recommended"])
    ch = c.post(f"/api/agent/incidents/{i['id']}/choose", json={"option": other}, headers=PM).json()
    assert ch["option"] == other
    assert c.post(f"/api/agent/incidents/{i['id']}/choose", json={"option": "nope"}, headers=PM).status_code == 409
    assert c.post(f"/api/agent/incidents/{i['id']}/resolve", json={"fix": "replaced nozzle"}, headers=KA).status_code == 403
    done = c.post(f"/api/agent/incidents/{i['id']}/resolve", json={"fix": "Replaced the nozzle head; spare kept at the line", "repair_hours": 6}, headers=TN).json()
    assert done["status"] == "RESOLVED" and done["resolved_by"] == "tanu"
    assert c.post(f"/api/agent/incidents/{i['id']}/resolve", json={"fix": "again"}, headers=TN).status_code == 409
    nxt = c.post("/api/agent/incidents", json={"kind": "MACHINE_DOWN", "line_id": "SMT-L1"}, headers=PM).json()
    rec = nxt["analysis"]["history"]["recorded"]
    assert rec and rec[0]["fix"].startswith("Replaced the nozzle") and rec[0]["match"] == "same line" and rec[0]["repair_hours"] == 6.0
    assert nxt["end_assumed"] and any("assumed" in f for f in nxt["analysis"]["findings"])


def test_late_production_orders_are_detected_once():
    first = c.post("/api/agent/detect", headers=PL).json()["raised"]
    assert all(r["kind"] in ("PO_DELAY", "MACHINE_DOWN") for r in first)
    po = [r for r in first if r["kind"] == "PO_DELAY"]
    if po:
        assert po[0]["analysis"]["options"][0]["key"] == "wait" and po[0]["production_orders"]
    assert c.post("/api/agent/detect", headers=PL).json()["raised"] == []          # raised once
    st = c.get("/api/agent/status", headers=VW).json()
    assert st["open_problems"] >= 1 and st["problem_rules"]["fault_min"] == 60


def test_the_agents_light_problem_list_matches_the_full_one():
    from app import incidents
    full = {i["id"]: i for i in incidents.list_()}
    light = incidents.list_(light=True)
    assert light and len(light) == len(full)
    for i in light:                                       # same facts the agents use, without reading the stored analysis
        f = full[i["id"]]
        assert (i["headline"], i["plant_id"], i["status"], i["option_name"]) == (f["headline"], f["plant_id"], f["status"], f["option_name"])
    assert any(i["option_name"] for i in light)

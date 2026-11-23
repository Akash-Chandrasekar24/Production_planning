"""Plan options: the agent makes four plans from the same data (keep dates, achievable dates, overtime, lowest cost); the production
manager chooses one, which is approved; choosing one with new dates makes them the agreed volume plan."""
from datetime import date, timedelta

from fastapi.testclient import TestClient

from app import data, planning_agent, store, volume
from app.config import Policy
from app.db import AgentTask, SessionLocal
from app.main import app

c = TestClient(app)
PM = {"x-role": "production_manager", "x-user": "maya"}
ENG = {"x-role": "engineer", "x-user": "eli"}
VW = {"x-role": "viewer", "x-user": "vic"}


def test_fill_is_capacity_bounded_and_in_priority_order():
    pol, inp = Policy(), data.to_inputs(store.D())
    f = volume.fill(inp, pol)
    assert all(r["capacity"] >= 0 for r in f["load"])
    rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    done = [(rank[o.priority], f["finish_week"][o.order_id]) for o in inp.orders if f["finish_week"].get(o.order_id)]
    for p in (0, 1):                                   # a higher priority never finishes after a lower one that started from the same capacity
        hi = [w for r, w in done if r == p]
        lo = [w for r, w in done if r > p]
        assert not hi or not lo or max(hi) <= max(lo)


def _overload(monkeypatch, units=150_000):
    """More demand than final assembly can make in the next weeks: a block of large high-priority orders due soon."""
    from app import order_watcher
    from app.domain import Order
    real = order_watcher.pending
    def pending(d=None, pol=None):
        p = real(d, pol)
        x = p["_inp"]
        rush = [Order(order_id=f"RUSH-{i}", product_id="SPH-A15-128", qty=units // 5, due=x.as_of.date() + timedelta(days=6 + i), priority="HIGH")
                for i in range(5)]
        x.orders += rush
        p["_now"].update({o.order_id: o for o in rush})
        return p
    monkeypatch.setattr(order_watcher, "pending", pending)


def test_agent_makes_five_plans_and_the_manager_chooses_one(monkeypatch):
    _overload(monkeypatch)
    run = planning_agent.plan("manual", "test", "pat")
    assert run["outcome"] == "PLANNED" and run["plan_set_id"]
    ps = c.get("/api/plan-sets/latest", headers=VW).json()
    assert ps["id"] == run["plan_set_id"] and ps["status"] == "OPEN" and ps["why"]
    plans = {x["key"]: x for x in ps["plans"]}
    assert set(plans) == {"penalty", "at_risk", "shifts", "outsource", "zero"} and sum(x["best"] for x in ps["plans"]) == 1
    assert run["schedule_id"] == ps["recommended"]                                                # the agent hands over its pick
    for x in ps["plans"]:
        assert store.get(x["schedule_id"]).status == "PENDING_APPROVAL" and x["gantt"]
        assert {"on_time_pct", "late_orders", "late_penalty_inr", "express_premium_inr", "total_cost_inr", "finish"} <= set(x["metrics"])
        assert x["metrics"]["redated_orders"] == 0                                                # no plan moves a customer's date
    pm = plans["penalty"]["metrics"]
    assert pm["express_jobs"] == 0 and pm["extra_shift_h"] == 0 and pm["outsourced_orders"] == 0                  # no extra spend
    assert plans["shifts"]["metrics"]["extra_shift_h"] > 0 and plans["shifts"]["metrics"]["extra_shift_inr"] > 0  # more line time, paid
    om = plans["outsource"]["metrics"]
    assert om["outsourced_orders"] > 0 and om["on_time_pct"] >= pm["on_time_pct"] and om["late_orders"] <= pm["late_orders"]
    zm = plans["zero"]["metrics"]                                                            # every lever, then outsource what is still late
    assert zm["late_penalty_inr"] <= min(plans[k]["metrics"]["late_penalty_inr"] for k in ("penalty", "at_risk", "shifts", "outsource"))
    assert zm["outsourced_orders"] >= om["outsourced_orders"] and zm["extra_shift_h"] > 0
    assert all({"new", "on_time", "late"} <= set(x["promise"]) for x in ps["plans"])            # the newest orders are checked in every plan
    out = set(store.get(plans["outsource"]["schedule_id"]).policy["outsource_orders"])
    assert out and not out & {a.customer_order_id for a in store.get(plans["outsource"]["schedule_id"]).assignments}  # off our lines
    assert not out & {o.order_id for o in store.inputs(plans["outsource"]["schedule_id"]).orders}                     # rebuilt the same way
    from app import final_plan
    doc = final_plan.build(plans["outsource"]["schedule_id"])                                 # its plan document draws the outsourced work too
    assert isinstance(doc["mps"]["weeks"], list) and any(r.get("external") and r["runs"] for r in doc["gantt"]["rows"])
    assert ps["plans"][0]["gap"]["units"] > 0                                                     # the overload is stated, not hidden
    with SessionLocal() as s:
        t = s.query(AgentTask).filter_by(schedule_id=ps["recommended"], role="production_manager").one()
        assert t.title.startswith("Choose one of the 5 plans")
    sid = plans["outsource"]["schedule_id"]
    body = {"schedule_id": sid, "acknowledge_risks": True}
    assert c.post(f"/api/plan-sets/{ps['id']}/choose", json=body, headers=ENG).status_code == 403    # the manager decides
    r = c.post(f"/api/plan-sets/{ps['id']}/choose", json=body, headers=PM)
    assert r.status_code == 200, r.text
    assert store.get(sid).status == "APPROVED"
    assert all(store.get(x["schedule_id"]).status == "REJECTED" for x in ps["plans"] if x["schedule_id"] != sid)
    with SessionLocal() as s:
        assert s.query(AgentTask).filter_by(schedule_id=sid, role="production_manager", kind="approve").count() == 1   # tasks moved
    assert c.post(f"/api/plan-sets/{ps['id']}/choose", json=body, headers=PM).status_code == 409    # chosen once
    assert c.get(f"/api/plan-sets/{ps['id']}", headers=VW).json()["status"] == "CHOSEN"
    h = c.get("/api/plan-sets", headers=VW).json()                                              # plan history: the chosen plan and where it stands
    assert h[0]["id"] == ps["id"] and h[0]["chosen"] == plans["outsource"]["title"] and h[0]["plan_status"] == "APPROVED"


def test_new_orders_that_dont_fit_raise_a_new_question_on_top_of_the_agreed_plan(monkeypatch):
    monkeypatch.setenv("VOLUME_GATE", "true")
    _overload(monkeypatch)
    from app import order_watcher
    from app.domain import Order
    x0 = order_watcher.pending()["_inp"]
    _, _, first = volume.check(x0, Policy())
    v, _ = volume.propose(x0, None, first, "test", "demand above capacity")
    from app.db import VolumePlan
    with SessionLocal() as s:
        s.get(VolumePlan, v.id).run_id = "test-run"                       # as when the agent proposes it during a run
        s.commit()
    volume.agree(v.id, "commit", "pat")                                  # committed dates agreed
    vp = volume.agreed()
    assert vp is not None
    x = order_watcher.pending()["_inp"]
    x.orders.append(Order(order_id="RUSH-NEW", product_id=x.orders[0].product_id, qty=200_000, due=x.as_of.date() + timedelta(days=3), priority="HIGH"))
    _, _, prop = volume.check(x, Policy())
    assert prop and any(r["order_id"] == "RUSH-NEW" for r in prop["late"])
    assert not any(r["order_id"] in (vp.redates or {}) and r["due"] != vp.redates[r["order_id"]] for r in prop["late"])  # committed dates stay


def test_the_agent_keeps_production_in_house_unless_the_case_is_extreme():
    from app import plan_sets
    pol = Policy()
    mk = lambda key, on, cost, out=0: {"key": key, "metrics": {"on_time_pct": on, "total_cost_inr": cost, "outsourced_orders": out}}
    plans = [mk("penalty", 32, 100), mk("at_risk", 33, 150), mk("shifts", 32, 120), mk("outsource", 70, 300, 800)]
    assert plan_sets.pick(plans, pol)["key"] == "penalty"              # 32% in-house is not extreme: stay in our plants (cheapest within 2 points)
    plans = [mk("penalty", 10, 100), mk("at_risk", 12, 150), mk("shifts", 11, 120), mk("outsource", 45, 300, 800)]
    assert plan_sets.pick(plans, pol)["key"] == "outsource"            # under 25% in-house and +33 points: the extreme case
    plans = [mk("penalty", 10, 100), mk("at_risk", 12, 150), mk("shifts", 11, 120), mk("outsource", 20, 300, 800)]
    assert plan_sets.pick(plans, pol)["key"] != "outsource"            # outsourcing that adds little is not worth it


def test_promise_gives_the_earliest_date_for_new_orders_a_plan_cannot_make_in_time():
    import datetime as dt
    from types import SimpleNamespace as NS
    from app.plan_sets import promise
    d = dt.date(2026, 11, 10)
    o = lambda i, due: NS(order_id=i, status="OPEN", due=due, customer_id="C", product_id="P", qty=10, unit_price_inr=100)
    x = NS(orders=[o("A", d), o("B", d), o("C", d), o("D", d), o("OLD", d)], horizon_end=dt.datetime(2026, 12, 31))
    s = NS(assignments=[NS(customer_order_id="A#1", fal_finish=dt.datetime(2026, 11, 9, 18)),       # on time
                        NS(customer_order_id="B", fal_finish=dt.datetime(2026, 11, 14, 8))],        # 4 days late
           unscheduled=[NS(customer_order_id="C")])                                                 # not in the planning period
    r = promise(s, x, {}, {"A", "B", "C", "D"}, {"D"})                                              # D goes to a contract manufacturer
    assert (r["new"], r["on_time"], r["late_count"]) == (4, 2, 2)
    assert [(l["id"], l["promise"], l["days_late"]) for l in r["late"]] == [("B", "2026-11-14", 4), ("C", None, None)]

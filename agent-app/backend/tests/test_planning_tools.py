"""Planning tools and the sourcing agent: every answer comes from the engine and the records (or the labelled generated data)."""
from fastapi.testclient import TestClient
from app import planning, planning_tools as T, sourcing_agent as S
from app.config import Policy
from app.main import app

c = TestClient(app)
VW = {"x-role": "viewer", "x-user": "v"}


def test_promise_answers_with_a_date_and_what_to_buy():
    r = c.get("/api/plan-tools/promise?product_id=SPH-A15-128&qty=500&due=2026-12-20", headers=VW).json()
    assert r["answer"] in ("yes", "with_purchase", "no") and r["promise_date"]
    if r["short"]:                                         # not all from stock: the parts to buy, each with its lead time
        assert r["short"]["buy"] and all(m["lead_days"] > 0 for m in r["short"]["buy"])
    elif r["answer"] == "no":                              # parts are there but the lines are booked: the earliest date is after the due date
        assert r["promise_date"] > r["due"]
    assert c.get("/api/plan-tools/promise?product_id=NOPE&qty=5", headers=VW).status_code == 400


def test_plan_vs_actual_and_otif():
    a = c.get("/api/plan-tools/adherence", headers=VW).json()
    assert a["finished_orders"] > 0 and 0 <= a["on_plan_pct"] <= 100 and a["otif"]["orders"] > 0
    assert all(b["gap"] > 0 for b in a["behind"])


def test_maintenance_blocks_line_time_in_the_plan():
    ref, x = T.working()
    assert x.maintenance and all(w["end"] > w["start"] for w in x.maintenance)
    w = x.maintenance[0]
    line = next(l for l in x.lines if l.line_id == w["line_id"])
    slot = next(s for s in line.slots if s.start <= w["start"] < s.end)
    assert slot.minutes <= Policy().default_shift_minutes - w["minutes"] + 1e-6
    assert c.get("/api/plan-tools/maintenance", headers=VW).json()["services"]


def test_changeovers_recommend_a_window_no_later_than_now():
    r = c.get("/api/plan-tools/changeovers", headers=VW).json()
    cur = next(o for o in r["options"] if o["current"])
    best = next(o for o in r["options"] if o["window_days"] == r["recommended"])
    assert best["changeover_min"] <= cur["changeover_min"] and best["late_unit_days"] <= cur["late_unit_days"] * 1.02 + 1


def test_materials_to_buy_have_dates_and_pack_sizes():
    r = c.get("/api/plan-tools/materials", headers=VW).json()
    for m in r["rows"]:
        assert m["order_by"] <= m["short_on"] and m["buy_qty"] >= m["short_qty"] and m["status"] in ("late", "order now", "planned")


def test_forecast_crews_yields_and_cost():
    f = c.get("/api/plan-tools/forecast?weeks=8", headers=VW).json()
    assert len(f["load"]) == 8 and all(len(p["forecast"]) == 8 for p in f["products"].values())
    cr = c.get("/api/plan-tools/crews?days=3", headers=VW).json()
    assert cr["available"] and all(r["available"] <= 17 for r in cr["rows"])
    y = c.get("/api/plan-tools/yields", headers=VW).json()
    assert y["rows"] and all(0 < r["yield_pct"] <= 100 for r in y["rows"])
    costs = c.get("/api/plan-tools/costs", headers=VW).json()
    assert costs and costs[0]["available"] and costs[0]["total_inr"] == sum(costs[0]["parts"].values())


def test_sourcing_only_suggests_a_move_when_every_check_passes(monkeypatch):
    """Give Hosur plenty of every part: Bengaluru's blocked orders can then be made there, and only checked moves are suggested."""
    ref, x = T.working("plant")
    from app.domain import Stock
    y = x.model_copy(deep=True)
    have = {s.material_id for s in y.stock if s.plant_id == "IN-TN-01"}
    y.stock += [Stock(plant_id="IN-TN-01", material_id=m, available=0, safety=0, unit_cost_inr=s.unit_cost_inr)
                for m, s in {s.material_id: s for s in y.stock}.items() if m not in have]
    for st in y.stock:
        if st.plant_id == "IN-TN-01":
            st.available = st.available + 1_000_000
    rich = planning.plan(y, Policy(**ref.policy))
    monkeypatch.setattr(T, "working", lambda scope=None, pol=None: (rich, y))
    S._cache.clear()
    r = S.analyse()
    assert r["items"]
    for i in r["items"]:
        hard = [ch for ch in i["checks"] if ch["hard"]]
        assert (i["verdict"] == "make_there") == all(ch["ok"] for ch in hard)
    ka = [i for i in r["items"] if i["home"] == "IN-KA-01"]
    assert ka and all(next(c for c in i["checks"] if c["check"] == "material")["ok"] for i in ka)      # Hosur has the parts now
    # with the parts there, a Bengaluru order moves unless Hosur's own lines can't take it without hurting its own orders
    for i in ka:
        if i["verdict"] != "make_there":
            assert {c["check"] for c in i["checks"] if c["hard"] and not c["ok"]} <= {"capacity", "own orders", "crew", "gain"}
    moves = [i for i in r["items"] if i["verdict"] == "make_there"]
    sig = S.sense()["signals"]
    assert len(sig) == len([i for i in r["items"] if i["verdict"] != "no_move"]) and all(s["key"].startswith("sourcing:") for s in sig)
    assert all(s["type"] == "sourcing.make_there" for s in sig if s["detail"]["production_order_id"] in {m["production_order_id"] for m in moves})
    S._cache.clear()


def test_plan_ahead_outlook_is_consistent():
    o = c.get("/api/plan-tools/outlook?weeks=8", headers=VW).json()
    assert len(o["weeks"]) == 8 and o["actions"]
    for w in o["weeks"]:
        assert w["demand"] >= w["firm"] and w["planned"] <= w["capacity"] + 1
    for m in o["materials"]:
        for b in m["buys"]:
            assert b["order_by"] <= b["arrives"] and b["qty"] > 0
    dates = [a["date"] for a in o["actions"]]
    assert dates == sorted(dates)

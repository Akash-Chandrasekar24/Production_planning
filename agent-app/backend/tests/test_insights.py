import pytest
from fastapi.testclient import TestClient
from app.main import app


def test_insights_endpoint_has_real_data():
    c = TestClient(app)
    j = c.get("/api/insights", headers={"x-role": "viewer", "x-user": "v"}).json()
    assert j["demand"]["open_orders"] == 1183 and j["demand"]["open_qty"] == 158721
    assert j["supply"]["open_po_qty"] > 0 and len(j["lines"]["per_line"]) == 4 and 0 < j["lines"]["attainment_pct"] <= 120
    assert sum(b["orders"] for b in j["demand"]["due_buckets"]) == 1183 and j["stock"]["lowest_cover"]


def test_insights_period_comparison():
    c = TestClient(app)
    p = c.get("/api/insights?days=30", headers={"x-role": "viewer", "x-user": "v"}).json()["period"]
    assert [k["key"] for k in p["kpis"]] == ["orders", "units", "output", "downtime", "revenue", "attainment"] and all(k["value"] >= 0 for k in p["kpis"])
    assert len(p["series"] if "series" in p else p["output"]["series"]) == 30 and len(p["weekday"]) == 7
    assert {r["product_id"] for r in p["products"]} == {"SPH-A15-128", "SPH-A15-256", "SPH-PRO-256"} and 0 <= p["promise"]["pct"] <= 100
    assert c.get("/api/insights?days=3", headers={"x-role": "viewer", "x-user": "v"}).status_code == 422


@pytest.mark.parametrize("key", ["orders", "units", "revenue", "output", "downtime", "attainment", "weekday", "products", "promise", "due", "supply", "stock", "lines"])
def test_every_card_has_a_detailed_view(key):
    c = TestClient(app)
    r = c.get(f"/api/insights/detail/{key}?days=30", headers={"x-role": "viewer", "x-user": "v"})
    j = r.json()
    assert r.status_code == 200 and j["title"] and j["sections"]
    for s in j["sections"]:
        assert s["type"] in {"stats", "series", "bars", "table"}
        if s["type"] == "table":
            assert s["columns"] and all(set(row) >= {c["key"] for c in s["columns"]} for row in s["rows"])


def test_unknown_detail_is_404_and_weekday_has_hover_details():
    c = TestClient(app)
    assert c.get("/api/insights/detail/nope", headers={"x-role": "viewer", "x-user": "v"}).status_code == 404
    wk = c.get("/api/insights?days=30", headers={"x-role": "viewer", "x-user": "v"}).json()["period"]["weekday"]
    assert len(wk) == 7 and all({"avg_good_units", "attainment_pct", "avg_downtime_min", "days", "best_line"} <= set(w) for w in wk)


H = {"x-role": "viewer", "x-user": "v"}


def test_filters_change_the_numbers_where_they_apply():
    c = TestClient(app)
    base = c.get("/api/insights?days=30", headers=H).json()
    opts = base["filters"]["options"]
    assert opts["plant"] == ["IN-KA-01", "IN-TN-01"] and len(opts["line"]) == 4 and opts["priority"] == ["HIGH", "MEDIUM", "LOW"]
    kp = c.get("/api/insights?days=30&plant=IN-KA-01", headers=H).json()
    kpi = lambda j, k: next(x["value"] for x in j["period"]["kpis"] if x["key"] == k)
    assert 0 < kpi(kp, "output") < kpi(base, "output")                                  # production narrows to one plant
    assert kpi(kp, "orders") == kpi(base, "orders")                                      # customer orders have no plant
    assert kp["filters"]["applies"]["orders"] == [] and kp["filters"]["applies"]["production"] == ["plant"]
    assert {r["line_id"] for r in kp["lines"]["per_line"]} == {"FAL-L2", "SMT-L2"}
    hi = c.get("/api/insights?days=30&priority=HIGH&product=SPH-A15-128", headers=H).json()
    assert 0 < kpi(hi, "orders") < kpi(base, "orders") and set(hi["demand"]["by_priority"]) == {"HIGH"}
    assert [p["product_id"] for p in hi["period"]["products"]] == ["SPH-A15-128"]
    assert hi["period"]["orders_range"] == base["period"]["orders_range"]               # same calendar days whatever the filter
    assert hi["stock"]["materials"] < base["stock"]["materials"]                         # only that product's BOM materials


def test_filter_combination_with_no_data_is_empty_not_an_error():
    c = TestClient(app)
    r = c.get("/api/insights?days=30&plant=IN-KA-01&line=FAL-L1", headers=H)            # FAL-L1 is in the other plant
    j = r.json()
    assert r.status_code == 200 and next(x["value"] for x in j["period"]["kpis"] if x["key"] == "output") == 0
    assert j["lines"]["attainment_pct"] is None and j["lines"]["per_line"] == []
    for key in ("output", "attainment", "weekday", "lines", "downtime"):
        assert c.get(f"/api/insights/detail/{key}?days=30&plant=IN-KA-01&line=FAL-L1", headers=H).status_code == 200, key


def test_unknown_filter_value_is_rejected():
    r = TestClient(app).get("/api/insights?plant=MARS-01", headers=H)
    assert r.status_code == 422 and r.json()["error"]["code"] == "UNKNOWN_FILTER_VALUE" and "plant" in r.json()["error"]["detail"]["options"]


def test_detail_and_order_browser_use_the_same_filters():
    c = TestClient(app)
    all_ = c.get("/api/insights/detail/orders?days=30", headers=H).json()["sections"][0]["items"][0]["value"]
    b2b = c.get("/api/insights/detail/orders?days=30&channel=B2B", headers=H).json()["sections"][0]["items"][0]["value"]
    assert 0 < b2b < all_
    j = c.get("/api/orders?priority=HIGH&only_overdue=true&limit=500", headers=H).json()
    assert j["total"] > 0 and all(r["priority"] == "HIGH" and r["is_open"] and r["confirmed_delivery_date"] < j["plan_start"] for r in j["rows"])
    assert c.get("/api/orders?q=co-00000", headers=H).json()["total"] >= 9


def test_order_lookup_for_any_order():
    c = TestClient(app)
    j = c.get("/api/orders/co-000001", headers={"x-role": "viewer", "x-user": "v"}).json()
    assert j["customer_order_id"] == "CO-000001" and j["open_qty"] == j["order_qty"] - j["shipped_qty"] and "days_to_due" in j
    assert c.get("/api/orders/NOPE-1", headers={"x-role": "viewer", "x-user": "v"}).status_code == 404

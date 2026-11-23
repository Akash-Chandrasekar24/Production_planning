"""Digital twin: every figure comes from the recorded hour; states follow the documented rules."""
import pandas as pd
from fastapi.testclient import TestClient
from app import data, twin
from app.main import app

c = TestClient(app)
H = {"x-role": "viewer", "x-user": "v"}
HP = data.load_csv()["fact_hourly_production"]
ALL = pd.concat([HP, data.load_csv()["gen_twin_line_hourly"]], ignore_index=True)        # the twin shows the generated bays too


def test_latest_hour_by_default_and_matches_the_records():
    j = c.get("/api/twin", headers=H).json()
    assert j["mode"] == "replay" and j["at"] == HP.event_ts.max() and j["range"] == [HP.event_ts.min(), HP.event_ts.max()]
    rec = ALL[ALL.event_ts == j["at"]]
    assert j["floor"]["good"] == int(rec.good_qty.sum()) and j["floor"]["target"] == int(rec.target_qty.sum()) and j["floor"]["generated_lines"] == 4
    assert {l["line_id"] for l in j["lines"] if not l["generated"]} == {"SMT-L1", "SMT-L2", "FAL-L1", "FAL-L2"} and len(j["plants"]) == 2
    l1 = next(l for l in j["lines"] if l["line_id"] == "SMT-L1")
    r = rec[rec.line_id == "SMT-L1"].iloc[0]
    assert l1["hour"]["good"] == r.good_qty and l1["hour"]["production_order_id"] == r.production_order_id and l1["plant_id"] == r.plant_id
    assert [m["type"] for m in l1["machines"]][:4] == ["SPI", "Pick and Place", "Reflow Oven", "AOI"]       # flow order
    assert len(l1["trend"]) == 12 and all(0 <= m["maintenance_due_days"] <= 30 for l in j["lines"] for m in l["machines"])


def test_warehouse_stock_is_that_days_count():
    j = c.get("/api/twin?at=2026-10-15T09:00", headers=H).json()
    inv = data.load_csv()["fact_inventory_daily_snapshot"]
    day = inv[inv.snapshot_date == "2026-10-15"]
    assert j["stock"]["day"] == "2026-10-15" and len(j["stock"]["warehouses"]) == day.groupby(["plant_id", "warehouse_id"]).ngroups
    assert sum(w["on_hand"] for w in j["stock"]["warehouses"]) == int(day.on_hand_qty.sum())


def test_material_flow_from_stores_to_trucks():
    at = "2026-10-30T10:00:00"
    j = c.get(f"/api/twin?at={at}", headers=H).json()
    D = data.load_csv()
    bom, co, inv = D["fact_bom_component"], D["fact_customer_order"], D["fact_inventory_daily_snapshot"]
    hp = ALL
    per_unit = (bom.component_qty * (1 + bom.scrap_factor)).groupby(bom.product_id).sum()
    rec = hp[hp.event_ts == at]
    for f in j["flow"]:
        r = rec[rec.plant_id == f["plant_id"]]
        smt, fal = r[r.line_id.str.startswith("SMT")], r[r.line_id.str.startswith("FAL")]
        assert f["assembled"] == int(fal.good_qty.sum()) and f["boards"] == int(smt.good_qty.sum())
        assert f["issued"] == int(round(sum(x.actual_qty * per_unit[x.product_id] for x in smt.itertuples())))
        day = inv[(inv.snapshot_date == "2026-10-30") & (inv.plant_id == f["plant_id"])]
        assert f["inbound"] == int(day.in_transit_qty.sum()) and f["fg"] == int(day[day.warehouse_id == "FG-WH-01"].on_hand_qty.sum())
    due = co[co.confirmed_delivery_date == "2026-10-30"]
    assert j["dispatch"] == {"day": "2026-10-30", "orders": len(due), "units": int(due.order_qty.sum()), "shipped": int(due.shipped_qty.sum()),
                             "delivered": int((due.order_status == "DELIVERED").sum())}


def test_states_follow_the_rules():
    down = HP[HP.downtime_min >= twin.DOWN_MIN].iloc[0]
    j = c.get(f"/api/twin?at={down.event_ts}", headers=H).json()
    line = next(l for l in j["lines"] if l["line_id"] == down.line_id)
    # the line is down; each machine has its own state: the one that stopped it is down, the others wait for it
    assert line["state"] == "down" and all(m["state"] in ("down", "waiting", "slowed") for m in line["machines"])
    assert sum(m["state"] == "down" for m in line["machines"]) <= 1
    assert any(e["line_id"] == down.line_id and e["ts"] == down.event_ts for e in j["events"])     # it is in the stoppage feed
    clean = HP[(HP.downtime_min == 0) & (HP.actual_qty >= HP.target_qty)].iloc[0]
    j = c.get(f"/api/twin?at={clean.event_ts}", headers=H).json()
    assert next(l for l in j["lines"] if l["line_id"] == clean.line_id)["state"] == "running"


def test_oee_is_availability_times_performance_times_quality():
    df = pd.DataFrame({"downtime_min": [30], "actual_qty": [100], "target_qty": [400], "good_qty": [90]})
    o = twin._oee(df)
    assert o["availability"] == 50.0 and o["performance"] == 50.0 and o["quality"] == 90.0 and o["oee"] == 22.5


def test_time_is_clamped_and_validated():
    early = c.get("/api/twin?at=2020-01-01T00:00", headers=H).json()
    assert early["at"] == HP.event_ts.min()
    assert c.get("/api/twin?at=2026-10-15T14:37", headers=H).json()["at"] == "2026-10-15T14:00:00"     # rounded down to the hour
    assert c.get("/api/twin?at=yesterday", headers=H).status_code == 422


def test_map_sites_place_each_plant_in_its_city_with_its_status():
    s = c.get("/api/twin/sites", headers=H).json()
    by = {p["plant_id"]: p for p in s}
    assert by["IN-TN-01"]["city"] == "Hosur" and by["IN-KA-01"]["city"] == "Bengaluru" and all(p["approximate"] for p in s)
    assert all(p["lat"] and p["lon"] and "open_problems" in p and "open_alerts" in p for p in s)
    assert by["IN-TN-01"]["lines"] == 3 and by["IN-TN-01"]["generated_lines"] == ["SMT-L3"] and by["IN-TN-01"]["store"] == "Conventional store"
    assert by["IN-KA-01"]["lines"] == 5 and sorted(by["IN-KA-01"]["generated_lines"]) == ["FAL-L4", "SMT-L4", "SMT-L5"] and by["IN-KA-01"]["store"] == "High-bay store"


def test_each_plant_has_its_own_bays_and_store():
    plants = {p["plant_id"]: p["layout"] for p in c.get("/api/twin?at=2026-10-31T14:00", headers=H).json()["plants"]}
    assert plants["IN-TN-01"]["bay_lines"] == ["SMT-L1", "FAL-L1", "SMT-L3"] and plants["IN-TN-01"]["bays"] == 3
    assert plants["IN-KA-01"]["bay_lines"] == ["SMT-L2", "FAL-L2", "SMT-L4", "FAL-L4", "SMT-L5"] and plants["IN-KA-01"]["bays"] == 5
    assert plants["IN-KA-01"]["rack_levels"] > plants["IN-TN-01"]["rack_levels"]
    hosur = c.get("/api/twin/warehouse/IN-TN-01/RM-WH-01?at=2026-10-31T14:00", headers=H).json()["storage"]
    blr = c.get("/api/twin/warehouse/IN-KA-01/RM-WH-01?at=2026-10-31T14:00", headers=H).json()["storage"]
    assert hosur["bulk_store"] and not blr["bulk_store"] and blr["store"] == "High-bay store"


def test_machine_details_from_the_records():
    m = c.get("/api/twin/machine/M-SMT-01?at=2026-10-31T14:00", headers=H).json()
    assert m["line_id"] == "SMT-L1" and m["plant_id"] == "IN-TN-01" and m["condition"]["label"] in ("Good", "Watch", "Needs attention")
    line = HP[(HP.line_id == "SMT-L1") & (HP.event_ts <= "2026-10-31T14:00:00")]
    today, year = m["output"][0], m["output"][3]
    assert today["units"] == line[line.event_date == "2026-10-31"].actual_qty.sum()       # on a serial line each unit passes every machine
    assert year["units"] == line.actual_qty.sum() and year["label"] == "2026 so far" and m["own_records"]
    hr = line[line.event_ts == "2026-10-31T14:00:00"].iloc[0]
    assert m["supply"]["product_id"] == hr.product_id and m["supply"]["parts"] and all("cover_days" in p for p in m["supply"]["parts"])
    sv = m["service"]
    assert pd.Timestamp(sv["next"]) - pd.Timestamp(sv["last"]) == pd.Timedelta(days=sv["interval_days"]) and pd.Timestamp(sv["last"]) <= pd.Timestamp("2026-10-31")
    fal = c.get("/api/twin/machine/M-FAL-01?at=2026-10-31T14:00", headers=H).json()
    assert fal["supply"]["upstream"]["line_id"] == "SMT-L1"                                # final assembly is fed by SMT
    assert c.get("/api/twin/machine/NOPE", headers=H).status_code == 404

def test_every_machine_has_its_own_records_and_maintenance_history():
    s = c.get("/api/twin?at=2026-10-31T14:00", headers=H).json()
    lines = {l["line_id"]: l for l in s["lines"]}
    assert {"SMT-L3", "SMT-L4", "FAL-L4", "SMT-L5"} <= set(lines) and "FAL-L3" not in lines and lines["SMT-L3"]["generated"] and not lines["SMT-L1"]["generated"]
    assert all(m["own_record"] for l in s["lines"] for m in l["machines"])                  # the generated bays included
    assert sum(len(l["machines"]) for l in s["lines"]) == 31
    m = c.get("/api/twin/machine/M-SMT-03?at=2026-10-31T14:00", headers=H).json()
    assert m["generated_line"] and m["service"]["basis"] == "maintenance records" and m["maintenance"]["events"]
    assert m["condition"]["mtbf_h"] is None or m["condition"]["mtbf_h"] > 0


def test_planning_never_sees_the_generated_bays():
    inp = data.to_inputs(data.load_csv())
    assert {l.line_id for l in inp.lines} == {"SMT-L1", "FAL-L1", "SMT-L2", "FAL-L2"}


def test_vehicles_warehouses_areas_and_crews_have_details():
    v = c.get("/api/twin/vehicle/IN-TN-01-AGV-2?at=2026-10-31T14:00", headers=H).json()
    fal = HP[(HP.line_id == "FAL-L1") & (HP.event_ts == "2026-10-31T14:00:00")].iloc[0]
    assert v["now"]["loads"] == fal.good_qty and v["output"][3]["loads"] > 0 and v["service"]["basis"] == "maintenance records"
    assert c.get("/api/twin/vehicle/IN-TN-01-LORRY-IN", headers=H).status_code == 404          # lorries have no records
    w = c.get("/api/twin/warehouse/IN-TN-01/RM-WH-01?at=2026-10-31T14:00", headers=H).json()
    assert w["totals"]["materials"] > 0 and any(m["cover_days"] is not None for m in w["materials"]) and len(w["trend"]) <= 30
    for kind in ("receiving", "shipping", "packing", "gate", "office"):
        assert c.get(f"/api/twin/area/IN-TN-01/{kind}?at=2026-10-31T14:00", headers=H).status_code == 200
    assert c.get("/api/twin/area/IN-TN-01/roof", headers=H).status_code == 404
    cr = c.get("/api/twin/crew/SMT-L1?at=2026-10-31T14:00", headers=H).json()
    assert cr["team"] == HP[(HP.line_id == "SMT-L1") & (HP.event_ts == "2026-10-31T14:00:00")].iloc[0].operator_team and cr["hours_on_line"] > 0
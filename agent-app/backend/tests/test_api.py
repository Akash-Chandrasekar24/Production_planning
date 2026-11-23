import pytest
from fastapi.testclient import TestClient
from app import publish, store
from app.integrations import NotConfigured
from app.main import app

c = TestClient(app)
PL = {"x-role": "planner", "x-user": "pat"}
VW = {"x-role": "viewer", "x-user": "vic"}
SPIKE = {"name": "spike x3", "demand_spikes": [{"multiplier": 3}]}


@pytest.fixture(scope="module")
def base():
    return c.post("/api/schedules", json={}, headers=PL).json()["id"]


@pytest.fixture
def risky(base):
    r = c.post(f"/api/schedules/{base}/scenarios", json=SPIKE, headers=PL)
    assert r.status_code == 201
    return r.json()["id"]


def test_auth_and_roles():
    assert c.get("/api/schedules").status_code == 401
    assert c.post("/api/schedules", json={}, headers=VW).status_code == 403
    assert c.post("/api/schedules", json={}, headers={"x-role": "agent"}).status_code == 403
    r = c.get("/api/schedules", headers={"x-role": ""})
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHENTICATED" and r.json()["request_id"]


def test_generate_is_separate_from_approval_and_nothing_to_approve(base):
    s = c.get(f"/api/schedules/{base}", headers=VW).json()
    # open production orders cover most demand with what they still owe; the rest is new production waiting for approval
    assert s["status"] == "PENDING_APPROVAL" and s["summary"]["po_covered_qty"] > 0 and s["assignments"]
    from app import data, planning
    inp = data.to_inputs(store.D())
    inp.orders, inp.forecast = [], {}                    # nothing to make: no orders and no forecast
    empty = planning.plan(inp)
    store.put(empty, actor="test", inp=inp)
    assert empty.feasibility == "NO_NEW_PRODUCTION_NEEDED"
    r = c.post(f"/api/schedules/{empty.id}/approve", json={}, headers=PL)
    assert r.status_code == 409 and r.json()["error"]["code"] == "NOTHING_TO_APPROVE"


def test_approval_requires_planner_and_risk_ack_and_is_audited(risky):
    assert c.post(f"/api/schedules/{risky}/approve", json={}, headers=VW).status_code == 403
    r = c.post(f"/api/schedules/{risky}/approve", json={}, headers=PL)
    assert r.status_code == 409 and r.json()["error"]["code"] == "RISK_ACK_REQUIRED" and r.json()["error"]["detail"]
    ok = c.post(f"/api/schedules/{risky}/approve", json={"acknowledge_risks": True, "note": "ok for pilot"}, headers=PL)
    assert ok.status_code == 200 and ok.json()["status"] == "APPROVED" and ok.json()["decided_by"] == "pat"
    assert c.post(f"/api/schedules/{risky}/approve", json={"acknowledge_risks": True}, headers=PL).json()["error"]["code"] == "INVALID_STATE"
    assert c.post(f"/api/schedules/{risky}/reject", json={}, headers=PL).status_code == 409
    actions = [e["action"] for e in c.get(f"/api/schedules/{risky}/audit", headers=VW).json()]
    assert actions[:2] == ["scenario_created", "approved"] and set(actions[2:]) <= {"plan_emailed"}    # emailed when approved


def test_reject_needs_no_ack(base):
    sid = c.post(f"/api/schedules/{base}/scenarios", json=SPIKE, headers=PL).json()["id"]
    assert c.post(f"/api/schedules/{sid}/reject", json={"note": "no"}, headers=PL).json()["status"] == "REJECTED"


def test_publish_never_claimed_without_connector_and_works_with_one(base, monkeypatch):
    sid = c.post(f"/api/schedules/{base}/scenarios", json=SPIKE, headers=PL).json()["id"]
    assert c.post(f"/api/schedules/{sid}/publish", headers=PL).json()["error"]["code"] == "NOT_APPROVED"
    c.post(f"/api/schedules/{sid}/approve", json={"acknowledge_risks": True}, headers=PL)
    r = c.post(f"/api/schedules/{sid}/publish", headers=PL)
    assert r.status_code == 501 and r.json()["error"]["code"] == "PUBLISH_CONNECTOR_UNAVAILABLE"
    assert store.get(sid).status == "APPROVED"                       # not published
    assert c.post(f"/api/schedules/{sid}/publish", headers=VW).status_code == 403

    class Fake:
        def publish(self, s, d):
            return {"rows": len(d)}
    monkeypatch.setattr(publish, "get_publisher", lambda: Fake())
    assert c.post(f"/api/schedules/{sid}/publish", headers=PL).json()["status"] == "PUBLISHED"
    assert "published" in [e["action"] for e in c.get(f"/api/schedules/{sid}/audit", headers=VW).json()]


def test_export_is_labelled_and_planner_only(risky):
    assert c.get(f"/api/schedules/{risky}/export", headers=VW).status_code == 403
    r = c.get(f"/api/schedules/{risky}/export", headers=PL)
    assert r.status_code == 200 and r.headers["x-export-label"].startswith("EXPORT-ONLY") and r.headers["x-approval-status"] == "PENDING_APPROVAL"
    assert r.text.splitlines()[0].startswith("approval_status,plant_id,line_id") and len(r.text.splitlines()) > 1


def test_validation_errors_are_consistent(base):
    r = c.post("/api/schedules", json={"horizon_days": 0, "as_of": "garbage"}, headers=PL)
    e = r.json()["error"]
    assert r.status_code == 422 and e["code"] == "INVALID_REQUEST" and {d["field"] for d in e["detail"]} == {"horizon_days", "as_of"}
    r = c.post(f"/api/schedules/{base}/scenarios", json={"name": "x", "machine_losses": [{"machine_id": "M-NOPE", "start": "2026-11-01T00:00:00", "end": "2026-11-02T00:00:00"}]}, headers=PL)
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_SCENARIO"
    assert c.get("/api/schedules/nope", headers=VW).json()["error"]["code"] == "SCHEDULE_NOT_FOUND"


def test_persistence_survives_cache_loss_and_lists(base, risky):
    store._inputs_cache.clear()
    assert c.get(f"/api/schedules/{risky}/material-calloff", headers=VW).status_code == 200
    assert c.get(f"/api/schedules/{risky}/capacity-gaps", headers=VW).status_code == 200
    assert c.get(f"/api/schedules/{risky}/dispatch", headers=VW).status_code == 200
    ids = [s["id"] for s in c.get("/api/schedules?limit=200", headers=VW).json()]
    assert base in ids and risky in ids
    assert len(c.get(f"/api/compare?base={base}&ids={risky}", headers=VW).json()) == 2


def test_status_reports_integrations_without_secrets(monkeypatch):
    monkeypatch.setenv("DOMO_TOKEN", "tok-should-not-leak")
    monkeypatch.setenv("DOMO_SUBDOMAIN", "mydomo-instance")
    r = c.get("/api/status", headers=VW)
    j = r.json()
    assert "tok-should-not-leak" not in r.text and "mydomo-instance" not in r.text
    assert j["integrations"]["domo_llm"] == "CONFIGURED" and j["integrations"]["erp_mes_publish"].startswith("NOT_AVAILABLE")
    assert j["auth"]["authenticated"] is False


def test_unverified_auth_mode_fails_closed(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "oidc")
    r = c.get("/api/schedules", headers=PL)
    assert r.status_code == 503 and r.json()["error"]["code"] == "AUTH_NOT_CONFIGURED"
    assert c.get("/health").status_code == 200


def test_ui_sources_have_no_secrets_and_new_endpoints_work():
    from pathlib import Path
    ui = Path(__file__).resolve().parents[2] / "frontend" / "src"
    for f in (ui.rglob("*.*") if ui.is_dir() else []):
        t = f.read_text(encoding="utf-8", errors="ignore")
        for bad in ("DOMO_TOKEN", "X-DOMO", "DATABASE_URL", "postgresql://", "npg_", "nvapi-", "napi_"):   # the sign-in token is per session, not in the code
            assert bad not in t, (f.name, bad)
    assert c.get("/api/reference", headers=VW).json()["machines"]
    assert "latest" in c.get("/api/status", headers=VW).json()["data"]


def test_engineer_plans_but_only_managers_approve():
    from app.config import Policy
    ra = Policy().role_actions
    assert "generate" in ra["engineer"] and "intake" in ra["engineer"] and "approve" not in ra["engineer"]
    assert "approve" in ra["production_manager"] and "generate" not in ra["production_manager"]
    assert "approve" not in ra["plant_head"] and "generate" not in ra["plant_head"]


def test_neon_connection_off_neon():
    # tests run on SQLite: the Neon card says so instead of failing, and no table can be previewed
    r = c.get("/api/integrations/neon", headers=VW)
    assert r.status_code == 200 and r.json()["connected"] is False
    assert c.get("/api/integrations/neon/tables/app_users", headers=VW).status_code == 404


def test_excel_sheet_becomes_csv():
    import base64, datetime as dt, io
    import pandas as pd
    b = io.BytesIO()
    pd.DataFrame({"po_number": ["PO-1"], "qty": [5], "due_date": [dt.datetime(2026, 12, 1)]}).to_excel(b, index=False)
    eng = {"x-role": "engineer", "x-user": "erin"}
    r = c.post("/api/orders/sheet", json={"data": base64.b64encode(b.getvalue()).decode()}, headers=eng)
    assert r.status_code == 200 and r.json()["csv"].splitlines()[1] == "PO-1,5,2026-12-01"
    assert c.post("/api/orders/sheet", json={"data": "aGVsbG8="}, headers=eng).status_code == 422
    assert c.post("/api/orders/sheet", json={"data": "aGVsbG8="}, headers=VW).status_code == 403


def test_approving_another_option_closes_the_agent_plan():
    sid = c.post("/api/schedules", json={}, headers=PL).json()["id"]
    sc = {"name": "Option: work through breaks", "extra_time": [{"start": "2020-01-01T00:00:00", "end": "2030-01-01T00:00:00", "minutes_per_shift": 30}]}
    assert c.post(f"/api/schedules/{sid}/approve-option", json={"scenario": sc}, headers={"x-role": "engineer", "x-user": "erin"}).status_code == 403
    r = c.post(f"/api/schedules/{sid}/approve-option", json={"scenario": sc, "acknowledge_risks": True}, headers={"x-role": "production_manager", "x-user": "mia"})
    assert r.status_code == 200 and r.json()["status"] == "APPROVED" and r.json()["id"] != sid
    assert c.get(f"/api/schedules/{sid}", headers=VW).json()["status"] == "REJECTED"
    assert c.get(f"/api/schedules/{r.json()['id']}", headers=VW).json()["name"] == "Option: work through breaks"


def test_forecast_series_for_each_section():
    for sec in ("demand", "production", "inventory", "machinery", "workforce"):
        r = c.get("/api/plan-tools/forecast-series", params={"section": sec, "weeks": 4}, headers=VW)
        assert r.status_code == 200, r.text
        j = r.json()
        assert len(j["forecast"]) >= 1 and all("week" in p and "v" in p for p in j["history"] + j["forecast"])
        if j["history"] and j["forecast"]:                         # the forecast starts after the last recorded week
            assert j["forecast"][0]["week"] > j["history"][-1]["week"]
    assert c.get("/api/plan-tools/forecast-series", params={"section": "machinery", "plant": "IN-TN-01"}, headers=VW).json()["plant"] == "IN-TN-01"
    assert c.get("/api/plan-tools/forecast-series", params={"section": "nope"}, headers=VW).status_code == 422


def test_forecast_agent_picks_a_method_and_explains():
    for sec in ("demand", "production", "machinery", "inventory", "workforce"):
        j = c.get("/api/plan-tools/forecast-agent", params={"section": sec, "weeks": 4}, headers=VW).json()
        a = j["agent"]
        assert a["thoughts"] and a["findings"] and all(f["tone"] in ("good", "warn", "bad") for f in a["findings"])
        assert len(j["forecast"]) >= 1
        if a["methods"]:                                           # back-tested: the chosen method is first and drives the range
            assert len(a["methods"]) == 4 and j["method"].startswith("Forecast agent")
            assert all(p["lo"] <= p["v"] <= p["hi"] for p in j["forecast"])


def test_use_case_kpis_has_the_seven_measures():
    j = c.get("/api/plan-tools/kpis", headers=VW).json()
    assert [i["key"] for i in j["items"]] == ["adherence", "attainment", "on_time", "changeover", "wip", "utilisation", "stability"]
    assert all(i["tone"] in (None, "ok", "warn", "bad") and i["note"] for i in j["items"])
    assert c.get("/api/plan-tools/kpis", params={"schedule_id": j["schedule_id"]}, headers=VW).json()["schedule_id"] == j["schedule_id"]
    assert c.get("/api/plan-tools/kpis", params={"schedule_id": "nope"}, headers=VW).status_code == 404


def test_material_readiness_follows_the_plan():
    j = c.get("/api/plan-tools/readiness", headers=VW).json()
    assert {"room", "held", "kits", "risk", "runs_out"} <= set(j)
    s = c.get(f"/api/schedules/{j['schedule_id']}", headers=VW).json()
    # every order the plan held back for material is listed, with the material it waits for
    want = {u["customer_order_id"] for u in s["unscheduled"] if u["reason"] in ("MATERIAL_SHORT", "MATERIAL_BLOCKED")}
    assert {h["order_id"] for h in j["held"]} == want and all(h["limiting"] for h in j["held"])
    # the daily kits add up to the plan's scheduled phones (jobs whose material is already issued included)
    assert sum(k["phones"] for k in j["kits"]) == sum(a["qty"] for a in s["assignments"])
    assert c.get("/api/plan-tools/readiness", params={"schedule_id": "nope"}, headers=VW).status_code == 404


def test_outlook_comes_with_ai_reasoning():
    r = c.get("/api/plan-tools/outlook", params={"weeks": 8}, headers=VW).json()["reasoning"]
    assert r["of"] == 10 and 1 <= r["pick"] <= 10                       # one of ten summaries
    assert 3 <= r["text"].count(". ") + 1 <= 5 and r["text"].endswith(".")   # a short summary, 3-4 sentences
    assert r == c.get("/api/plan-tools/outlook", params={"weeks": 8}, headers=VW).json()["reasoning"]   # the same until the agent runs again


def test_a_what_if_can_be_added_to_the_plan_and_the_agent_replans():
    from app import incidents
    pm = {"x-role": "production_manager", "x-user": "maya"}
    eng = {"x-role": "engineer", "x-user": "eve"}
    base = incidents._base()[0].id
    wi = c.post(f"/api/schedules/{base}/scenarios", json={"name": "Demand +20%", "demand_spikes": [{"multiplier": 1.2}]}, headers=eng).json()["id"]
    assert c.post(f"/api/schedules/{base}/adopt", headers=eng).status_code == 409                  # a plan is not a what-if
    assert c.post(f"/api/schedules/{wi}/adopt", headers=pm).status_code == 403                      # adding needs what-if rights
    r = c.post(f"/api/schedules/{wi}/adopt", headers=eng)
    assert r.status_code == 201, r.text
    j = r.json()
    new = j["schedule_id"]
    assert new != wi and j["base_id"] == base and "Demand +20%" in j["name"] and j["tasks"] >= 1
    assert c.post(f"/api/schedules/{wi}/adopt", headers=eng).status_code == 409                     # only once
    row = next(x for x in c.get("/api/schedules?limit=50", headers=VW).json() if x["id"] == new)
    assert row["adopted"] and row["status"] == "PENDING_APPROVAL"                                    # a real plan, waiting for people
    assert next(x for x in c.get("/api/schedules?limit=50", headers=VW).json() if x["id"] == wi)["adopted"] is False
    tasks = c.get("/api/agent/tasks", headers=VW).json()
    assert any(t["schedule_id"] == new and t["role"] == "production_manager" for t in tasks)          # the production manager is told
    ok = c.post(f"/api/schedules/{new}/approve", json={"acknowledge_risks": True}, headers=pm)
    assert ok.status_code == 200, ok.text
    assert incidents._base()[0].id == new                                                            # approved: the plan everyone works from


def test_every_stage_approves_then_the_plan_is_fully_approved_and_emailed(monkeypatch):
    from app import signoff
    sent = []
    monkeypatch.setattr(signoff, "email_plan", lambda sid: sent.append(sid))           # the email runs in the background; checked below
    pm = {"x-role": "production_manager", "x-user": "maya"}
    sid = c.post("/api/schedules", json={}, headers=PL).json()["id"]
    plants = signoff.plants(store.get(sid))
    assert plants
    head = lambda p: {"x-role": "plant_head", "x-user": f"head-{p}", "x-plant": p}
    assert c.post(f"/api/schedules/{sid}/plant-approve", headers=head(plants[0])).status_code == 409   # the production manager goes first
    assert c.post(f"/api/schedules/{sid}/plant-approve", headers=pm).status_code == 403               # only production heads approve a plant
    assert c.post(f"/api/schedules/{sid}/approve", json={"acknowledge_risks": True}, headers=pm).status_code == 200
    import time
    for _ in range(50):
        if sent:
            break
        time.sleep(0.1)
    assert sent == [sid]                                                                               # emailed once, when the manager approves
    assert c.post(f"/api/schedules/{sid}/plant-approve", headers=head("XX-NONE")).status_code == 409  # no work for that plant
    for i, p in enumerate(plants):
        st = c.post(f"/api/schedules/{sid}/plant-approve", headers=head(p)).json()
        assert st["fully_approved"] is (i == len(plants) - 1)
    assert c.post(f"/api/schedules/{sid}/plant-approve", headers=head(plants[0])).status_code == 409   # once per plant
    assert sent == [sid]                                                                               # not again when the plants approve
    assert next(x for x in c.get("/api/schedules?limit=50", headers=VW).json() if x["id"] == sid)["fully_approved"]
    ap = next(x for x in c.get("/api/approved-plans", headers=VW).json() if x["id"] == sid)
    assert all(s["done"] and s["by"] for s in ap["stages"]) and len(ap["stages"]) == len(plants) + 1


def test_plan_email_has_the_details_and_goes_to_the_people_concerned(monkeypatch):
    from app import notify, signoff
    from app.db import SessionLocal, TeamMember
    sid = c.post("/api/schedules", json={}, headers=PL).json()["id"]
    s = store.get(sid)
    subject, text, body = signoff.content(s, signoff.stages(s), [{"material_id": "M-1", "required_qty": 10, "available_qty": 4,
                                                                   "shortfall_qty": 6, "shortfall_cost_inr": 600}])
    a = s.assignments[0]
    assert s.name in subject and a.plant_id in body and "M-1" in body and "On-time delivery" in body and "cid:gwcmark" in body
    assert a.customer_order_id not in body                                  # the email is a summary: every job is in the CSV
    files = dict(signoff.attachments(s, [{"material_id": "M-1", "shortfall_qty": 6}]))
    assert a.customer_order_id in files[f"plan-{sid}-jobs.csv"].decode("utf-8-sig") and f"plan-{sid}-materials-short.csv" in files
    with SessionLocal() as db:
        db.add_all([TeamMember(name="Maya", role="production_manager", email="maya@example.com"),
                    TeamMember(name="Far", role="plant_head", plant_id="XX-NONE", email="far@example.com"),
                    TeamMember(name="Ada", role="admin", email="ada@example.com"), TeamMember(name="Eli", role="engineer", email="eli@example.com")])
        db.commit()
    got = []

    class Fake:
        def send_message(self, m):
            got.append((m["To"], [p.get_filename() for p in m.iter_attachments()]))

        def quit(self):
            pass
    monkeypatch.setattr(notify, "configured", lambda: True)
    monkeypatch.setattr(notify, "_smtp", lambda: Fake())
    r = signoff.email_plan(sid)
    assert r["status"] == "SENT" and any(to == "maya@example.com" and f"plan-{sid}-jobs.csv" in fs for to, fs in got)
    assert all(to != "far@example.com" for to, _ in got)                                   # a production head of another plant is left out
    assert not {to for to, _ in got} & {"ada@example.com", "eli@example.com"}             # never admins or engineers


def test_plant_brief_uses_the_standard_terms_and_a_head_sees_only_their_plant():
    sid = c.post("/api/schedules", json={}, headers=PL).json()["id"]
    url = f"/api/plan-tools/plant-brief?schedule_id={sid}&plant="
    head = {"x-role": "plant_head", "x-user": "head-tn", "x-plant": "IN-TN-01"}
    assert c.get(url + "IN-KA-01", headers=head).status_code == 403                  # another plant's plan
    b = c.get(url + "IN-TN-01", headers=head).json()
    assert b["available"] and b["units"] > 0
    jobs = [a for a in store.get(sid).assignments if a.plant_id == "IN-TN-01"]
    assert sum(w["total"] for w in b["mps"]["weeks"]) == b["units"] == sum(a.qty for a in jobs)          # MPS adds up to the plan
    assert sum(b["time_fences"][k]["jobs"] for k in ("frozen", "slushy", "flexible")) == b["jobs"]       # every job sits in one fence
    assert {t["stage"] for t in b["takt"]} == {"SMT", "FAL"} and all(t["takt_s"] > 0 and t["cycle_s"] > 0 for t in b["takt"])
    assert b["bottleneck"] == max(b["utilisation"], key=lambda u: u["utilisation_pct"])["line_id"]
    assert 0 <= b["otd_pct"] <= 100 and all(e["runs"] >= 1 for e in b["epe"])
    assert all(r["order_by"] <= r["short_on"] for r in b["mrp"]["rows"])                                # ordered a lead time ahead
    assert c.get(url + "IN-KA-01", headers=PL).status_code == 200                                       # others may look at either plant


def test_demand_budget_document_execution_notes_and_reports(monkeypatch):
    sid = c.post("/api/schedules", json={}, headers=PL).json()["id"]
    pm = {"x-role": "production_manager", "x-user": "maya"}
    tn = {"x-role": "plant_head", "x-user": "head-tn", "x-plant": "IN-TN-01"}
    # 1 demand: forecast review, budget, rough-cut
    f = c.get(f"/api/demand/forecast?schedule_id={sid}", headers=VW).json()
    row = f["rows"][0]
    assert f["products"] and row["model"] >= 0 and row["agreed"] is None
    assert c.put("/api/demand/forecast", json={"product_id": row["product_id"], "week": row["week"], "qty": 1234}, headers=VW).status_code == 403
    assert c.put("/api/demand/forecast", json={"product_id": row["product_id"], "week": row["week"], "qty": 1234, "note": "sales call"}, headers=pm).status_code == 200
    from app import data, store as st
    assert any(w["qty"] == 1234 and w["agreed"] for w in data.to_inputs(st.D()).forecast[row["product_id"]] if w["week"] == row["week"])   # the next plan uses it
    c.put("/api/demand/forecast", json={"product_id": row["product_id"], "week": row["week"], "qty": None}, headers=pm)                  # back to the model
    b = c.get(f"/api/demand/budget?schedule_id={sid}", headers=VW).json()
    for r in b["rows"]:
        assert r["required"] == max(0, r["expected_sales"] + r["desired_ending_stock"] - r["beginning_stock"] - r["open_production_orders"])
    assert c.get(f"/api/demand/rough-cut?schedule_id={sid}", headers=VW).json()["weeks"]
    # the plan document has its seven parts
    d = c.get(f"/api/plan-document/{sid}", headers=VW).json()
    assert all(d[k] is not None for k in ("forecast", "budget", "inventory", "resources", "cost", "schedule", "control"))
    # 5 execution: a head sees and moves their own plant's jobs; blocked needs a reason and alerts the manager
    assert c.get("/api/execution?plant=IN-KA-01", headers=tn).status_code == 403
    board = c.get("/api/execution?plant=IN-TN-01", headers=tn).json()
    j = board["jobs"][0]
    up = lambda status, note=None, h=tn: c.post("/api/execution/jobs", json={"order_id": j["customer_order_id"], "stage": j["stage"], "status": status, "note": note}, headers=h)
    assert up("BLOCKED").status_code == 400
    assert up("BLOCKED", "feeder jammed").json()["status"] == "BLOCKED"
    assert any(t["kind"] == "alert" and "blocked" in t["title"].lower() for t in c.get("/api/agent/tasks?role=production_manager", headers=VW).json())
    assert up("DONE").status_code == 200
    again = c.get(f"/api/execution?plant=IN-TN-01&day={board['day']}", headers=tn).json()
    assert next(x for x in again["jobs"] if x["customer_order_id"] == j["customer_order_id"] and x["stage"] == j["stage"])["progress"] == "DONE"
    # comments and the risk log
    assert c.post(f"/api/schedules/{sid}/notes", json={"kind": "risk", "text": "Supplier strike"}, headers=tn).status_code == 400   # needs a severity
    rk = c.post(f"/api/schedules/{sid}/notes", json={"kind": "risk", "text": "Supplier strike", "severity": "high", "owner": "maya"}, headers=tn).json()
    c.post(f"/api/schedules/{sid}/notes", json={"kind": "comment", "text": "Looks fine for Hosur"}, headers=pm)
    assert c.post(f"/api/notes/{rk['id']}/close", headers=pm).json()["status"] == "CLOSED"
    assert {n["kind"] for n in c.get(f"/api/schedules/{sid}/notes", headers=VW).json()} == {"risk", "comment"}
    # reports: preview, send once a day, and only managers/admins may send
    assert c.get("/api/reports/preview?kind=daily&plant=IN-TN-01", headers=VW).json()["subject"].startswith("Daily brief")
    assert c.post("/api/reports/send", json={"kind": "weekly"}, headers=tn).status_code == 403
    from datetime import datetime
    from app import reports
    monkeypatch.setattr(reports, "send", lambda r, to=None: {"to": 0, "sent": 0, "failed": 0})
    assert reports.run_due(datetime(2026, 11, 16, 8)) == []            # scheduled report emails are off unless switched on
    from app.config import Policy
    monkeypatch.setattr(reports, "Policy", lambda: Policy(reports_enabled=True), raising=False)
    first = reports.run_due(datetime(2026, 11, 16, 8))                 # a Monday after 07:00: two daily briefs and the weekly report
    assert sorted(k.split(":")[0] for k in first) == ["daily", "daily", "weekly"] and reports.run_due(datetime(2026, 11, 16, 9)) == []
    assert reports.run_due(datetime(2026, 11, 17, 6)) == []            # before the hour: nothing yet


def test_overview_solutions_and_delivery_heatmap():
    c.post("/api/schedules", json={}, headers=PL)
    sol = c.get("/api/overview/solutions", headers=VW).json()
    assert len(sol["items"]) == 5 and all(i["title"] and i["value"] and i["label"] and i["problem"] for i in sol["items"])
    assert not any(w in " ".join(i["title"] + i["text"] + i["problem"] for i in sol["items"]).lower() for w in ("orchestrator", "watcher", "sourcing agent", "floor agent"))
    h = c.get("/api/overview/delivery-heatmap", headers=VW).json()
    assert h["windows"][0]["label"] == "Overdue" and h["rows"]
    for r in h["rows"]:
        assert len(r["cells"]) == len(h["windows"])
        assert sum(x["orders"] for x in r["cells"] if x) == r["orders"]
        assert all(0 <= x["pct"] <= 100 for x in r["cells"] if x and x["pct"] is not None)


def test_final_production_plan_has_every_part_and_adds_up():
    sid = c.post("/api/schedules", json={}, headers=PL).json()["id"]
    f = c.get(f"/api/final-plan/{sid}", headers=VW).json()
    assert f["badge"] == "pending" and f["period"]["from"] <= f["period"]["to"]
    for part in ("kpis", "strategy", "mps", "daily", "mrp", "machines", "workforce", "gantt", "maintenance", "quality", "cost", "risks", "recommendations", "outcome"):
        assert part in f
    s = store.get(sid)
    planned = sum(a.qty for a in s.assignments)
    assert f["kpis"]["planned"] == planned == sum(r["planned"] for r in f["strategy"]) == sum(r["total"] for r in f["mps"]["rows"])
    assert abs(sum(r["mix_pct"] for r in f["strategy"]) - 100) < 1
    assert all(r["status"] == ("Overloaded" if r["utilisation"] > 100 else "High load" if r["utilisation"] >= 90 else "Available") for r in f["machines"] if r["utilisation"] is not None)
    assert all(r["shortage"] == 0 or r["action"].startswith("Procure") for r in f["mrp"])
    assert f["gantt"]["rows"] and all(r["runs"] and all(0 <= x["left"] <= 100 and x["width"] > 0 for x in r["runs"]) for r in f["gantt"]["rows"])


def test_the_production_manager_can_delete_an_approval(monkeypatch):
    from app import signoff
    monkeypatch.setattr(signoff, "email_plan", lambda sid: None)
    pm, eng = {"x-role": "production_manager", "x-user": "maya"}, {"x-role": "engineer", "x-user": "eli"}
    sid = c.post("/api/schedules", json={}, headers=PL).json()["id"]
    assert c.post(f"/api/schedules/{sid}/approve", json={"acknowledge_risks": True}, headers=pm).status_code == 200
    assert any(x["id"] == sid for x in c.get("/api/approved-plans", headers=VW).json())
    assert c.delete(f"/api/approvals/{sid}", headers=eng).status_code == 403                        # only the production manager
    r = c.delete(f"/api/approvals/{sid}", headers=pm)
    assert r.status_code == 200 and r.json()["history_entries"] >= 1, r.text
    assert store.get(sid).status == "REJECTED"
    assert not any(x["id"] == sid for x in c.get("/api/approved-plans", headers=VW).json())        # gone from the history
    assert "approved" not in [e["action"] for e in c.get(f"/api/schedules/{sid}/audit", headers=VW).json()]
    from app.db import OrderFreeze, SessionLocal
    with SessionLocal() as s:
        assert s.query(OrderFreeze).filter_by(schedule_id=sid).count() == 0                          # its orders can change again
    assert c.delete(f"/api/approvals/{sid}", headers=pm).status_code == 409                         # nothing left to delete

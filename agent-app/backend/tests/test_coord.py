"""Running as several processes: leases, mutexes, one leader per loop, cache versions, durable jobs, emails sent once."""
import threading
import time

from fastapi.testclient import TestClient

from app import coord, jobs, notify, store
from app.db import AgentJob, Notification, SessionLocal
from app.main import app

c = TestClient(app)
PL = {"x-role": "planner", "x-user": "pat"}


def test_a_lease_has_one_holder_until_it_expires():
    assert coord.acquire("t-lease", 0.3, "proc-A")
    assert not coord.acquire("t-lease", 0.3, "proc-B")            # another process waits
    assert coord.acquire("t-lease", 0.3, "proc-A")                # the holder renews
    time.sleep(0.4)
    assert coord.acquire("t-lease", 5, "proc-B")                  # a holder that stopped renewing loses it
    coord.release("t-lease", "proc-B")
    assert coord.acquire("t-lease", 5, "proc-A")


def test_a_mutex_excludes_other_threads_and_is_reentrant():
    m, order = coord.Mutex("t-mutex", ttl=5, wait=5), []

    def other():
        with coord.Mutex("t-mutex", ttl=5, wait=5):
            order.append("B")
    with m:
        with m:                                                   # the same thread may enter again
            t = threading.Thread(target=other)
            t.start()
            time.sleep(0.3)
            order.append("A")
    t.join(5)
    assert order == ["A", "B"]


def test_another_process_holding_the_lock_makes_us_wait():
    assert coord.acquire("t-busy", 0.5, "someone-else:mutex")
    t0 = time.monotonic()
    with coord.Mutex("t-busy", ttl=5, wait=5):                    # waits for the other process's lease to run out
        assert time.monotonic() - t0 >= 0.3


def test_only_one_process_leads_a_loop():
    assert coord.acquire("loop:t", 30, "p1:loop:t") and not coord.acquire("loop:t", 30, "p2:loop:t")


def test_a_change_in_another_process_clears_our_caches():
    hit = []
    coord.on_change(lambda: hit.append(1), "t-key")
    coord.bump("t-key")
    coord._checked[0] = 0
    coord.sync(0)                                                 # first look: remembers the version
    coord.bump("t-key")                                           # "another process" changes it
    coord._checked[0] = 0
    assert coord.sync(0) and hit == [1]


def test_jobs_are_queued_claimed_once_and_survive_in_the_database(monkeypatch):
    monkeypatch.setenv("RUN_MODE", "api")                          # the API only queues; a worker runs it
    jobs.RUNNERS["t-echo"] = lambda payload, say, think: {"echo": payload["x"]}
    jid = jobs.queue("t-echo", {"x": 42}, {"steps": {}})
    assert jobs.get(jid)["status"] == "running" and SessionLocal().get(AgentJob, jid).status == "queued"
    assert jobs.claim_and_run(jid) and not jobs.claim_and_run(jid)   # claimed once
    assert jobs.get(jid)["result"] == {"echo": 42} and jobs.get(jid)["status"] == "done"
    with SessionLocal() as s:                                     # a job whose worker died is marked failed, not left running
        s.add(AgentJob(id="dead1", kind="t-echo", status="running", payload={}, state={"status": "running"},
                       updated_at=coord._now() - __import__("datetime").timedelta(hours=1)))
        s.commit()
    jobs.work_once()
    assert jobs.get("dead1")["status"] == "failed"


def test_each_email_is_sent_once_even_with_two_senders(monkeypatch):
    sent = []

    class Smtp:
        def send_message(self, m):
            time.sleep(0.05)
            sent.append(m["To"])

        def quit(self):
            pass
    monkeypatch.setattr(notify, "_smtp", lambda: Smtp())
    with SessionLocal() as s:
        s.query(Notification).filter_by(email_status="PENDING").update({"email_status": "NOT_SENT"})
        for i in range(6):
            s.add(Notification(role="production_manager", email=f"p{i}@example.com", title="t", body="b", email_status="PENDING"))
        s.commit()
    ts = [threading.Thread(target=notify.send_pending) for _ in range(2)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert sorted(sent) == sorted(f"p{i}@example.com" for i in range(6))      # all six, none twice


def test_plan_jobs_are_rows_queryable_across_plans():
    sid = c.post("/api/schedules", json={}, headers=PL).json()["id"]
    s = store.get(sid)
    rows = c.get(f"/api/plan-jobs?schedule_id={sid}&limit=5000", headers=PL).json()
    assert len(rows) == 2 * len(s.assignments)
    line = rows[0]["line_id"]
    assert all(r["line_id"] == line for r in c.get(f"/api/plan-jobs?line_id={line}", headers=PL).json())
    head = {"x-role": "plant_head", "x-user": "h", "x-plant": "IN-TN-01"}
    assert all(r["plant_id"] == "IN-TN-01" for r in c.get("/api/plan-jobs?limit=200", headers=head).json())
    assert c.get("/api/plan-jobs?plant=IN-KA-01", headers=head).status_code == 403


def test_the_live_snapshot_has_what_the_page_needs():
    from app.auth import Principal
    from app.flow_api import _snapshot
    snap = _snapshot(Principal(user="pat", role="planner"), None)
    assert set(snap) >= {"unread", "newest", "ran"}


def test_a_leader_keeps_the_shared_schedule_and_hands_over(monkeypatch):
    monkeypatch.setattr(coord, "TICK", 0.05)
    monkeypatch.setattr(coord, "BEAT", 0.05)
    runs = []
    coord.leader_loop("t-sched", 3600, lambda: runs.append(1), first=True)
    for _ in range(100):
        if runs:
            break
        time.sleep(0.05)
    assert runs == [1] and "t-sched" in coord.leading()            # ran once at start
    time.sleep(0.3)
    assert runs == [1]                                              # not again until the interval has passed
    assert float(coord.get_state("loop:t-sched:last")) > time.time() - 60   # the schedule is shared through the database


def test_cached_pages_still_check_the_plant_and_clear_on_change():
    sid = c.post("/api/schedules", json={}, headers=PL).json()["id"]
    tn = {"x-role": "plant_head", "x-user": "h1", "x-plant": "IN-TN-01"}
    ka = {"x-role": "plant_head", "x-user": "h2", "x-plant": "IN-KA-01"}
    url = f"/api/plan-tools/plant-brief?schedule_id={sid}&plant=IN-TN-01"
    assert c.get(url, headers=tn).status_code == 200
    assert c.get(url, headers=ka).status_code == 403                 # a cached answer is never handed to the other plant's head
    n0 = len(c.get(f"/api/schedules/{sid}/notes", headers=PL).json())
    c.post(f"/api/schedules/{sid}/notes", json={"kind": "comment", "text": "hello"}, headers=PL)
    assert len(c.get(f"/api/schedules/{sid}/notes", headers=PL).json()) == n0 + 1      # a new comment shows at once
    before = {r["id"] for r in c.get("/api/schedules?limit=50", headers=PL).json()}
    new = c.post("/api/schedules", json={}, headers=PL).json()["id"]
    assert new not in before and new in {r["id"] for r in c.get("/api/schedules?limit=50", headers=PL).json()}   # new plans list at once

"""New purchase orders (customer POs) arriving: receive them, let every agent look at the plant, then decide per order.

  1. Receive: validate each order against the products, channels and dates in the data; all or nothing.
  2. Look: the orchestrator asks the floor agent (lines, breakdowns, maintenance), the inventory agent (stock and cover for the
     ordered products) and the sourcing agent (work blocked for parts, the other plant) what they see right now.
  3. Assess each new order on the planning engine (the working plan: the plan plus open production orders, with the new orders in):
     covered by open production orders, planned on time, late, or blocked for parts; and what it does to the orders already there.
     An order needs action when it is large (Policy.agent_min_new_units or more), high priority and due within
     Policy.agent_urgent_days, or cannot be met as the plan stands. Otherwise the current plan absorbs it and nothing is done.
  4. Act, only for orders that need it: the order watcher's thresholds send the planning agent to re-plan (a plan for approval and
     work for the production manager and plant heads), and each such order gets a task that says what it needs (parts to buy with
     lead times, the earliest realistic date, the orders it pushes back, crew shortfalls). Orders that fit get no task.
Every upload and what the agents did is kept (agent_runs, trigger "upload"). Nothing is approved, released or changed by an agent.
"""
from concurrent.futures import ThreadPoolExecutor
import csv
import datetime as dt
import io
import json
from collections import defaultdict
from zoneinfo import ZoneInfo
from . import coord, incidents, jobs, orchestrator, order_watcher as watcher, store
from .config import Policy
from .db import AgentRun, AgentTask, IntakeOrder, OrderFreeze, SessionLocal, TeamMember, UploadLog

from .intake_common import log, NAME, ALIASES, FIELDS, Invalid, today  # noqa: F401  (re-exported: other modules use intake_flow.Invalid etc.)
from .intake_assess import _assess, _recommend, _facts


def parse_csv(text: str) -> list[dict]:
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        raise Invalid("The file has no orders (a header row and at least one order are needed)")
    out = []
    for r in rows:
        o = {}
        for k, v in r.items():
            if k is None or v is None or not str(v).strip():
                continue
            key = ALIASES.get(k.strip().lower(), k.strip().lower())
            if key in FIELDS:
                o[key] = str(v).strip()
        out.append(o)
    return out


def receive(orders: list[dict], source: str, user: str) -> tuple[list[str], list[str]]:
    """Validate and store orders; returns (ids, ids that replaced an earlier upload). Raises Invalid (nothing stored) if any order has a
    problem. An order number received before is updated with the new figures, unless it is frozen: part of an approved plan."""
    d = store.D()
    co = d["fact_customer_order"]
    products, channels = set(d["dim_product_routing"].product_id), set(co.channel)
    price = co.groupby("product_id").unit_price_inr.median().to_dict()
    t0 = today()
    with SessionLocal() as s:
        prior = {r.customer_order_id: r for r in s.query(IntakeOrder)}
        book = set(co.customer_order_id) - set(prior)               # the order book's own orders (not received here)
        frozen = {f.customer_order_id: f for f in s.query(OrderFreeze)}
        tz = ZoneInfo(Policy().time_zone)
        errors, rows, seen, n = [], [], set(), len(prior)
        for i, o in enumerate(orders):
            oid = str(o.get("customer_order_id") or f"CO-IN{n + i + 1:05d}").strip().upper()[:40]
            p = []
            try:
                qty = int(float(o.get("order_qty", 0)))
            except ValueError:
                qty = 0
            try:
                due = dt.date.fromisoformat(str(o.get("requested_delivery_date", ""))[:10])
            except ValueError:
                due = None
            conf = o.get("confirmed_delivery_date")
            prio = str(o.get("priority", "MEDIUM")).upper()
            chan = str(o.get("channel", "B2B")).upper()
            for msg, bad in [(f"unknown product '{o.get('product_id')}'", o.get("product_id") not in products),
                             ("quantity must be a whole number above 0", qty <= 0 or qty > 1_000_000),
                             ("delivery date must be a date (YYYY-MM-DD)", due is None), ("delivery date is before today", due is not None and due < t0),
                             (f"priority must be HIGH, MEDIUM or LOW, not '{prio}'", prio not in ("HIGH", "MEDIUM", "LOW")),
                             (f"unknown channel '{chan}'", chan not in channels),
                             (f"order id '{oid}' is already in the order book", oid in book), (f"order id '{oid}' appears twice in this file", oid in seen),
                             (f"order id '{oid}' is frozen: it is in the plan approved by {frozen[oid].frozen_by} on "
                              f"{frozen[oid].frozen_at.replace(tzinfo=dt.timezone.utc).astimezone(tz):%d %b at %H:%M}, so a new upload can't change it"
                              if oid in frozen else "", oid in frozen)]:
                if bad:
                    p.append(msg)
            if p:
                errors.append({"row": i + 1, "customer_order_id": oid, "problems": p})
                continue
            seen.add(oid)
            rows.append(IntakeOrder(customer_order_id=oid, customer_id=str(o.get("customer_id") or "CUS-NEW").strip().upper()[:20], product_id=o["product_id"],
                                    order_date=t0.isoformat(), requested_delivery_date=due.isoformat(), confirmed_delivery_date=str(conf or due.isoformat())[:10],
                                    order_qty=qty, unit_price_inr=float(o.get("unit_price_inr") or price.get(o["product_id"], 0)), priority=prio, channel=chan,
                                    source=source[:20], received_by=user))
        if errors:
            fz = [e for e in errors if any("is frozen" in m for m in e["problems"])]
            if fz:
                raise Invalid(f"{len(fz)} order{'s are' if len(fz) != 1 else ' is'} frozen: already in a plan the production manager approved, so "
                              "a new upload can't change them. Nothing was received. Remove them from the file, or use new order numbers.", errors)
            raise Invalid(f"{len(errors)} of {len(orders)} orders have problems; nothing was received", errors)
        updated = [r.customer_order_id for r in rows if r.customer_order_id in prior]
        for i in updated:                                         # received before and not frozen: the new figures replace the old ones
            s.delete(prior[i])
        s.flush()
        s.add_all(rows)
        s.commit()
        ids = [r.customer_order_id for r in rows]
    coord.changed()                                               # every process drops what it had cached about the order book
    return ids, updated


def freeze(schedule_id: str, user: str) -> int:
    """On approval: the received orders this plan was made with are frozen (a new upload can no longer change them)."""
    try:
        ids = {incidents._cid(o.order_id) for o in store.inputs(schedule_id).orders}
    except Exception:                                             # plan inputs unavailable: nothing to freeze, never block the approval
        log.exception("freeze: plan inputs unavailable")
        return 0
    with SessionLocal() as s:
        got = {x for (x,) in s.query(IntakeOrder.customer_order_id).filter(IntakeOrder.customer_order_id.in_(ids))} if ids else set()
        have = {x for (x,) in s.query(OrderFreeze.customer_order_id)}
        new = sorted(got - have)
        s.add_all([OrderFreeze(customer_order_id=i, schedule_id=schedule_id, frozen_by=user) for i in new])
        s.commit()
    return len(new)


def log_upload(user: str, role: str, filename: str, status: str, orders: list[dict] | None = None, new=(), updated=(), problems=None,
               message: str | None = None, job_id: str | None = None) -> None:
    """One row in upload_log for every upload, received or refused."""
    units = 0
    for o in orders or []:
        try:
            units += int(float(o.get("order_qty", 0) or 0))
        except (TypeError, ValueError):
            pass
    try:
        with SessionLocal() as s:
            s.add(UploadLog(username=user, role=role, filename=(filename or "")[:200], status=status, orders=len(orders or []), units=units,
                            new_orders=list(new), updated_orders=list(updated), problems=problems, message=(message or "")[:2000] or None, job_id=job_id))
            s.commit()
    except Exception:                                             # the log never stops an upload
        log.exception("upload log not written")


def samples() -> str:
    """Four test POs from today's figures: two the plant absorbs (small, later, on products whose open production orders have spare
    units) and two that need action (thousands of units, high priority, due within a week, on products without spare units)."""
    d = store.D()
    inp = incidents._base()[1]
    dem = defaultdict(int)
    for o in inp.orders:
        dem[o.product_id] += o.qty
    spare = sorted(((inp.open_po_supply.get(p, 0) - q, p) for p, q in dem.items()), reverse=True)
    roomy, tight = spare[0][1], spare[-1][1]
    second = spare[1][1] if len(spare) > 1 else roomy
    t0 = today()
    stamp = dt.datetime.now().strftime("%m%d%H%M%S")
    price = d["fact_customer_order"].groupby("product_id").unit_price_inr.median().round().to_dict()
    rows = [(f"PO-{stamp}-1", "CUS-002", roomy, 400, t0 + dt.timedelta(days=35), "LOW", "B2B"),
            (f"PO-{stamp}-2", "CUS-005", second, 600, t0 + dt.timedelta(days=28), "MEDIUM", "DISTRIBUTOR"),
            (f"PO-{stamp}-3", "CUS-001", tight, 9000, t0 + dt.timedelta(days=5), "HIGH", "E_COMMERCE"),
            (f"PO-{stamp}-4", "CUS-003", second, 7500, t0 + dt.timedelta(days=6), "HIGH", "B2B")]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["po_number", "customer_id", "product_id", "qty", "due_date", "priority", "channel", "unit_price_inr"])
    for r in rows:
        w.writerow([r[0], r[1], r[2], r[3], r[4].isoformat(), r[5], r[6], int(price.get(r[2], 0))])
    return buf.getvalue()


# ------------------------------------------------------------------ 3. assess
STEPS = ["read", "lines", "stock", "plants", "plan", "impact", "decide"]


def run(ids: list[str], actor: str, filename: str, say=None, think=None, four_plans: bool = False) -> dict:
    """The flow for received orders. `say(step, state, detail, facts)` reports progress and `think(step, text)` the agent's reasoning
    as it goes (the review page shows both as they happen). Every thought is built from what the checks actually found.
    four_plans: a person uploaded a sheet, so the agent always ends with the five plans (an automated feed waits for enough demand)."""
    say = say or (lambda *a, **k: None)
    think = think or (lambda *a, **k: None)
    day = lambda v: dt.date.fromisoformat(str(v)[:10]).strftime("%d %b")
    steps = [{"step": "receive", "title": f"Received {len(ids)} purchase order{'s' if len(ids) != 1 else ''}", "detail": filename}]
    # 2. every agent looks at the plant now, all three at once; what they find is raised and routed in the background
    new_findings, agents, found = 0, [], []
    looks = (("floor", "lines", "Reading the production lines: output, stops, breakdowns and planned maintenance"),
             ("inventory", "stock", "Reading the stock in every warehouse against what open orders need"),
             ("sourcing", "plants", "Comparing both plants: work waiting for parts and what the other plant could take"))
    pool = ThreadPoolExecutor(max_workers=3)
    futs = {key: pool.submit(orchestrator.look, key, "upload", actor) for key, _, _ in looks}
    for key, step, what in looks:
        say(step, "running", what)
        think(step, f"Asking the {dict((k, n) for k, n, _ in orchestrator.AGENTS)[key].lower()} what it sees right now")
        try:
            r = futs[key].result()
        except Exception:                                             # one agent failing must not stop the others
            log.exception("%s look failed", key)
            r = {"summary": None, "signals": []}
        found += r["signals"]
        new_findings += len(r["signals"])
        seen = r["signals"]
        for x in seen[:5]:
            think(step, ("Urgent: " if x["severity"] == "critical" else "Noticed: ") + str(x["title"]))
        if len(seen) > 5:
            think(step, f"…and {len(seen) - 5} more of the same kind")
        if not seen:
            think(step, "Nothing out of the ordinary here")
        think(step, f"Handing {len(seen)} finding{'s' if len(seen) != 1 else ''} to the orchestrator; anything new goes to the people who own it"
              if seen else "No one needs to be told anything")
        agents.append({"agent": key, "name": dict((k, n) for k, n, _ in orchestrator.AGENTS)[key], "summary": r.get("summary"), "signals": len(seen)})
        say(step, "done", what, _facts(r.get("summary")))
    routing = pool.submit(orchestrator.handle, found, "upload", actor)          # raise and route the findings while planning goes on
    pool.shutdown(wait=False)
    steps.append({"step": "look", "title": "The orchestrator asked the floor, inventory and sourcing agents what they see",
                  "agents": agents, "new_findings": new_findings})
    # 3. each new order on the planning engine
    say("plan", "running", "Placing each new order in the production plan, with every open order and production order")
    think("plan", f"Running {len(ids)} new order{'s' if len(ids) != 1 else ''} through the planning engine with every open order")
    items, ctx = _assess(ids)
    act = [i for i in items if i["needs_action"]]
    for i in items[:8]:
        think("plan", f"{i['order_id']}: {i['qty']:,} × {i['product_id']}, due {day(i['due'])}: {i['result_text']}"
              + (f" (because {', '.join(i['why'])})" if i.get("why") else ""))
    if len(items) > 8:
        think("plan", f"…and {len(items) - 8} more orders")
    counts = defaultdict(int)
    for i in items:
        counts[i["result"]] += 1
    say("plan", "done", "Placed each new order in the production plan",
        [f"{n} {({'covered': 'covered by open production orders', 'on_time': 'planned on time', 'late': 'planned late', 'blocked': 'waiting for parts or line time'})[k]}"
         for k, n in counts.items()])
    say("impact", "running", "Checking what the new orders do to orders already promised")
    hit = [i for i in act if i.get("pushes_back", {}).get("left_short_orders") or i.get("pushes_back", {}).get("orders")]
    think("impact", "Looking for promised orders that would lose line time or parts to the new ones")
    for i in hit[:6]:
        pb = i["pushes_back"]
        think("impact", f"{i['order_id']} would leave {pb.get('left_short_orders', 0)} promised orders {pb.get('left_short_units', 0):,} units short"
              + (f" and move {pb['orders']} orders back" if pb.get("orders") else ""))
    for i in [i for i in act if i.get("earliest")][:6]:
        think("impact", f"{i['order_id']} can't make {day(i['due'])}; the earliest realistic date is {day(i['earliest'])}")
    if not hit and not any(i.get("earliest") for i in act):
        think("impact", "No promised order is affected")
    say("impact", "done", "Checked the effect on orders already promised",
        [f"{i['order_id']}: {i['pushes_back']['left_short_orders']} existing orders would lose {i['pushes_back']['left_short_units']:,} units" for i in hit
         if i["pushes_back"].get("left_short_orders")] + [f"{i['order_id']}: earliest realistic date {dt.date.fromisoformat(i['earliest']):%d %b}"
                                                          for i in act if i.get("earliest")] or ["No existing order is affected"])
    steps.append({"step": "assess", "title": f"Checked each order on the planning engine: {len(act)} need action, {len(items) - len(act)} fit the plan"})
    # 4. act only where needed
    say("decide", "running", "Deciding what needs to happen")
    plan, tasks = None, []
    heavy = [i for i in act if i.get("heavy")]
    think("decide", f"{len(act)} of {len(items)} order{'s' if len(items) != 1 else ''} need action" if act else "Every new order fits the current plan")
    think("decide", f"Large or urgent: {', '.join(i['order_id'] for i in heavy)}, so re-plan now with everything the agents found" if heavy
          else "Nothing large or urgent, so the order watcher decides whether enough new demand has built up to re-plan")
    try:
        routing.result()                                              # the findings are routed before any re-plan (newest event = the re-plan)
    except Exception:
        log.exception("routing the agents' findings failed")
    if heavy:                                                     # large or urgent: re-plan now, with the watcher's reason when its thresholds are met too
        pol = Policy()
        p = watcher.pending(pol=pol)
        go, why = watcher.reason(p, pol) if p["baseline"] else (False, "")
        if not go:
            why = "New orders need action: " + "; ".join(f"{i['order_id']}: {', '.join(i['why'])}" for i in act)
        sig = {"type": "orders.replan", "agent": watcher.NAME, "severity": "critical" if any(i["priority"] == "HIGH" for i in act) else "warn",
               "title": f"New orders need action: {', '.join(i['order_id'] for i in act)}"[:400],
               "detail": {"why": why, "trigger": "intake", **{k: v for k, v in p.items() if not k.startswith("_") and k != "new_ids"}}}
        plan = orchestrator.submit(sig, actor).get("plan")
        tasks = _order_tasks(act, plan)
        steps.append({"step": "act", "title": f"The planning agent re-planned with everything the agents found; {len(tasks)} tasks handed to people",
                      "plan": plan and {"schedule_id": plan.get("schedule_id"), "tasks": len(plan.get("tasks", []))}})
        decision = plan
    else:                                                         # nothing large or urgent; the order watcher still counts the arrivals
        decision = orchestrator.run("upload", actor, ["orders"]).get("orders")
        if decision and decision.get("outcome") == "PLANNED":
            plan = decision
        elif four_plans:                                          # a person's upload always ends with the five plans to choose from
            from . import planning_agent
            decision = plan = planning_agent.plan("upload", f"{len(items)} new orders uploaded from {filename}", actor, orchestrator.context())
        tasks = _order_tasks(act, plan) if act else []
        steps.append({"step": "act", "title": (("The planning agent made five plans from the latest data for the production manager to choose from. " if plan else "")
                                               + (f"{len(act)} order{'s' if len(act) != 1 else ''} the plan cannot meet: {len(tasks)} tasks handed to people" if act
                                                  else "" if plan else "No action: the current plan absorbs these orders, so nobody was asked to do anything")).strip()})
    rc = _recommend(ids, items, ctx, plan)
    short = [m for m in rc["materials"] if not m["enough"]]
    think("decide", f"Materials: the new orders use {len(rc['materials'])} materials; " + (f"{len(short)} are short and must be bought "
          f"(last arrives in {max(m['lead_days'] for m in short)} days)" if short else "all are in stock"))
    for k in rc["machines"]["kinds"]:
        think("decide", f"{k['label']}: the new orders need {k['need_hours']:g} h before {dt.date.fromisoformat(k['by']):%d %b}; "
              f"{k['free_hours']:g} h are free" + ("" if k["enough"] else f", {k['short_hours']:g} h short"))
    for c in (rc.get("plants") or {}).get("choices", []):
        think("decide", f"{c['title']}: materials ready in {c['materials_days']} days ({c['from_stock']} from stock, {c['to_transfer']} by truck, "
              f"{c['to_order']} to order), then {c['make_days']} days to make; ready {dt.date.fromisoformat(c['ready']):%d %b}"
              + (f", {c['late_days']} days after the due date" if c.get("late_days") else ", on time") if c.get("ready") else f"{c['title']}: no free line time")
    if rc["options"]:
        b = next(o for o in rc["options"] if o.get("best"))
        think("decide", f"Compared {len(rc['options'])} ways to run the plan; best value: \u201c{b['title']}\u201d")
    think("decide", "Five plans go to the production manager to choose from; the chosen plan's orders freeze once approved" if plan
          else "The current plan stays as it is")
    if plan and plan.get("plan_set_id"):                         # the new orders checked before their dates are confirmed
        from . import plan_sets
        ps = plan_sets.get(plan["plan_set_id"]) or {}
        pr = next((p.get("promise") for p in ps.get("plans", []) if p.get("best")), None)
        if pr and pr.get("new"):
            think("decide", f"Delivery dates: {pr['on_time']} of {pr['new']} new orders can be delivered on the date asked"
                  + (f"; {pr['late_count']} need a later date agreed with the customer before accepting (earliest dates are on the Plan page)"
                     if pr.get("late_count") else ""))
    if tasks:
        think("decide", f"{len(tasks)} people told what to do for the orders that need action")
    say("decide", "done", "Decided", (["Five plans sent to the production manager to choose from"] if plan else ["The current plan stays as it is"])
        + ([f"{len(act)} order{'s' if len(act) != 1 else ''} need{'s' if len(act) == 1 else ''} action: {len(tasks)} people told what to do"] if act
           else ["Nobody needs to do anything"]))
    trace = {"file": filename, "orders": items, "steps": steps, "needs_action": len(act), "tasks": tasks,
             "recommendation": rc,
             "schedule_id": plan.get("schedule_id") if plan else None, "decision": decision}
    with SessionLocal() as s:
        run_ = AgentRun(trigger="upload", outcome="ACTED" if act else "ABSORBED", reason=steps[-1]["title"][:500], new_orders=len(ids),
                        new_units=sum(i["qty"] for i in items), open_orders=[], schedule_id=trace["schedule_id"],
                        details=json.loads(json.dumps(trace, default=str)), actor=actor)
        s.add(run_)
        s.commit()
        trace["id"], trace["at"] = run_.id, run_.started_at
    return json.loads(json.dumps(trace, default=str))


# ------------------------------------------------------------------ uploads run as durable jobs the page follows step by step (jobs.py)
@jobs.runner("upload")
def _run_upload(payload: dict, say, think) -> dict:
    res = run(payload["ids"], payload["actor"], payload["filename"], say, think, four_plans=True)
    try:                                                          # keep the finished run with its agent run (the review page shows it)
        if res and res.get("id"):
            with SessionLocal() as s:
                r = s.get(AgentRun, res["id"])
                if r:
                    r.details = {**(r.details or {}), "job_file": payload["filename"]}
                    s.commit()
    except Exception:
        log.exception("upload job not kept")
    return res


def start(ids: list[str], actor: str, filename: str, read_facts: list[str]) -> str:
    state = {"status": "running", "file": filename, "by": actor, "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
             "steps": {k: {"state": "waiting", "detail": None, "facts": [], "thoughts": []} for k in STEPS}, "result": None, "error": None}
    state["steps"]["read"] = {"state": "done", "detail": f"Read {filename} and checked every order", "facts": read_facts,
                              "thoughts": [f"Opened {filename}", "Checked each row's product, quantity, due date, priority and sales channel", *read_facts[:2]]}
    return jobs.queue("upload", {"ids": ids, "actor": actor, "filename": filename}, state)


def job(jid: str) -> dict | None:
    return jobs.get(jid)


def ran() -> dict:
    """Has the planning agent finished an upload run since this server started? Until it has, the app shows no plan and no warnings."""
    at = jobs.done_since("upload", coord.BOOT)
    return {"ran": at is not None, "at": at.isoformat() if at else None}


def latest_job() -> dict | None:
    """The most recent upload run (kept in the database)."""
    return jobs.latest("upload")


def _order_tasks(items: list[dict], plan: dict | None) -> list[str]:
    """One task per order that needs action: the production manager decides; the production head of the plant it is planned in prepares."""
    out = []
    with SessionLocal() as s:
        team = s.query(TeamMember).all()
        run_ = AgentRun(trigger="upload", outcome="ALERT", reason="New orders need action", open_orders=[], actor=NAME,
                        details={"orders": [i["order_id"] for i in items]})
        s.add(run_)
        s.flush()
        for i in items:
            what = []
            if i.get("buy"):
                what.append("order " + ", ".join(f"{b['material_id']} ×{b['qty']:,} ({b['lead_days']} d)" for b in i["buy"][:4]))
            if i.get("earliest"):
                what.append(f"earliest {dt.date.fromisoformat(i['earliest']):%d %b}")
            pb = i.get("pushes_back", {})
            if pb.get("left_short_orders"):
                what.append(f"{pb['left_short_orders']} existing orders left {pb['left_short_units']:,} units short")
            if pb.get("orders"):
                what.append(f"{pb['orders']} orders move back")
            if i.get("overtime_h"):
                what.append(f"{i['overtime_h']} overtime hours")
            title = f"New order {i['order_id']} ({i['qty']:,} × {i['product_id']}, due {dt.date.fromisoformat(i['due']):%d %b}): " + \
                    ("; ".join(what) if what else i["result_text"])
            title = title if len(title) <= 200 else title[:197].rsplit(" ", 1)[0] + "…"
            targets = [("production_manager", None)] + [("plant_head", p) for p in i.get("plants", [])]
            for role, p in targets:
                name = next((t.name for t in team if t.role == role and (t.plant_id or None) == p), None)
                t = AgentTask(run_id=run_.id, role=role, plant_id=p, assignee=name, kind="alert", status="OPEN", title=title,
                              schedule_id=(plan or {}).get("schedule_id"),
                              details=json.loads(json.dumps({"agent": "orchestrator", "severity": "critical" if i["priority"] == "HIGH" else "warn",
                                                             "type": "orders.new", "order": i}, default=str)))
                s.add(t)
                s.flush()
                out.append(t.id)
        s.commit()
    return out


def uploads(limit: int = 20) -> list[dict]:
    with SessionLocal() as s:
        q = s.query(AgentRun).filter(AgentRun.trigger == "upload", AgentRun.outcome.in_(["ACTED", "ABSORBED"])).order_by(AgentRun.started_at.desc()).limit(limit)
        return [{**(r.details or {}), "id": r.id, "at": r.started_at, "outcome": r.outcome} for r in q]

from datetime import date, datetime, timedelta
from app import data, planning, scenarios
from app.config import Policy

D = data.load()
NO_PO = Policy(net_open_po_supply=False, plan_open_po_work=False)   # mechanics tests exercise the scheduler, not production orders
INP = data.to_inputs(D, pol=NO_PO)
P = planning.plan(INP, NO_PO)


def test_no_line_overbooked_and_fal_after_smt():
    assert all(u <= 1.0 + 1e-9 for u in P.line_utilisation.values())
    assert all(a.fal_start >= a.smt_finish for a in P.assignments)


def test_demand_fully_accounted():
    s = P.summary
    assert s["scheduled_qty"] + s["unscheduled_qty"] + s["po_covered_qty"] == s["demand_qty"]
    assert P.feasibility in {"INCOMPLETE", "COMPLETE_ON_TIME", "COMPLETE_WITH_LATE_RISK", "NOTHING_SCHEDULABLE"}


waits = lambda a: any(r.startswith("WAITS_FOR_PURCHASE") for r in a.reason_codes)


def test_material_never_overconsumed():
    from_stock = P.model_copy(update={"assignments": [a for a in P.assignments if not waits(a)]})   # bought parts are not stock
    for r in planning.material_calloff(INP, from_stock):
        assert r["scheduled_consumption"] <= r["available_qty"] + 1   # rounding


def test_priority_gets_material_first():
    rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    bought = [rank[a.priority] for a in P.assignments if waits(a) and "SPLIT_FOR_PURCHASE" not in a.reason_codes]
    assert bought and max(rank[a.priority] for a in P.assignments if not waits(a)) <= min(bought)
    old = planning.plan(INP, NO_PO.model_copy(update={"plan_purchases": False}))      # without MRP purchases the rest stays unscheduled
    blocked = [rank[u.priority] for u in old.unscheduled if u.reason.startswith("MATERIAL")]
    assert blocked and max(rank[a.priority] for a in old.assignments) <= min(blocked)


def test_mrp_starts_what_stock_cannot_cover_after_the_parts_arrive():
    for a in P.assignments:
        for r in a.reason_codes:
            if r.startswith("WAITS_FOR_PURCHASE:"):
                m = r.split(":", 1)[1]
                assert a.smt_start >= INP.as_of + timedelta(days=INP.lead_days.get(m, NO_PO.purchase_default_lead_days)) - timedelta(seconds=1)
    assert not [u for u in P.unscheduled if u.reason.startswith("MATERIAL")]          # nothing left waiting without a date


def test_frozen_zone_is_locked_on_a_replan_with_the_same_inputs():
    again = planning.plan(INP, NO_PO, previous=P)
    was = {(a.customer_order_id, a.qty, a.smt_start): a for a in P.assignments}
    frozen = [a for a in again.assignments if a.frozen]
    assert frozen and all(a.smt_start < INP.as_of + timedelta(days=NO_PO.frozen_days) for a in frozen)
    assert all((a.customer_order_id, a.qty, a.smt_start) in was for a in frozen)       # same start, same setup: nothing moved
    assert not any(d.code == "FROZEN_JOB_MOVED" for d in again.diagnostics)
    assert not [c for c in again.changes if c.kind == "PLANT_CHANGED"]                 # stability: orders stay in their plant


def test_bottleneck_levelling_balances_final_assembly():
    flat = planning.plan(INP, NO_PO.model_copy(update={"level_bottleneck": False}))
    gap = lambda s: abs(s.line_utilisation["FAL-L1"] - s.line_utilisation["FAL-L2"])
    assert gap(P) <= gap(flat) + 1e-9


def test_forecast_is_planned_last_net_of_orders_and_never_counted_as_demand():
    x = INP.model_copy(deep=True)
    x.orders = x.orders[:5]                                                            # a thin order book: room for stock
    s = planning.plan(x, NO_PO)
    fc = [a for a in s.assignments if a.customer_order_id.startswith("FC-")]
    assert fc and all(a.priority == "LOW" and "FORECAST_REPLENISHMENT" in a.reason_codes and a.status == "ON_TIME" for a in fc)
    assert s.summary["scheduled_qty"] == sum(a.qty for a in s.assignments if not a.customer_order_id.startswith("FC-"))
    assert s.summary["forecast_qty"] == sum(a.qty for a in fc)
    assert not any(any(r.startswith("WAITS_FOR_PURCHASE") for r in a.reason_codes) for a in fc)   # forecast never buys parts


def test_dispatch_sequence_contiguous():
    rows = planning.dispatch_list(P)
    for line in {r["line_id"] for r in rows}:
        seqs = [r["seq"] for r in rows if r["line_id"] == line and r["stage"] == ("SMT" if line.startswith("SMT") else "FAL")]
        assert seqs == list(range(1, len(seqs) + 1))


def test_missing_routing_is_reported_not_invented():
    inp = INP.model_copy(deep=True)
    inp.routing = [r for r in inp.routing if r.product_id != "SPH-PRO-256"]
    s = planning.plan(inp, NO_PO)
    assert any(u.reason == "MISSING_ROUTING" for u in s.unscheduled)
    assert not any(a.product_id == "SPH-PRO-256" for a in s.assignments)


def test_material_shortage_scenario_blocks_everything():
    s = scenarios.run(INP, P, scenarios.Scenario(name="no material", material_factor={"*": 0}), NO_PO)
    # frozen jobs from the baseline keep running; everything else waits for parts to be bought (MRP), nothing comes from stock
    assert all(a.frozen or waits(a) for a in s.assignments)
    off = scenarios.run(INP, P, scenarios.Scenario(name="no material", material_factor={"*": 0}), NO_PO.model_copy(update={"plan_purchases": False}))
    assert all(a.frozen for a in off.assignments) and any(u.reason == "MATERIAL_BLOCKED" for u in off.unscheduled)


def test_machine_loss_moves_frozen_and_lowers_stability():
    # an SMT machine: stability compares SMT starts. Only that line loses time (each line has its own shifts)
    sc = scenarios.Scenario(name="loss", machine_losses=[scenarios.MachineLoss(machine_id="M-SMT-01", start=INP.as_of, end=datetime(2026, 11, 20))])
    x = scenarios.apply(INP, sc)
    assert all(s.minutes == 0 for l in x.lines if l.line_id == "SMT-L1" for s in l.slots if s.start < datetime(2026, 11, 20))
    assert all(s.minutes > 0 for l in x.lines if l.line_id != "SMT-L1" for s in l.slots[:3])
    s = scenarios.run(INP, P, sc, NO_PO)
    assert s.summary["stability_pct"] < 100
    assert any(d.code == "FROZEN_JOB_MOVED" for d in s.diagnostics)
    assert s.summary["late_jobs"] >= P.summary["late_jobs"]


def test_urgent_goes_first_when_unfrozen_and_after_frozen_work_on_replan():
    sc = scenarios.Scenario(name="urgent+spike", urgent_orders=[scenarios.UrgentOrder(product_id="SPH-A15-128", qty=500, due=date(2026, 11, 5))],
                            demand_spikes=[scenarios.DemandSpike(multiplier=1.5)])
    fresh = planning.plan(scenarios.apply(INP, sc), NO_PO)                  # no baseline -> nothing frozen
    assert fresh.summary["demand_qty"] > P.summary["demand_qty"]
    assert min(fresh.assignments, key=lambda a: a.smt_start).customer_order_id == "URGENT-001"
    replan = scenarios.run(INP, P, sc, NO_PO)                               # frozen baseline jobs keep their place
    urgent = [a for a in replan.assignments if a.urgent]
    assert sum(a.frozen for a in replan.assignments) > 0
    # frozen work keeps its material first; the urgent order is then either scheduled (not frozen) or reported as blocked, never invented
    assert (urgent and not urgent[0].frozen) or any(u.customer_order_id == "URGENT-001" and u.reason.startswith("MATERIAL") for u in replan.unscheduled)


def test_product_grouping_cuts_changeovers_and_safety_stock_is_kept():
    from app.config import Policy
    off = planning.plan(INP, Policy(net_open_po_supply=False, batch_window_days=0))
    on = planning.plan(INP, Policy(net_open_po_supply=False, batch_window_days=7))
    assert on.summary["changeovers"] <= off.summary["changeovers"]
    keep = planning.plan(INP, Policy(net_open_po_supply=False, respect_safety_stock=True))
    spend = planning.plan(INP, Policy(net_open_po_supply=False, respect_safety_stock=False))
    assert keep.summary["scheduled_qty"] <= spend.summary["scheduled_qty"]
    assert any(d.code == "SAFETY_STOCK_RESERVED" for d in keep.diagnostics)


def test_default_policy_nets_open_po_supply():
    inp = data.to_inputs(D)
    s = planning.plan(inp)
    # open production orders count as supply for what they still owe (ordered - confirmed good), capped by each product's demand
    owe = data.open_po(D).groupby("product_id").remaining.sum()
    dem = {}
    for o in inp.orders:
        if o.status != "PRODUCTION_ORDER":                                 # customer demand only; production orders' own work is not netted
            dem[o.product_id] = dem.get(o.product_id, 0) + o.qty
    assert s.summary["po_covered_qty"] == sum(min(q, int(owe.get(p, 0))) for p, q in dem.items())
    assert any(d.code == "OPEN_PO_SUPPLY_APPLIED" for d in s.diagnostics)
    po_jobs = [a for a in s.assignments if a.customer_order_id.startswith("PO-")]
    assert po_jobs and all(a.plant_id == next(o.plant_id for o in inp.orders if o.order_id == a.customer_order_id) for a in po_jobs)   # in their own plant
    big = scenarios.run(inp, s, scenarios.Scenario(name="spike x3", demand_spikes=[scenarios.DemandSpike(multiplier=3)]))
    assert big.summary["po_covered_qty"] < big.summary["demand_qty"]      # spike exceeds PO supply -> new production planned/blocked


def test_job_times_are_coherent_and_never_overlap_on_a_line():
    from collections import defaultdict
    assert P.assignments
    for a in P.assignments:
        assert a.smt_finish >= a.smt_start and a.fal_finish >= a.fal_start and a.fal_start >= a.smt_finish
    for kind in ("smt", "fal"):
        by = defaultdict(list)
        for a in P.assignments:
            by[getattr(a, kind + "_line")].append((getattr(a, kind + "_start"), getattr(a, kind + "_finish")))
        for jobs in by.values():
            jobs.sort()
            assert all(jobs[i][1] <= jobs[i + 1][0] for i in range(len(jobs) - 1)), "overlapping jobs on one line"

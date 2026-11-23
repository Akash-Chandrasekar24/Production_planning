"""Digital twin of the plants (read-only, deterministic), replayed hour by hour from the records.

Recorded (source data): per line and hour the shift, product, production order, crew, target / actual / good / reject units and
downtime with a reason; the machines of the recorded lines; daily stock per material, lot and warehouse.
Generated (app/generate.py, source="generated", derived from the recorded feed): the bays beyond the two recorded lines (Hosur has 3
bays, Bengaluru 5; twin-only lines that
planning never uses), each machine's own hourly record (state, run / stop / wait minutes, units, rejects found, energy,
temperature), the maintenance history (planned services and repairs), and the forklifts' and AGVs' hourly records.
Without the generated tables the twin falls back to line-level data (each machine shows its line's state).
OEE per line = availability x performance x quality, with availability = 1 - downtime / 60 per hour, performance = actual units vs
target scaled to the time available, quality = good / actual.
"""
import bisect
import json
import pandas as pd
from .config import Settings

# Where each plant is, for the map. The data has no addresses: these are the plants' cities (IN-TN-01: Tamil Nadu -> Hosur,
# IN-KA-01: Karnataka -> Bengaluru) at approximate points. Set PLANT_SITES (JSON) to the real sites.
SITES = {"IN-TN-01": {"city": "Hosur", "region": "Tamil Nadu", "lat": 12.7409, "lon": 77.8253},
         "IN-KA-01": {"city": "Bengaluru", "region": "Karnataka", "lat": 12.8452, "lon": 77.6602}}

# order of equipment along each line type (material flows left to right)
FLOW = {"SPI": 1, "Pick and Place": 2, "Reflow Oven": 3, "AOI": 4, "ICT Tester": 5,
        "Assembly Cell": 1, "RF Tester": 2, "Camera Tester": 2, "Aging Chamber": 3, "Leak Tester": 4}
DOWN_MIN = 45            # a line that lost 45+ minutes of the hour (or made nothing) is shown as down
SLOW_PERF = 0.85         # running below 85% of its rate (or with any stop) is shown as slowed
MSTATE = {"RUNNING": "running", "SLOWED": "slowed", "DOWN": "down", "STOPPED": "waiting", "IDLE": "idle"}
WH = {"RM-WH-01": "Raw materials", "SMT-KIT-01": "SMT kits", "WIP-WH-01": "Work in progress", "FG-WH-01": "Finished goods"}
# How each plant is built: Hosur is a compact 3-bay hall with a conventional store (selective racks plus a bulk floor store),
# Bengaluru a taller 5-bay hall with a high-bay store. The 3D twin draws the building and racks from this.
LAYOUT = {"IN-TN-01": {"bays": 3, "store": "Conventional store", "racking": "Selective pallet racking, 3 levels, forklift aisles",
                       "bulk_store": "Floor-stacked bulk store for raw materials in the hall", "rack_levels": 3, "height_m": 6, "roof": "gable"},
          "IN-KA-01": {"bays": 5, "store": "High-bay store", "racking": "High-bay pallet racking, 6 levels, narrow aisles",
                       "bulk_store": None, "rack_levels": 6, "height_m": 9, "roof": "sawtooth"}}
DEFAULT_LAYOUT = {"bays": None, "store": "Store", "racking": "Pallet racking, 4 levels", "bulk_store": None, "rack_levels": 4, "height_m": 6, "roof": "flat"}


def _bay_order(ls: list[dict]) -> list[str]:
    """Bays in order: the recorded SMT then final-assembly line, then the generated lines by number (SMT before FAL)."""
    key = lambda l: (l["generated"], int(l["line_id"].rsplit("L", 1)[-1]) if l["generated"] else 0, l["kind"] != "SMT")
    return [l["line_id"] for l in sorted(ls, key=key)]


def _oee(df: pd.DataFrame) -> dict:
    """Availability, performance, quality and OEE over the given hours (None when there is nothing to measure)."""
    if df.empty or not df.target_qty.sum():
        return {"availability": None, "performance": None, "quality": None, "oee": None}
    hours = len(df)
    a = max(0.0, 1 - df.downtime_min.sum() / (60 * hours))
    p = min(1.0, df.actual_qty.sum() / (df.target_qty.sum() * a)) if a else 0.0
    q = df.good_qty.sum() / df.actual_qty.sum() if df.actual_qty.sum() else 0.0
    return {k: round(100 * v, 1) for k, v in {"availability": a, "performance": p, "quality": q, "oee": a * p * q}.items()}


def _state(r) -> str:
    if r is None:
        return "no_data"
    if r.downtime_min >= DOWN_MIN or r.actual_qty == 0:
        return "down"
    perf = r.actual_qty / (r.target_qty * (1 - r.downtime_min / 60)) if r.target_qty else 1
    return "slowed" if r.downtime_min > 0 or perf < SLOW_PERF else "running"


def hours_range(d: dict) -> tuple[str, str]:
    hp = d["fact_hourly_production"]
    return hp.event_ts.min(), hp.event_ts.max()


def _at(d: dict, at: str | None) -> pd.Timestamp:
    first, last = hours_range(d)
    return pd.Timestamp(last) if not at else min(max(pd.Timestamp(at).floor("h"), pd.Timestamp(first)), pd.Timestamp(last))


def _hourly(d: dict) -> pd.DataFrame:
    """Recorded lines plus the generated bays (twin only)."""
    hp = d["fact_hourly_production"].assign(source="recorded")
    tl = d.get("gen_twin_line_hourly")
    return hp if tl is None else pd.concat([hp, tl[hp.columns]], ignore_index=True)


def _machines(d: dict, plant_of: dict) -> pd.DataFrame:
    m = d["dim_machine"].assign(source="recorded")
    tm = d.get("gen_twin_machine")
    if tm is not None:
        m = pd.concat([m, tm[m.columns]], ignore_index=True)
    return m.assign(plant_note=lambda x: x.plant_id != x.line_id.map(plant_of), plant_id=lambda x: x.line_id.map(plant_of).fillna(x.plant_id))


def _planned_starts(mt: pd.DataFrame) -> dict:
    """Start times of each asset's planned services, sorted; worked out once per loaded dataset (it was a full scan per machine)."""
    from .data import _once
    return _once("planned_starts", mt, lambda: {a: sorted(g) for a, g in mt[mt.kind == "PLANNED"].groupby("asset_id").started_at})


def _service(d: dict, asset_id: str, interval: int, commissioned: str, t: pd.Timestamp) -> dict:
    """Last planned service and next due date: from the maintenance records when there are any, else from the interval."""
    mt = d.get("gen_maintenance_event")
    starts = _planned_starts(mt).get(asset_id, []) if mt is not None else []
    k = bisect.bisect_right(starts, t.strftime("%Y-%m-%dT%H:%M:%S"))          # planned services that started by `t`
    if k:
        last = pd.Timestamp(starts[k - 1]).normalize()
        nxt = last + pd.Timedelta(days=interval)
        basis = "maintenance records"
    else:
        age = (t.normalize() - pd.Timestamp(commissioned)).days
        nxt = t.normalize() + pd.Timedelta(days=(-age) % interval)
        last = nxt - pd.Timedelta(days=interval)
        basis = "planned interval from the commissioning date"
    return {"interval_days": interval, "last": str(last.date()), "next": str(nxt.date()), "days_to_next": int((nxt - t.normalize()).days), "basis": basis}


def snapshot(d: dict, at: str | None = None, trend_hours: int = 12) -> dict:
    hp = _hourly(d)
    first, last = hours_range(d)
    t_at = _at(d, at)
    at = t_at.strftime("%Y-%m-%dT%H:%M:%S")
    ts = pd.to_datetime(hp.event_ts)
    cur = hp[hp.event_ts == at].set_index("line_id")
    win = hp[(ts > t_at - pd.Timedelta(hours=trend_hours)) & (ts <= t_at)]
    today = hp[(hp.event_date == at[:10]) & (ts <= t_at)]
    plant_of = hp.drop_duplicates("line_id").set_index("line_id").plant_id.to_dict()
    gen_lines = set(hp[hp.source == "generated"].line_id)
    mach = _machines(d, plant_of)
    mh = d.get("gen_machine_hourly")
    mnow = mh[mh.event_ts == at].set_index("machine_id") if mh is not None else None

    lines = []
    for line in sorted(plant_of, key=lambda x: (x in gen_lines, not x.startswith("SMT"), x)):     # recorded first, SMT feeds final assembly
        r = cur.loc[line] if line in cur.index else None
        st = _state(r)
        ms = mach[mach.line_id == line].assign(o=lambda x: x.machine_type.map(FLOW).fillna(9)).sort_values(["o", "machine_id"])
        machines = []
        for m in ms.itertuples():
            mr = mnow.loc[m.machine_id] if mnow is not None and m.machine_id in mnow.index else None
            sv = _service(d, m.machine_id, int(m.planned_maintenance_interval_days), m.commissioned_date, t_at)
            machines.append({"machine_id": m.machine_id, "name": m.machine_name, "type": m.machine_type, "criticality": m.criticality,
                             "maintenance_due_days": sv["days_to_next"], "state": MSTATE.get(mr.state, st) if mr is not None else st,
                             "own_record": mr is not None, "plant_note": bool(m.plant_note), "generated": m.source == "generated",
                             "down_min": int(mr.down_min) if mr is not None else None, "wait_min": int(mr.wait_min) if mr is not None else None})
        tr = win[win.line_id == line].sort_values("event_ts")
        lines.append({
            "line_id": line, "plant_id": plant_of[line], "kind": "SMT" if line.startswith("SMT") else "FAL", "state": st, "generated": line in gen_lines,
            "hour": None if r is None else {"shift": r.shift_id, "product_id": r.product_id, "production_order_id": r.production_order_id,
                                            "team": r.operator_team, "target": int(r.target_qty), "actual": int(r.actual_qty), "good": int(r.good_qty),
                                            "reject": int(r.reject_qty), "downtime_min": int(r.downtime_min),
                                            "downtime_reason": None if r.downtime_reason == "NONE" else r.downtime_reason},
            "hour_oee": _oee(cur.loc[[line]]) if r is not None else _oee(cur.iloc[0:0]),
            "today": {**_oee(today[today.line_id == line]), "good": int(today[today.line_id == line].good_qty.sum()),
                      "target": int(today[today.line_id == line].target_qty.sum())},
            "trend": [{"ts": x.event_ts, "good": int(x.good_qty), "target": int(x.target_qty), "downtime_min": int(x.downtime_min)} for x in tr.itertuples()],
            "machines": machines,
        })

    last24 = hp[(ts > t_at - pd.Timedelta(hours=24)) & (ts <= t_at)]
    stops = last24[last24.downtime_min >= 15].sort_values("event_ts", ascending=False).head(16)
    plants = []
    for p in sorted(set(plant_of.values())):
        ls = [l for l in lines if l["plant_id"] == p]
        h = cur[cur.plant_id == p]
        plants.append({"plant_id": p, "lines": [l["line_id"] for l in ls], "layout": {**LAYOUT.get(p, DEFAULT_LAYOUT), "bay_lines": _bay_order(ls)},
                       "hour": {"good": int(h.good_qty.sum()), "target": int(h.target_qty.sum())}, "oee": _oee(h)["oee"]})
    floor = {"good": int(cur.good_qty.sum()), "target": int(cur.target_qty.sum()), "downtime_min": int(cur.downtime_min.sum()),
             "running": sum(l["state"] in ("running", "slowed") for l in lines), "lines": len(lines), "oee": _oee(cur.reset_index())["oee"],
             "today_good": int(today.good_qty.sum()), "generated_lines": len(gen_lines)}
    # warehouses: stock on the day's count (the latest count on or before that day); plant labels on stock rows change from day to day
    s, sday = _stock_day(d, at)
    g = s.groupby(["plant_id", "warehouse_id"]).agg(on_hand=("on_hand_qty", "sum"), usable=("usable", "sum"), lots=("lot_id", "count"),
                                                   materials=("material_id", "nunique")).reset_index()
    stock = {"day": sday, "warehouses": g.to_dict("records")}

    # material flow for the hour: inbound -> stores -> issued to the line -> assembled -> finished goods -> dispatched
    bom = d["fact_bom_component"]
    per_unit = (bom.component_qty * (1 + bom.scrap_factor)).groupby(bom.product_id).sum()      # parts consumed per unit built
    wh = lambda p, w: int(g[(g.plant_id == p) & (g.warehouse_id == w)].on_hand.sum())
    flow = []
    for p in sorted(set(plant_of.values())):
        h = cur[cur.plant_id == p]
        smt, fal = h[h.index.str.startswith("SMT")], h[h.index.str.startswith("FAL")]
        flow.append({"plant_id": p,
                     "inbound": int(s[s.plant_id == p].in_transit_qty.sum()),
                     "stores": wh(p, "RM-WH-01") + wh(p, "SMT-KIT-01"),
                     "issued": int(round(sum(r.actual_qty * per_unit.get(r.product_id, 0) for r in smt.itertuples()))),
                     "boards": int(smt.good_qty.sum()), "assembled": int(fal.good_qty.sum()), "fg": wh(p, "FG-WH-01")})
    dispatch = _dispatch(d, at[:10])
    return {"mode": "replay", "range": [first, last], "at": at, "floor": floor, "plants": plants, "lines": lines, "stock": stock,
            "flow": flow, "dispatch": dispatch, "records": {"machines": mh is not None, "generated_lines": sorted(gen_lines)},
            "events": [{"ts": e.event_ts, "line_id": e.line_id, "plant_id": e.plant_id, "minutes": int(e.downtime_min), "generated": e.source == "generated",
                        "reason": None if e.downtime_reason == "NONE" else e.downtime_reason, "shift": e.shift_id} for e in stops.itertuples()]}


def _stock_day(d: dict, at: str) -> tuple[pd.DataFrame, str]:
    inv = d["fact_inventory_daily_snapshot"]
    days = inv.snapshot_date[inv.snapshot_date <= at[:10]]
    sday = days.max() if len(days) else inv.snapshot_date.min()
    return inv[inv.snapshot_date == sday].assign(usable=lambda x: x.on_hand_qty - x.allocated_qty - x.blocked_qty - x.quality_hold_qty), sday


def _dispatch(d: dict, day: str) -> dict:
    # customer orders carry no plant, so dispatch is for the whole company: orders whose confirmed delivery date is this day
    co = d["fact_customer_order"]
    due = co[co.confirmed_delivery_date == day]
    return {"day": day, "orders": int(len(due)), "units": int(due.order_qty.sum()), "shipped": int(due.shipped_qty.sum()),
            "delivered": int((due.order_status == "DELIVERED").sum())}


def sites(d: dict, at: str | None = None) -> list[dict]:
    """Each plant on the map: where it is, and how it is running at that hour."""
    try:
        cfg = {**SITES, **json.loads(Settings().plant_sites)} if Settings().plant_sites else SITES
    except ValueError:
        cfg = SITES
    snap = snapshot(d, at, trend_hours=1)
    out = []
    for p in snap["plants"]:
        s = cfg.get(p["plant_id"], {})
        ls = [l for l in snap["lines"] if l["plant_id"] == p["plant_id"]]
        out.append({"plant_id": p["plant_id"], "city": s.get("city"), "region": s.get("region"), "lat": s.get("lat"), "lon": s.get("lon"),
                    "approximate": p["plant_id"] in SITES and s == SITES[p["plant_id"]], "oee": p["oee"], "hour": p["hour"],
                    "lines": len(ls), "store": p["layout"]["store"], "running": sum(l["state"] in ("running", "slowed") for l in ls), "down": sum(l["state"] == "down" for l in ls),
                    "states": {l["line_id"]: l["state"] for l in ls}, "generated_lines": [l["line_id"] for l in ls if l["generated"]]})
    return out


def _windows(frame: pd.DataFrame, t: pd.Timestamp, fn) -> list[dict]:
    """fn(rows, label) for today, the last 7 and 30 days and the year so far (up to the hour shown)."""
    day0 = t.normalize()
    return [fn(frame[frame.ts > s], label) for s, label in ((day0 - pd.Timedelta(seconds=1), "Today"), (t - pd.Timedelta(days=7), "Last 7 days"),
                                                         (t - pd.Timedelta(days=30), "Last 30 days"),
                                                         (pd.Timestamp(t.year, 1, 1) - pd.Timedelta(seconds=1), f"{t.year} so far"))]


def _history(d: dict, asset_id: str, t: pd.Timestamp) -> dict:
    mt = d.get("gen_maintenance_event")
    if mt is None:
        return {"events": [], "repairs_30d": 0, "mttr_min": None, "cost_ytd_inr": None}
    e = mt[(mt.asset_id == asset_id) & (mt.started_at <= t.strftime("%Y-%m-%dT%H:%M:%S"))]
    rep = e[e.kind == "REPAIR"]
    r30 = rep[rep.started_at > (t - pd.Timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")]
    ytd = e[e.started_at >= f"{t.year}-01-01"]
    return {"events": e.sort_values("started_at", ascending=False).head(8)[["maintenance_id", "kind", "started_at", "finished_at", "duration_min", "technician",
                                                                             "work_done", "parts_replaced", "cost_inr"]].to_dict("records"),
            "repairs_30d": int(len(r30)), "repairs_total": int(len(rep)), "mttr_min": round(rep.duration_min.mean()) if len(rep) else None,
            "cost_ytd_inr": int(ytd.cost_inr.sum())}


def machine(d: dict, machine_id: str, at: str | None = None) -> dict | None:
    """One machine from its own records: condition, what it is supplied with now, units per day / week / month / year, energy,
    service dates and repair history. Without generated machine records, falls back to its line's records."""
    hp = _hourly(d)
    plant_of = hp.drop_duplicates("line_id").set_index("line_id").plant_id.to_dict()
    mach = _machines(d, plant_of)
    row = mach[mach.machine_id == machine_id]
    if row.empty:
        return None
    m = row.iloc[0]
    t = _at(d, at)
    line, plant = m.line_id, m.plant_id
    lh = hp[hp.line_id == line].assign(ts=lambda x: pd.to_datetime(x.event_ts))
    lh = lh[lh.ts <= t]
    mh = d.get("gen_machine_hourly")
    if mh is not None:
        r = mh[mh.machine_id == machine_id].assign(ts=lambda x: pd.to_datetime(x.event_ts))
        r = r[r.ts <= t]
    else:                                                            # line-level fallback
        r = lh.rename(columns={"actual_qty": "units", "downtime_min": "down_min", "reject_qty": "rejects"}).assign(
            wait_min=0, run_min=lambda x: 60 - x.down_min, power_kwh=None, temp_c=None, state=None, down_reason=lambda x: x.downtime_reason)

    def window(w, label):
        days = max(1, w.event_date.nunique())
        mins = 60 * len(w) or 1
        return {"label": label, "from": str(w.event_date.min()) if len(w) else None, "units": int(w.units.sum()), "rejects": int(w.rejects.sum()),
                "days": int(w.event_date.nunique()), "per_day": round(w.units.sum() / days), "run_pct": round(100 * w.run_min.sum() / mins, 1),
                "down_min": int(w.down_min.sum()), "wait_min": int(w.wait_min.sum()),
                "energy_kwh": round(float(w.power_kwh.sum()), 1) if w.power_kwh.notna().any() else None}
    output = _windows(r, t, window)
    daily = r[r.ts > t - pd.Timedelta(days=30)].groupby("event_date").agg(units=("units", "sum")).reset_index()

    # condition: the last 7 days against this machine's own normal
    per_day = r.groupby("event_date").agg(down=("down_min", "sum"), rej=("rejects", "sum"), units=("units", "sum"))
    last7 = r[r.ts > t - pd.Timedelta(days=7)]
    down7, norm_down = int(last7.down_min.sum()), 7 * per_day.down.mean() if len(per_day) else 0
    rate = per_day.rej / per_day.units.where(per_day.units > 0)
    rej7 = last7.rejects.sum() / last7.units.sum() if last7.units.sum() else 0
    now = r[r.ts == t]
    nowr = now.iloc[0] if len(now) else None
    state = (MSTATE.get(nowr.state, "running") if nowr is not None and nowr.state else _state(lh[lh.ts == t].iloc[0]) if (lh.ts == t).any() else "no_data")
    sv = _service(d, machine_id, int(m.planned_maintenance_interval_days), m.commissioned_date, t)
    hist = _history(d, machine_id, t)
    reasons, score = [], 0
    if state == "down":
        score += 2; reasons.append("Stopped by its own fault this hour")
    if down7 > 1.5 * norm_down and down7 >= 60:
        score += 1; reasons.append(f"{down7} min of its own stops in 7 days, above its normal ({round(norm_down)} min)")
    if len(rate.dropna()) > 7 and rate.std() and rej7 > rate.mean() + 3 * rate.std():
        score += 1; reasons.append(f"Finds {100 * rej7:.1f}% rejects, above its normal {100 * rate.mean():.1f}%")
    if hist["repairs_30d"] >= 3:
        score += 1; reasons.append(f"{hist['repairs_30d']} repairs in the last 30 days")
    if sv["days_to_next"] <= 2:
        score += 1; reasons.append("Planned service " + ("overdue" if sv["days_to_next"] < 0 else "due today" if sv["days_to_next"] == 0 else f"due in {sv['days_to_next']} days"))
    temps = r[r.ts > t - pd.Timedelta(hours=24)].temp_c.dropna() if "temp_c" in r else pd.Series(dtype=float)
    if not reasons:
        reasons.append("Running within its normal range")
    run_h = r.run_min.sum() / 60
    condition = {"label": "Needs attention" if score >= 2 else "Watch" if score else "Good", "tone": "bad" if score >= 2 else "warn" if score else "ok",
                 "reasons": reasons, "state": state, "own_down_7d_min": down7, "normal_down_7d_min": round(norm_down),
                 "wait_7d_min": int(last7.wait_min.sum()), "rejects_7d_pct": round(100 * rej7, 2),
                 "normal_rejects_pct": round(100 * rate.mean(), 2) if len(rate.dropna()) else None,
                 "run_7d_pct": round(100 * last7.run_min.sum() / (60 * len(last7) or 1), 1),
                 "temp_now_c": float(nowr.temp_c) if nowr is not None and pd.notna(nowr.get("temp_c")) else None,
                 "temp_24h": [round(float(temps.min()), 1), round(float(temps.max()), 1)] if len(temps) else None,
                 "power_now_kw": float(nowr.power_kwh) if nowr is not None and pd.notna(nowr.get("power_kwh")) else None,
                 "mtbf_h": round(run_h / hist["repairs_total"]) if hist.get("repairs_total") else None, "mttr_min": hist["mttr_min"],
                 "now_down_min": int(nowr.down_min) if nowr is not None else None, "now_wait_min": int(nowr.wait_min) if nowr is not None else None,
                 "now_wait_reason": nowr.wait_reason if nowr is not None and "wait_reason" in nowr and pd.notna(nowr.wait_reason) else None}

    # what it is being supplied with now: the product on the line this hour, its parts, and (final assembly) the boards from SMT
    supply = None
    cur = lh[lh.ts == t]
    if len(cur):
        lr = cur.iloc[0]
        bom = d["fact_bom_component"]
        parts = bom[(bom.product_id == lr.product_id) & (bom.effective_from <= str(t.date())) & (bom.effective_to >= str(t.date()))]
        upstream = None
        if line.startswith("FAL"):
            smt_line = line.replace("FAL", "SMT")
            smt = hp[(hp.line_id == smt_line) & (hp.event_ts == t.strftime("%Y-%m-%dT%H:%M:%S"))]
            if len(smt):
                upstream = {"line_id": smt_line, "state": _state(smt.iloc[0]), "boards": int(smt.iloc[0].good_qty)}
        supply = {"product_id": lr.product_id, "production_order_id": lr.production_order_id, "team": lr.operator_team, "shift": lr.shift_id,
                  "rate_per_hour": int(lr.target_qty), "made_this_hour": int(lr.actual_qty), "upstream": upstream,
                  "parts": [{"material_id": b.material_id, "per_unit": float(b.component_qty), "scrap": float(b.scrap_factor),
                             "need_per_hour": round(lr.target_qty * b.component_qty * (1 + b.scrap_factor))} for b in parts.itertuples()]}
    return {"machine_id": m.machine_id, "name": m.machine_name, "type": m.machine_type, "line_id": line, "plant_id": plant, "criticality": m.criticality,
            "commissioned": str(m.commissioned_date), "age_days": int((t.normalize() - pd.Timestamp(m.commissioned_date)).days), "plant_note": bool(m.plant_note),
            "at": t.strftime("%Y-%m-%dT%H:%M:%S"), "own_records": mh is not None, "generated_line": m.source == "generated",
            "condition": condition, "supply": supply, "output": output,
            "daily": [{"day": x.event_date, "units": int(x.units)} for x in daily.itertuples()],
            "records_from": str(r.event_date.min()) if len(r) else None, "service": sv, "maintenance": hist}


def vehicle(d: dict, vehicle_id: str, at: str | None = None) -> dict | None:
    """A forklift or AGV from its generated records: now, condition, trips and loads per day / week / month / year, service."""
    veh, vh = d.get("gen_vehicle"), d.get("gen_vehicle_hourly")
    if veh is None or vh is None or vehicle_id not in set(veh.vehicle_id):
        return None
    v = veh[veh.vehicle_id == vehicle_id].iloc[0]
    t = _at(d, at)
    r = vh[vh.vehicle_id == vehicle_id].assign(ts=lambda x: pd.to_datetime(x.event_ts))
    r = r[r.ts <= t]

    def window(w, label):
        days = max(1, w.event_date.nunique())
        return {"label": label, "trips": int(w.trips.sum()), "loads": int(w.loads.sum()), "km": round(float(w.distance_km.sum()), 1),
                "per_day": round(w.loads.sum() / days), "run_h": round(w.run_min.sum() / 60, 1), "days": int(w.event_date.nunique())}
    now = r[r.ts == t]
    nr = now.iloc[0] if len(now) else None
    last30 = r[r.ts > t - pd.Timedelta(days=30)]
    sv = _service(d, vehicle_id, int(v.service_interval_days), v.commissioned_date, t)
    hist = _history(d, vehicle_id, t)
    faults = int(last30.fault_min.sum())
    reasons, score = [], 0
    if nr is not None and nr.state == "DOWN":
        score += 2; reasons.append("Stopped by a fault this hour")
    if hist["repairs_30d"] >= 2:
        score += 1; reasons.append(f"{hist['repairs_30d']} repairs in the last 30 days")
    if sv["days_to_next"] <= 3:
        score += 1; reasons.append("Planned service " + ("overdue" if sv["days_to_next"] < 0 else "due today" if sv["days_to_next"] == 0 else f"due in {sv['days_to_next']} days"))
    if nr is not None and nr.battery_pct < 35:
        reasons.append(f"Battery at {nr.battery_pct:g}%: charging soon")
    if not reasons:
        reasons.append("Working normally")
    return {"vehicle_id": vehicle_id, "name": v["name"], "type": v.vehicle_type, "role": v.role, "line_id": v.line_id if pd.notna(v.line_id) else None,
            "plant_id": v.plant_id, "model": v.model, "battery_kwh": float(v.battery_kwh), "commissioned": v.commissioned_date, "at": t.strftime("%Y-%m-%dT%H:%M:%S"),
            "now": None if nr is None else {"state": nr.state.lower(), "trips": int(nr.trips), "loads": int(nr.loads), "battery_pct": float(nr.battery_pct),
                                            "km": float(nr.distance_km), "run_min": int(nr.run_min), "charge_min": int(nr.charge_min)},
            "load_unit": nr.load_unit if nr is not None else ("units" if v.vehicle_type == "agv" else "pallets"),
            "condition": {"label": "Needs attention" if score >= 2 else "Watch" if score else "Good", "tone": "bad" if score >= 2 else "warn" if score else "ok",
                          "reasons": reasons, "faults_30d_min": faults, "run_30d_h": round(last30.run_min.sum() / 60, 1),
                          "charges_30d": int((last30.charge_min > 0).sum())},
            "output": _windows(r, t, window), "service": sv, "maintenance": hist}


def warehouse(d: dict, plant: str, wh: str, at: str | None = None) -> dict:
    """A warehouse on the day's stock count: every material and lot, what is usable, on hold or in transit, value, and the trend."""
    t = _at(d, at)
    s, sday = _stock_day(d, t.strftime("%Y-%m-%dT%H:%M:%S"))
    rows = s[(s.plant_id == plant) & (s.warehouse_id == wh)]
    mats = rows.groupby("material_id").agg(on_hand=("on_hand_qty", "sum"), usable=("usable", "sum"), allocated=("allocated_qty", "sum"),
                                           blocked=("blocked_qty", "sum"), quality_hold=("quality_hold_qty", "sum"), in_transit=("in_transit_qty", "sum"),
                                           safety=("safety_stock_qty", "sum"), lots=("lot_id", "nunique"), unit_cost=("unit_cost_inr", "first"),
                                           expiry=("expiry_date", "min")).reset_index()
    mats["value_inr"] = (mats.on_hand * mats.unit_cost).round()
    lots = rows.sort_values(["material_id", "lot_id"])[["material_id", "lot_id", "on_hand_qty", "allocated_qty", "blocked_qty", "quality_hold_qty",
                                                         "in_transit_qty", "inventory_status", "expiry_date"]]
    inv = d["fact_inventory_daily_snapshot"]
    hist = inv[(inv.plant_id == plant) & (inv.warehouse_id == wh) & (inv.snapshot_date <= sday)].groupby("snapshot_date").on_hand_qty.sum().tail(30)
    lay = LAYOUT.get(plant, DEFAULT_LAYOUT)
    storage = {"store": lay["store"], "racking": lay["racking"], "levels": lay["rack_levels"],
               "bulk_store": lay["bulk_store"] if wh == "RM-WH-01" else None}
    return {"plant_id": plant, "warehouse_id": wh, "name": WH.get(wh, wh), "day": sday, "storage": storage,
            "totals": {k: int(mats[k].sum()) for k in ("on_hand", "usable", "allocated", "blocked", "quality_hold", "in_transit", "safety")} | {
                "materials": int(len(mats)), "lots": int(rows.lot_id.nunique()), "value_inr": int(mats.value_inr.sum()),
                "on_hold_lots": int((rows.inventory_status != "AVAILABLE").sum())},
            "materials": mats.fillna({"expiry": ""}).to_dict("records"), "lots": lots.fillna({"expiry_date": ""}).head(60).to_dict("records"),
            "trend": [{"day": k, "on_hand": int(v)} for k, v in hist.items()]}


def area(d: dict, plant: str, kind: str, at: str | None = None) -> dict | None:
    """The receiving dock, shipping dock, packing, the gate or the office: what passes through them, from the records."""
    t = _at(d, at)
    at_s = t.strftime("%Y-%m-%dT%H:%M:%S")
    snap = snapshot(d, at_s, trend_hours=1)
    flow = next((f for f in snap["flow"] if f["plant_id"] == plant), {})
    s, sday = _stock_day(d, at_s)
    ps = s[s.plant_id == plant]
    vh = d.get("gen_vehicle_hourly")
    day = vh[(vh.plant_id == plant) & (vh.event_date == at_s[:10]) & (vh.event_ts <= at_s)] if vh is not None else None
    fk = lambda n: (int(day[day.vehicle_id == f"{plant}-FL-{n}"].loads.sum()) if day is not None else None)
    lines = [l for l in snap["lines"] if l["plant_id"] == plant]
    if kind == "receiving":
        tr = ps.groupby("material_id").in_transit_qty.sum().sort_values(ascending=False)
        return {"kind": kind, "title": "Receiving dock", "plant_id": plant, "day": sday, "in_transit": int(tr.sum()),
                "in_transit_by_material": [{"material_id": k, "qty": int(v)} for k, v in tr.head(12).items()],
                "pallets_received_today": fk(1), "stores_on_hand": flow.get("stores"),
                "on_hold_lots": int((ps[ps.warehouse_id.isin(["RM-WH-01", "SMT-KIT-01"])].inventory_status != "AVAILABLE").sum())}
    if kind in ("shipping", "gate"):
        out = {"kind": kind, "title": "Shipping dock" if kind == "shipping" else "Gate", "plant_id": plant, "day": sday, "dispatch": snap["dispatch"],
               "fg_on_hand": flow.get("fg"), "pallets_shipped_today": fk(2), "pallets_received_today": fk(1), "in_transit": flow.get("inbound")}
        return out
    if kind == "packing":
        fal = [l for l in lines if l["kind"] == "FAL"]
        return {"kind": kind, "title": "Packing", "plant_id": plant, "packed_this_hour": sum(l["hour"]["good"] for l in fal if l["hour"]),
                "packed_today": sum(l["today"]["good"] for l in fal), "fg_on_hand": flow.get("fg"),
                "lines": [{"line_id": l["line_id"], "good_hour": l["hour"]["good"] if l["hour"] else 0, "good_today": l["today"]["good"], "generated": l["generated"]} for l in fal]}
    if kind == "office":
        return {"kind": kind, "title": "Plant office", "plant_id": plant, "at": at_s,
                "lines": [{"line_id": l["line_id"], "state": l["state"], "team": l["hour"]["team"] if l["hour"] else None, "shift": l["hour"]["shift"] if l["hour"] else None,
                           "good_today": l["today"]["good"], "oee_today": l["today"]["oee"], "generated": l["generated"]} for l in lines],
                "good_today": sum(l["today"]["good"] for l in lines), "stock_value_inr": int((ps.on_hand_qty * ps.unit_cost_inr).sum())}
    return None


def crew(d: dict, line: str, at: str | None = None) -> dict | None:
    """The crew working a line this hour, and how that crew's hours on the line compare with the line's normal."""
    hp = _hourly(d)
    t = _at(d, at)
    lh = hp[hp.line_id == line]
    now = lh[lh.event_ts == t.strftime("%Y-%m-%dT%H:%M:%S")]
    if lh.empty or now.empty:
        return None
    team = now.iloc[0].operator_team
    th = lh[(lh.operator_team == team) & (lh.event_ts <= t.strftime("%Y-%m-%dT%H:%M:%S"))]
    rate = lambda x: round(100 * x.good_qty.sum() / x.target_qty.sum(), 1) if x.target_qty.sum() else None
    return {"line_id": line, "plant_id": now.iloc[0].plant_id, "team": team, "shift": now.iloc[0].shift_id, "hours_on_line": int(len(th)),
            "good_per_hour": round(th.good_qty.mean()) if len(th) else None, "team_pct_of_target": rate(th), "line_pct_of_target": rate(lh),
            "reject_pct": round(100 * th.reject_qty.sum() / th.actual_qty.sum(), 2) if th.actual_qty.sum() else None,
            "stops_min": int(th.downtime_min.sum()), "generated": bool((now.source == "generated").any())}

"""Generated records for the digital twin, derived from the real feed (deterministic: the same input gives the same output).

The source data records production per LINE and hour, and nothing about maintenance or vehicles. The twin needs records for every
machine and vehicle, so this module derives them and writes gen_*.csv next to the source files (and src_gen_* tables in the
database with `python -m app.generate --push`). Every row carries source="generated".

  gen_twin_machine       machines of the generated bays (Hosur IN-TN-01 has 3 bays: bay 3 = SMT-L3; Bengaluru IN-KA-01 has 5:
                         bays 3-5 = SMT-L4, FAL-L4, SMT-L5), modelled on the plant's recorded lines
  gen_twin_line_hourly   hourly records for those lines: the plant's recorded line one week earlier (same hour), with its own
                         production orders and crews. Planning never reads these.
  gen_machine_hourly     per machine and hour: the line's stop is put on the machine that caused it (feeder jams on the placer,
                         faults on a weighted pick, short stops anywhere); the other machines wait. Units, rejects (at the inspection
                         stations), energy and temperature.
  gen_maintenance_event  planned services on each machine's interval from commissioning, and repairs for each equipment fault of
                         30+ minutes; vehicles get planned services too.
  gen_vehicle            forklifts and AGVs of each plant; gen_vehicle_hourly their trips, loads, distance, battery and stops.
                         AGVs move what their line makes; forklifts move receipts and shipments worked out from the stock counts.
Lorries are not generated.
Planning data the source lacks (plausible values, labelled generated; replace with real data when it exists):
  gen_material_supplier  supplier, lead time, MOQ and pack size per material (lead times by material family)
  gen_crew / gen_crew_leave  people rostered per plant and shift, certifications, crew size per line; leave per day (incl. Diwali)
  gen_cost_rate          labour, overtime, line running cost, late penalty, carrying cost, plant-to-plant transfer cost
  gen_shipment           shipments of customer orders that shipped (ship dates around the confirmed date) for on-time-in-full
  gen_po_confirmation    actual start / finish / good units per production order (the feed's order tags are not usable for this)
"""
import argparse
import math
import zlib
import numpy as np
import pandas as pd
from .config import DATA_DIR

SEED = 20261101
GEN_TABLES = ["gen_twin_machine", "gen_twin_line_hourly", "gen_machine_hourly", "gen_maintenance_event", "gen_vehicle", "gen_vehicle_hourly",
              "gen_material_supplier", "gen_crew", "gen_crew_leave", "gen_cost_rate", "gen_shipment", "gen_po_confirmation"]
# Hosur (IN-TN-01) is a 3-bay plant, Bengaluru (IN-KA-01) a 5-bay plant. Bays 1-2 are the recorded lines; the rest are generated:
# (modelled on, new line, bay, weeks back) - each copies its model line's hours that many weeks earlier.
REAL = {"IN-TN-01": ("SMT-L1", "FAL-L1"), "IN-KA-01": ("SMT-L2", "FAL-L2")}
BAYS = {"IN-TN-01": [("SMT-L1", "SMT-L3", 3, 1)],
        "IN-KA-01": [("SMT-L2", "SMT-L4", 3, 1), ("FAL-L2", "FAL-L4", 4, 1), ("SMT-L2", "SMT-L5", 5, 2)]}
KW = {"SPI": 2.5, "Pick and Place": 6.0, "Reflow Oven": 28.0, "AOI": 2.0, "ICT Tester": 1.5, "Assembly Cell": 4.0, "RF Tester": 1.2,
      "Camera Tester": 1.0, "Aging Chamber": 9.0, "Leak Tester": 1.5}
TEMP = {"Reflow Oven": 245.0, "Aging Chamber": 45.0}                         # process temperature; others report cabinet temperature
FAULT_W = {"Pick and Place": 4, "Reflow Oven": 2, "SPI": 1, "AOI": 1, "ICT Tester": 1, "Assembly Cell": 4, "RF Tester": 1, "Camera Tester": 1,
           "Aging Chamber": 1, "Leak Tester": 1}
INSPECT = {"SPI": 0.3, "AOI": 0.5, "ICT Tester": 0.2, "RF Tester": 0.4, "Camera Tester": 0.4, "Leak Tester": 0.3, "Aging Chamber": 0.3}
SERVICE_MIN = {"SPI": 60, "Pick and Place": 120, "Reflow Oven": 90, "AOI": 60, "ICT Tester": 60, "Assembly Cell": 120, "RF Tester": 60,
               "Camera Tester": 60, "Aging Chamber": 90, "Leak Tester": 60}
WORK = {"Pick and Place": ("Replaced worn nozzle and recalibrated the head", "nozzle set"), "Reflow Oven": ("Replaced a heater element and re-profiled zone 4", "heater element"),
        "SPI": ("Cleaned optics and recalibrated the height sensor", "-"), "AOI": ("Replaced the lighting ring and re-taught the program", "LED ring"),
        "ICT Tester": ("Replaced worn test-fixture pins", "pogo pins"), "Assembly Cell": ("Replaced the screwdriver bit and torque sensor", "torque sensor"),
        "RF Tester": ("Re-seated the RF shield box cable and recalibrated", "RF cable"), "Camera Tester": ("Cleaned the target chart and recalibrated focus", "-"),
        "Aging Chamber": ("Replaced a failed fan in the chamber", "fan"), "Leak Tester": ("Replaced the seal gasket", "gasket")}
SERVICE_WORK = "Planned service: clean, lubricate, inspect wear parts, calibrate"


def _rng(*key) -> np.random.Generator:
    return np.random.default_rng([SEED, *[zlib.crc32(str(k).encode()) for k in key]])        # stable across runs (unlike hash())


def twin_machines(mach: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for plant, m in BAYS.items():
        for src, dst, bay, _ in m:
            for r in mach[mach.line_id == src].itertuples():
                code = r.machine_id.split("-")[1]
                rows.append({"machine_id": f"M-{code}-0{dst[-1]}", "machine_name": f"{r.machine_name} · bay {bay}",
                             "machine_type": r.machine_type, "line_id": dst, "plant_id": plant, "criticality": r.criticality,
                             "commissioned_date": "2025-03-01", "planned_maintenance_interval_days": r.planned_maintenance_interval_days, "source": "generated"})
    return pd.DataFrame(rows)


def twin_line_hourly(hp: pd.DataFrame) -> pd.DataFrame:
    out = []
    for plant, m in BAYS.items():
        for src, dst, bay, weeks in m:
            s = hp[hp.line_id == src].sort_values("event_ts").reset_index(drop=True)
            k = weeks * 7 * 24
            shifted = pd.concat([s.iloc[-k:], s.iloc[:-k]], ignore_index=True)                 # the same hour `weeks` earlier
            g = s[["event_ts", "event_date", "year", "month", "week", "day", "hour", "shift_id"]].copy()
            for c in ["product_id", "target_qty", "actual_qty", "good_qty", "reject_qty", "rework_qty", "downtime_min", "downtime_reason"]:
                g[c] = shifted[c].values
            run = (g.product_id != g.product_id.shift()).cumsum()
            g["production_order_id"] = [f"PO-B{bay}{dst[-1]}-{n:04d}" for n in run]
            g["operator_team"] = shifted.operator_team.str.replace("TEAM-", f"TEAM-B{bay}", regex=False).values
            g["plant_id"], g["line_id"], g["source_system"], g["data_quality"], g["source"] = plant, dst, "TWIN", "VALID", "generated"
            g["event_id"] = [f"GEN-{dst}-{i:05d}" for i in range(len(g))]
            out.append(g)
    return pd.concat(out, ignore_index=True)[list(hp.columns) + ["source"]]


def machine_hourly(hp: pd.DataFrame, mach: pd.DataFrame) -> pd.DataFrame:
    rows = []
    plant_of = hp.drop_duplicates("line_id").set_index("line_id").plant_id.to_dict()
    for line, ms in mach.groupby("line_id"):
        lh = hp[hp.line_id == line]
        if lh.empty:
            continue
        ms = ms.reset_index(drop=True)
        types = list(ms.machine_type)
        rng = _rng("mh", line)
        w = np.array([FAULT_W.get(t, 1) for t in types], dtype=float)
        placer = next((i for i, t in enumerate(types) if t in ("Pick and Place", "Assembly Cell")), 0)
        insp = {i: INSPECT[t] for i, t in enumerate(types) if t in INSPECT}
        tot = sum(insp.values()) or 1
        for r in lh.itertuples():
            reason, dmin = r.downtime_reason, int(r.downtime_min)
            culprit = None
            if dmin and reason == "FEEDER_JAM":
                culprit = placer
            elif dmin and reason == "EQUIPMENT_FAULT":
                culprit = int(rng.choice(len(types), p=w / w.sum()))
            elif dmin and reason in ("MICRO_STOP", "NONE"):
                culprit = int(rng.integers(len(types)))
            for i, mm in ms.iterrows():
                own = dmin if i == culprit else 0
                wait = 0 if i == culprit or not dmin else dmin
                wait_reason = None if not wait else (reason if reason in ("NO_MATERIAL", "CHANGEOVER") else f"WAITING_FOR_{types[culprit].upper().replace(' ', '_')}" if culprit is not None else reason)
                run = max(0, 60 - own - wait)
                rej = int(round(r.reject_qty * insp[i] / tot)) if i in insp else 0
                load = run / 60
                kw = KW.get(mm.machine_type, 2.0)
                temp = TEMP.get(mm.machine_type)
                rows.append({"machine_id": mm.machine_id, "line_id": line, "plant_id": plant_of[line], "event_ts": r.event_ts, "event_date": r.event_date,
                             "state": "DOWN" if own >= 45 else "STOPPED" if wait >= 45 else "SLOWED" if own or wait else ("RUNNING" if r.actual_qty else "IDLE"),
                             "run_min": run, "down_min": own, "down_reason": reason if own else None, "wait_min": wait, "wait_reason": wait_reason,
                             "units": int(r.actual_qty), "rejects": rej,
                             "power_kwh": round(kw * (0.15 + 0.85 * load) * rng.uniform(0.93, 1.07), 2),
                             "temp_c": round((temp if temp and run else rng.uniform(28, 34)) + rng.normal(0, 1.5 if temp else 0.6), 1),
                             "source": "generated"})
    return pd.DataFrame(rows)


def vehicles() -> pd.DataFrame:
    rows = []
    for plant, (smt, fal) in REAL.items():
        rows += [{"vehicle_id": f"{plant}-FL-1", "vehicle_type": "forklift", "name": "Forklift 1 · receiving", "role": "receiving", "line_id": None,
                  "model": "Electric counterbalance forklift, 2.0 t", "battery_kwh": 24.0, "commissioned_date": "2024-02-01", "service_interval_days": 90},
                 {"vehicle_id": f"{plant}-FL-2", "vehicle_type": "forklift", "name": "Forklift 2 · shipping", "role": "shipping", "line_id": None,
                  "model": "Electric counterbalance forklift, 2.0 t", "battery_kwh": 24.0, "commissioned_date": "2024-02-01", "service_interval_days": 90},
                 {"vehicle_id": f"{plant}-AGV-1", "vehicle_type": "agv", "name": "AGV 1 · kits to SMT", "role": "kits", "line_id": smt,
                  "model": "Tugger AGV, 500 kg", "battery_kwh": 5.0, "commissioned_date": "2024-06-01", "service_interval_days": 60},
                 {"vehicle_id": f"{plant}-AGV-2", "vehicle_type": "agv", "name": "AGV 2 · products to packing", "role": "products", "line_id": fal,
                  "model": "Tugger AGV, 500 kg", "battery_kwh": 5.0, "commissioned_date": "2024-06-01", "service_interval_days": 60}]
    for r in rows:
        r["plant_id"], r["source"] = r["vehicle_id"][:8], "generated"
    return pd.DataFrame(rows)


def vehicle_hourly(hp: pd.DataFrame, inv: pd.DataFrame, bom: pd.DataFrame, veh: pd.DataFrame) -> pd.DataFrame:
    """AGVs: one trip per cart (60 kits or 40 products). Forklifts: receipts and shipments per day from the stock counts
    (receipts = rise in materials stock + what the lines consumed; shipments = finished goods made - rise in finished goods),
    in pallets, spread over the day shifts."""
    per_unit = (bom.component_qty * (1 + bom.scrap_factor)).groupby(bom.product_id).sum()
    hours = sorted(hp.event_ts.unique())
    by = {k: g.set_index("event_ts") for k, g in hp.groupby("line_id")}
    rows = []
    for v in veh.itertuples():
        rng = _rng("veh", v.vehicle_id)
        plant = v.plant_id
        if v.vehicle_type == "forklift":
            st = inv[inv.plant_id == plant]
            wh = ["RM-WH-01", "SMT-KIT-01"] if v.role == "receiving" else ["FG-WH-01"]
            stock = st[st.warehouse_id.isin(wh)].groupby("snapshot_date").on_hand_qty.sum()
            ph = hp[hp.plant_id == plant]
            if v.role == "receiving":
                used = ph[ph.line_id.str.startswith("SMT")].assign(u=lambda x: x.actual_qty * x.product_id.map(per_unit).fillna(0)).groupby("event_date").u.sum()
                daily = (stock.diff().fillna(0) + used.reindex(stock.index).fillna(0)).clip(lower=0) / 1000      # pallets of ~1,000 parts
            else:
                made = ph[ph.line_id.str.startswith("FAL")].groupby("event_date").good_qty.sum()
                daily = (made.reindex(stock.index).fillna(0) - stock.diff().fillna(0)).clip(lower=0) / 200     # pallets of 200 phones
        battery = 100.0
        for ts in hours:
            hr, day = int(ts[11:13]), ts[:10]
            fault = int(rng.random() < 0.004) * int(rng.integers(15, 61))
            if v.vehicle_type == "agv":
                lr = by[v.line_id].loc[ts] if ts in by[v.line_id].index else None
                units = 0 if lr is None else int(lr.actual_qty if v.role == "kits" else lr.good_qty)
                cart = 60 if v.role == "kits" else 40
                trips = math.ceil(units / cart) if units else 0
                dist = trips * 0.09
            else:
                pallets_day = float(daily.get(day, 0.0)) if len(daily) else 0.0
                share = 1 / 16 if 6 <= hr < 22 else 0.0
                units = int(round(pallets_day * share + rng.normal(0, 0.3))) if share else 0
                units = max(0, units)
                trips = units
                dist = trips * 0.12
            run = min(60 - fault, trips * (3 if v.vehicle_type == "agv" else 6))
            charge = 0
            battery -= run * (0.12 if v.vehicle_type == "agv" else 0.09)
            if battery < 30 and not fault:
                charge, battery = 45, min(100.0, battery + 55)
                run = min(run, 15)
            rows.append({"vehicle_id": v.vehicle_id, "plant_id": plant, "event_ts": ts, "event_date": day,
                         "state": "DOWN" if fault >= 30 else "CHARGING" if charge else "MOVING" if run else "IDLE",
                         "trips": int(trips), "loads": int(units), "load_unit": "units" if v.vehicle_type == "agv" else "pallets",
                         "distance_km": round(dist, 2), "run_min": int(run), "charge_min": charge, "fault_min": fault,
                         "battery_pct": round(max(5.0, battery), 1), "source": "generated"})
    return pd.DataFrame(rows)


def maintenance(mach: pd.DataFrame, mh: pd.DataFrame, veh: pd.DataFrame, vh: pd.DataFrame, end: str) -> pd.DataFrame:
    rows = []
    end_ts = pd.Timestamp(end)
    for m in mach.itertuples():
        rng = _rng("svc", m.machine_id)
        t = pd.Timestamp(m.commissioned_date) + pd.Timedelta(days=int(m.planned_maintenance_interval_days))
        while t <= end_ts:
            start = t + pd.Timedelta(hours=6)                                    # at the start of shift A, after the night changeover
            dur = SERVICE_MIN.get(m.machine_type, 60) + int(rng.integers(-10, 21))
            rows.append({"asset_id": m.machine_id, "asset_kind": "machine", "line_id": m.line_id, "plant_id": None, "kind": "PLANNED",
                         "started_at": start, "duration_min": dur, "technician": f"MNT-{'ABC'[int(rng.integers(3))]}", "work_done": SERVICE_WORK,
                         "parts_replaced": "-", "cost_inr": int(1500 + dur * 40)})
            t = t + pd.Timedelta(days=int(m.planned_maintenance_interval_days))
    f = mh[(mh.down_reason == "EQUIPMENT_FAULT") & (mh.down_min >= 30)].merge(mach[["machine_id", "machine_type"]], on="machine_id")
    for r in f.itertuples():
        work, part = WORK.get(r.machine_type, ("Repaired the fault", "-"))
        start = pd.Timestamp(r.event_ts) + pd.Timedelta(minutes=60 - int(r.down_min))
        rows.append({"asset_id": r.machine_id, "asset_kind": "machine", "line_id": r.line_id, "plant_id": r.plant_id, "kind": "REPAIR",
                     "started_at": start, "duration_min": int(r.down_min), "technician": f"MNT-{'ABC'[int(pd.Timestamp(r.event_ts).hour // 8)]}",
                     "work_done": work, "parts_replaced": part, "cost_inr": int(2500 + r.down_min * 120)})
    for v in veh.itertuples():
        t = pd.Timestamp(v.commissioned_date) + pd.Timedelta(days=int(v.service_interval_days))
        while t <= end_ts:
            rows.append({"asset_id": v.vehicle_id, "asset_kind": "vehicle", "line_id": v.line_id, "plant_id": v.plant_id, "kind": "PLANNED",
                         "started_at": t + pd.Timedelta(hours=14), "duration_min": 90, "technician": "MNT-A",
                         "work_done": "Planned service: brakes, tyres, battery check, safety inspection", "parts_replaced": "-", "cost_inr": 4500})
            t = t + pd.Timedelta(days=int(v.service_interval_days))
    for r in vh[vh.fault_min >= 30].itertuples():
        rows.append({"asset_id": r.vehicle_id, "asset_kind": "vehicle", "line_id": None, "plant_id": r.plant_id, "kind": "REPAIR",
                     "started_at": pd.Timestamp(r.event_ts), "duration_min": int(r.fault_min), "technician": "MNT-A",
                     "work_done": "Reset the drive controller and replaced a worn wheel", "parts_replaced": "drive wheel", "cost_inr": 6000})
    df = pd.DataFrame(rows).sort_values(["started_at", "asset_id"]).reset_index(drop=True)
    plant_of = mach.set_index("line_id").plant_id.to_dict()
    df["plant_id"] = df.plant_id.fillna(df.line_id.map(plant_of))
    df["finished_at"] = (df.started_at + pd.to_timedelta(df.duration_min, unit="m")).dt.strftime("%Y-%m-%dT%H:%M:%S")
    df["started_at"] = df.started_at.dt.strftime("%Y-%m-%dT%H:%M:%S")
    df.insert(0, "maintenance_id", [f"MNT-{i + 1:06d}" for i in range(len(df))])
    df["status"], df["source"] = "DONE", "generated"
    return df


# ---- planning data the source does not have (suppliers, crews, cost rates, shipments) ----
LEAD = {"SOC": (21, 500), "MEM": (14, 1000), "NAND": (14, 1000), "PCB": (10, 2000), "DSP": (18, 1000), "BAT": (12, 1000), "CAM": (15, 1000),
        "HSG": (7, 2000), "GLS": (9, 2000), "SPK": (6, 5000), "ANT": (6, 5000), "CON": (5, 5000), "BOX": (4, 5000), "CBL": (5, 5000),
        "SCR": (3, 10000), "PASTE": (5, 200), "ADH": (5, 200)}                 # material family -> (supplier lead time days, pack size)


def suppliers(inv: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for m, cost in inv.groupby("material_id").unit_cost_inr.first().items():
        fam = m.split("-")[1]
        lead, pack = LEAD.get(fam, (10, 1000))
        rng = _rng("sup", m)
        rows.append({"material_id": m, "supplier_id": f"SUP-{fam}", "supplier_name": f"{fam.title()} supplier", "lead_time_days": lead,
                     "moq": pack * 2, "pack_qty": pack, "unit_cost_inr": float(cost), "on_time_pct": round(float(rng.uniform(82, 97)), 1),
                     "source": "generated"})
    return pd.DataFrame(rows)


CREW_SIZE = {"SMT": 6, "FAL": 9}                     # people needed to run one line for a shift
HEADCOUNT = {"IN-TN-01": 17, "IN-KA-01": 16}          # people rostered per shift (both lines + 1-2 relief)
HOLIDAYS = {"2026-11-08": 0.45, "2026-11-09": 0.3, "2026-12-25": 0.25}   # Diwali and Christmas: share of the crew on leave


def crews() -> pd.DataFrame:
    return pd.DataFrame([{"plant_id": p, "shift_id": s, "team_id": f"TEAM-{p[3:5]}-{s}", "headcount": n, "smt_certified": round(n * 0.45),
                          "fal_certified": round(n * 0.7), "crew_size_smt": CREW_SIZE["SMT"], "crew_size_fal": CREW_SIZE["FAL"], "source": "generated"}
                         for p, n in HEADCOUNT.items() for s in "ABC"])


def crew_leave(start: str, days: int = 90) -> pd.DataFrame:
    rows = []
    for p, n in HEADCOUNT.items():
        for s in "ABC":
            rng = _rng("leave", p, s)
            for d in pd.date_range(start, periods=days):
                k = d.strftime("%Y-%m-%d")
                out = int(rng.poisson(1.2)) + int(round(n * HOLIDAYS.get(k, 0)))
                rows.append({"date": k, "plant_id": p, "shift_id": s, "people_on_leave": min(n, out), "source": "generated"})
    return pd.DataFrame(rows)


def cost_rates() -> pd.DataFrame:
    base = {"labour_inr_per_hour": 220, "overtime_multiplier": 2.0, "smt_line_inr_per_min": 85, "fal_line_inr_per_min": 40,
            "late_penalty_pct_per_day": 0.5, "carrying_pct_per_year": 18, "fg_transfer_inr_per_unit": 35, "material_transfer_pct": 0.4,
            "truck_trip_inr": 6000}
    return pd.DataFrame([{"plant_id": "IN-TN-01", **base, "source": "generated"},
                         {"plant_id": "IN-KA-01", **base, "labour_inr_per_hour": 260, "smt_line_inr_per_min": 95, "source": "generated"}])


def shipments(co: pd.DataFrame) -> pd.DataFrame:
    """One or two shipments per customer order that shipped something. Ship dates: around the confirmed date (HIGH priority
    ships earlier); DELIVERED orders shipped in full."""
    rows = []
    for r in co[co.shipped_qty > 0].itertuples():
        rng = _rng("ship", r.customer_order_id)
        due = pd.Timestamp(r.confirmed_delivery_date)
        slip = int(np.clip(rng.normal({"HIGH": -0.5, "MEDIUM": 0.3, "LOW": 1.0}.get(r.priority, 0.3), 1.8), -3, 7))
        parts = [r.shipped_qty] if r.order_status == "DELIVERED" or r.shipped_qty < 50 else [r.shipped_qty // 2, r.shipped_qty - r.shipped_qty // 2]
        for i, q in enumerate(parts):
            d = max(pd.Timestamp(r.order_date) + pd.Timedelta(days=2), due + pd.Timedelta(days=slip - (len(parts) - 1 - i) * 3))
            rows.append({"shipment_id": f"SHP-{r.customer_order_id[3:]}-{i + 1}", "customer_order_id": r.customer_order_id, "ship_date": d.strftime("%Y-%m-%d"),
                         "qty": int(q), "source": "generated"})
    return pd.DataFrame(rows)


def po_confirmations(po: pd.DataFrame, end: str) -> pd.DataFrame:
    """Production confirmations per production order (what an MES reports when an order starts, progresses and finishes). The
    hourly feed's order tags cannot be used for this (each order appears across ~70 days at ~2x its quantity), so: started orders
    start around their planned start, take their planned time or longer, and finish with 97-100.5 % of the quantity as good units;
    in-progress orders report the share done by the end of the feed. Released orders have not started (no row)."""
    end_ts = pd.Timestamp(end)
    rows = []
    for r in po[po.order_status != "RELEASED"].itertuples():
        rng = _rng("conf", r.production_order_id)
        ps, pf = pd.Timestamp(r.planned_start), pd.Timestamp(r.planned_finish)
        start = min(ps + pd.Timedelta(hours=float(np.clip(rng.normal(8, 24), -24, 96))), end_ts - pd.Timedelta(days=1))
        dur = (pf - ps) * float(rng.uniform(0.9, 1.6))
        if r.order_status in ("COMPLETED", "CLOSED"):
            fin = min(start + dur, end_ts - pd.Timedelta(hours=1))
            good = int(round(r.order_qty * rng.uniform(0.97, 1.005)))
            rows.append({"production_order_id": r.production_order_id, "plant_id": r.plant_id, "actual_start": start.strftime("%Y-%m-%dT%H:%M:%S"),
                         "actual_finish": fin.strftime("%Y-%m-%dT%H:%M:%S"), "good_qty": good, "scrap_qty": int(round(good * rng.uniform(0.01, 0.03))),
                         "status": "FINISHED", "source": "generated"})
        else:
            share = float(np.clip((end_ts - start) / max(dur, pd.Timedelta(hours=1)), 0.05, 0.95) * rng.uniform(0.6, 1.0))
            good = int(round(r.order_qty * share))
            rows.append({"production_order_id": r.production_order_id, "plant_id": r.plant_id, "actual_start": start.strftime("%Y-%m-%dT%H:%M:%S"), "actual_finish": None,
                         "good_qty": good, "scrap_qty": int(round(good * rng.uniform(0.01, 0.03))), "status": "IN_PROGRESS", "source": "generated"})
    return pd.DataFrame(rows)


def build(src: dict) -> dict[str, pd.DataFrame]:
    hp, mach = src["fact_hourly_production"], src["dim_machine"]
    tm = twin_machines(mach)
    tl = twin_line_hourly(hp)
    allm = pd.concat([mach.assign(source="recorded"), tm], ignore_index=True)
    # a machine belongs to its line's plant (two source rows name the other plant)
    plant_of = pd.concat([hp, tl]).drop_duplicates("line_id").set_index("line_id").plant_id
    allm["plant_id"] = allm.line_id.map(plant_of).fillna(allm.plant_id)
    mh = machine_hourly(pd.concat([hp, tl.drop(columns="source")], ignore_index=True), allm)
    veh = vehicles()
    vh = vehicle_hourly(hp, src["fact_inventory_daily_snapshot"], src["fact_bom_component"], veh)
    mt = maintenance(allm, mh, veh, vh, hp.event_ts.max())
    start = (pd.Timestamp(src["fact_inventory_daily_snapshot"].snapshot_date.max()) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    return {"gen_twin_machine": tm, "gen_twin_line_hourly": tl, "gen_machine_hourly": mh, "gen_maintenance_event": mt, "gen_vehicle": veh, "gen_vehicle_hourly": vh,
            "gen_material_supplier": suppliers(src["fact_inventory_daily_snapshot"]), "gen_crew": crews(), "gen_crew_leave": crew_leave(start),
            "gen_cost_rate": cost_rates(), "gen_shipment": shipments(src["fact_customer_order"]),
            "gen_po_confirmation": po_confirmations(src["fact_production_order"], hp.event_ts.max())}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Generate the twin's records from the source CSVs")
    ap.add_argument("--push", action="store_true", help="also write src_gen_* tables to the database")
    a = ap.parse_args()
    from . import data
    out = build(data.load_csv())
    for name, df in out.items():
        df.to_csv(DATA_DIR / f"{name}.csv", index=False)
        print(f"{name}: {len(df):,} rows")
    if a.push:
        eng = data._engine()
        try:
            for name, df in out.items():
                df.to_sql(f"src_{name}", eng, if_exists="replace", index=True, index_label="_row", method="multi", chunksize=1000)
                print(f"src_{name} written")
        finally:
            eng.dispose()

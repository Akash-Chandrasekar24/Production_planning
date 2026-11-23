"""CSV source parsing -> domain PlanningInput, plus data-quality report. Swap load() for Domo datasets later."""
import hashlib
import json
import os
import pickle
from datetime import datetime, timedelta
from pathlib import Path
import numpy as np
import pandas as pd
from sqlalchemy.exc import ProgrammingError
from .config import DATA_DIR, Policy, Settings, database_url
from .domain import BomLine, LineSpec, Order, PlanningInput, RoutingOp, Slot, Stock

TABLES = ["dim_machine", "dim_product_routing", "dim_shift_calendar", "fact_bom_component",
          "fact_customer_order", "fact_production_order", "fact_hourly_production",
          "fact_inventory_daily_snapshot"]


SOURCE = {"name": None}          # where the last load() read from: "csv" | "db"


def _engine():
    from sqlalchemy import create_engine
    return create_engine(database_url().replace("postgresql://", "postgresql+psycopg2://", 1))


from .generate import GEN_TABLES   # generated records (app/generate.py). Optional: features fall back or say the data is missing without them.


def load_csv(path=DATA_DIR) -> dict[str, pd.DataFrame]:
    d = {t: pd.read_csv(path / f"{t}.csv") for t in TABLES}
    d.update({t: pd.read_csv(path / f"{t}.csv") for t in GEN_TABLES if (path / f"{t}.csv").exists()})
    return d


def load_db() -> dict[str, pd.DataFrame]:
    from sqlalchemy import inspect, text
    eng = _engine()
    try:
        with eng.connect() as c:           # explicit order: the CSV row order is part of the data contract
            d = {t: pd.read_sql(text(f"SELECT * FROM src_{t} ORDER BY _row"), c).drop(columns="_row") for t in TABLES}
            have = set(inspect(c).get_table_names())
            d.update({t: pd.read_sql(text(f"SELECT * FROM src_{t} ORDER BY _row"), c).drop(columns="_row") for t in GEN_TABLES if f"src_{t}" in have})
            return d
    finally:
        eng.dispose()


SNAP_DIR = Path(__file__).resolve().parents[1] / "var" / "snapshot"
_base_mem: dict = {}                 # fingerprint -> (base data, connected datasets): this process's copy


def _fingerprint() -> str:
    """Cheap test of whether the base data changed: exact row counts of every source and connector table, and the dataset choices.
    One round trip for the table list, one for all the counts together."""
    # Postgres's own write counters per table (no table is scanned): any insert, update or delete changes them, and a table that is
    # replaced gets a new relid. If the counters were reset (a restart), the fingerprint changes and the snapshot is rebuilt: never stale.
    from sqlalchemy import text
    from .db import engine
    with engine.connect() as c:
        stats = sorted(tuple(r) for r in c.execute(text(
            "SELECT relname, relid, n_tup_ins, n_tup_upd, n_tup_del FROM pg_stat_user_tables "
            "WHERE relname LIKE 'src\\_%' OR relname LIKE 'ext\\_%' OR relname = 'dataset_sources'")))
    return hashlib.sha1(json.dumps(stats, default=str).encode()).hexdigest()[:16]


def _db_base() -> dict[str, pd.DataFrame]:
    """The base data from the database, through a snapshot file every process on this machine shares: loading it from the database
    takes about a minute, the snapshot about a second. A new fingerprint (an import, a dataset switch) makes a new snapshot."""
    fp = _fingerprint()
    if fp in _base_mem:
        d, SOURCE["connected"] = _base_mem[fp]
        return d
    f = SNAP_DIR / f"base-{fp}.pkl"
    d = None
    if f.exists():
        try:
            with open(f, "rb") as fh:
                d, conn = pickle.load(fh)
            SOURCE["connected"] = conn
        except Exception:                        # a half-written or old-format file: rebuild it
            d = None
    if d is None:
        d = apply_sources(load_db())
        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(f".{os.getpid()}.tmp")
        with open(tmp, "wb") as fh:
            pickle.dump((d, dict(SOURCE.get("connected", {}))), fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, f)                       # atomic: other processes see the whole file or none
        for old in sorted(SNAP_DIR.glob("base-*.pkl"), key=lambda p: p.stat().st_mtime)[:-3]:
            old.unlink(missing_ok=True)
    _base_mem.clear()
    _base_mem[fp] = (d, dict(SOURCE.get("connected", {})))
    return d


def load(path=DATA_DIR) -> dict[str, pd.DataFrame]:
    """DATA_SOURCE=auto: database tables (src_*) when DATABASE_URL is set and they exist, else the CSV files.
    Datasets an admin switched to a connector (dataset_sources) are then read from their staging table instead.
    Received orders (order_intake) are always read fresh: they are small and change with every upload."""
    mode = Settings().data_source
    if mode != "csv" and database_url():
        try:
            d = _db_base()
            SOURCE["name"] = "db"
            return apply_intake(d)
        except ProgrammingError:                    # src_* tables not created yet
            if mode == "db":
                raise FileNotFoundError("src_* tables not found in the database; run `python -m app.data`")
    d = load_csv(path)
    SOURCE["name"] = "csv"
    return apply_intake(apply_sources(d))


def apply_sources(d: dict) -> dict:
    from sqlalchemy import inspect, text
    from .db import engine
    SOURCE["connected"] = {}
    if not inspect(engine).has_table("dataset_sources"):
        return d
    with engine.connect() as c:
        for ds, tbl in c.execute(text("SELECT dataset, table_name FROM dataset_sources")).all():
            if ds in d and tbl.startswith("ext_") and tbl.replace("_", "").isalnum():     # names are generated by the app
                d[ds] = pd.read_sql(text(f'SELECT * FROM "{tbl}" ORDER BY _row'), c).drop(columns="_row")
                SOURCE["connected"][ds] = tbl
    return d


def apply_intake(d: dict) -> dict:
    """Orders received through the intake API join the order book as OPEN orders with nothing shipped yet."""
    from sqlalchemy import inspect, text
    from .db import engine
    if not inspect(engine).has_table("order_intake"):
        return d
    with engine.connect() as c:
        x = pd.read_sql(text("SELECT * FROM order_intake ORDER BY received_at, customer_order_id"), c)
    if x.empty:
        return d
    co = d["fact_customer_order"]
    x = x[~x.customer_order_id.isin(co.customer_order_id)].assign(shipped_qty=0, order_status="OPEN")
    d = dict(d)
    d["fact_customer_order"] = pd.concat([co, x[co.columns]], ignore_index=True)
    return d


def push_to_db(path=DATA_DIR) -> dict[str, int]:
    """Copy the CSVs into src_<table> tables (replacing them). Source files stay the original of record."""
    eng = _engine()
    counts = {}
    try:
        for t, df in load_csv(path).items():
            df.to_sql(f"src_{t}", eng, if_exists="replace", index=True, index_label="_row", method="multi", chunksize=1000)
            counts[t] = len(df)
    finally:
        eng.dispose()
    return counts


if __name__ == "__main__":
    for t, n in push_to_db().items():
        print(f"src_{t}: {n} rows")


_memo: dict = {}                          # (what, id(source)) -> (source, result): reused until the data is reloaded


def _once(what: str, src, make):
    """Work a pure result out once per loaded dataset. The source object is kept with it, so a reloaded dataset (a new object)
    is never mistaken for the old one."""
    k = (what, id(src))
    hit = _memo.get(k)
    if hit is None or hit[0] is not src:
        if len(_memo) > 16:
            _memo.clear()
        hit = _memo[k] = (src, make())
    return hit[1]


def dq_report(d: dict) -> list[dict]:
    """Known issues, recomputed from the data (counts, not hardcoded); once per loaded dataset."""
    return [dict(x) for x in _once("dq", d["fact_hourly_production"], lambda: _dq_report(d))]


def _dq_report(d: dict) -> list[dict]:
    """Known issues, recomputed from the data (counts, not hardcoded)."""
    co, po, hp, m = d["fact_customer_order"], d["fact_production_order"], d["fact_hourly_production"], d["dim_machine"]
    inv = d["fact_inventory_daily_snapshot"]
    issues = []
    add = lambda code, n, msg, sev="warn": issues.append({"code": code, "count": int(n), "severity": sev, "message": msg})
    add("PO_CO_LINK_BROKEN", (~po.customer_order_id.isin(co.customer_order_id)).sum(),
        "production_order.customer_order_id not found in customer orders; pegging disabled and PO supply ignored", "blocker")
    j = hp.merge(po[["production_order_id", "product_id", "plant_id"]], on="production_order_id", suffixes=("", "_po"))
    add("HOURLY_PO_MISMATCH", ((j.product_id != j.product_id_po) | (j.plant_id != j.plant_id_po)).sum(),
        "hourly rows whose product/plant differ from their PO; actuals used per line only")
    lp = hp[["line_id", "plant_id"]].drop_duplicates()
    bad = m.merge(lp, on="line_id", suffixes=("", "_hp"))
    add("MACHINE_PLANT_MISMATCH", (bad.plant_id != bad.plant_id_hp).sum(), "machine plant differs from its line's plant; line plant taken from hourly actuals")
    add("STATUS_SHIPPED_INCONSISTENT", ((co.order_status.isin(["OPEN", "CONFIRMED"])) & (co.shipped_qty > 0)).sum(),
        "OPEN/CONFIRMED orders with shipped_qty>0; open qty = ordered - shipped used")
    add("INV_STATUS_VS_HOLD", ((inv.inventory_status == "AVAILABLE") & (inv.quality_hold_qty > 0)).sum(),
        "AVAILABLE rows holding quality_hold_qty; availability computed from quantities, not status")
    w = inv.sort_values("snapshot_date").groupby(["material_id", "warehouse_id"]).plant_id
    add("INV_PLANT_UNSTABLE", (w.shift() != inv.sort_values("snapshot_date").plant_id).sum() - inv.groupby(["material_id", "warehouse_id"]).ngroups,
        "plant_id of a material/warehouse stock row changes between days (each material/warehouse appears under one plant per day); plant-level stock unreliable -> material_scope=pooled", "blocker")
    add("CALENDAR_ENDS_BEFORE_ASOF", 1, f"calendar ends {d['dim_shift_calendar'].calendar_date.max()}; later dates use default shifts", "info")
    return issues


def holt(y: np.ndarray, h: int, a: float = 0.4, b: float = 0.15, phi: float = 0.8) -> np.ndarray:
    """Holt's method with a damped trend: a recent rise or fall levels off instead of running on for ever."""
    lvl, tr = y[0], (y[min(3, len(y) - 1)] - y[0]) / max(1, min(3, len(y) - 1))
    for v in y[1:]:
        lvl, prev = a * v + (1 - a) * (lvl + phi * tr), lvl
        tr = b * (lvl - prev) + (1 - b) * phi * tr
    return np.maximum(0, lvl + tr * np.cumsum(phi ** np.arange(1, h + 1)))


def fg_stock(inv: pd.DataFrame, as_of: datetime, products: set) -> dict[str, int]:
    """Finished units on hand per product on the day's count (rows whose item is a product). None in the current data."""
    day = inv[(inv.snapshot_date <= as_of.date().isoformat()) & inv.material_id.isin(products)]
    if day.empty:
        return {}
    day = day[day.snapshot_date == day.snapshot_date.max()]
    return {k: int(v) for k, v in (day.on_hand_qty - day.allocated_qty - day.blocked_qty).groupby(day.material_id).sum().items() if v > 0}


def weekly_forecast(d: dict, as_of: datetime, weeks: int) -> dict[str, list[dict]]:
    """Forecast units per product for the weeks from as_of's week on: Holt's damped trend on weekly ordered units (8+ weeks of history)."""
    co = d["fact_customer_order"]
    wk = pd.to_datetime(co.order_date).dt.to_period("W-SUN")
    last_full = pd.Period(pd.Timestamp(co.order_date.max()), "W-SUN") - 1
    first = pd.Period(pd.Timestamp(as_of), "W-SUN")
    h = (first - last_full).n + weeks - 1
    if h < 1:
        return {}
    g = co.assign(w=wk)[wk <= last_full].groupby(["product_id", "w"]).order_qty.sum().unstack(fill_value=0)
    out = {}
    for p, row in g.iterrows():
        y = row.values.astype(float)
        if len(y) >= 8:
            out[p] = [{"week": str((last_full + i + 1).start_time.date()), "qty": int(round(v))}
                      for i, v in enumerate(holt(y, h)) if last_full + i + 1 >= first]
    return out


SHIFT_OF_HOUR = {6: "A", 14: "B", 22: "C"}       # shift start hours in the calendar


def crew_share(d: dict) -> tuple[dict, dict]:
    """(plant, shift) -> (headcount, people a shift needs for both lines); (date, plant, shift) -> people on leave."""
    cr, lv = d.get("gen_crew"), d.get("gen_crew_leave")
    if cr is None:
        return {}, {}
    need = {(r.plant_id, r.shift_id): (int(r.headcount), int(r.crew_size_smt + r.crew_size_fal)) for r in cr.itertuples()}
    leave = lv.groupby(["date", "plant_id", "shift_id"]).people_on_leave.sum().to_dict() if lv is not None else {}
    return need, leave


def agreed_forecast(fc: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """The forecast the plan uses: the model's numbers, replaced where someone agreed a different number (forecast review)."""
    try:
        from .db import ForecastOverride, SessionLocal
        with SessionLocal() as s:
            ov = {(o.product_id, o.week): o.qty for o in s.query(ForecastOverride)}
    except Exception:                                   # no table yet: the model's numbers
        ov = {}
    return {p: [{**w, "model": w["qty"], "qty": ov.get((p, w["week"]), w["qty"]), "agreed": (p, w["week"]) in ov} for w in ws] for p, ws in fc.items()}


def to_inputs(d: dict, as_of: str | None = None, pol: Policy | None = None) -> PlanningInput:
    pol = pol or Policy()
    inv = d["fact_inventory_daily_snapshot"]
    as_of_dt = datetime.fromisoformat(as_of) if as_of else datetime.fromisoformat(inv.snapshot_date.max()) + timedelta(days=1)
    end = as_of_dt + timedelta(days=pol.horizon_days)
    day = as_of_dt.date().isoformat()
    issues = dq_report(d)

    # calendar: file rows, then synthesised A/B/C shifts (assumption) past the file
    cal = d["dim_shift_calendar"]
    rows = [(pd.Timestamp(r.shift_start).to_pydatetime(), pd.Timestamp(r.shift_end).to_pydatetime(), float(r.planned_minutes))
            for r in cal.itertuples() if r.is_working_day]
    t = max(e for _, e, _ in rows)
    while t < end:
        rows.append((t, t + timedelta(hours=8), float(pol.default_shift_minutes)))
        t += timedelta(hours=8)
    slots = [Slot(start=s, end=e, minutes=m) for s, e, m in rows if e > as_of_dt and s < end]

    # lines: plant + rate from hourly actuals
    hp = d["fact_hourly_production"]
    g = hp.groupby("line_id").agg(t=("target_qty", "max"), good=("good_qty", "sum"), tgt=("target_qty", "sum"), plant=("plant_id", "first"))
    mach = d["dim_machine"].groupby("line_id").machine_id.apply(list).to_dict()
    lines = [LineSpec(line_id=l, plant_id=r.plant, kind=l.split("-")[0], rate_per_min=float(r.t * r.good / r.tgt / 60.0),
                      machine_ids=mach.get(l, []), slots=[s.model_copy() for s in slots]) for l, r in g.iterrows()]   # own copies: what-ifs change one line's shifts

    if pol.crew_limits:                                # a shift short of people runs its lines for that share of its minutes
        need, leave = crew_share(d)
        cut = 0.0
        for l in lines:
            for s in l.slots:
                sh = SHIFT_OF_HOUR.get(s.start.hour)
                if (l.plant_id, sh) not in need:
                    continue
                head, n = need[(l.plant_id, sh)]
                have = head - int(leave.get((s.start.strftime("%Y-%m-%d"), l.plant_id, sh), 0))
                if n and have < n:
                    cut += s.minutes * (1 - max(0, have) / n)
                    s.minutes *= max(0, have) / n
        if cut:
            issues = issues + [{"code": "CREW_SHORT", "severity": "info", "count": int(cut // 60),
                                "message": "hours of line time removed: shifts short of people run for the share of the crew that is there"}]

    co = d["fact_customer_order"]
    o = co[co.order_status.isin(pol.open_statuses)].assign(q=lambda x: x.order_qty - x.shipped_qty)
    skipped = int((o.q <= 0).sum())
    if skipped:
        issues.append({"code": "ORDER_NO_OPEN_QTY", "count": skipped, "severity": "info", "message": "open orders with shipped >= ordered skipped"})
    orders = [Order(order_id=r.customer_order_id, product_id=r.product_id, qty=int(r.q), due=r.confirmed_delivery_date,
                    priority=r.priority, status=r.order_status, unit_price_inr=float(r.unit_price_inr), customer_id=r.customer_id)
              for r in o[o.q > 0].itertuples()]

    rt = d["dim_product_routing"]
    routing = [RoutingOp(product_id=r.product_id, seq=r.operation_seq, station_id=r.station_id, segment=r.station_id.split("-")[1],
                         cycle_sec=r.standard_cycle_sec, setup_min=r.setup_min) for r in rt.itertuples()]
    b = d["fact_bom_component"]
    bom = [BomLine(product_id=r.product_id, material_id=r.material_id, qty_per=r.component_qty, scrap=r.scrap_factor,
                   effective_from=r.effective_from, effective_to=r.effective_to)
           for r in b.itertuples() if r.effective_from <= day <= r.effective_to]

    snap = inv[(inv.snapshot_date <= day) & inv.warehouse_id.isin(pol.material_warehouses)]
    snap = snap[snap.snapshot_date == snap.snapshot_date.max()]
    snap = snap.assign(free=snap.on_hand_qty - snap.allocated_qty - snap.blocked_qty - snap.quality_hold_qty
                       + (snap.in_transit_qty if pol.include_in_transit else 0))
    st = snap.groupby(["plant_id", "material_id"]).agg(free=("free", "sum"), safety=("safety_stock_qty", "sum"), cost=("unit_cost_inr", "first"))
    if pol.material_scope == "pooled":
        st = snap.groupby("material_id").agg(free=("free", "sum"), safety=("safety_stock_qty", "sum"), cost=("unit_cost_inr", "first"))
        st.index = pd.MultiIndex.from_tuples([("ALL", m) for m in st.index])
    stock = [Stock(plant_id=p, material_id=m, available=max(0.0, float(r.free)), safety=float(r.safety), unit_cost_inr=float(r.cost))
             for (p, m), r in st.iterrows()]
    op = open_po(d, pol)
    po_supply = op.groupby("product_id").remaining.sum().astype(int).to_dict()        # what open orders will still make
    if pol.plan_open_po_work:                          # their remaining work goes on the lines, in their own plant
        orders += [Order(order_id=r.production_order_id, product_id=r.product_id, qty=int(r.remaining), due=str(r.due_date)[:10], priority=r.priority,
                         status="PRODUCTION_ORDER", customer_id=r.customer_order_id, plant_id=r.plant_id, kitted=r.order_status == "IN_PROGRESS")
                   for r in op.itertuples() if r.remaining > 0]
    maint = service_windows(d, as_of_dt, end) if pol.plan_maintenance else []
    for w in maint:                                    # a service stops its line (serial line): those minutes are not plannable
        for l in lines:
            if l.line_id == w["line_id"]:
                for s in l.slots:
                    ov = (min(s.end, w["end"]) - max(s.start, w["start"])).total_seconds() / 60
                    if ov > 0:
                        s.minutes = max(0.0, s.minutes - ov)
    inp = PlanningInput(as_of=as_of_dt, open_po_supply=po_supply, horizon_end=end, orders=orders, routing=routing, bom=bom, lines=lines, stock=stock,
                         machine_line=d["dim_machine"].set_index("machine_id").line_id.to_dict(), data_issues=issues, maintenance=maint,
                         yields=yields(hp, pol.yield_days) if pol.yield_aware else {},
                         lead_days=({r.material_id: int(r.lead_time_days) for r in sup.itertuples()} if (sup := d.get("gen_material_supplier")) is not None else {}),
                         forecast=agreed_forecast(weekly_forecast(d, as_of_dt, pol.forecast_weeks)),
                         fg_stock=fg_stock(inv, as_of_dt, set(co.product_id)))
    if pol.volume_plan_id:                             # made with an agreed volume plan: its committed dates and extra minutes
        from . import volume
        inp = volume.apply(inp, volume.load(pol.volume_plan_id))
    if pol.fill_shifts or pol.outsource_orders:        # a plan option's capacity levers: extra shifts, outsourced orders
        from . import volume
        inp = volume.levers(inp, pol)
    return inp


def open_po(d: dict, pol: Policy | None = None) -> pd.DataFrame:
    """Open production orders with the units they still owe: ordered minus good units confirmed (production confirmations,
    gen_po_confirmation); without confirmations, minus good units off final assembly in the hourly feed."""
    pol = pol or Policy()
    po = d["fact_production_order"]
    po = po[po.order_status.isin(pol.open_po_statuses)].copy()
    conf = d.get("gen_po_confirmation")
    if conf is not None:
        made = conf.groupby("production_order_id").good_qty.sum()
    else:
        h = d["fact_hourly_production"]
        made = h[h.line_id.str.startswith("FAL")].groupby("production_order_id").good_qty.sum()
    po["made"] = po.production_order_id.map(made).fillna(0).astype(int)
    po["remaining"] = (po.order_qty - po.made).clip(lower=0).astype(int)
    return po


def yields(hp: pd.DataFrame, days: int = 30) -> dict[str, float]:
    """Good / made per line and product over the last `days` days of records (and the line's average); once per dataset."""
    return dict(_once(f"yields{days}", hp, lambda: _yields(hp, days)))


def _yields(hp: pd.DataFrame, days: int = 30) -> dict[str, float]:
    h = hp[hp.event_date > (pd.Timestamp(hp.event_date.max()) - pd.Timedelta(days=days)).strftime("%Y-%m-%d")]
    out = {}
    for (l, p), g in h.groupby(["line_id", "product_id"]):
        if g.actual_qty.sum() >= 500:                  # enough made to trust the figure
            out[f"{l}|{p}"] = round(float(g.good_qty.sum() / g.actual_qty.sum()), 4)
    for l, g in h.groupby("line_id"):
        if g.actual_qty.sum():
            out[f"{l}|*"] = round(float(g.good_qty.sum() / g.actual_qty.sum()), 4)
    return out


def service_windows(d: dict, start: datetime, end: datetime) -> list[dict]:
    """Planned machine services due between start and end, one window per line and day (services on one line run together).
    Next service = last recorded planned service + the machine's interval (maintenance records), else commissioning + intervals."""
    m = d["dim_machine"]
    ev = d.get("gen_maintenance_event")
    last, dur = {}, {}
    if ev is not None:
        pl = ev[(ev.kind == "PLANNED") & (ev.asset_kind == "machine")]
        last = pl.groupby("asset_id").started_at.max().to_dict()
        dur = pl.groupby("asset_id").duration_min.median().to_dict()
    days = {}
    for r in m.itertuples():
        iv = int(r.planned_maintenance_interval_days)
        t0 = pd.Timestamp(start).normalize()
        if r.machine_id in last:
            t = pd.Timestamp(last[r.machine_id]).normalize() + pd.Timedelta(days=iv)
        else:
            t = pd.Timestamp(r.commissioned_date)
            while t < t0:
                t += pd.Timedelta(days=iv)
        t = max(t, t0)                                                # overdue: on the first day of the plan
        while t < pd.Timestamp(end):
            k = (r.line_id, t.strftime("%Y-%m-%d"))
            w = days.setdefault(k, {"line_id": r.line_id, "machine_ids": [], "minutes": 0.0})
            w["machine_ids"].append(r.machine_id)
            w["minutes"] = max(w["minutes"], float(dur.get(r.machine_id, 90)))
            t += pd.Timedelta(days=iv)
    out = []
    for (line, day), w in sorted(days.items()):
        s = datetime.fromisoformat(day) + timedelta(hours=6)          # start of shift A, as the records show
        out.append({**w, "start": s, "end": s + timedelta(minutes=w["minutes"])})
    return [w for w in out if w["end"] > start]

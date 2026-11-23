"""One-off rework of the source data, asked for by the business (13 Nov 2026), so the figures look like a real plant:

  downtime     the feed had ~10 days of downtime per line in 92 days (48 % of hours). Keep ~1.5 days per line, spread evenly over
               the period (a random, deterministic selection of the recorded stops, so the reasons keep their mix); hours whose stop is
               removed get normal output back (target x the line's usual pace, the line's usual yield).
  due dates    1,065 of 1,183 open customer orders and all 206 open production orders were already past due when the plan starts.
               Open customer orders are re-dated 1-6 weeks ahead (about 4 % stay 1-3 days late); their requested date moves by the
               same number of days, so the promise kept / not kept stays as it was. Open production orders move with them: the due
               date 1-4 weeks ahead, planned start / finish keep their length and slack.

The originals are copied to Data/original/ first (once) and the rework always starts from them, so running it again gives the same
result. Then: `python -m app.generate` (derived records) and `python -m app.data` (push everything to the database).
Run: python -m app.rework
"""
import shutil
import zlib
import numpy as np
import pandas as pd
from .config import DATA_DIR

SEED = 20261113
KEEP_DAYS_PER_LINE = 1.5
OPEN_CO = ("OPEN", "CONFIRMED", "IN_PRODUCTION", "PARTIALLY_SHIPPED")
OPEN_PO = ("RELEASED", "IN_PROGRESS")
FILES = ["fact_hourly_production", "fact_customer_order", "fact_production_order"]


def _rng(*key):
    return np.random.default_rng([SEED, *[zlib.crc32(str(k).encode()) for k in key]])


def originals() -> dict[str, pd.DataFrame]:
    orig = DATA_DIR / "original"
    orig.mkdir(exist_ok=True)
    for f in FILES:
        if not (orig / f"{f}.csv").exists():
            shutil.copy2(DATA_DIR / f"{f}.csv", orig / f"{f}.csv")
    return {f: pd.read_csv(orig / f"{f}.csv") for f in FILES}


def downtime(hp: pd.DataFrame) -> pd.DataFrame:
    hp = hp.copy()
    budget = KEEP_DAYS_PER_LINE * 1440
    for line, g in hp.groupby("line_id"):
        rng = _rng("dt", line)
        stops = g[g.downtime_min > 0].index.to_numpy()
        rng.shuffle(stops)
        keep, total = set(), 0
        for i in stops:
            m = int(hp.at[i, "downtime_min"])
            if total + m <= budget:
                keep.add(i)
                total += m
        ok = g[g.downtime_min == 0]
        pace = float((ok.actual_qty / ok.target_qty.replace(0, np.nan)).median()) if len(ok) else 0.94
        yld = float(ok.good_qty.sum() / ok.actual_qty.sum()) if ok.actual_qty.sum() else 0.98
        for i in stops:
            if i in keep:
                continue
            t = int(hp.at[i, "target_qty"])
            a = int(round(t * float(np.clip(rng.normal(pace, 0.03), 0.8, 1.0))))
            good = int(round(a * float(np.clip(rng.normal(yld, 0.004), 0.9, 1.0))))
            hp.loc[i, ["actual_qty", "good_qty", "reject_qty", "downtime_min", "downtime_reason"]] = [a, good, a - good, 0, "NONE"]
            hp.at[i, "rework_qty"] = min(int(hp.at[i, "rework_qty"]), a - good)
    return hp


def due_dates(co: pd.DataFrame, po: pd.DataFrame, start: pd.Timestamp) -> tuple[pd.DataFrame, pd.DataFrame]:
    co, po = co.copy(), po.copy()
    for i, r in co[co.order_status.isin(OPEN_CO)].iterrows():
        rng = _rng("co", r.customer_order_id)
        conf = pd.Timestamp(r.confirmed_delivery_date)
        if conf >= start:
            continue
        new = start + pd.Timedelta(days=int(-rng.integers(1, 4) if rng.random() < 0.04 else rng.integers(1, 43)))
        shift = new - conf
        co.at[i, "confirmed_delivery_date"] = new.strftime("%Y-%m-%d")
        co.at[i, "requested_delivery_date"] = (pd.Timestamp(r.requested_delivery_date) + shift).strftime("%Y-%m-%d")
    for i, r in po[po.order_status.isin(OPEN_PO)].iterrows():
        rng = _rng("po", r.production_order_id)
        due = pd.Timestamp(r.due_date)
        if due >= start:
            continue
        ps, pf = pd.Timestamp(r.planned_start), pd.Timestamp(r.planned_finish)
        new_due = start + pd.Timedelta(days=int(rng.integers(5, 29)))
        slack, length = due - pf, pf - ps
        new_pf = new_due - slack
        new_ps = new_pf - length
        if r.order_status == "IN_PROGRESS":                       # already running: it started before the plan's first day
            new_ps = min(new_ps, start - pd.Timedelta(days=int(rng.integers(1, 4))))
        else:                                                     # not started yet: it starts on or after the first day
            new_ps = max(new_ps, start)
            new_pf = max(new_pf, new_ps + length)
        po.at[i, "planned_start"] = new_ps.strftime("%Y-%m-%dT%H:%M:%S")
        po.at[i, "planned_finish"] = new_pf.strftime("%Y-%m-%dT%H:%M:%S")
        po.at[i, "due_date"] = max(new_due, new_pf.normalize()).strftime("%Y-%m-%d")
    return co, po


def run() -> dict:
    src = originals()
    inv = pd.read_csv(DATA_DIR / "fact_inventory_daily_snapshot.csv", usecols=["snapshot_date"])
    start = pd.Timestamp(inv.snapshot_date.max()) + pd.Timedelta(days=1)             # the plan's first day
    hp = downtime(src["fact_hourly_production"])
    co, po = due_dates(src["fact_customer_order"], src["fact_production_order"], start)
    for name, df in (("fact_hourly_production", hp), ("fact_customer_order", co), ("fact_production_order", po)):
        df.to_csv(DATA_DIR / f"{name}.csv", index=False)
    oc = co[co.order_status.isin(OPEN_CO)]
    return {"downtime_days_per_line": (hp.groupby("line_id").downtime_min.sum() / 1440).round(2).to_dict(),
            "hours_with_downtime_pct": round(100 * float((hp.downtime_min > 0).mean()), 1),
            "open_orders_past_due": int((oc.confirmed_delivery_date < start.strftime("%Y-%m-%d")).sum()), "open_orders": int(len(oc)),
            "open_pos_past_due": int((po[po.order_status.isin(OPEN_PO)].due_date < start.strftime("%Y-%m-%d")).sum())}


if __name__ == "__main__":
    for k, v in run().items():
        print(f"{k}: {v}")

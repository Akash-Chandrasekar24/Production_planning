"""Inventory agent: looks after materials and supplies.

Uses the same stock view as the planner (usable warehouses: on hand - allocated - blocked - quality hold, plus stock in transit when
the policy counts it) and the bills of materials. Daily usage is the average final-assembly output of the last
Policy.inv_usage_days days turned into material through the bills of materials.
  inventory.low_cover     materials that cover fewer than Policy.inv_min_cover_days days of production (critical below
                          Policy.inv_critical_cover_days)
  inventory.below_safety  usable stock below safety stock
  inventory.shortfall     open customer orders need more of a material than there is
  inventory.expiring      lots expiring within Policy.inv_expiry_days days
It also answers the orchestrator: which materials are at risk, overall or for given products (context for the planning agent).
It never orders, moves or blocks stock; it tells people.
"""
from collections import defaultdict
from datetime import date, timedelta
from . import data, store
from .config import Policy

NAME = "inventory agent"


def _view() -> dict:
    """Per material: usable stock, safety stock, daily usage, days of cover, and what open orders need."""
    pol, d = Policy(), store.D()
    inp = data.to_inputs(d, None, pol)
    free, safety = defaultdict(float), defaultdict(float)
    for s in inp.stock:
        free[s.material_id] += s.available
        safety[s.material_id] += s.safety
    per = defaultdict(list)
    for b in inp.bom:
        per[b.product_id].append(b)
    h = d["fact_hourly_production"]
    dates = sorted(h.event_date.unique())[-pol.inv_usage_days:]
    made = h[h.line_id.str.startswith("FAL") & h.event_date.isin(dates)].groupby("product_id").actual_qty.sum() / max(1, len(dates))
    use, need = defaultdict(float), defaultdict(float)
    for prod, q in made.items():
        for b in per[prod]:
            use[b.material_id] += q * b.qty_per * (1 + b.scrap)
    for o in inp.orders:
        for b in per[o.product_id]:
            need[b.material_id] += o.qty * b.qty_per * (1 + b.scrap)
    rows = []
    for m in sorted(set(free) | set(use)):
        cover = free[m] / use[m] if use[m] else None
        rows.append({"material_id": m, "free": round(free[m]), "safety": round(safety[m]), "daily_use": round(use[m]),
                     "cover_days": round(cover, 1) if cover is not None else None, "open_need": round(need[m]),
                     "shortfall": round(max(0.0, need[m] - free[m])),
                     "products": sorted(p for p, bs in per.items() if any(b.material_id == m for b in bs))})
    return {"as_of": inp.as_of.date(), "materials": rows}


def context(products: list | None = None) -> dict:
    """Materials at risk, for the planning agent (optionally only those used by `products`)."""
    pol = Policy()
    v = _view()
    ms = [m for m in v["materials"] if not products or set(m["products"]) & set(products)]
    return {"as_of": str(v["as_of"]),
            "low_cover": sorted((m for m in ms if m["cover_days"] is not None and m["cover_days"] < pol.inv_min_cover_days), key=lambda m: m["cover_days"]),
            "below_safety": [m for m in ms if m["free"] < m["safety"]],
            "shortfall": sorted((m for m in ms if m["shortfall"] > 0), key=lambda m: -m["shortfall"])}


def sense() -> dict:
    pol, d = Policy(), store.D()
    c = context()
    day = c["as_of"]
    out = []
    names = lambda ms, f: ", ".join(f(m) for m in ms[:6]) + (f" and {len(ms) - 6} more" if len(ms) > 6 else "")
    crit = [m for m in c["low_cover"] if m["cover_days"] < pol.inv_critical_cover_days]
    if crit:
        out.append({"type": "inventory.low_cover", "agent": NAME, "severity": "critical", "key": f"inventory.low_cover:critical:{day}",
                    "title": f"{len(crit)} material{'s' if len(crit) != 1 else ''} will run out within a day: " + names(crit, lambda m: f"{m['material_id']} ({m['cover_days']:g} d)"),
                    "detail": {"materials": crit}})
    warn = [m for m in c["low_cover"] if m not in crit]
    if warn:
        out.append({"type": "inventory.low_cover", "agent": NAME, "severity": "warn", "key": f"inventory.low_cover:warn:{day}",
                    "title": f"{len(warn)} material{'s' if len(warn) != 1 else ''} cover less than {pol.inv_min_cover_days:g} days of production: "
                             + names(warn, lambda m: f"{m['material_id']} ({m['cover_days']:g} d)"), "detail": {"materials": warn}})
    if c["below_safety"]:
        out.append({"type": "inventory.below_safety", "agent": NAME, "severity": "warn", "key": f"inventory.below_safety:{day}",
                    "title": "Below safety stock: " + names(c["below_safety"], lambda m: m["material_id"]), "detail": {"materials": c["below_safety"]}})
    if c["shortfall"]:
        out.append({"type": "inventory.shortfall", "agent": NAME, "severity": "warn", "key": f"inventory.shortfall:{day}",
                    "title": f"Open orders need more than the stock of {len(c['shortfall'])} material{'s' if len(c['shortfall']) != 1 else ''}: "
                             + names(c["shortfall"], lambda m: f"{m['material_id']} short {m['shortfall']:,}"), "detail": {"materials": c["shortfall"][:20]}})
    inv = d["fact_inventory_daily_snapshot"]
    snap = inv[inv.snapshot_date == inv.snapshot_date.max()]
    lim = str(date.fromisoformat(snap.snapshot_date.max()) + timedelta(days=pol.inv_expiry_days))
    exp = snap[snap.expiry_date.notna() & (snap.expiry_date.astype(str) <= lim) & (snap.on_hand_qty > 0)]
    if len(exp):
        lots = exp.groupby("material_id").agg(qty=("on_hand_qty", "sum"), first=("expiry_date", "min")).reset_index().to_dict("records")
        out.append({"type": "inventory.expiring", "agent": NAME, "severity": "warn", "key": f"inventory.expiring:{day}",
                    "title": f"Stock expiring within {pol.inv_expiry_days} days: " + names(lots, lambda m: f"{m['material_id']} ({int(m['qty']):,} by {m['first']})"),
                    "detail": {"lots": lots}})
    return {"signals": out, "summary": f"Stock of {snap.snapshot_date.max()} · {len(c['low_cover'])} under {pol.inv_min_cover_days:g} days of cover · "
                                       f"{len(c['below_safety'])} below safety · {len(c['shortfall'])} short for open orders"}

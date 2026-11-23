"""Scheduled reports: a daily brief to each plant's production head (today's jobs, where they stand, what is late or waiting for parts,
open floor problems and risks) and a weekly report to the production managers (success measures, capacity, approvals, risks).
Each goes out once a day / week (report_log), by email when a mail server is set up and always as a bell notification."""
import html
import logging
from datetime import date, datetime, timedelta
from email.message import EmailMessage

from . import coord, execution, incidents, notify, planning_tools, signoff, store
from .config import Policy, Settings
from .db import Incident, Notification, PlanNote, ReportLog, SessionLocal, TeamMember, _now

log = logging.getLogger("planner.reports")
PLANT = {"IN-TN-01": "Hosur", "IN-KA-01": "Bengaluru"}
_lock = coord.Mutex("reports", ttl=300, wait=300)


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def daily(plant: str, day: date | None = None) -> dict:
    """Today's brief for one plant: from the plan in use and the execution board."""
    day = day or date.today()
    s, _ = incidents._base()
    b = execution.board(plant, day.isoformat())
    jobs = b["jobs"]
    mine = [a for a in s.assignments if a.plant_id == plant]
    with SessionLocal() as db:
        probs = [i.description for i in db.query(Incident).filter_by(status="OPEN", plant_id=plant).limit(5)]
        risks = [n.text for n in db.query(PlanNote).filter_by(schedule_id=s.id, kind="risk", status="OPEN").limit(5)]
    late = [a for a in mine if a.status == "LATE_RISK"]
    waits = [a for a in mine if any(r.startswith("WAITS_FOR_PURCHASE") for r in a.reason_codes)]
    return {"kind": "daily", "plant_id": plant, "plant": PLANT.get(plant, plant), "day": b["day"] or day.isoformat(), "plan": s.name, "plan_id": s.id,
            "kpis": [("Jobs today", len(jobs)), ("Units today", sum(j["qty"] for j in jobs)), ("Done", b["counts"].get("DONE", 0)),
                     ("Blocked", b["counts"].get("BLOCKED", 0))],
            "sections": [("Today's jobs, in order", [(f"{j['line_id']} · {j['customer_order_id']}", f"{j['product_id']} · {j['qty']:,} units · {j['progress'].replace('_', ' ').lower()}")
                                                     for j in jobs[:12]]),
                         ("Worth watching", [("Late in this plan", f"{len(late)} jobs"), ("Waiting for bought parts", f"{len(waits)} jobs"),
                                             ("Open floor problems", str(len(probs)))] + [("Risk", r) for r in risks]),
                         ("Floor problems", [("Open", p) for p in probs])],
            "link": "Plan"}


def weekly(day: date | None = None) -> dict:
    """The week's report for the production managers."""
    day = day or date.today()
    s, _ = incidents._base()
    k = planning_tools.kpis(s.id)
    rc = s.summary.get("rough_cut") or []
    approved = [r for r in signoff.approved(20) if r.get("fully_approved_at") and str(r["fully_approved_at"])[:10] >= (day - timedelta(days=7)).isoformat()]
    with SessionLocal() as db:
        risks = db.query(PlanNote).filter_by(kind="risk", status="OPEN").count()
        probs = db.query(Incident).filter_by(status="OPEN").count()
    fmt = lambda it: "–" if it["value"] is None else f"{it['value']}%" if it["unit"] == "%" else f"{it['value']} {it['unit']}"
    return {"kind": "weekly", "week": _monday(day).isoformat(), "plan": s.name, "plan_id": s.id,
            "kpis": [(it["label"], fmt(it)) for it in k["items"][:4]],
            "sections": [("Success measures", [(it["label"], fmt(it)) for it in k["items"]]),
                         ("Capacity, next weeks", [(f"Week of {r['week']}", f"demand {r['load_pct']}% of final assembly") for r in rc[:6]]),
                         ("This week", [("Plans fully approved", str(len(approved))), ("Open risks", str(risks)), ("Open floor problems", str(probs)),
                                        ("Jobs waiting for bought parts", str(s.summary.get("purchase_wait_jobs", 0)))])],
            "link": "Plan document"}


def html_of(r: dict) -> tuple[str, str, str]:
    """(subject, text, html) in the plan email's look: GWC mark, palette and font."""
    C, F, e = signoff.C, f"font-family:{signoff.FONT}", html.escape
    title = f"Daily brief · {r['plant']} · {r['day']}" if r["kind"] == "daily" else f"Weekly planning report · week of {r['week']}"
    link = f"{Settings().app_url.rstrip('/')}/#{r['link'].replace(' ', '%20')}"
    tx = [title, f"Plan in use: {r['plan']}", ""] + [f"{a}: {b}" for a, b in r["kpis"]]
    for h, rows in r["sections"]:
        if rows:
            tx += ["", h] + [f"  {a}: {b}" for a, b in rows]
    tx += ["", f"Open the app: {link}"]
    kp = "".join(f"<td width='25%' style='padding:4px'><div style='background:{C['soft']};border-radius:10px;padding:12px'><div style='{F};font-size:12px;color:{C['text2']}'>{e(str(a))}</div>"
                 f"<div style='{F};font-size:16px;font-weight:800;color:{C['navy']};margin-top:4px'>{e(str(b))}</div></div></td>" for a, b in r["kpis"])
    sec = "".join(f"<tr><td style='padding:16px 28px 0'><div style='{F};font-size:13px;font-weight:700;color:{C['navy']};margin-bottom:4px'>{e(h)}</div>"
                  "<table role='presentation' width='100%' cellpadding='0' cellspacing='0'>"
                  + "".join(f"<tr><td style='{F};font-size:13px;color:{C['navy']};padding:7px 0;border-top:1px solid {C['line']}'>{e(str(a))}</td>"
                            f"<td align='right' style='{F};font-size:13px;color:{C['text2']};padding:7px 0;border-top:1px solid {C['line']}'>{e(str(b))}</td></tr>" for a, b in rows)
                  + "</table></td></tr>" for h, rows in r["sections"] if rows)
    h = f"""<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;background:{C['bg']}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{C['bg']}"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid {C['line']}">
<tr><td style="height:5px;line-height:5px;font-size:0;background:{C['purple']}"><span style="display:inline-block;width:30%;height:5px;background:{C['orange']}"></span></td></tr>
<tr><td style="padding:20px 28px 0"><table role="presentation" cellpadding="0" cellspacing="0"><tr><td style="padding-right:10px"><img src="cid:gwcmark" width="36" height="36" alt="GWC DATA.AI" style="display:block"></td>
<td><div style="{F};font-size:16px;font-weight:800;color:{C['purple']}">GWC <span style="color:{C['navy']}">DATA.&#8203;AI</span></div><div style="{F};font-size:12px;font-weight:700;color:{C['orange']};letter-spacing:2px">PRODUCTION PLANNING</div></td></tr></table></td></tr>
<tr><td style="padding:20px 28px 0"><div style="{F};font-size:16px;font-weight:800;color:{C['navy']}">{e(title)}</div><div style="{F};font-size:13px;color:{C['text2']};margin-top:4px">Plan in use: {e(r['plan'])}</div></td></tr>
<tr><td style="padding:14px 24px 0"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{kp}</tr></table></td></tr>{sec}
<tr><td style="padding:22px 28px"><a href="{e(link)}" style="display:inline-block;{F};font-size:14px;font-weight:700;color:#ffffff;background:{C['purple']};border-radius:8px;padding:11px 20px;text-decoration:none">Open the app</a></td></tr>
<tr><td style="padding:14px 28px;background:{C['bg']};{F};font-size:12px;color:{C['muted']}">Sent automatically every {'day' if r['kind'] == 'daily' else 'week'}. The planning agents propose; people decide.</td></tr>
</table></td></tr></table></body></html>"""
    return title, "\n".join(tx), h


def _recipients(r: dict) -> list[TeamMember]:
    with SessionLocal() as db:
        team = db.query(TeamMember).all()
        db.expunge_all()
    if r["kind"] == "daily":
        return [m for m in team if m.role == "plant_head" and m.plant_id == r["plant_id"]]
    return [m for m in team if m.role == "production_manager"]


def send(r: dict, to: str | None = None) -> dict:
    """Send one report: to `to` only (a preview), else to its people, with a bell notification each."""
    subject, text, body = html_of(r)
    people = [TeamMember(name=to, role="preview", email=to)] if to else _recipients(r)
    res = {"to": len(people), "sent": 0, "failed": 0, "email": notify.configured()}
    if not to:
        with SessionLocal() as db:
            for m in people or [None]:
                db.add(Notification(role=m.role if m else ("plant_head" if r["kind"] == "daily" else "production_manager"),
                                    plant_id=r.get("plant_id"), member=m.name if m else None, email=m.email if m else None, severity="info",
                                    title=subject[:200], body=text.split("\n", 3)[-1][:1500], link=r["link"],
                                    email_status="SENT" if notify.configured() and m and m.email else "NO_EMAIL"))
            db.commit()
    if notify.configured() and any(m.email for m in people):
        c = notify._smtp()
        try:
            for m in people:
                if not m.email:
                    continue
                msg = EmailMessage()
                msg["Subject"], msg["From"], msg["To"] = ("[Preview] " if to else "") + subject, Settings().smtp_from, m.email
                msg.set_content(text)
                msg.add_alternative(body, subtype="html")
                with open(signoff.LOGO, "rb") as fh:
                    msg.get_payload()[1].add_related(fh.read(), maintype="image", subtype="png", cid="<gwcmark>")
                try:
                    c.send_message(msg)
                    res["sent"] += 1
                except Exception:
                    res["failed"] += 1
        finally:
            try:
                c.quit()
            except Exception:
                pass
    return res


def due(now: datetime | None = None) -> list[tuple[str, dict]]:
    """Reports due now that haven't gone out yet: (key, report)."""
    now, pol = now or datetime.now(), Policy()
    if not pol.reports_enabled or now.hour < pol.report_daily_hour:
        return []
    with SessionLocal() as db:
        sent = {k for (k,) in db.query(ReportLog.key)}
    out = []
    for p in PLANT:
        k = f"daily:{now.date()}:{p}"
        if k not in sent:
            out.append((k, daily(p, now.date())))
    if now.weekday() == pol.report_weekly_day and f"weekly:{now.date()}" not in sent:
        out.append((f"weekly:{now.date()}", weekly(now.date())))
    return out


def run_due(now: datetime | None = None) -> list[str]:
    done = []
    with _lock:
        for k, r in due(now):
            res = send(r)
            with SessionLocal() as db:
                db.add(ReportLog(key=k, kind=r["kind"], detail=res))
                db.commit()
            done.append(k)
            log.info("report %s sent: %s", k, res)
    return done


def start() -> None:
    coord.leader_loop("reports", 600, run_due)                          # every 10 minutes, in one process

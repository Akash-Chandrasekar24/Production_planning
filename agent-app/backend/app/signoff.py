"""Approval stages of a plan, and the plan email.

Stage 1: the production manager approves the plan (Review & approve). Stage 2: the production head of every plant that has work in the plan
approves it for their plant. When the last stage is done the plan is fully approved: it joins the approved plans (admins and production
heads see them) and shows in its own colour on the Plan page.
The plan email goes out once, when the production manager approves: to the production heads of the plan's plants and the senior
officials (production managers). Never to admins or engineers. Without a mail server (SMTP_*) it is recorded as not sent and can be
sent again later.
"""
import csv
import html
import io
import logging
import os
import threading
from collections import defaultdict
from email.message import EmailMessage

from . import coord, notify, planning, store
from .config import Settings
from .db import Notification, SessionLocal, TeamMember, _now

log = logging.getLogger("planner.signoff")
# the last two plant approvals can't both "complete" the plan: one approval per plan at a time, across processes (coord.Mutex)
ROLE = {"production_manager": "Production manager", "plant_head": "Production head"}
TO_ROLES = ("production_manager",)                 # senior officials; production heads of the plan's plants are added; never admin


def plants(s) -> list[str]:
    return sorted({a.plant_id for a in s.assignments})


def stages(s, ev: list[dict] | None = None) -> dict:
    """Where the plan stands: one row per stage (who approved it and when), fully approved or not, and the email result."""
    ev = store.events(s.id) if ev is None else ev
    pm = next((e for e in reversed(ev) if e["action"] == "approved"), None)
    got = {e["detail"].get("plant"): e for e in ev if e["action"] == "plant_approved"}
    rows = [{"stage": "production_manager", "label": "Production manager", "plant": None, "done": bool(pm) and s.status in ("APPROVED", "PUBLISHED"),
             "by": pm and pm["actor"], "at": pm and pm["ts"]}]
    for p in plants(s):
        e = got.get(p)
        rows.append({"stage": "plant_head", "label": f"Production head · {p}", "plant": p, "done": bool(e), "by": e and e["actor"], "at": e and e["ts"]})
    full = next((e for e in ev if e["action"] == "fully_approved"), None)
    mail = next((e for e in reversed(ev) if e["action"] == "plan_emailed"), None)
    return {"id": s.id, "name": s.name, "status": s.status, "stages": rows, "fully_approved": bool(full), "fully_approved_at": full and full["ts"],
            "email": mail and mail["detail"]}


def approve_plant(sid: str, user: str, role: str, plant: str | None) -> dict:
    """A production head approves an approved plan for their plant. The last stage completes the plan and sends the email."""
    s = store.get(sid)
    if not s:
        raise KeyError(sid)
    if s.status not in ("APPROVED", "PUBLISHED"):
        raise ValueError("The production manager approves the plan first")
    if not plant or plant not in plants(s):
        raise ValueError("This plan has no work for your plant")
    with coord.Mutex(f"signoff:{sid}", ttl=120, wait=120):
        st = stages(s)
        if any(r["plant"] == plant and r["done"] for r in st["stages"]):
            raise ValueError(f"{plant} has already approved this plan")
        store.audit(sid, user, role, "plant_approved", {"plant": plant})
        coord.changed("planlist")                                  # approval stages and "fully approved" show at once
        st = stages(s)
        if all(r["done"] for r in st["stages"]) and not st["fully_approved"]:
            store.audit(sid, user, role, "fully_approved", {"stages": [{k: r[k] for k in ("label", "by", "at")} for r in st["stages"]]})
            st = stages(s)
    return st


def approved(limit: int = 20) -> list[dict]:
    """Approved plans (newest first) with their stages; what-ifs nobody added to the plan are left out."""
    out = []
    for r in store.list_(100):
        if r["status"] in ("APPROVED", "PUBLISHED") and not store.is_what_if(r):
            s = store.get(r["id"])
            if s:
                out.append({**stages(s), "created_at": r["created_at"], "summary": r["summary"]})
        if len(out) >= limit:
            break
    return out


APPROVAL_EVENTS = ("approved", "plan_option_chosen", "plant_approved", "fully_approved", "plan_emailed")


def remove_approval(sid: str, user: str) -> dict:
    """Delete a plan's approval: the plan is closed (it is no longer the approved plan), its approval history entries are removed,
    the orders it froze can be changed again, and the work it released is withdrawn. Returns what changed."""
    from .db import AgentTask, OrderFreeze, PlanSet
    s = store.get(sid)
    if s is None:
        raise KeyError(sid)
    if s.status not in ("APPROVED", "PUBLISHED"):
        raise ValueError("This plan is not approved")
    store._run("UPDATE schedules SET status='REJECTED', decided_by=?, decided_at=?, decision_note=? WHERE id=?",
               (user, store._now(), f"Approval deleted by {user}", sid))
    ph = ",".join("?" * len(APPROVAL_EVENTS))
    _, n_ev = store._run(f"DELETE FROM events WHERE schedule_id=? AND action IN ({ph})", (sid, *APPROVAL_EVENTS))
    with SessionLocal() as db:
        n_fr = db.query(OrderFreeze).filter_by(schedule_id=sid).delete(synchronize_session=False)
        n_t = 0
        for t in db.query(AgentTask).filter(AgentTask.schedule_id == sid, AgentTask.status.notin_(("DONE", "CANCELLED"))):
            t.status, t.note, t.updated_by = "CANCELLED", f"Approval deleted by {user}", user
            n_t += 1
        for p in db.query(PlanSet).filter_by(chosen=sid):
            p.status = "SUPERSEDED"
        db.commit()
    coord.changed("plans")
    coord.changed("planlist")
    log.info("approval of plan %s deleted by %s", sid, user)
    return {"id": sid, "history_entries": n_ev, "orders_unfrozen": n_fr, "tasks_withdrawn": n_t}


# ------------------------------------------------------------------ the email
def _people(s) -> list[TeamMember]:
    ps = set(plants(s))
    with SessionLocal() as db:
        team = db.query(TeamMember).all()
        db.expunge_all()
    seen, out = set(), []
    for m in team:
        if m.email and (m.role in TO_ROLES or (m.role == "plant_head" and m.plant_id in ps)) and m.email.lower() not in seen:
            seen.add(m.email.lower())
            out.append(m)
    return out


def _n(v) -> str:
    return "–" if v is None else f"{v:,.0f}" if isinstance(v, (int, float)) else str(v)


def _inr(v) -> str:
    if v is None:
        return "–"
    return f"₹{v / 1e7:,.2f} Cr" if v >= 1e7 else f"₹{v / 1e5:,.2f} L" if v >= 1e5 else f"₹{v:,.0f}"


def _t(v) -> str:
    return v.strftime("%d %b %H:%M") if hasattr(v, "strftime") else str(v or "")


def _when(v) -> str:
    """'2026-11-19T10:51:03' -> '19 Nov, 10:51'."""
    import datetime as dt
    try:
        return dt.datetime.fromisoformat(str(v)[:19]).strftime("%d %b, %H:%M")
    except ValueError:
        return str(v or "")


PLANT = {"IN-TN-01": "Hosur", "IN-KA-01": "Bengaluru"}
LOGO = os.path.join(os.path.dirname(__file__), "assets", "gwc-mark.png")
# the app's palette and font (theme.css); email clients that can't load Montserrat fall back to Segoe UI / Arial
C = {"purple": "#6f3dba", "orange": "#f58a3a", "navy": "#1f2f59", "text2": "#54627e", "muted": "#98a2b3", "line": "#e7eaf1",
     "bg": "#f5f7fa", "soft": "#f3eeff", "peach": "#fff2eb", "ok": "#1f9d6b", "okbg": "#e8f7f0"}
FONT = "Montserrat,Segoe UI,Arial,sans-serif"                 # no quotes: it goes inside single-quoted style attributes


def content(s, st: dict, shortages: list[dict]) -> tuple[str, str, str]:
    """(subject, plain text, HTML) for the chosen plan: a short summary. Every job, short material and unscheduled order is in the CSVs."""
    S, e = s.summary, html.escape
    subject = f"Plan approved: {s.name}"
    at = _when(st.get("fully_approved_at")) if st.get("fully_approved_at") else ""
    kpis = [("On-time delivery", f"{S['on_time_qty_pct']}%" if S.get("on_time_qty_pct") is not None else "–"),
            ("Units scheduled", _n(S.get("scheduled_qty"))), ("Late jobs", _n(S.get("late_jobs"))), ("Material cost", _inr(S.get("material_cost_inr")))]
    plants = []
    for p in sorted({a.plant_id for a in s.assignments}):
        js = [a for a in s.assignments if a.plant_id == p]
        plants.append({"name": f"{PLANT.get(p, p)} · {p}", "jobs": len(js), "units": sum(a.qty for a in js),
                       "lines": len({a.smt_line for a in js} | {a.fal_line for a in js}),
                       "from": min(a.smt_start for a in js).strftime("%d %b"), "to": max(a.fal_finish for a in js).strftime("%d %b")})
    watch = []
    if S.get("late_jobs"):
        watch.append(f"{_n(S['late_jobs'])} jobs may finish after their due date")
    if shortages:
        watch.append(f"{len(shortages)} materials are short ({', '.join(m['material_id'] for m in shortages[:3])}{'…' if len(shortages) > 3 else ''})")
    if S.get("unscheduled_qty"):
        watch.append(f"{_n(S['unscheduled_qty'])} units could not be scheduled")
    files = ["every job, line by line"] + (["the materials that are short"] if shortages else []) + (["the orders not scheduled"] if s.unscheduled else [])
    link = f"{Settings().app_url.rstrip('/')}/#Plan"

    tx = [f"Plan approved: {s.name}", f"Every approval stage signed off this plan{' on ' + at if at else ''}.", ""]
    tx += [f"{k}: {v}" for k, v in kpis] + [""]
    tx += [f"{p['name']}: {p['jobs']} jobs, {p['units']:,} units on {p['lines']} lines, {p['from']} – {p['to']}" for p in plants] + [""]
    tx += ["Approved by:"] + [f"  {r['label']}: {r['by'] or 'waiting'}{' · ' + _when(r['at']) if r['at'] else ''}" for r in st["stages"]]
    if watch:
        tx += ["", "Worth watching:"] + [f"  - {w}" for w in watch]
    tx += ["", f"Attached (CSV): {', '.join(files)}.", f"Open the plan: {link}"]

    f = f"font-family:{FONT}"
    kp = "".join(f"<td width='25%' style='padding:4px'><div style='background:{C['soft']};border-radius:10px;padding:12px'>"
                 f"<div style='{f};font-size:12px;color:{C['text2']}'>{e(k)}</div>"
                 f"<div style='{f};font-size:16px;font-weight:800;color:{C['navy']};margin-top:4px'>{e(v)}</div></div></td>" for k, v in kpis)
    pl = "".join(f"<tr><td style='{f};font-size:13px;font-weight:700;color:{C['navy']};padding:10px 0;border-top:1px solid {C['line']}'>{e(p['name'])}</td>"
                 f"<td align='right' style='{f};font-size:13px;color:{C['text2']};padding:10px 0;border-top:1px solid {C['line']}'>"
                 f"{p['jobs']} jobs · {p['units']:,} units · {p['from']} – {p['to']}</td></tr>" for p in plants)
    ap = "".join(f"<tr><td width='26' style='padding:6px 0;vertical-align:top'><span style='display:inline-block;width:18px;height:18px;line-height:18px;border-radius:9px;"
                 f"text-align:center;font-size:12px;color:#fff;background:{C['ok'] if r['done'] else C['muted']}'>{'&#10003;' if r['done'] else '&middot;'}</span></td>"
                 f"<td style='{f};font-size:13px;color:{C['navy']};padding:6px 0'><b>{e(r['label'])}</b>"
                 f"<span style='color:{C['text2']}'> · {e(r['by'] or 'waiting')}{' · ' + e(_when(r['at'])) if r['at'] else ''}</span></td></tr>" for r in st["stages"])
    wt = ("<tr><td style='padding:18px 28px 0'><div style='background:" + C["peach"] + ";border-left:4px solid " + C["orange"] + ";border-radius:8px;padding:12px 14px'>"
          f"<div style='{f};font-size:13px;font-weight:700;color:{C['navy']};margin-bottom:4px'>Worth watching</div>"
          + "".join(f"<div style='{f};font-size:13px;color:{C['text2']};line-height:1.6'>&bull; {e(w)}</div>" for w in watch) + "</div></td></tr>") if watch else ""
    h = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>body,td,div,a{{font-family:{FONT}}}</style></head>
<body style="margin:0;padding:0;background:{C['bg']}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{C['bg']}"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid {C['line']}">
<tr><td style="height:5px;line-height:5px;font-size:0;background:{C['purple']}"><span style="display:inline-block;width:30%;height:5px;background:{C['orange']}"></span></td></tr>
<tr><td style="padding:20px 28px 0"><table role="presentation" cellpadding="0" cellspacing="0"><tr>
  <td style="padding-right:10px"><img src="cid:gwcmark" width="36" height="36" alt="GWC DATA.AI" style="display:block"></td>
  <td><div style="{f};font-size:16px;font-weight:800;color:{C['purple']};letter-spacing:.3px">GWC <span style="color:{C['navy']}">DATA.&#8203;AI</span></div>
      <div style="{f};font-size:12px;font-weight:700;color:{C['orange']};letter-spacing:2px">PRODUCTION PLANNING</div></td></tr></table></td></tr>
<tr><td style="padding:22px 28px 0">
  <span style="display:inline-block;{f};font-size:12px;font-weight:700;color:{C['ok']};background:{C['okbg']};border-radius:999px;padding:4px 10px">&#10003; Approved by the production manager</span>
  <div style="{f};font-size:16px;font-weight:800;color:{C['navy']};margin-top:10px">{e(s.name)}</div>
  <div style="{f};font-size:13px;color:{C['text2']};margin-top:4px;line-height:1.5">The production manager approved this plan{' on ' + e(at) if at else ''}. Each plant's production head now confirms it for their plant.</div></td></tr>
<tr><td style="padding:16px 24px 0"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{kp}</tr></table></td></tr>
<tr><td style="padding:18px 28px 0"><div style="{f};font-size:13px;font-weight:700;color:{C['navy']};margin-bottom:4px">By plant</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{pl}</table></td></tr>
<tr><td style="padding:16px 28px 0"><div style="{f};font-size:13px;font-weight:700;color:{C['navy']};margin-bottom:2px">Approved by</div>
  <table role="presentation" cellpadding="0" cellspacing="0">{ap}</table></td></tr>
{wt}
<tr><td style="padding:22px 28px 6px"><a href="{e(link)}" style="display:inline-block;{f};font-size:14px;font-weight:700;color:#ffffff;background:{C['purple']};border-radius:8px;padding:11px 20px;text-decoration:none">Open the plan</a></td></tr>
<tr><td style="padding:10px 28px 22px;{f};font-size:12px;color:{C['text2']};line-height:1.5">Attached as CSV: {e(', '.join(files))}.</td></tr>
<tr><td style="padding:14px 28px;background:{C['bg']};{f};font-size:12px;color:{C['muted']}">The planning agents propose; people decide. This message was sent automatically.</td></tr>
</table></td></tr></table></body></html>"""
    return subject, "\n".join(tx), h


def _table(rows: list[dict], cols: list[str]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


def _csv(s) -> bytes:
    return _table(planning.dispatch_list(s), ["plant_id", "line_id", "seq", "stage", "customer_order_id", "product_id", "qty", "start", "finish",
                                               "setup_min", "priority", "urgent", "status"])


def attachments(s, shortages: list[dict]) -> list[tuple[str, bytes]]:
    """The detail behind the email, as CSV files."""
    out = [(f"plan-{s.id}-jobs.csv", _csv(s))]
    if shortages:
        out.append((f"plan-{s.id}-materials-short.csv", _table(shortages, ["material_id", "required_qty", "available_qty", "scheduled_consumption",
                                                                          "shortfall_qty", "safety_stock", "shortfall_cost_inr"])))
    if s.unscheduled:
        out.append((f"plan-{s.id}-not-scheduled.csv", _table([u.model_dump() for u in s.unscheduled], list(s.unscheduled[0].model_dump()))))
    return out


def message(s, st: dict, shortages: list[dict], to: str, subject_prefix: str = "") -> EmailMessage:
    """The email, ready to send: summary, the GWC mark inline, and the CSVs."""
    subject, text, body = content(s, st, shortages)
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject_prefix + subject, Settings().smtp_from, to
    msg.set_content(text)
    msg.add_alternative(body, subtype="html")
    with open(LOGO, "rb") as fh:
        msg.get_payload()[1].add_related(fh.read(), maintype="image", subtype="png", cid="<gwcmark>")
    for name, data in attachments(s, shortages):
        msg.add_attachment(data, maintype="text", subtype="csv", filename=name)
    return msg


def email_plan(sid: str) -> dict:
    """Email the plan the production manager approved to the production heads and senior officials; one bell notification each.
    Returns counts (never credentials)."""
    s = store.get(sid)
    if not s:
        return {"sent": 0, "failed": 0, "to": 0}
    st = stages(s)
    try:
        short = [m for m in planning.material_calloff(store.inputs(sid), s) if m["shortfall_qty"] > 0 and m["scheduled_consumption"] > 0]
    except Exception:                                                     # the email still goes out without the material table
        log.exception("material shortfall for the plan email failed")
        short = []
    text = content(s, st, short)[1]
    people, smtp = _people(s), notify.configured()
    res = {"sent": 0, "failed": 0, "to": len(people), "status": "NOT_CONFIGURED" if not smtp else "NO_EMAIL" if not people else "SENT"}
    with SessionLocal() as db:
        ns = []
        for m in people or [None]:
            n = Notification(role=m.role if m else "production_manager", plant_id=m.plant_id if m else None, member=m.name if m else None,
                             email=m.email if m else None, severity="info", title=f"Plan approved: {s.name}"[:200], link="Plan document",
                             body=f"The production manager approved the plan. {text.split(chr(10), 1)[0]}",
                             email_status="PENDING" if smtp and m else res["status"] if m else "NO_EMAIL")
            db.add(n)
            ns.append(n)
        db.commit()
        if smtp and people:
            try:
                c = notify._smtp()
            except Exception as ex:
                for n in ns:
                    n.email_status, n.email_error = "FAILED", f"Mail server: {type(ex).__name__}"
                res.update(failed=len(ns), status="FAILED")
                c = None
            if c:
                try:
                    for n in ns:
                        try:
                            c.send_message(message(s, st, short, n.email))
                            n.email_status, n.emailed_at, n.email_error = "SENT", _now(), None
                            res["sent"] += 1
                        except Exception as ex:
                            n.email_status, n.email_error = "FAILED", type(ex).__name__
                            res["failed"] += 1
                finally:
                    try:
                        c.quit()
                    except Exception:
                        pass
                res["status"] = "SENT" if res["sent"] and not res["failed"] else "PARTLY_SENT" if res["sent"] else "FAILED"
        db.commit()
    store.audit(sid, "planning agent", "agent", "plan_emailed", {**res, "recipients": [m.name for m in people]})
    return res

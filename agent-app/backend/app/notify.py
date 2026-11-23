"""Notifications: every task the agents hand to a person becomes a notification in the app (the bell) and, when the person has an
email address and a mail server is set up (SMTP_*), an email.

Hooked to the database session, so it covers every place that creates tasks (planning agent, recovery, orchestrator alerts) and
the moment a waiting task is released (a plan approved -> the plant heads' "prepare" tasks open). Tasks waiting for approval are
not notified until they are released. Emails go out in the background; the result (SENT / FAILED / NO_EMAIL / NOT_CONFIGURED) is
kept on the notification. Credentials are never logged or shown.
"""
import logging
import smtplib
import threading
from sqlalchemy import text
from email.message import EmailMessage
from sqlalchemy import event, inspect
from .config import Settings
from .db import AgentTask, Notification, SessionLocal, TeamMember, _now

log = logging.getLogger("planner.notify")
ROLE = {"production_manager": "Production manager", "plant_head": "Production head", "engineer": "Engineer", "planner": "Planner"}
KIND = {"approve": "Decision needed", "review": "For review", "prepare": "Prepare your plant", "repair": "Fix needed", "alert": "Alert"}


def configured() -> bool:
    s = Settings()
    return bool(s.smtp_host and s.smtp_from)


def _severity(t: AgentTask) -> str:
    d = t.details or {}
    return d.get("severity") or {"repair": "critical", "approve": "warn"}.get(t.kind, "info")


def _body(t: AgentTask, released: bool) -> str:
    d, out = t.details or {}, []
    if released:
        out.append("The plan was approved: you can start preparing now.")
    for k, label in (("why", "Why"), ("headline", "Problem"), ("option", "Recovery"), ("agent", "From")):
        if d.get(k):
            out.append(f"{label}: {d[k] if k != 'agent' else 'the ' + d[k]}")
    if t.kind == "prepare" and d.get("jobs") is not None:
        out.append(f"{d['jobs']} jobs, {d.get('units', 0):,} units" + (f", {d['late_jobs']} late" if d.get("late_jobs") else ""))
    for m in (d.get("materials") or [])[:5]:
        out.append(f"{m.get('material_id')}: " + ", ".join(x for x in (f"{m['cover_days']} days of cover" if m.get("cover_days") is not None else "",
                                                                         f"short {m['shortfall']:,}" if m.get("shortfall") else "") if x))
    for f in (d.get("findings") or [])[:3]:
        out.append(f)
    if t.note:
        out.append(f"Note: {t.note}")
    return "\n".join(out)


def _collect(session, _ctx):
    new = session.info.setdefault("notify", [])
    for o in session.new:
        if isinstance(o, AgentTask) and o.status == "OPEN":
            new.append((o, False))
    for o in session.dirty:
        if isinstance(o, AgentTask):
            h = inspect(o).attrs.status.history
            if h.deleted and h.deleted[0] == "WAITING_APPROVAL" and h.added and h.added[0] == "OPEN":
                new.append((o, True))


def _after_commit(session):
    items = session.info.pop("notify", [])
    if items:
        try:
            create([(t.id, t.role, t.plant_id, t.title, _severity(t), _body(t, rel), t.kind) for t, rel in items])
        except Exception:                                            # a notification problem must never undo the agents' work
            log.exception("creating notifications failed")


def _after_rollback(session):
    session.info.pop("notify", None)


event.listen(SessionLocal, "after_flush", _collect)
event.listen(SessionLocal, "after_commit", _after_commit)
event.listen(SessionLocal, "after_soft_rollback", lambda s, _t: _after_rollback(s))


def create(items: list[tuple]) -> list[str]:
    """One notification per person in the role (and plant), or one for the role when no one is named yet. Returns the ids.
    In the app only: the agents' alerts and tasks are never emailed. The one email is the plan report the production manager's
    approval sends (signoff.email_plan)."""
    ids = []
    with SessionLocal() as s:
        team = s.query(TeamMember).all()
        for task_id, role, plant, title, severity, body, kind in items:
            people = [m for m in team if m.role == role and (role != "plant_head" or m.plant_id == plant)] or [None]
            for m in people:
                email = m.email if m else None
                n = Notification(task_id=task_id, role=role, plant_id=plant, member=m.name if m else None, email=email, severity=severity,
                                 title=f"{KIND.get(kind, 'Task')}: {title}"[:200], body=body,
                                 email_status="IN_APP")
                s.add(n)
                s.flush()
                ids.append(n.id)
        s.commit()
    return ids


def _message(n: Notification) -> EmailMessage:
    s = Settings()
    link = f"{s.app_url.rstrip('/')}/#{(n.link if n.link and n.link != 'Planning agent' else 'Review & approve').replace(' ', '%20')}"   # column defaults apply only once saved
    msg = EmailMessage()
    msg["Subject"] = f"[Production planning] {n.title}"
    msg["From"] = s.smtp_from
    msg["To"] = n.email
    who = f"{ROLE.get(n.role, n.role)}" + (f" · {n.plant_id}" if n.plant_id else "")
    msg.set_content(f"{n.title}\n\n{n.body}\n\nFor: {who}\nOpen it in the app: {link}\n\n"
                    "The planning agents propose; people decide. This message was sent automatically.")
    esc = lambda x: x.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    lines = "".join(f"<p style='margin:4px 0'>{esc(x)}</p>" for x in n.body.splitlines() if x)
    msg.add_alternative(f"<div style='font-family:Arial,sans-serif;font-size:14px'><h3 style='margin:0 0 8px'>{esc(n.title)}</h3>{lines}"
                        f"<p style='margin:12px 0;color:#555'>For: {esc(who)}</p><p><a href='{link}'>Open it in the app</a></p>"
                        f"<p style='color:#888;font-size:12px'>The planning agents propose; people decide. This message was sent automatically.</p></div>",
                        subtype="html")
    return msg


def _smtp():
    s = Settings()
    if s.smtp_ssl:
        c = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=20)
    else:
        c = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20)
        if s.smtp_starttls:
            c.starttls()
    if s.smtp_user and s.smtp_password:
        c.login(s.smtp_user, s.smtp_password.get_secret_value())
    return c


_send_lock = threading.Lock()


def send_pending() -> dict:
    """Email every notification still PENDING. Returns counts."""
    out = {"sent": 0, "failed": 0}
    with _send_lock, SessionLocal() as s:
        ids = [i for (i,) in s.query(Notification.id).filter_by(email_status="PENDING").order_by(Notification.created_at).limit(200)]
        mine = [i for i in ids if s.execute(text("UPDATE notifications SET email_status='SENDING' WHERE id=:i AND email_status='PENDING'"),
                                            {"i": i}).rowcount == 1]
        s.commit()
        todo = s.query(Notification).filter(Notification.id.in_(mine)).order_by(Notification.created_at).all() if mine else []
        if not todo:
            return out
        try:
            c = _smtp()
        except Exception as e:
            for n in todo:
                n.email_status, n.email_error = "FAILED", f"Mail server: {type(e).__name__}"
            s.commit()
            log.warning("mail server unavailable: %s", type(e).__name__)
            return {"sent": 0, "failed": len(todo)}
        try:
            for n in todo:
                try:
                    c.send_message(_message(n))
                    n.email_status, n.emailed_at, n.email_error = "SENT", _now(), None
                    out["sent"] += 1
                except Exception as e:
                    n.email_status, n.email_error = "FAILED", type(e).__name__
                    out["failed"] += 1
            s.commit()
        finally:
            try:
                c.quit()
            except Exception:
                pass
    return out


def test_email(to: str, name: str) -> str:
    """Send one test email straight away. Returns "SENT" or the error type (never the server's credentials)."""
    if not configured():
        return "NOT_CONFIGURED"
    n = Notification(role="", title="Test message from production planning", body=f"Hello {name}, notifications by email are working.", email=to)
    try:
        c = _smtp()
        try:
            c.send_message(_message(n))
        finally:
            c.quit()
        return "SENT"
    except Exception as e:
        return type(e).__name__


def out(n: Notification) -> dict:
    return {"id": n.id, "task_id": n.task_id, "role": n.role, "plant_id": n.plant_id, "member": n.member, "severity": n.severity, "title": n.title,
            "body": n.body, "link": n.link, "email_status": n.email_status, "emailed_at": n.emailed_at, "read": n.read_at is not None,
            "created_at": n.created_at}

"""Sign-in and user accounts (AUTH_MODE=password).

Passwords: salted scrypt hashes (stdlib), never stored or logged in clear. Sign-in returns a random token; only its SHA-256 is
stored (app_sessions), it expires after Settings.session_hours, and signing out deletes it. Repeated wrong passwords lock that
username for a few minutes. Admins create accounts and set each person's role (and plant for a production head).
"""
import datetime as dt
import hashlib
import hmac
import logging
import secrets
import time
from collections import defaultdict
from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from .config import Policy, Settings
from .db import SessionLocal, User, UserSession
from .errors import ApiError

log = logging.getLogger("planner.users")
router = APIRouter(prefix="/api")
ROLE_LABEL = {"engineer": "Engineer", "production_manager": "Production manager", "plant_head": "Production head", "admin": "Admin"}
PLANTS = ("IN-TN-01", "IN-KA-01")
MAX_FAILS, LOCK_S = 5, 300
_fails: dict[str, list[float]] = defaultdict(list)       # ponytail: in-process lockout, move to the database if several servers run


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        return hmac.compare_digest(hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1).hex(), h)
    except ValueError:
        return False


def _now():
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _out(u: User) -> dict:
    return {"id": u.id, "username": u.username, "name": u.name, "role": u.role, "role_label": ROLE_LABEL.get(u.role, u.role),
            "plant_id": u.plant_id, "active": u.active, "last_login": u.last_login}


def ensure_admin() -> None:
    """First start: when there are no accounts and ADMIN_PASSWORD is set, create the admin account from it."""
    st = Settings()
    with SessionLocal() as s:
        if s.query(User).count():
            return
        if not st.admin_password:
            log.warning("no user accounts yet: set ADMIN_PASSWORD in .env and restart to create the first admin")
            return
        s.add(User(username=st.admin_username.lower(), name="Administrator", role="admin", password_hash=hash_password(st.admin_password)))
        s.commit()
        log.info("created the first admin account")


_seen: dict = {}                       # token hash -> (valid until, user): a signed-in user is checked against the database once a minute
SEEN_S = 60


def forget_sessions() -> None:
    """Sign-outs, password and access changes take effect at once."""
    _seen.clear()


def session_user(token: str) -> User | None:
    if not token:
        return None
    k, now = _sha(token), time.monotonic()
    hit = _seen.get(k)
    if hit and hit[0] > now:
        return hit[1]
    with SessionLocal() as s:
        row = s.get(UserSession, k)
        if not row or row.expires_at < _now():
            return None
        u = s.get(User, row.user_id)
        if not u or not u.active:
            return None
        left = (row.expires_at - _now()).total_seconds()
        if len(_seen) > 5000:
            _seen.clear()
        _seen[k] = (now + min(SEEN_S, max(0, left)), u)
        return u


# ------------------------------------------------------------------ sign-in
class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


@router.post("/auth/login")
def login(body: LoginIn):
    name = body.username.strip().lower()
    now = time.monotonic()
    _fails[name] = [t for t in _fails[name] if now - t < LOCK_S]
    if len(_fails[name]) >= MAX_FAILS:
        raise ApiError(429, "TOO_MANY_ATTEMPTS", "Too many wrong passwords. Please wait a few minutes and try again.")
    with SessionLocal() as s:
        u = s.query(User).filter_by(username=name).first()
        if not u or not u.active or not check_password(body.password, u.password_hash):
            _fails[name].append(now)
            raise ApiError(401, "BAD_LOGIN", "That username and password don't match.")
        _fails.pop(name, None)
        token = secrets.token_urlsafe(32)
        s.query(UserSession).filter(UserSession.expires_at < _now()).delete()          # tidy expired sign-ins
        s.add(UserSession(token_hash=_sha(token), user_id=u.id, expires_at=_now() + dt.timedelta(hours=Settings().session_hours)))
        u.last_login = _now()
        s.commit()
        return {"token": token, "user": _out(u)}


def _bearer(authorization: str = Header("")) -> str:
    return authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""


@router.get("/auth/me")
def me(token: str = Depends(_bearer)):
    u = session_user(token)
    if not u:
        raise ApiError(401, "UNAUTHENTICATED", "Please sign in.")
    return _out(u)


@router.post("/auth/logout")
def logout(token: str = Depends(_bearer)):
    with SessionLocal() as s:
        s.query(UserSession).filter_by(token_hash=_sha(token)).delete()
        forget_sessions()
        s.commit()
    return {"ok": True}


class PasswordIn(BaseModel):
    current: str = Field(min_length=1, max_length=200)
    new: str = Field(min_length=8, max_length=200)


@router.post("/auth/password")
def change_password(body: PasswordIn, token: str = Depends(_bearer)):
    u = session_user(token)
    if not u:
        raise ApiError(401, "UNAUTHENTICATED", "Please sign in.")
    if not check_password(body.current, u.password_hash):
        raise ApiError(400, "BAD_PASSWORD", "Your current password is not right.")
    with SessionLocal() as s:
        s.get(User, u.id).password_hash = hash_password(body.new)
        s.query(UserSession).filter(UserSession.user_id == u.id, UserSession.token_hash != _sha(token)).delete()   # sign out elsewhere
        forget_sessions()
        s.commit()
    return {"ok": True}


# ------------------------------------------------------------------ accounts (admin)
def _admin(token: str = Depends(_bearer)):
    from .auth import principal_from_token
    p = principal_from_token(token)
    if "users" not in Policy().role_actions.get(p.role, ()):
        raise ApiError(403, "FORBIDDEN", "Only an admin can manage accounts.")
    return p


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    name: str = Field(min_length=1, max_length=100)
    role: str
    plant_id: str | None = None
    password: str = Field(min_length=8, max_length=200)


class UserPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    role: str | None = None
    plant_id: str | None = None
    active: bool | None = None
    password: str | None = Field(None, min_length=8, max_length=200)


def _check_role(role, plant):
    if role not in ROLE_LABEL:
        raise ApiError(422, "UNKNOWN_ROLE", f"Role must be one of {', '.join(ROLE_LABEL.values())}.")
    if role == "plant_head" and plant not in PLANTS:
        raise ApiError(422, "PLANT_REQUIRED", "A production head needs a plant.")


@router.get("/users")
def list_users(_=Depends(_admin)):
    with SessionLocal() as s:
        return [_out(u) for u in s.query(User).order_by(User.role, User.username)]


@router.post("/users", status_code=201)
def add_user(body: UserIn, _=Depends(_admin)):
    _check_role(body.role, body.plant_id)
    with SessionLocal() as s:
        name = body.username.lower()
        if s.query(User).filter_by(username=name).first():
            raise ApiError(409, "USERNAME_TAKEN", "That username is already used.")
        u = User(username=name, name=body.name.strip(), role=body.role, plant_id=body.plant_id if body.role == "plant_head" else None,
                 password_hash=hash_password(body.password))
        s.add(u)
        s.commit()
        return _out(u)


@router.patch("/users/{uid}")
def edit_user(uid: str, body: UserPatch, p=Depends(_admin)):
    with SessionLocal() as s:
        u = s.get(User, uid)
        if not u:
            raise ApiError(404, "USER_NOT_FOUND", "No such account.")
        role, plant = body.role or u.role, body.plant_id if body.plant_id is not None else u.plant_id
        _check_role(role, plant)
        if u.username == p.user and (role != "admin" or body.active is False):
            raise ApiError(400, "KEEP_ONE_ADMIN", "You can't remove your own admin access.")
        u.role, u.plant_id = role, plant if role == "plant_head" else None
        if body.name:
            u.name = body.name.strip()
        if body.active is not None:
            u.active = body.active
        if body.password:
            u.password_hash = hash_password(body.password)
        if body.password or body.active is False or body.role:
            s.query(UserSession).filter_by(user_id=u.id).delete()          # changed access: they sign in again
            forget_sessions()
        s.commit()
        return _out(u)

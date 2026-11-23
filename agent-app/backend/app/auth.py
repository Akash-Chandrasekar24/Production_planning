"""Authorization boundary.

AUTH_MODE=password (default): every request carries "Authorization: Bearer <token>" from POST /api/auth/login (app/users.py);
the role and plant come from that person's account, never from the browser.
AUTH_MODE=dev_headers (tests only): identity from X-User / X-Role / X-Plant headers. NOT authentication.
"""
import logging
from fastapi import Depends, Header
from pydantic import BaseModel
from .config import Policy, Settings
from .errors import ApiError

log = logging.getLogger("planner.auth")


class Principal(BaseModel):
    user: str
    role: str
    plant: str | None = None          # plant heads: which plant they lead (X-Plant header in dev_headers mode)


def principal_from_token(token: str) -> Principal:
    from .users import session_user
    u = session_user(token)
    if not u:
        raise ApiError(401, "UNAUTHENTICATED", "Please sign in.")
    return Principal(user=u.username, role=u.role, plant=u.plant_id)


def principal(x_role: str = Header(""), x_user: str = Header(""), x_plant: str = Header(""), authorization: str = Header("")) -> Principal:
    mode = Settings().auth_mode
    if mode == "password":
        return principal_from_token(authorization[7:].strip() if authorization.lower().startswith("bearer ") else "")
    if mode != "dev_headers":
        raise ApiError(503, "AUTH_NOT_CONFIGURED", f"AUTH_MODE '{mode}' is not supported", "Use 'password' (or 'dev_headers' in tests)")
    role = x_role.strip().lower()
    if not role:
        raise ApiError(401, "UNAUTHENTICATED", "Missing X-Role header (dev_headers auth mode)")
    return Principal(user=(x_user.strip() or "unknown")[:64], role=role, plant=x_plant.strip().upper()[:20] or None)


def require(action: str):
    def dep(p: Principal = Depends(principal)) -> Principal:
        if action not in Policy().role_actions.get(p.role, ()):
            raise ApiError(403, "FORBIDDEN", f"Role '{p.role}' may not perform '{action}'", {"required_action": action})
        return p
    return dep

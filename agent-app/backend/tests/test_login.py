from fastapi.testclient import TestClient
from app import users
from app.db import SessionLocal, User
from app.main import app

c = TestClient(app)


def _admin():
    with SessionLocal() as s:
        if not s.query(User).filter_by(username="root").first():
            s.add(User(username="root", name="Root", role="admin", password_hash=users.hash_password("rootpass123")))
            s.commit()


def _login(u, pw):
    return c.post("/api/auth/login", json={"username": u, "password": pw})


def test_password_hash_is_salted_and_checked():
    h = users.hash_password("secret-pass")
    assert "secret-pass" not in h and h != users.hash_password("secret-pass")
    assert users.check_password("secret-pass", h) and not users.check_password("wrong", h)


def test_sign_in_roles_and_sign_out(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "password")
    _admin()
    assert c.get("/api/schedules").status_code == 401                                    # no token: nothing
    assert c.get("/api/schedules", headers={"X-Role": "admin"}).status_code == 401       # headers no longer grant a role
    assert _login("root", "nope").status_code == 401
    tok = _login("ROOT", "rootpass123").json()["token"]
    H = {"Authorization": f"Bearer {tok}"}
    assert c.get("/api/auth/me", headers=H).json()["role"] == "admin"
    r = c.post("/api/users", headers=H, json={"username": "head.ka", "name": "Head KA", "role": "plant_head", "plant_id": "IN-KA-01", "password": "headpass123"})
    assert r.status_code == 201, r.text
    assert c.post("/api/users", headers=H, json={"username": "x1", "name": "X", "role": "plant_head", "password": "headpass123"}).status_code == 422
    h2 = {"Authorization": "Bearer " + _login("head.ka", "headpass123").json()["token"]}
    me = c.get("/api/auth/me", headers=h2).json()
    assert me["role"] == "plant_head" and me["plant_id"] == "IN-KA-01"
    assert c.get("/api/users", headers=h2).status_code == 403                            # only an admin manages accounts
    c.patch(f"/api/users/{r.json()['id']}", headers=H, json={"active": False})
    assert c.get("/api/auth/me", headers=h2).status_code == 401                          # switched off: signed out
    c.post("/api/auth/logout", headers=H)
    assert c.get("/api/auth/me", headers=H).status_code == 401


def test_wrong_passwords_lock_the_username(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "password")
    _admin()
    for _ in range(users.MAX_FAILS):
        _login("root", "bad")
    assert _login("root", "rootpass123").status_code == 429
    users._fails.clear()

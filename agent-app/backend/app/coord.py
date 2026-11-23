"""Coordination between processes, through the database (so the API can run as several workers next to a separate agent worker).

Lease / Mutex : a named lock any process can take; it expires on its own if the holder dies (no stuck locks).
leader_loop   : a background loop that runs in exactly one process at a time (the one holding the lease).
bump / sync   : a version number per kind of data; a process that sees it change clears its in-memory caches.
RUN_MODE      : all (API + agent loops in one process, the default) | api (requests only) | worker (agent loops and jobs only).
"""
import datetime as dt
import logging
import os
import socket
import threading
import time
import uuid

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .db import AppState, Lease, SessionLocal, _now

log = logging.getLogger("planner.coord")
PROCESS = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
BOOT = _now()
_local = threading.local()
_stop = threading.Event()


def run_mode() -> str:
    m = os.getenv("RUN_MODE", "all").strip().lower()
    return m if m in ("all", "api", "worker") else "all"


def _holder() -> str:
    return f"{PROCESS}:{threading.get_ident()}"


def acquire(name: str, ttl: float, holder: str | None = None) -> bool:
    """Take or renew the lease `name` for `ttl` seconds. Atomic in SQLite and Postgres (one row per lease, conditional update)."""
    holder, now = holder or _holder(), _now()
    exp = now + dt.timedelta(seconds=ttl)
    with SessionLocal() as s:
        try:
            s.add(Lease(name=name, holder=holder, expires_at=exp))
            s.commit()
            return True
        except IntegrityError:
            s.rollback()
        r = s.execute(text("UPDATE leases SET holder=:h, expires_at=:e WHERE name=:n AND (expires_at < :now OR holder=:h)"),
                      {"h": holder, "e": exp, "n": name, "now": now})
        s.commit()
        return r.rowcount == 1


def release(name: str, holder: str | None = None) -> None:
    with SessionLocal() as s:
        s.execute(text("UPDATE leases SET expires_at=:now WHERE name=:n AND holder=:h"), {"now": _now(), "n": name, "h": holder or _holder()})
        s.commit()


_local_locks: dict = {}
_ll_guard = threading.Lock()


class Mutex:
    """`with Mutex("plan-change"):` one holder across every process and thread. Re-entrant in the same thread.
    Two layers: a lock in this process (threads hand over instantly), then the database lease (other processes).
    ttl bounds how long a crashed holder blocks others; wait bounds how long a caller waits before giving up."""

    def __init__(self, name: str, ttl: float = 600, wait: float = 900):
        self.name, self.ttl, self.wait = name, ttl, wait
        with _ll_guard:
            self._lk = _local_locks.setdefault(name, threading.RLock())

    def __enter__(self):
        depth = getattr(_local, "depth", None)
        if depth is None:
            depth = _local.depth = {}
        if depth.get(self.name):
            depth[self.name] += 1
            return self
        if not self._lk.acquire(timeout=self.wait):
            raise TimeoutError(f"{self.name} is busy")
        end, pause = time.monotonic() + self.wait, 0.05
        try:
            while not acquire(self.name, self.ttl, f"{PROCESS}:mutex"):     # one holder name per process: this process holds the local lock
                if time.monotonic() > end:
                    raise TimeoutError(f"{self.name} is busy in another process")
                time.sleep(pause)
                pause = min(1.0, pause * 1.5)
        except BaseException:
            self._lk.release()
            raise
        depth[self.name] = 1
        return self

    def __exit__(self, *exc):
        _local.depth[self.name] -= 1
        if not _local.depth[self.name]:
            try:
                release(self.name, f"{PROCESS}:mutex")
            finally:
                self._lk.release()
        return False


LEASE_TTL, BEAT, TICK = 90.0, 30.0, 5.0      # a leader renews every 30 s; a stopped one is replaced within 90 s
_held: dict = {}                             # loop name -> this process leads it


def get_state(key: str) -> str | None:
    with SessionLocal() as s:
        return s.execute(text("SELECT value FROM app_state WHERE key=:k"), {"k": key}).scalar()


def set_state(key: str, value: str) -> None:
    with SessionLocal() as s:
        if s.execute(text("UPDATE app_state SET value=:v, updated_at=:now WHERE key=:k"), {"v": value, "now": _now(), "k": key}).rowcount == 0:
            s.add(AppState(key=key, value=value))
        try:
            s.commit()
        except IntegrityError:
            s.rollback()


def leader_loop(name: str, every: float, fn, first: bool = False) -> None:
    """Run fn() every `every` seconds in the one process holding the lease `loop:<name>`; the others stand by.
    The lease is renewed by its own heartbeat (so a long round never loses it, and a stopped leader is replaced within 90 s), and
    the time of the last run is shared, so a new leader keeps to the same schedule instead of starting the clock again."""
    holder, key = f"{PROCESS}:loop:{name}", f"loop:{name}"

    def beat():
        while True:
            was = _held.get(name)
            try:
                _held[name] = acquire(key, LEASE_TTL, holder)
            except Exception:
                _held[name] = False
            if _held[name] != was:
                log.info("%s %s loop", "leading" if _held[name] else "standing by for", name)
            if _stop.wait(BEAT):
                if _held.get(name):
                    release(key, holder)                     # a clean shutdown hands over at once
                return

    def due() -> bool:
        last = get_state(f"{key}:last")
        if last is None:
            if not first:
                set_state(f"{key}:last", str(time.time()))   # start the clock: the first run is one interval from now
            return first
        return time.time() - float(last) >= every

    def loop():
        while not _stop.wait(TICK):
            try:
                if _held.get(name) and due():
                    set_state(f"{key}:last", str(time.time()))
                    t0 = time.monotonic()
                    fn()
                    log.info("%s round done in %.1f s", name, time.monotonic() - t0)
            except Exception:
                log.exception("%s round failed", name)
    threading.Thread(target=beat, name=f"lease-{name}", daemon=True).start()
    threading.Thread(target=loop, name=f"loop-{name}", daemon=True).start()


def leading() -> list[str]:
    return sorted(n for n, v in _held.items() if v)


# ---------------------------------------------------------------- data versions: caches stay right across processes
def bump(key: str = "data") -> None:
    """Tell every process this kind of data changed (a new value each time: no read-modify-write race)."""
    v, now = uuid.uuid4().hex[:12], _now()
    with SessionLocal() as s:
        if s.execute(text("UPDATE app_state SET value=:v, updated_at=:now WHERE key=:k"), {"v": v, "now": now, "k": key}).rowcount == 0:
            s.add(AppState(key=key, value=v))
        try:
            s.commit()
        except IntegrityError:                          # another process created it first: update it
            s.rollback()
            s.execute(text("UPDATE app_state SET value=:v, updated_at=:now WHERE key=:k"), {"v": v, "now": now, "k": key})
            s.commit()


_seen: dict = {}
_checked = [0.0]
_clearers: dict = {}                  # key -> cache-clearing functions


def start_sync(every: float = 2.0) -> None:
    """Check the data versions every `every` seconds in the background (requests never wait for it)."""
    def loop():
        while not _stop.wait(every):
            sync(0)
    threading.Thread(target=loop, name="cache-sync", daemon=True).start()


def changed(key: str = "data") -> None:
    """This process changed the data: clear its own caches now and tell the others (they clear at their next sync)."""
    for fn in _clearers.get(key, []):
        fn()
    bump(key)
    with SessionLocal() as s:
        _seen[key] = s.execute(text("SELECT value FROM app_state WHERE key=:k"), {"k": key}).scalar()


def on_change(fn, key: str = "data") -> None:
    """Register a cache-clearing function, called when another process changed that kind of data."""
    _clearers.setdefault(key, []).append(fn)


def sync(every: float = 2.0) -> bool:
    """At most every `every` seconds: for each kind of data whose version moved, clear this process's caches of it."""
    now = time.monotonic()
    if now - _checked[0] < every:
        return False
    _checked[0] = now
    try:
        with SessionLocal() as s:
            vs = dict(s.execute(text("SELECT key, value FROM app_state")).all())
    except Exception:
        return False
    cleared = False
    for key, fns in _clearers.items():
        v = vs.get(key)
        if key in _seen and v is not None and _seen[key] != v:
            for fn in fns:
                try:
                    fn()
                except Exception:
                    log.exception("cache clear failed")
            cleared = True
        _seen[key] = v
    return cleared

"""Short-lived answers for heavy, read-only pages. Each entry lives `ttl` seconds and is dropped at once (in every process, through
coord) when the kind of data it depends on changes: "data" (orders, stock), "plans" (a plan decided), "exec" (a job moved)."""
import threading
import time

from . import coord

_store: dict = {}
_lock = threading.Lock()
_inflight: dict = {}


def _clear(kind: str):
    def fn():
        with _lock:
            for k in [k for k in _store if kind in k[0]]:
                _store.pop(k, None)
    return fn


for _k in ("data", "plans", "planlist", "exec", "notes"):
    coord.on_change(_clear(_k), _k)


def get(name: str, args: tuple, ttl: float, fn, kinds: tuple = ("data", "plans")):
    """The cached answer for (name, args), or fn() computed once (callers asking at the same moment wait for that one answer)."""
    key = (kinds, name, args)
    now = time.monotonic()
    with _lock:
        hit = _store.get(key)
        if hit and hit[0] > now:
            return hit[1]
        ev = _inflight.get(key)
        mine = ev is None
        if mine:
            ev = _inflight[key] = threading.Event()
    if not mine:
        ev.wait(120)
        with _lock:
            hit = _store.get(key)
        if hit:
            return hit[1]
    try:
        val = fn()
        with _lock:
            _store[key] = (time.monotonic() + ttl, val)
        return val
    finally:
        if mine:
            with _lock:
                _inflight.pop(key, None)
            ev.set()


def route(ttl: float, kinds: tuple = ("data", "plans"), scoped: bool = False):
    """Cache an endpoint's answer by its arguments; everyone shares it. scoped=True: the caller's role and plant are part of the key,
    for endpoints whose answer or access check depends on who asks (a production head sees only their plant). Errors are never cached."""
    import functools

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            parts = []
            for k, v in sorted(kw.items()):
                if hasattr(v, "role") and hasattr(v, "user"):           # the signed-in person
                    if scoped:
                        parts.append((k, v.role, getattr(v, "plant", None)))
                else:
                    parts.append((k, repr(v)))
            return get(f"{fn.__module__}.{fn.__name__}", tuple(parts), ttl, lambda: fn(*a, **kw), kinds)
        return wrapper
    return deco

"""The agents' process: `python -m app.worker`. Runs the background loops and the job queue, next to an API started with RUN_MODE=api
(any number of API workers). Coordination is through the database (coord.py), so a second worker is safe: it stands by."""
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("RUN_MODE", "worker")

from . import coord, store, users  # noqa: E402
from .db import init_db  # noqa: E402
from .main import start_workers  # noqa: E402

log = logging.getLogger("planner.worker")


LOG_FILE = Path(__file__).resolve().parents[1] / "var" / "worker.log"
FMT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def setup_logging() -> None:
    """Console (written at once, never held in a buffer) and a rotating file (var/worker.log, 5 x 2 MB) to read any time."""
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(line_buffering=True)
        except Exception:
            pass
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format=FMT, handlers=[logging.StreamHandler(sys.stderr), fh], force=True)


def status() -> str:
    from .db import AgentJob, SessionLocal
    with SessionLocal() as s:
        q = s.query(AgentJob).filter_by(status="queued").count()
        r = s.query(AgentJob).filter_by(status="running").count()
    lead = coord.leading()
    return f"leading {', '.join(lead) if lead else 'nothing (standing by)'} · jobs queued {q}, running {r}"


def main() -> None:
    setup_logging()
    log.info("agent worker %s starting (log file %s)", coord.PROCESS, LOG_FILE)
    init_db()
    from sqlalchemy.exc import OperationalError
    for attempt in range(1, 7):                          # a slow route to the database must not stop the worker: try again
        try:
            users.ensure_admin()
            store._ensure()
            store.D()                                    # the data, loaded once
            break
        except OperationalError as e:
            if attempt == 6:
                raise
            log.warning("database slow at start-up (%s); trying again (%d of 6)", type(e.orig).__name__, attempt + 1)
            time.sleep(5 * attempt)
    start_workers()
    try:
        log.info("agent worker %s running · %s", coord.PROCESS, status())
    except Exception:                                    # only a log line: never a reason to stop
        log.info("agent worker %s running (status not available yet: the database is slow)", coord.PROCESS)
    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    last = time.monotonic()
    while not done.wait(1):
        coord.sync()                                     # pick up data changed by the API (uploads, agreed forecasts)
        if time.monotonic() - last > 300:
            last = time.monotonic()
            try:
                log.info("status: %s", status())
            except Exception:
                log.exception("status check failed")
    log.info("agent worker stopping: handing its loops over")
    coord._stop.set()
    time.sleep(1)


if __name__ == "__main__":
    main()

"""Single-container mode: web UI/API and the worker in one container.

For platforms that give each service its own disk (Railway, Render, Fly.io), where the API
and the worker could not share the SQLite database as two services.

    python -m quantdesk.run_all

* The worker runs as a child process and is restarted if it exits unexpectedly.
* The API serves on $PORT (set by the platform) or QD_PORT, on all interfaces.
* SIGTERM stops both gracefully (the worker gets time to finish its current step).
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time

log = logging.getLogger("quantdesk.run_all")


def _check_data_dir() -> None:
    from .config import load_config
    d = load_config().data_dir
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        sys.exit(f"Data directory {d} is not writable ({e}). On Railway: attach a volume at {d} and set the "
                 "variable RAILWAY_RUN_UID=0 so the app can write to it.")


def main() -> None:
    logging.basicConfig(level=os.environ.get("QD_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.environ.setdefault("QD_BIND_HOST", "0.0.0.0")
    if os.environ.get("PORT"):
        os.environ["QD_PORT"] = os.environ["PORT"]
    _check_data_dir()
    from .config import load_config
    if not load_config(reload=True).admin_password:
        log.warning("QD_ADMIN_PASSWORD is not set: the UI will refuse all non-local visitors")

    stopping = threading.Event()
    child: dict = {"p": None}

    def supervise():
        backoff = 5
        while not stopping.is_set():
            p = subprocess.Popen([sys.executable, "-m", "quantdesk.worker"])
            child["p"] = p
            started = time.time()
            code = p.wait()
            if stopping.is_set():
                return
            if time.time() - started > 300:
                backoff = 5  # it had been running fine: restart quickly
            log.error("worker exited with code %s; restarting in %ss", code, backoff)
            stopping.wait(backoff)
            backoff = min(backoff * 2, 120)  # repeated quick crashes back off

    threading.Thread(target=supervise, daemon=True, name="worker-supervisor").start()

    def shutdown(*_):
        if stopping.is_set():
            return
        stopping.set()
        p = child["p"]
        if p and p.poll() is None:
            p.send_signal(signal.SIGTERM)
            try:
                p.wait(timeout=50)
            except subprocess.TimeoutExpired:
                p.kill()
        os._exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    import uvicorn
    cfg = load_config()
    server = uvicorn.Server(uvicorn.Config("quantdesk.api.app:app", host=cfg.bind_host, port=cfg.bind_port,
                                           proxy_headers=True,
                                           forwarded_allow_ips=os.environ.get("QD_TRUSTED_PROXIES", "127.0.0.1")))
    server.install_signal_handlers = lambda: None  # we handle signals ourselves
    try:
        server.run()
    finally:
        shutdown()


if __name__ == "__main__":
    main()

"""Safe, persistent local demo. Run ``python -m quantdesk.preview``.

Never loads .env, inherits provider credentials or opens the normal account DB.
The Windows launcher starts this module without requiring a PowerShell command.
"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
PREVIEW_DIR = ROOT / "data" / "local-preview-v1"
MARKER = "QuantDesk isolated local preview v1\n"
SYMBOLS = {"NASDAQ": ["QQQ", "AAPL", "MSFT"],
           "CRYPTO": ["BTC-USD", "ETH-USD", "SOL-USD"]}
HISTORY_DAYS = 180
log = logging.getLogger("quantdesk.preview")


def _utf8_console() -> None:
    # Setting PYTHONUTF8 after interpreter startup does not change existing streams.
    # This also protects direct `python -m ...` calls in Windows paths containing ı.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def preview_environment(data_dir: Path, port: int, parent: dict | None = None) -> dict:
    """An explicit demo-only environment; production overrides cannot leak through."""
    env = dict(os.environ if parent is None else parent)
    for key in list(env):
        if key.upper().startswith(("QD_", "ALPACA_", "APCA_")) or key.upper() in {
                "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "PORT"}:
            del env[key]
    env.update({
        "PYTHONUTF8": "1", "QD_ENV_FILE": os.devnull,
        "QD_DATA_DIR": str(data_dir), "QD_DB_PATH": str(data_dir / "quantdesk.db"),
        "QD_MARKET_DATA": "synthetic", "QD_LLM_PROVIDER": "none",
        "ANTHROPIC_API_KEY": "", "QD_LLM_API_KEY": "",
        "ALPACA_API_KEY_ID": "", "ALPACA_API_SECRET_KEY": "",
        "QD_BIND_HOST": "127.0.0.1", "QD_PORT": str(port),
        "QD_ADMIN_PASSWORD": "", "QD_COOKIE_SECURE": "0",
        "QD_TRUSTED_PROXIES": "", "QD_SANDBOX_MODE": "local",
    })
    return env


def check_preview_directory(data_dir: Path) -> None:
    """Refuse to adopt an unmarked DB, even if someone placed it in the demo folder."""
    marker = data_dir / ".quantdesk-preview"
    if marker.exists():
        if marker.read_text(encoding="utf-8") != MARKER:
            raise RuntimeError("Unrecognized preview marker; no data was changed.")
    elif (data_dir / "quantdesk.db").exists():
        raise RuntimeError("Refusing to open an existing database without a preview marker.")
    data_dir.mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        marker.write_text(MARKER, encoding="utf-8")


def _configure_history() -> None:
    from .marketdata import ingest
    ingest.BACKFILL_DAYS.update({venue: HISTORY_DAYS for venue in SYMBOLS})


def prepare_database() -> None:
    from . import db, settings
    from .agent import journal
    from .config import load_config
    from .trading import ledger
    cfg = load_config()
    if (cfg.market_data_mode != "synthetic" or cfg.llm_provider != "none"
            or cfg.db_path != cfg.data_dir / "quantdesk.db"):
        raise RuntimeError("Preview safety configuration is invalid.")
    check_preview_directory(cfg.data_dir)
    db.init(cfg.db_path)
    ledger.init_account()
    if db.get_meta("local_preview_initialized") != "1":
        settings.operator_update("universe", SYMBOLS, actor="operator:local-preview")
        settings.operator_update("schedule", {"quote_poll_sec": 5}, actor="operator:local-preview")
        db.set_meta("local_preview_initialized", "1")
        journal.write("system", "Isolated local preview",
                      "Synthetic data and the demo rules analyst only. This database is separate from your "
                      "normal account and Railway. The normal gates and risk engine still apply; a flat "
                      "account is expected when no strategy qualifies.", author="system")
    if db.get_meta("local_preview_seeded") != "1":
        _seed_history()
        db.set_meta("local_preview_seeded", "1")


def _seed_history() -> None:
    from . import clock, db
    from .agent import journal
    from .marketdata import ingest
    from .trading import mtm
    for venue, symbols in SYMBOLS.items():
        print(f"Preparing {venue} synthetic history...", flush=True)
        ingest.ingest_bars(venue, symbols)
        ingest.ingest_quotes(venue, symbols)
        # Record the unchanged bar size before the worker's first recovery.
        from .venues import VENUES
        db.set_meta(f"timeframe:{venue}", VENUES[venue].timeframe)
    mtm.snapshot()
    journal.set_status("starting", "Synthetic history is ready. The worker will run the first research cycles.",
                       [{"what": "First demo research cycles", "at": clock.now()}])


def _open_when_ready(url: str, stop: threading.Event) -> None:
    deadline = time.monotonic() + 45
    # Never send a loopback readiness probe through an inherited HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while not stop.is_set() and time.monotonic() < deadline:
        try:
            with opener.open(url + "/api/session", timeout=1) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    return
        except (OSError, urllib.error.URLError):
            pass
        stop.wait(0.25)


def stop_worker(worker: subprocess.Popen) -> None:
    if worker.poll() is not None:
        return
    try:
        worker.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
    except OSError:
        # Headless Windows callers may not have a console for CTRL_BREAK_EVENT.
        worker.terminate()
    try:
        worker.wait(timeout=35)
    except subprocess.TimeoutExpired:
        worker.kill()
        worker.wait(timeout=5)
    except ProcessLookupError:
        pass


def main() -> None:
    _utf8_console()
    parser = argparse.ArgumentParser(description="QuantDesk isolated synthetic preview")
    parser.add_argument("--port", type=int, default=8127)
    parser.add_argument("--no-browser", action="store_true", help="Do not open the system browser")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    # Per-process only: never edits .env or the user's shell/production environment.
    env = preview_environment(PREVIEW_DIR, args.port)
    os.environ.clear()
    os.environ.update(env)
    from .config import load_config
    load_config(reload=True)
    _configure_history()
    if args.worker:
        check_preview_directory(PREVIEW_DIR)
        if os.name == "nt":
            # A dedicated process group lets the parent stop only its own worker.
            def on_break(*_):
                handler = signal.getsignal(signal.SIGTERM)
                if callable(handler):
                    handler(signal.SIGTERM, None)
                else:
                    raise SystemExit(0)
            signal.signal(signal.SIGBREAK, on_break)
        from .worker import main as worker_main
        worker_main()
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    url = f"http://127.0.0.1:{args.port}"
    print("\nQUANTDESK LOCAL PREVIEW — synthetic data, no paid AI calls", flush=True)
    print(f"Separate preview data: {PREVIEW_DIR}\nDashboard: {url}", flush=True)
    print("Keep this window open. Press Ctrl+C to stop the API and worker.\n", flush=True)
    # Reserve the port BEFORE touching any DB or starting a worker.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", args.port))
        sock.listen(128)
    except OSError:
        sock.close()
        parser.exit(1, f"Port {args.port} is already in use. Close the existing preview window and try again.\n")
    worker = None
    stop = threading.Event()
    try:
        prepare_database()
        import uvicorn
        worker = subprocess.Popen([sys.executable, "-m", "quantdesk.preview", "--worker", "--port", str(args.port)],
                                  cwd=str(ROOT), env=env,
                                  creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
        if not args.no_browser:
            threading.Thread(target=_open_when_ready, args=(url, stop), daemon=True).start()
        server = uvicorn.Server(uvicorn.Config("quantdesk.api.app:app", host="127.0.0.1", port=args.port,
                                               proxy_headers=False, log_level="info"))
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        sock.close()
        if worker is not None:
            stop_worker(worker)


if __name__ == "__main__":
    main()

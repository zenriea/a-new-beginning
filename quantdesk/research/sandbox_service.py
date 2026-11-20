"""Sandbox service for docker-compose: watches the spool dir and runs jobs in children
with resource limits. This container has no network and no secrets."""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path

from .sandbox import SandboxError, _run_child


def _check_isolation():
    if sys.platform != "linux" or os.geteuid() == 0:
        raise SystemExit("The sandbox service requires an unprivileged Linux container")
    if any(name != "lo" for _, name in socket.if_nameindex()):
        raise SystemExit("The sandbox service requires network_mode: none")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spool", default="/spool")
    a = ap.parse_args()
    _check_isolation()
    spool = Path(a.spool)
    (spool / "in").mkdir(parents=True, exist_ok=True)
    (spool / "out").mkdir(parents=True, exist_ok=True)
    print(f"sandbox service watching {spool}", flush=True)
    while True:
        for f in sorted((spool / "in").glob("*.json")):
            try:
                job = json.loads(f.read_text())
                f.unlink()
            except (OSError, ValueError):
                continue
            try:
                res = _run_child(job)
            except SandboxError as e:
                res = {"ok": False, "error": str(e)}
            tmp = spool / "out" / (f.stem + ".tmp")
            tmp.write_text(json.dumps(res))
            tmp.rename(spool / "out" / f.name)
        time.sleep(0.2)


if __name__ == "__main__":
    main()

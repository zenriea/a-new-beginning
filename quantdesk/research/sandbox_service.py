"""Sandbox service for docker-compose: watches the spool dir and runs jobs in children
with resource limits. This container has no network and no secrets."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .sandbox import SandboxError, run_local


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spool", default="/spool")
    a = ap.parse_args()
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
                res = run_local(job)
            except SandboxError as e:
                res = {"ok": False, "error": str(e)}
            tmp = spool / "out" / (f.stem + ".tmp")
            tmp.write_text(json.dumps(res))
            tmp.rename(spool / "out" / f.name)
        time.sleep(0.2)


if __name__ == "__main__":
    main()

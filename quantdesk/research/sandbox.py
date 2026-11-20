"""Run model-generated strategy code in isolation.

Modes (QD_SANDBOX_MODE):
  disabled - skip Python strategies and use the DSL.
  local - subprocess with cleared env, resource limits, a private network namespace and
          a separate unprivileged UID. Refuse to run if either boundary is unavailable.
  spool - hand the job to the separate ``sandbox`` container (docker-compose: no network,
          no secrets, read-only fs) through a shared spool directory.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from functools import lru_cache
from pathlib import Path

import numpy as np

from ..config import load_config
from .sandbox_policy import check_source  # noqa: F401  (re-export)

CPU_SECONDS = 20
MEMORY_BYTES = 1024 * 1024 * 1024
WALL_TIMEOUT = 60
PKG_ROOT = Path(__file__).resolve().parents[2]


class SandboxError(RuntimeError):
    pass


def _limits():
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    except (ValueError, OSError):
        pass
    os.setsid()
    _drop_privileges()


def _drop_privileges():
    if os.geteuid() == 0:
        import pwd
        nobody = pwd.getpwnam("nobody")
        os.setgroups([])
        os.setgid(nobody.pw_gid)
        os.setuid(nobody.pw_uid)


@lru_cache(maxsize=1)
def _netns_prefix() -> tuple[str, ...]:
    """Use a private network namespace if this kernel/container permits it."""
    if sys.platform != "linux":
        return ()
    exe = shutil.which("unshare")
    if not exe:
        return ()
    try:
        r = subprocess.run([exe, "-rn", "true"], capture_output=True, timeout=5,
                           preexec_fn=_drop_privileges)
        return (exe, "-rn") if r.returncode == 0 else ()
    except Exception:
        return ()


def isolation_report() -> dict:
    cfg = load_config()
    local_supported = cfg.sandbox_mode == "local" and local_available()
    return {"mode": cfg.sandbox_mode, "network_namespace": local_supported,
            "local_supported": local_supported,
            "drops_to_nobody": hasattr(os, "geteuid") and os.geteuid() == 0, "cpu_seconds": CPU_SECONDS,
            "memory_mb": MEMORY_BYTES // (1024 * 1024), "env": "cleared (no secrets)"}


def local_available() -> bool:
    return sys.platform == "linux" and os.geteuid() == 0 and bool(_netns_prefix())


def python_strategies_allowed() -> bool:
    mode = load_config().sandbox_mode
    return mode == "spool" or (mode == "local" and local_available())


def run_local(job: dict) -> dict:
    if sys.platform != "linux":
        raise SandboxError("local Python sandbox requires Linux; use Docker or Linux/WSL")
    if not local_available():
        raise SandboxError("local Python sandbox requires a separate UID and network namespace; use the sandbox container")
    return _run_child(job, _netns_prefix())


def _run_child(job: dict, prefix: tuple[str, ...] = ()) -> dict:
    child = str(Path(__file__).with_name("sandbox_child.py"))
    cmd = [*prefix, sys.executable, "-I", child]
    try:
        p = subprocess.run(cmd, input=json.dumps(job), capture_output=True, text=True,
                           timeout=WALL_TIMEOUT, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                           cwd=str(PKG_ROOT), preexec_fn=_limits, close_fds=True)
    except subprocess.TimeoutExpired:
        raise SandboxError(f"strategy code exceeded the {WALL_TIMEOUT}s wall-clock limit")
    except (OSError, subprocess.SubprocessError):
        raise SandboxError("could not start the isolated strategy process") from None
    if p.returncode != 0 and not p.stdout:
        raise SandboxError(f"sandbox exited with code {p.returncode}: {p.stderr[-300:]}")
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise SandboxError(f"sandbox produced no result: {p.stderr[-300:]}")


def run_spool(job: dict, timeout: float = WALL_TIMEOUT + 30) -> dict:
    spool = load_config().sandbox_spool_dir
    (spool / "in").mkdir(parents=True, exist_ok=True)
    (spool / "out").mkdir(parents=True, exist_ok=True)
    jid = uuid.uuid4().hex
    tmp = spool / "in" / f"{jid}.tmp"
    tmp.write_text(json.dumps(job))
    tmp.rename(spool / "in" / f"{jid}.json")
    out = spool / "out" / f"{jid}.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if out.exists():
            res = json.loads(out.read_text())
            out.unlink(missing_ok=True)
            return res
        time.sleep(0.25)
    raise SandboxError("sandbox service did not answer in time (is the sandbox container running?)")


def run_job(job: dict) -> dict:
    mode = load_config().sandbox_mode
    if mode == "spool":
        return run_spool(job)
    if mode == "local":
        return run_local(job)
    raise SandboxError("Python strategies are disabled; use a DSL strategy or configure the sandbox container")


def run_code_signals(code: str, panel: dict, params: dict, runner=None):
    check_source(code)
    series = []
    T, N = panel["close"].shape
    for j in range(N):
        series.append({k: np.nan_to_num(panel[f], nan=np.nan).astype(float)[:, j].tolist()
                       for k, f in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume"))})
    job = {"code": code, "series": series, "params": params}
    res = (runner or run_job)(job)
    if not res.get("ok"):
        raise SandboxError(f"strategy code failed: {res.get('error')}")
    if not res.get("causal"):
        raise SandboxError("look-ahead detected: " + "; ".join(res.get("detail", [])[:3]))
    entry = np.array(res["entry"], dtype=bool).T
    exit_ = np.array(res["exit"], dtype=bool).T
    return entry, exit_

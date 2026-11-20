"""Consistent online backup of the SQLite database (safe while the worker runs).

    python -m quantdesk.backup /path/to/backup.db
"""
import os
import sqlite3
import sys
import time

from .config import load_config


def backup(src_path, dest) -> None:
    # Read-only: a mistyped path must fail, not create an empty source that then overwrites a good backup.
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    tmp = f"{dest}.tmp"
    try:
        if not src.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone():
            raise SystemExit(f"{src_path} is not a QuantDesk database; backup not written")
        out = sqlite3.connect(tmp)
        try:
            src.backup(out)
            if out.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise SystemExit("backup failed its integrity check; destination left unchanged")
        finally:
            out.close()
        os.replace(tmp, dest)
    finally:
        src.close()
        if os.path.exists(tmp):
            os.remove(tmp)


def main():
    cfg = load_config()
    dest = sys.argv[1] if len(sys.argv) > 1 else str(cfg.data_dir / f"backup-{time.strftime('%Y%m%d-%H%M%S')}.db")
    try:
        backup(cfg.db_path, dest)
    except sqlite3.OperationalError as e:
        raise SystemExit(f"cannot read {cfg.db_path}: {e}; backup not written") from None
    print(dest)


if __name__ == "__main__":
    main()

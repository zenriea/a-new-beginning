"""Consistent online backup of the SQLite database (safe while the worker runs).

    python -m quantdesk.backup /path/to/backup.db
"""
import sqlite3
import sys
import time

from .config import load_config


def main():
    cfg = load_config()
    dest = sys.argv[1] if len(sys.argv) > 1 else str(cfg.data_dir / f"backup-{time.strftime('%Y%m%d-%H%M%S')}.db")
    src = sqlite3.connect(str(cfg.db_path))
    out = sqlite3.connect(dest)
    src.backup(out)
    out.close()
    src.close()
    print(dest)


if __name__ == "__main__":
    main()

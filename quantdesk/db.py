"""SQLite persistence (WAL mode). One connection per thread; short write transactions.

Every table that the agent uses for continuity (hypotheses, experiments, strategies,
decisions, journal, llm_calls) is append-mostly so the history stays auditable.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL, updated_by TEXT);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  detail TEXT);

CREATE TABLE IF NOT EXISTS bars (
  symbol TEXT NOT NULL, timeframe TEXT NOT NULL, ts INTEGER NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
  volume REAL NOT NULL, PRIMARY KEY (symbol, timeframe, ts)) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS quotes (
  symbol TEXT PRIMARY KEY, venue TEXT NOT NULL, ts REAL NOT NULL, bid REAL, ask REAL,
  bid_size REAL, ask_size REAL, last REAL, last_ts REAL, source TEXT, received_at REAL);

CREATE TABLE IF NOT EXISTS data_status (
  key TEXT PRIMARY KEY, ok INTEGER NOT NULL, last_ok_at REAL, last_error TEXT,
  changed_at REAL);

CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, purpose TEXT NOT NULL, model TEXT,
  served_model TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
  cache_write_tokens INTEGER DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0,
  cost_usd REAL DEFAULT 0, status TEXT NOT NULL, error TEXT, request TEXT, response TEXT);

CREATE TABLE IF NOT EXISTS journal (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
  body TEXT NOT NULL, refs TEXT, author TEXT NOT NULL, llm_call_id INTEGER,
  dedupe_key TEXT UNIQUE);

CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, venue TEXT NOT NULL, data_end INTEGER,
  summary TEXT NOT NULL, notable TEXT);

CREATE TABLE IF NOT EXISTS hypotheses (
  id INTEGER PRIMARY KEY, created_at REAL NOT NULL, venue TEXT NOT NULL,
  title TEXT NOT NULL, rationale TEXT NOT NULL, prediction TEXT NOT NULL,
  falsification TEXT, status TEXT NOT NULL, parent_id INTEGER, observation_id INTEGER,
  author TEXT NOT NULL, llm_call_id INTEGER, updated_at REAL);

CREATE TABLE IF NOT EXISTS experiments (
  id INTEGER PRIMARY KEY, hypothesis_id INTEGER NOT NULL, created_at REAL NOT NULL,
  venue TEXT NOT NULL, kind TEXT NOT NULL, spec TEXT NOT NULL, spec_hash TEXT NOT NULL,
  frozen_at REAL, windows TEXT, chosen_params TEXT, grid_results TEXT,
  train TEXT, validation TEXT, test TEXT, challenge TEXT, gates TEXT,
  leakage TEXT, status TEXT NOT NULL, error TEXT, completed_at REAL,
  equity_curve TEXT);

CREATE TABLE IF NOT EXISTS strategies (
  id INTEGER PRIMARY KEY, hypothesis_id INTEGER NOT NULL, experiment_id INTEGER NOT NULL,
  name TEXT NOT NULL, version INTEGER NOT NULL, venue TEXT NOT NULL, spec TEXT NOT NULL,
  spec_hash TEXT NOT NULL, status TEXT NOT NULL, approved_allocation REAL NOT NULL,
  allocation REAL NOT NULL, thesis TEXT NOT NULL, exit_plan TEXT NOT NULL,
  expectations TEXT NOT NULL, invalidation TEXT NOT NULL, created_at REAL NOT NULL,
  activated_at REAL, retired_at REAL, retire_reason TEXT, last_signal_bar INTEGER,
  last_review_at REAL, probe INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS strategy_state (
  strategy_id INTEGER NOT NULL, symbol TEXT NOT NULL, in_position INTEGER NOT NULL,
  entry_price REAL, entry_bar INTEGER, bars_held INTEGER DEFAULT 0,
  updated_bar INTEGER, PRIMARY KEY (strategy_id, symbol));

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, subject_type TEXT NOT NULL,
  subject_id INTEGER, rationale TEXT NOT NULL, details TEXT, author TEXT NOT NULL,
  llm_call_id INTEGER);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY, client_order_id TEXT NOT NULL UNIQUE, strategy_id INTEGER,
  symbol TEXT NOT NULL, venue TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL,
  status TEXT NOT NULL, reason TEXT NOT NULL, rationale TEXT NOT NULL,
  signal_snapshot TEXT, risk_check TEXT, origin TEXT NOT NULL,
  created_at REAL NOT NULL, submitted_at REAL, eligible_at REAL, expires_at REAL,
  filled_qty REAL DEFAULT 0, avg_price REAL, fees REAL DEFAULT 0, closed_at REAL,
  status_note TEXT);
CREATE INDEX IF NOT EXISTS orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, ts REAL NOT NULL, symbol TEXT NOT NULL,
  side TEXT NOT NULL, qty REAL NOT NULL, price REAL NOT NULL, fee REAL NOT NULL,
  ref_bid REAL, ref_ask REAL, ref_ts REAL, price_basis TEXT, spread_bps REAL,
  slippage_bps REAL, notes TEXT);

CREATE TABLE IF NOT EXISTS positions (
  strategy_id INTEGER NOT NULL, symbol TEXT NOT NULL, venue TEXT NOT NULL,
  qty REAL NOT NULL, avg_cost REAL NOT NULL, realized_pnl REAL NOT NULL DEFAULT 0,
  fees_paid REAL NOT NULL DEFAULT 0, opened_at REAL, updated_at REAL,
  PRIMARY KEY (strategy_id, symbol));

CREATE TABLE IF NOT EXISTS cash_ledger (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, delta REAL NOT NULL, kind TEXT NOT NULL,
  fill_id INTEGER, strategy_id INTEGER, note TEXT);

CREATE TABLE IF NOT EXISTS equity_snapshots (
  ts REAL PRIMARY KEY, equity REAL NOT NULL, cash REAL NOT NULL, gross REAL NOT NULL,
  crypto_gross REAL NOT NULL, benchmark_equity REAL, crypto_benchmark_equity REAL,
  peak_equity REAL NOT NULL, detail TEXT);

CREATE TABLE IF NOT EXISTS risk_events (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, detail TEXT,
  order_id INTEGER);

CREATE TABLE IF NOT EXISTS jobs (
  name TEXT PRIMARY KEY, last_started_at REAL, last_finished_at REAL, last_status TEXT,
  last_error TEXT, run_count INTEGER DEFAULT 0, last_key TEXT);

CREATE TABLE IF NOT EXISTS worker_lock (
  id INTEGER PRIMARY KEY CHECK (id = 1), holder TEXT NOT NULL, heartbeat_at REAL NOT NULL,
  started_at REAL NOT NULL);

CREATE TABLE IF NOT EXISTS beliefs (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, venue TEXT, statement TEXT NOT NULL,
  confidence TEXT NOT NULL, evidence TEXT NOT NULL, status TEXT NOT NULL,
  author TEXT NOT NULL, llm_call_id INTEGER);

CREATE TABLE IF NOT EXISTS questions (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, question TEXT NOT NULL, answer TEXT,
  refs TEXT, author TEXT, llm_call_id INTEGER);

CREATE TABLE IF NOT EXISTS corporate_actions (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, symbol TEXT NOT NULL, kind TEXT NOT NULL,
  ratio REAL NOT NULL, detail TEXT);

CREATE TABLE IF NOT EXISTS agent_status (
  id INTEGER PRIMARY KEY CHECK (id = 1), activity TEXT, detail TEXT, updated_at REAL,
  next_actions TEXT);

CREATE INDEX IF NOT EXISTS fills_order ON fills(order_id, id);
CREATE INDEX IF NOT EXISTS orders_strategy_status ON orders(strategy_id, status);
CREATE INDEX IF NOT EXISTS llm_calls_ts ON llm_calls(ts);
CREATE INDEX IF NOT EXISTS decisions_subject ON decisions(subject_type, subject_id, id);
CREATE INDEX IF NOT EXISTS experiments_venue_created ON experiments(venue, created_at);
CREATE INDEX IF NOT EXISTS journal_kind_id ON journal(kind, id);
"""

_local = threading.local()
_db_path: Path | None = None


def init(db_path: Path) -> None:
    global _db_path
    _db_path = Path(db_path)
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect()
    conn.executescript(SCHEMA)
    _migrate(conn)
    cur = conn.execute("SELECT value FROM meta WHERE key='schema_version'")
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations for databases created by older versions."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(strategies)")}
    if "probe" not in cols:
        conn.execute("ALTER TABLE strategies ADD COLUMN probe INTEGER NOT NULL DEFAULT 0")


def connect() -> sqlite3.Connection:
    """Thread-local connection."""
    if _db_path is None:
        raise RuntimeError("db.init() not called")
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "path", None) == _db_path:
        return conn
    if conn is not None:
        conn.close()
    conn = sqlite3.connect(str(_db_path), timeout=30, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    _local.conn = conn
    _local.path = _db_path
    return conn


def close_thread_connection() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


@contextmanager
def tx():
    """IMMEDIATE transaction: serialises writers, all-or-nothing."""
    conn = connect()
    if conn.in_transaction:  # nested use joins the outer transaction
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def q(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, tuple(params)).fetchall()


def q1(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return connect().execute(sql, tuple(params)).fetchone()


def ex(sql: str, params: Iterable[Any] = ()) -> int:
    cur = connect().execute(sql, tuple(params))
    return cur.lastrowid


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=_json_default, separators=(",", ":"))


def loads(s: str | None, default: Any = None) -> Any:
    if s is None or s == "":
        return default
    return json.loads(s)


def _json_default(o: Any):
    try:
        import numpy as np
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return None if not np.isfinite(o) else float(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
        if isinstance(o, np.bool_):
            return bool(o)
    except ImportError:  # pragma: no cover
        pass
    raise TypeError(f"not JSON serialisable: {type(o)}")


def row_dict(row: sqlite3.Row | None, json_fields: Iterable[str] = ()) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for f in json_fields:
        if f in d:
            d[f] = loads(d[f])
    return d


def get_meta(key: str, default: str | None = None) -> str | None:
    r = q1("SELECT value FROM meta WHERE key=?", (key,))
    return r["value"] if r else default


def set_meta(key: str, value: str) -> None:
    ex("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
       (key, value))


def now() -> float:
    return time.time()

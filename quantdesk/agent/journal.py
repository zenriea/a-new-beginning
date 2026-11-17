"""The analyst journal. Entries reference the records they are based on (``refs``), and
LLM-written entries link to the stored prompt/response (``llm_call_id``) for audit."""
from __future__ import annotations

import queue
import threading

from .. import clock, db

KINDS = {"observation", "research", "hypothesis", "test", "decision", "intent", "order", "fill",
         "risk", "review", "data", "system", "answer", "belief"}

_subscribers: list[queue.Queue] = []
_lock = threading.Lock()


def write(kind: str, title: str, body: str, refs: dict | None = None, author: str = "rules",
          llm_call_id: int | None = None, dedupe_key: str | None = None) -> int | None:
    assert kind in KINDS, kind
    ts = clock.now()
    try:
        jid = db.ex("INSERT INTO journal(ts, kind, title, body, refs, author, llm_call_id, dedupe_key) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (ts, kind, title[:300], body[:8000], db.dumps(refs or {}), author, llm_call_id, dedupe_key))
    except Exception as e:  # duplicate dedupe_key -> already journaled (e.g. after restart)
        if "UNIQUE" in str(e):
            return None
        raise
    return jid


def recent(limit: int = 100, after_id: int = 0, kinds: list[str] | None = None) -> list[dict]:
    sql = "SELECT * FROM journal WHERE id>?"
    params: list = [after_id]
    if kinds:
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        params += kinds
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [db.row_dict(r, ["refs"]) for r in db.q(sql, params)]


def since(after_id: int, limit: int = 50) -> list[dict]:
    """Oldest unseen entries first: bounded stream batches must not skip a backlog."""
    return [db.row_dict(r, ["refs"]) for r in db.q(
        "SELECT * FROM journal WHERE id>? ORDER BY id ASC LIMIT ?", (after_id, limit))]


def set_status(activity: str, detail: str = "", next_actions: list | None = None) -> None:
    db.ex("INSERT INTO agent_status(id, activity, detail, updated_at, next_actions) VALUES(1,?,?,?,?) "
          "ON CONFLICT(id) DO UPDATE SET activity=excluded.activity, detail=excluded.detail, "
          "updated_at=excluded.updated_at, next_actions=COALESCE(excluded.next_actions, agent_status.next_actions)",
          (activity, detail, clock.now(), db.dumps(next_actions) if next_actions is not None else None))

"""Background worker: the only process that trades, researches and writes market data.

Run:  python -m quantdesk.worker
Restart-safe: a DB lock with heartbeat prevents two workers; on start it reconciles
interrupted work; every trading action is idempotent (see oms.client_order_id).
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import traceback
import uuid

from . import clock, db, settings, venues
from .agent import journal
from .config import load_config
from .marketdata import freshness, ingest, store
from .trading import execution, ledger, live, mtm, oms, risk

log = logging.getLogger("quantdesk.worker")
LOCK_STALE = 30.0


class LockHeld(RuntimeError):
    pass


def acquire_lock(holder: str) -> float | None:
    """Take the single-worker lock. Returns the previous holder's last heartbeat (for
    downtime reporting) or None on first start. Raises LockHeld if another live worker owns it."""
    now = clock.now()
    with db.tx():
        r = db.q1("SELECT * FROM worker_lock WHERE id=1")
        if r and r["holder"] != holder and now - r["heartbeat_at"] < LOCK_STALE:
            raise LockHeld(f"worker {r['holder']} is alive (heartbeat {now - r['heartbeat_at']:.0f}s ago)")
        prev = r["heartbeat_at"] if r else None
        db.ex("INSERT INTO worker_lock(id, holder, heartbeat_at, started_at) VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE "
              "SET holder=excluded.holder, heartbeat_at=excluded.heartbeat_at, started_at=excluded.started_at",
              (holder, now, now))
    return prev


def heartbeat(holder: str) -> bool:
    n = db.connect().execute("UPDATE worker_lock SET heartbeat_at=? WHERE id=1 AND holder=?", (clock.now(), holder)).rowcount
    return n == 1


def job_due(name: str, every: float) -> bool:
    r = db.q1("SELECT last_started_at FROM jobs WHERE name=?", (name,))
    return r is None or r["last_started_at"] is None or clock.now() - r["last_started_at"] >= every


def run_job(name: str, fn, *args):
    now = clock.now()
    db.ex("INSERT INTO jobs(name, last_started_at, run_count) VALUES(?,?,1) ON CONFLICT(name) DO UPDATE SET "
          "last_started_at=excluded.last_started_at, run_count=jobs.run_count+1", (name, now))
    try:
        out = fn(*args)
        db.ex("UPDATE jobs SET last_finished_at=?, last_status='ok', last_error=NULL WHERE name=?", (clock.now(), name))
        return out
    except Exception as e:
        log.error("job %s failed: %s", name, traceback.format_exc())
        db.ex("UPDATE jobs SET last_finished_at=?, last_status='error', last_error=? WHERE name=?",
              (clock.now(), f"{type(e).__name__}: {e}"[:1000], name))
        return None


# ------------------------------------------------------------------ recovery
def recover(prev_heartbeat: float | None) -> dict:
    ledger.init_account()
    now = clock.now()
    info = {"downtime": None}
    if prev_heartbeat:
        down = now - prev_heartbeat
        info["downtime"] = down
        if down > 120:
            journal.write("system", f"Back online after {down / 60:.0f} minutes offline",
                          f"The worker was not running from {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(prev_heartbeat))}. "
                          "Nothing was traded while offline. Missing market data is being backfilled; signals for bars "
                          "that closed during the outage are not replayed. Orders that outlived their time-to-live expire.",
                          author="system", dedupe_key=f"restart:{int(prev_heartbeat)}")
    n = oms.recover_stuck_intents(max_age=0)
    if n:
        info["cancelled_intents"] = n
    for e in db.q("SELECT id, hypothesis_id FROM experiments WHERE status='running'"):
        db.ex("UPDATE experiments SET status='interrupted', error='worker restarted during the experiment', completed_at=? "
              "WHERE id=?", (now, e["id"]))
        db.ex("UPDATE hypotheses SET status='abandoned', updated_at=? WHERE id=? AND status='testing'", (now, e["hypothesis_id"]))
        journal.write("system", f"Experiment {e['id']} interrupted by a restart",
                      "It was not completed and will not be resumed half-way (that could mix data snapshots). "
                      "The idea can be re-tested as a new experiment.", refs={"experiment_id": e["id"]}, author="system",
                      dedupe_key=f"interrupted:{e['id']}")
    chk = ledger.integrity_check()
    info["integrity"] = chk
    if not chk["ok"]:
        db.set_meta("system_halt", "1")
        journal.write("risk", "Ledger integrity check FAILED - new risk halted",
                      f"Recomputing from fills does not match stored balances: {chk}. New buys are blocked until the "
                      "operator investigates and clears the halt.", author="system")
    return info


# ------------------------------------------------------------------ jobs
def market_tick() -> None:
    """Quotes -> fills -> marks -> risk monitor."""
    uni = settings.get("universe")
    now = clock.now()
    for venue, syms in uni.items():
        if not syms:
            continue
        if venues.is_open(venue, now):
            need = True
        else:  # closed venue: refresh marks occasionally for display only
            need = job_due(f"closed_quotes:{venue}", 1800)
            if need:
                run_job(f"closed_quotes:{venue}", lambda: None)
        if need:
            _journal_data_events(ingest.ingest_quotes(venue, syms), "quote", venue)
    execution.process_orders()
    if job_due("mtm", 60):
        run_job("mtm", mtm.snapshot)
    for ev in risk.monitor():
        if ev["kind"] == "drawdown_halt":
            journal.write("risk", "Max drawdown reached - new risk halted",
                          f"Equity ${ev['equity']:,.0f} is {ev['drawdown']:.1%} below the peak ${ev['peak']:,.0f}. No new "
                          "positions until the operator resets the halt. Exits still run.", author="system")
        else:
            journal.write("risk", "Daily loss limit reached",
                          f"Equity is down {ev['loss']:.2%} today. No new positions until the next UTC day.", author="system")


def _journal_data_events(events: list[dict], what: str, venue: str) -> None:
    """One journal entry per venue and direction, not one per symbol."""
    down = [e for e in events if not e["ok"]]
    up = [e for e in events if e["ok"]]
    if down:
        syms = ", ".join(e["symbol"] for e in down)
        errs = sorted({(e["error"] or "")[:200] for e in down})
        journal.write("data", f"{venue} {what} data unavailable ({len(down)} symbol{'s' if len(down) > 1 else ''})",
                      f"Affected: {syms}. Reason: {' | '.join(errs)}. I am not trading these symbols or evaluating "
                      "signals that depend on them until the data recovers.", author="system")
    if up:
        journal.write("data", f"{venue} {what} data recovered ({len(up)} symbol{'s' if len(up) > 1 else ''})",
                      f"Current again: {', '.join(e['symbol'] for e in up)}. Dependent actions resume.", author="system")


def bars_tick() -> None:
    uni = settings.get("universe")
    sch = settings.get("schedule")
    now = clock.now()
    for venue, syms in uni.items():
        tf = venues.VENUES[venue].timeframe
        exp = freshness.expected_last_bar(venue, now)
        behind = [s for s in syms if (store.last_bar_ts(s, tf) or 0) < exp]
        key = f"bars:{venue}"
        if (behind and job_due(key + ":behind", 60)) or job_due(key, sch["bar_refresh_min"] * 60):
            run_job(key, lambda: None)
            run_job(key + ":behind", lambda: None)
            _journal_data_events(ingest.ingest_bars(venue, syms), "bar", venue)


def signals_tick() -> None:
    sch = settings.get("schedule")
    now = clock.now()
    for s in db.q("SELECT * FROM strategies WHERE status='active'"):
        s = dict(s)
        if s["venue"] == "NASDAQ":
            close = venues.last_close_before(now)
            if now < close + sch["nasdaq_signal_delay_min"] * 60:
                continue
        exp = freshness.expected_last_bar(s["venue"], now)
        if (s["last_signal_bar"] or 0) >= exp:
            continue
        journal.set_status("trading", f"Evaluating {s['name']} v{s['version']} on the latest bar")
        run_job(f"signals:{s['id']}", live.evaluate_strategy, s)


class AnalystThread(threading.Thread):
    """Runs research and reviews one at a time, off the market loop."""

    def __init__(self, stop: threading.Event, ready: threading.Event):
        super().__init__(daemon=True, name="analyst")
        self.stop = stop
        self.ready = ready
        self.busy = False

    def run(self):
        while not self.stop.is_set() and not self.ready.wait(5):
            pass
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                log.error("analyst tick: %s", traceback.format_exc())
            self.stop.wait(15)

    def tick(self):
        from .agent.research_loop import research_cycle
        from .agent.review import review_all
        sch = settings.get("schedule")
        now = clock.now()
        # daily review
        today = int(now // 86400)
        if time.gmtime(now).tm_hour >= sch["review_hour_utc"] and db.get_meta("last_review_day") != str(today):
            if db.q1("SELECT 1 FROM strategies WHERE status IN ('active','incubating')"):
                self.busy = True
                journal.set_status("reviewing", "Daily review of live strategies")
                run_job("review", review_all)
                self.busy = False
            db.set_meta("last_review_day", str(today))
        # research
        requested = db.get_meta("research_requested")
        day0 = now - now % 86400
        n_today = db.q1("SELECT COUNT(*) n FROM experiments WHERE created_at>=?", (day0,))["n"]
        if settings.get("agent").get("paused") and not requested:
            journal.set_status("paused", "Operator paused the agent: no new research or positions")
            return
        due = job_due("research", sch["research_interval_hours"] * 3600)
        if (due and n_today < sch["max_research_per_day"]) or requested:
            venue = requested if requested in settings.get("universe") else self.next_venue()
            db.set_meta("research_requested", "")
            if venue is None:
                journal.set_status("waiting", "No venue has enough market data to research yet", next_actions=next_actions())
                return
            prev = db.q1("SELECT last_started_at FROM jobs WHERE name='research'")
            self.busy = True
            res = run_job("research", research_cycle, venue)
            self.busy = False
            db.set_meta("last_research_venue", venue)
            if res and res.get("status") in ("no_data", "budget", "llm_error"):
                # a cycle that could not run does not use up the research slot
                db.ex("UPDATE jobs SET last_started_at=? WHERE name='research'", (prev["last_started_at"] if prev else None,))
        journal.set_status("waiting", "Monitoring positions and markets; nothing scheduled right now",
                           next_actions=next_actions())

    @staticmethod
    def next_venue() -> str | None:
        """Alternate between venues that have enough stored history to research."""
        order = [v for v in ("NASDAQ", "CRYPTO") if venue_ready(v)]
        if not order:
            return None
        last = db.get_meta("last_research_venue")
        return order[(order.index(last) + 1) % len(order)] if last in order else order[0]


def venue_ready(venue: str, min_bars: int = 300) -> bool:
    bench = settings.get("benchmarks")[venue]
    tf = venues.VENUES[venue].timeframe
    r = db.q1("SELECT COUNT(*) n FROM bars WHERE symbol=? AND timeframe=?", (bench, tf))
    return r["n"] >= min_bars


def next_actions() -> list[dict]:
    sch = settings.get("schedule")
    now = clock.now()
    out = []
    r = db.q1("SELECT last_started_at FROM jobs WHERE name='research'")
    nxt = (r["last_started_at"] + sch["research_interval_hours"] * 3600) if r and r["last_started_at"] else now
    nv = AnalystThread.next_venue()
    out.append({"what": f"Research cycle ({nv})" if nv else "Research (waiting for market data)", "at": max(nxt, now)})
    out.append({"what": "Next crypto hourly bar close", "at": (int(now // 3600) + 1) * 3600 + 60})
    try:
        if venues.is_open("NASDAQ", now):
            sess = venues.session_for(now)
            out.append({"what": "NASDAQ close, then daily signal evaluation",
                        "at": venues.session_bounds(sess)[1] + sch["nasdaq_signal_delay_min"] * 60})
        else:
            out.append({"what": "NASDAQ opens (queued orders become eligible)", "at": venues.next_open(now)})
    except Exception:
        pass
    day = now - now % 86400 + sch["review_hour_utc"] * 3600
    out.append({"what": "Daily strategy review", "at": day if day > now else day + 86400})
    return sorted(out, key=lambda x: x["at"])


def main() -> None:
    logging.basicConfig(level=os.environ.get("QD_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    db.init(cfg.db_path)
    holder = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    while True:
        try:
            prev = acquire_lock(holder)
            break
        except LockHeld as e:
            log.warning("%s; waiting", e)
            time.sleep(10)
    info = recover(prev)
    log.info("worker %s started; recovery: %s", holder, info)
    journal.write("system", "Worker started", f"Mode: {'DEMO rules analyst (no LLM key)' if cfg.demo_llm else 'LLM ' + cfg.llm_model}; "
                  f"market data: {cfg.market_data_mode}; NASDAQ data: {'Alpaca ' + cfg.alpaca_feed if cfg.alpaca_configured else 'NOT CONFIGURED'}.",
                  author="system")
    stop = threading.Event()

    def _sig(*_):
        log.info("shutdown requested")
        stop.set()
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    ready = threading.Event()
    analyst = AnalystThread(stop, ready)
    analyst.start()
    lost = threading.Event()

    def _beat():
        # Separate thread: slow network calls in the main loop can't make the lock look stale.
        while not stop.is_set():
            try:
                if not heartbeat(holder):
                    lost.set()
                    stop.set()
                    return
            except Exception:
                log.error("heartbeat failed: %s", traceback.format_exc())
            stop.wait(5)
    threading.Thread(target=_beat, daemon=True, name="heartbeat").start()
    last_tick = 0.0
    while not stop.is_set():
        if lost.is_set():
            log.error("lost the worker lock to another process; exiting")
            break
        sch = settings.get("schedule")
        if time.time() - last_tick >= sch["quote_poll_sec"]:
            last_tick = time.time()
            run_job("market_tick", market_tick)
            run_job("bars_tick", bars_tick)
            run_job("signals_tick", signals_tick)
            ready.set()
        stop.wait(2)
    analyst.join(timeout=30)
    db.ex("UPDATE worker_lock SET heartbeat_at=? WHERE id=1 AND holder=?", (clock.now() - LOCK_STALE, holder))
    log.info("worker stopped")


if __name__ == "__main__":
    main()

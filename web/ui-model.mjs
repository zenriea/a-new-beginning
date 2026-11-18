// Small, dependency-free display models shared by the UI and its regression tests.
export function limitUsage(value, limit) {
  if (!Number.isFinite(value) || !Number.isFinite(limit) || limit <= 0) return 0;
  return Math.min(100, Math.max(0, Math.abs(value) / limit * 100));
}

export function equityInRange(rows, range, now) {
  const seconds = { day: 86400, week: 7 * 86400, month: 30 * 86400 }[range];
  if (!seconds) return rows;
  return rows.filter((row) => row.ts >= now - seconds);
}

export function deskHealth(s) {
  const c = s.controls;
  if (c.emergency_stop) return { tone: "bad", label: "Emergency stop active" };
  if (c.system_halt) return { tone: "bad", label: "Ledger integrity halt" };
  if (c.drawdown_halt) return { tone: "bad", label: "Drawdown limit reached" };
  if (!s.worker.alive) return { tone: "bad", label: "Worker offline" };
  if (c.paused) return { tone: "warn", label: "Agent paused · exits monitored" };
  if (s.data.problems.length || (!s.mode.alpaca_configured && s.mode.market_data !== "synthetic")) {
    return { tone: "warn", label: "Market data needs attention" };
  }
  if (s.mode.llm !== "demo" && (s.budget.day_remaining <= 0 || s.budget.month_remaining <= 0)) {
    return { tone: "warn", label: "AI budget exhausted" };
  }
  return { tone: "ok", label: "Desk online · paper mode" };
}

// One sentence that answers "why is (or isn't) the desk trading right now?".
export function tradingVerdict(s) {
  const c = s.controls, p = s.pipeline || {};
  const live = (p.live?.strategies || 0) + (p.live?.probes || 0);
  const fmt = (v) => (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v * 100).toFixed(1) + "%";
  if (c.emergency_stop) return { tone: "bad", text: "Emergency stop is on, so nothing trades." };
  if (!s.worker.alive) return { tone: "bad", text: "The worker is offline, so nothing is researched or traded." };
  if (c.drawdown_halt || c.system_halt) return { tone: "bad", text: "A risk halt blocks new positions; exits still run." };
  if (s.positions.length) return { tone: "ok", text: `Trading: ${s.positions.length} open position${s.positions.length > 1 ? "s" : ""} across ${live} live sleeve${live === 1 ? "" : "s"}.` };
  if (live) return { tone: "ok", text: `${live} live (${p.live.probes || 0} probe${p.live.probes === 1 ? "" : "s"}). Waiting for entry signals on the next bar close.` };
  if (c.paused) return { tone: "warn", text: "The agent is paused: no research and no new positions." };
  if (!p.finished) return { tone: "info", text: "No finished experiment in the last 7 days yet. The first research cycle needs market data." };
  const best = (p.candidates || [])[0];
  const top = (p.gate_failures || [])[0];
  if (best && best.test_return > 0) {
    if (best.probe_blocker) return { tone: "warn", text: `Best idea #${best.id} made ${fmt(best.test_return)} on its sealed test, but no probe started: ${best.probe_blocker}.` };
    return { tone: "info", text: `Best idea #${best.id} made ${fmt(best.test_return)} on test; a probe starts after the next research decision.` };
  }
  return { tone: "warn", text: `None of the ${p.finished} experiment${p.finished === 1 ? "" : "s"} made money on its sealed test (after costs), so nothing qualifies.` +
    (top ? ` Most common failure: ${top.gate.replaceAll("_", " ")} (${top.n}×).` : "") };
}

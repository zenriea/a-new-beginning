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

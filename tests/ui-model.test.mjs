import { strict as assert } from "node:assert";
import { test } from "node:test";
import { deskHealth, equityInRange, limitUsage } from "../web/ui-model.mjs";

const normal = () => ({
  controls: { emergency_stop: false, system_halt: false, drawdown_halt: false, paused: false },
  worker: { alive: true }, data: { problems: [] },
  mode: { alpaca_configured: true, market_data: "live", llm: "configured" },
  budget: { day_remaining: 5, month_remaining: 40 },
});

test("a connected paper desk is explicitly labelled paper", () => {
  assert.deepEqual(deskHealth(normal()), { tone: "ok", label: "Desk online · paper mode" });
});

for (const [flag, label] of [
  ["emergency_stop", "Emergency stop active"], ["system_halt", "Ledger integrity halt"],
  ["drawdown_halt", "Drawdown limit reached"],
]) {
  test(`${flag} cannot be hidden behind a healthy worker`, () => {
    const s = normal(); s.controls[flag] = true;
    assert.deepEqual(deskHealth(s), { tone: "bad", label });
  });
}

test("emergency stop takes precedence over pause and data problems", () => {
  const s = normal(); s.controls.emergency_stop = s.controls.paused = true; s.data.problems.push({});
  assert.equal(deskHealth(s).label, "Emergency stop active");
});

test("an offline worker is never reported as a paused-but-healthy worker", () => {
  const s = normal(); s.worker.alive = false; s.controls.paused = true;
  assert.deepEqual(deskHealth(s), { tone: "bad", label: "Worker offline" });
});

test("pause makes it clear that exit monitoring continues", () => {
  const s = normal(); s.controls.paused = true;
  assert.deepEqual(deskHealth(s), { tone: "warn", label: "Agent paused · exits monitored" });
});

test("stale data warns without calling the desk offline", () => {
  const s = normal(); s.data.problems.push({ key: "bars:AAPL" });
  assert.deepEqual(deskHealth(s), { tone: "warn", label: "Market data needs attention" });
});

test("missing stock-data credentials warn only for non-synthetic data", () => {
  const s = normal(); s.mode.alpaca_configured = false;
  assert.equal(deskHealth(s).tone, "warn");
  s.mode.market_data = "synthetic";
  assert.equal(deskHealth(s).tone, "ok");
});

test("exhausting either live AI budget warns", () => {
  for (const key of ["day_remaining", "month_remaining"]) {
    const s = normal(); s.budget[key] = 0;
    assert.equal(deskHealth(s).label, "AI budget exhausted");
  }
});

test("the rules analyst does not falsely require an AI budget", () => {
  const s = normal(); s.mode.llm = "demo"; s.budget.day_remaining = s.budget.month_remaining = 0;
  assert.equal(deskHealth(s).tone, "ok");
});

test("risk-meter ratios clamp safely, including negative drawdown", () => {
  assert.equal(limitUsage(-0.05, 0.1), 50);
  assert.equal(limitUsage(0.4, 0.8), 50);
  assert.equal(limitUsage(1.2, 0.8), 100);
  assert.equal(limitUsage(0, 0.8), 0);
});

test("missing or invalid limits cannot produce NaN/Infinity CSS widths", () => {
  for (const [value, limit] of [[0.1, 0], [0.1, -1], [null, 1], [NaN, 1], [1, undefined], [Infinity, 1]]) {
    assert.equal(limitUsage(value, limit), 0);
  }
});

const now = 100 * 86400;
const rows = [40, 69, 70, 92, 93, 98, 99, 100].map((day) => ({ ts: day * 86400, equity: day }));
for (const [range, days] of [
  ["day", [99, 100]], ["week", [93, 98, 99, 100]], ["month", [70, 92, 93, 98, 99, 100]],
]) {
  test(`${range} range includes the boundary and preserves chronological order`, () => {
    assert.deepEqual(equityInRange(rows, range, now).map((row) => row.equity), days);
    assert.equal(rows.length, 8); // Filtering must never mutate the cached history.
  });
}

test("All retains every real snapshot without inventing history", () => {
  assert.equal(equityInRange(rows, "all", now), rows);
  assert.deepEqual(equityInRange([], "all", now), []);
});

test("a stale account has an empty recent range rather than relabelled old data", () => {
  assert.deepEqual(equityInRange(rows, "day", now + 2 * 86400), []);
});

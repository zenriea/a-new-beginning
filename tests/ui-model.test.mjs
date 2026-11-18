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

test("trading verdict explains why nothing trades", async () => {
  const { tradingVerdict } = await import("../web/ui-model.mjs");
  const base = () => ({ controls: { emergency_stop: false, paused: false }, worker: { alive: true }, positions: [],
    pipeline: { finished: 4, live: { strategies: 0, probes: 0 }, gate_failures: [{ gate: "test_sharpe", n: 4 }],
      candidates: [{ id: 7, test_return: -0.02 }] } });
  assert.match(tradingVerdict(base()).text, /None of the 4 experiments made money.*Test Sharpe \(4×\)/);
  const s = base(); s.pipeline.candidates[0] = { id: 9, test_return: 0.031, probe_blocker: "3 probe(s) already running (limit 3)" };
  assert.match(tradingVerdict(s).text, /#9 made \+3\.1%.*already running/);
  s.pipeline.live.probes = 1; assert.equal(tradingVerdict(s).tone, "probe");  // probes are not evidence: never success green
  s.pipeline.live.strategies = 1; assert.equal(tradingVerdict(s).tone, "ok"); s.pipeline.live.strategies = 0;
  s.positions = [{}]; assert.match(tradingVerdict(s).text, /^Trading: 1 open position across 1 probe\./);
  s.controls.emergency_stop = true; assert.equal(tradingVerdict(s).tone, "bad");
});

test("probe-only positions are labelled as forward tests, not an edge", async () => {
  const { tradingVerdict } = await import("../web/ui-model.mjs");
  const s = { controls: {}, worker: { alive: true }, positions: [{}], pipeline: { live: { strategies: 0, probes: 1 } } };
  const v = tradingVerdict(s);
  assert.equal(v.tone, "probe");
  assert.match(v.text, /not evidence of an edge/);
});

test("raw gate keys in server text become the same labels the pipeline uses", async () => {
  const { friendlyGates, gateLabel, gateCount } = await import("../web/ui-model.mjs");
  assert.equal(friendlyGates("Failed gates: validation_sharpe, test_sharpe_delay_1bar, test_sharpe."),
    "Failed gates: Validation Sharpe, 1-bar delay, Test Sharpe.");
  assert.equal(gateLabel("test_trades"), "Min trades");
  assert.equal(gateLabel("brand_new_gate"), "brand new gate");
  assert.equal(gateCount(9, 4), "5 of 9 passed");
  assert.equal(gateCount(9, 0), "9 of 9 passed");
});

test("limits are shown as percentages and read back as exact fractions", async () => {
  const { toDisplay, fromDisplay, formatLimit } = await import("../web/ui-model.mjs");
  assert.equal(toDisplay(0.07, "pct"), "7");
  assert.equal(toDisplay(0.025, "pct"), "2.5");
  assert.deepEqual(fromDisplay("7", "pct"), { value: 0.07 });
  assert.deepEqual(fromDisplay("12.5%", "pct", [0, 1]), { value: 0.125 });
  assert.match(fromDisplay("150", "pct", [0, 1]).error, /Allowed range is 0% to 100%/);
  assert.match(fromDisplay("", "pct").error, /Enter a value/);
  assert.match(fromDisplay("abc", "usd").error, /Enter a number/);
  assert.match(fromDisplay("2.5", "int").error, /whole number/);
  assert.equal(formatLimit(20000, "usd"), "$20,000");
  assert.equal(formatLimit(7, "hour"), "07:00 UTC");
});

test("loosening is detected in the direction that adds risk", async () => {
  const { changeDirection, LIMIT_FIELDS } = await import("../web/ui-model.mjs");
  const f = (k) => LIMIT_FIELDS.find((x) => x.k === k);
  assert.equal(changeDirection(f("max_drawdown_pct"), 0.1, 0.25), "loosens");
  assert.equal(changeDirection(f("max_drawdown_pct"), 0.1, 0.05), "tightens");
  assert.equal(changeDirection(f("min_test_sharpe"), 0.5, 0), "loosens");
  assert.equal(changeDirection(f("min_test_trades"), 8, 20), "tightens");
  assert.equal(changeDirection(f("enabled"), false, true), "loosens");
  assert.equal(changeDirection(f("crypto_research_hours"), 2, 8), "changes");
  for (const field of LIMIT_FIELDS) assert.ok([-1, 0, 1].includes(field.loosens), field.k);
});

test("a paused desk never reads as waiting to trade, in any live state", async () => {
  const { tradingVerdict } = await import("../web/ui-model.mjs");
  const mk = (live, positions) => ({ controls: { paused: true }, worker: { alive: true }, positions,
    pipeline: { finished: 3, live, candidates: [], gate_failures: [] } });
  for (const [live, positions] of [[{ strategies: 0, probes: 0 }, []], [{ strategies: 0, probes: 1 }, []],
    [{ strategies: 2, probes: 1 }, []], [{ strategies: 1, probes: 0 }, [{}, {}]]]) {
    const v = tradingVerdict(mk(live, positions));
    assert.equal(v.tone, "warn");
    assert.match(v.text, /^Paused: no research and no new entries\./);
    assert.doesNotMatch(v.text, /Waiting for entry|Trading:/);
  }
  assert.match(tradingVerdict(mk({ strategies: 0, probes: 1 }, [])).text, /1 probe keeps its sleeve/);
  assert.match(tradingVerdict(mk({ strategies: 1, probes: 0 }, [{}, {}])).text, /2 open positions are held/);
});

test("each control state outranks the next: stop, worker, halt, pause", async () => {
  const { tradingVerdict } = await import("../web/ui-model.mjs");
  const s = { controls: { emergency_stop: true, paused: true, drawdown_halt: true }, worker: { alive: false }, positions: [{}],
    pipeline: { live: { strategies: 1, probes: 0 } } };
  assert.match(tradingVerdict(s).text, /^Emergency stop/);
  s.controls.emergency_stop = false; assert.match(tradingVerdict(s).text, /worker is offline/);
  s.worker.alive = true; assert.match(tradingVerdict(s).text, /risk halt/);
  s.controls.drawdown_halt = false; assert.match(tradingVerdict(s).text, /^Paused/);
  s.controls.paused = false; assert.match(tradingVerdict(s).text, /^Trading: 1 open position across 1 strategy\./);
});

test("live labels count strategies and probes separately", async () => {
  const { liveLabel } = await import("../web/ui-model.mjs");
  assert.equal(liveLabel(1, 2), "1 strategy and 2 probes");
  assert.equal(liveLabel(0, 1), "1 probe");
  assert.equal(liveLabel(3, 0), "3 strategies");
});

// QuantDesk UI. Vanilla JS; every server string is escaped before it touches the DOM.
import { deskHealth, equityInRange, limitUsage, tradingVerdict } from "./ui-model.mjs?v=20261118-pipeline";

const $ = (s, el = document) => el.querySelector(s);
const state = { s: null, journal: [], lastId: 0, filter: new Set(), tab: "research", charts: {}, symbol: null, es: null,
  timers: [], booting: false, tabVersion: 0, lastRefresh: null, equityRows: [], equityRange: "all" };

// ---------------------------------------------------------------- helpers
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const rich = (v) => esc(v).replace(/`([^`\n]{1,300})`/g, "<code>$1</code>");
const money = (v, d = 0) => v == null || isNaN(v) ? "–" : (v < 0 ? "−$" : "$") + Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const pct = (v, d = 2, sign = true) => v == null || isNaN(v) ? "–" : (sign && v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v * 100).toFixed(d) + "%";
const num = (v, d = 2) => v == null || isNaN(v) ? "–" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: Math.min(d, 2) > 0 && Math.abs(v) < 1000 ? Math.min(d, 2) : 0 });
const qty = (v) => v == null ? "–" : Number.isInteger(v) ? v.toLocaleString() : Number(v).toLocaleString(undefined, { maximumFractionDigits: 6 });
const px = (v) => v == null ? "–" : Number(v).toLocaleString(undefined, { maximumFractionDigits: v < 0.01 ? 10 : v < 1 ? 6 : v < 100 ? 4 : 2, minimumFractionDigits: 2 });
const signCls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
const arrow = (v) => (v > 0 ? "▲ " : v < 0 ? "▼ " : "");
const dt = (ts) => ts ? new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "–";
const day = (ts) => ts ? new Date(ts * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "–";
function ago(ts) {
  if (!ts) return "–";
  const s = Date.now() / 1000 - ts;
  if (s < 0) return "in " + until(ts);
  if (s < 60) return Math.round(s) + "s ago";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
}
function until(ts) {
  const s = ts - Date.now() / 1000;
  if (s <= 0) return "now";
  if (s < 3600) return Math.round(s / 60) + "m";
  if (s < 86400) return (s / 3600).toFixed(1) + "h";
  return (s / 86400).toFixed(1) + "d";
}
const authorLabel = (a) => ({ llm: "AI analyst", rules: "Rules engine", system: "System", operator: "You" }[a] || a);

async function api(path, opts = {}) {
  const init = { credentials: "same-origin", headers: {} };
  if (opts.body !== undefined) {
    init.method = "POST"; init.headers["Content-Type"] = "application/json"; init.headers["X-QD"] = "1";
    init.body = JSON.stringify(opts.body);
  } else if (opts.method) { init.method = opts.method; init.headers["X-QD"] = "1"; }
  const r = await fetch(path, init);
  if (r.status === 401 && path !== "/api/login") { showLogin(); throw new Error("login required"); }
  if (!r.ok) {
    let m = r.statusText;
    try { const d = (await r.json()).detail; m = Array.isArray(d) ? d.map((x) => x.msg).join("; ") : d || m; } catch {}
    throw new Error(m);
  }
  return r.json();
}
// Toasts stack in one live region; errors and results stay longer than confirmations.
function toast(msg, opts = {}) {
  const tone = opts.tone || (/fail|error|denied|invalid|must|not |unknown|cannot/i.test(msg) ? "bad" : "info");
  let stack = $("#toasts");
  if (!stack) { stack = document.createElement("div"); stack.id = "toasts"; stack.className = "toast-stack"; stack.setAttribute("aria-live", "polite"); document.body.appendChild(stack); }
  const t = document.createElement("div"); t.className = `toast ${tone}`;
  t.innerHTML = `<span class="toast-dot" aria-hidden="true"></span><div class="toast-text"><b></b>${opts.detail ? "<small></small>" : ""}</div><button class="toast-x" aria-label="Dismiss">×</button>`;
  t.querySelector("b").textContent = msg;
  if (opts.detail) t.querySelector("small").textContent = opts.detail;
  if (opts.exp) { t.classList.add("clickable"); t.dataset.exp = opts.exp; }
  const remove = () => { t.classList.add("out"); setTimeout(() => t.remove(), 200); };
  t.querySelector(".toast-x").onclick = (e) => { e.stopPropagation(); remove(); };
  stack.appendChild(t);
  while (stack.children.length > 4) stack.firstChild.remove();
  setTimeout(remove, opts.ms || (tone === "bad" ? 9000 : opts.detail ? 8000 : 4000));
}
// Journal entries worth interrupting the operator for (research outcomes, trades, risk).
function notifyFromJournal(j) {
  const t = j.title || "", refs = j.refs || {};
  const research = j.kind === "research" || j.kind === "decision" || (j.kind === "test" && /^Re-judged/.test(t));
  const failed = j.kind === "system" && /failed|paused|could not/i.test(t);
  if (!(research || failed || j.kind === "fill" || j.kind === "risk")) return;
  const tone = j.kind === "risk" || failed ? "bad" : /^(Allocating|New (strategy|probe) live)/.test(t) || j.kind === "fill" ? "good" : "info";
  toast(t, { tone, detail: (j.body || "").slice(0, 160) + ((j.body || "").length > 160 ? "…" : ""), exp: refs.experiment_id });
}
const CTL_MSG = { research_nasdaq: ["NASDAQ research started", "Takes about a minute. You'll get a notification with the result."],
  research_crypto: ["Crypto research started", "Takes about a minute. You'll get a notification with the result."],
  flatten_all: ["Flatten-all requested", "Positions close at the next usable quotes."] };
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
const outlineIcon = (path) => `<svg viewBox="0 0 24 24" aria-hidden="true">${path}</svg>`;
const researchIcon = outlineIcon('<path d="M9 3h6m-5 0v6L4 19a1.4 1.4 0 0 0 1.2 2h13.6a1.4 1.4 0 0 0 1.2-2L14 9V3M7 15h10"/>');
function emptyState(title, description, action = "") {
  return `<div class="empty-state"><span class="empty-symbol">${researchIcon}</span><h3>${esc(title)}</h3><p>${esc(description)}</p>${action}</div>`;
}

// Navigation jumps to the existing live records, not a second copy of the data.
function markNavigation(key) {
  document.querySelectorAll(".nav-item").forEach((item) => {
    const active = item.dataset.nav === key;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "location"); else item.removeAttribute("aria-current");
  });
}
function selectTab(key) {
  const tab = $(`#tabs [data-tab="${key}"]`);
  if (!tab) return;
  document.querySelectorAll("#tabs .tab").forEach((item) => {
    const active = item === tab;
    item.classList.toggle("active", active); item.setAttribute("aria-selected", String(active)); item.tabIndex = active ? 0 : -1;
  });
  $("#tab-body").setAttribute("aria-labelledby", tab.id);
  state.tab = key; markNavigation(key); renderTab();
}
document.addEventListener("click", (event) => {
  const link = event.target.closest("[data-nav]");
  if (!link) return;
  event.preventDefault();
  const key = link.dataset.nav;
  if (!["overview", "journal", "pipeline"].includes(key)) selectTab(key); else if (key !== "pipeline") markNavigation(key);
  if (key === "pipeline") markNavigation(key);
  const target = $(key === "overview" ? "#overview" : key === "journal" ? "#journal" : key === "pipeline" ? "#pipeline" : "#records");
  if (target) { target.scrollIntoView({ behavior: "instant", block: "start" }); target.focus({ preventScroll: true }); }
});

// ---------------------------------------------------------------- auth
function stopUpdates() {
  state.timers.forEach(clearInterval); state.timers = [];
  if (state.es) { state.es.close(); state.es = null; }
}
function showLogin() {
  stopUpdates(); if (closeDrawer) closeDrawer();
  $("#app").classList.add("hidden"); $("#login-view").classList.remove("hidden");
}
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { await api("/api/login", { body: { password: $("#login-pw").value } }); $("#login-pw").value = ""; $("#login-err").textContent = ""; await boot(); }
  catch (err) { $("#login-err").textContent = err.message; }
});
$("#btn-logout").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); showLogin(); $("#login-pw").focus(); }
  catch (err) { toast(err.message); }
});

// ---------------------------------------------------------------- theme
function applyTheme(t) {
  if (t) document.documentElement.setAttribute("data-theme", t); else document.documentElement.removeAttribute("data-theme");
  Object.values(state.charts).forEach((c) => c && c.theme && c.theme());
}
try { applyTheme(localStorage.getItem("qd-theme") || "dark"); } catch { applyTheme("dark"); }  // black is the house theme
$("#btn-theme").addEventListener("click", () => {
  const dark = document.documentElement.getAttribute("data-theme") === "dark" ||
    (!document.documentElement.getAttribute("data-theme") && matchMedia("(prefers-color-scheme: dark)").matches);
  const t = dark ? "light" : "dark";
  try { localStorage.setItem("qd-theme", t); } catch {}
  applyTheme(t);
});

// ---------------------------------------------------------------- top bar & alerts
function renderBadges(s) {
  const b = [];
  b.push(`<span class="badge paper">PAPER · no real money</span>`);
  b.push(s.mode.llm === "demo"
    ? `<span class="badge warn"><span class="dot"></span>DEMO analyst · no AI configured</span>`
    : `<span class="badge ok" title="AI analyst: ${esc(s.mode.llm)}"><span class="dot"></span>AI: ${esc(s.mode.llm.split(" (")[0])}</span>`);
  if (s.mode.market_data === "synthetic") b.push(`<span class="badge warn"><span class="dot"></span>SYNTHETIC market data</span>`);
  b.push(`<span class="badge ${s.worker.alive ? "ok" : "bad"}"><span class="dot"></span>Worker ${s.worker.alive ? "running" : "offline"}</span>`);
  const n = s.sessions.NASDAQ;
  b.push(`<span class="badge ${n.open ? "ok" : ""}"><span class="dot"></span>NASDAQ ${n.open ? "open · closes in " + until(n.closes_at) : "closed · opens in " + (n.opens_at ? until(n.opens_at) : "?")}</span>`);
  b.push(`<span class="badge ok"><span class="dot"></span>Crypto 24/7</span>`);
  const bud = s.budget;
  b.push(`<span class="badge ${bud.day_remaining <= 0 || bud.month_remaining <= 0 ? "bad" : ""}" title="AI spend this month (daily $${bud.day_spent.toFixed(2)} / $${bud.daily_cap})">AI spend $${bud.month_spent.toFixed(2)} / $${bud.monthly_cap}</span>`);
  $("#badges").innerHTML = b.join("");
  const c = s.controls;
  $("#btn-pause").textContent = c.paused ? "Resume agent" : "Pause agent";
  $("#btn-stop").textContent = c.emergency_stop ? "Clear emergency stop" : "Emergency stop";
  $("#btn-pause").setAttribute("aria-pressed", String(c.paused));
  $("#nav-research-count").textContent = s.counts.experiments.toLocaleString();
  const live = (s.pipeline?.live.strategies || 0) + (s.pipeline?.live.probes || 0);
  $("#nav-live-count").textContent = live ? `${live} live` : "idle";
  $("#nav-live-count").classList.toggle("on", live > 0);
  const health = deskHealth(s);
  $("#desk-health").className = `desk-health ${health.tone}`;
  $("#desk-health").innerHTML = `<span class="health-indicator"></span><div><b>${esc(health.label)}</b><small>Updated ${esc(new Date().toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }))}</small></div>`;
  $("#desk-health").title = health.label;
}
function renderAlerts(s) {
  const a = [];
  const c = s.controls;
  if (c.emergency_stop) a.push(["critical", "!", "Emergency stop is active. All agent trading is halted; positions are held. Use Flatten all (Limits & system tab) to close them."]);
  else if (c.paused) a.push(["warning", "‖", "The agent is paused: no new research or positions. Exits, fills and risk monitoring continue."]);
  if (c.drawdown_halt) a.push(["critical", "!", `Max drawdown limit reached. New risk is halted until you reset it (Limits & system).`]);
  if (c.system_halt) a.push(["critical", "!", "Ledger integrity check failed at startup; new buys are blocked. See Limits & system."]);
  if (!s.worker.alive) a.push(["critical", "!", "The background worker is not running, so nothing is being researched or traded. Check the worker service."]);
  if (!s.mode.alpaca_configured && s.mode.market_data !== "synthetic") a.push(["warning", "!", "NASDAQ data is not configured (Alpaca keys missing). NASDAQ research and trading are stopped; crypto continues."]);
  if (s.data.problems.length) a.push(["warning", "!", `Data unavailable or stale: ${s.data.problems.slice(0, 6).map((d) => esc(d.key)).join(", ")}${s.data.problems.length > 6 ? "…" : ""}. Dependent actions are stopped.`]);
  if (s.mode.llm === "demo") a.push(["info", "i", "Demo mode: no AI provider is configured (QD_LLM_* or ANTHROPIC_API_KEY in .env), so a deterministic rules engine stands in for the AI analyst. Its entries are labelled “Rules engine”. Research, tests and trading are real computations."]);
  $("#alerts").innerHTML = a.map(([k, i, t]) => `<div class="alert ${k}"><span class="icon">${i}</span><div>${t}</div></div>`).join("");
}
$("#btn-pause").addEventListener("click", async () => {
  const act = state.s?.controls.paused ? "resume" : "pause";
  try { await api(`/api/control/${act}`, { method: "POST" }); await refresh(); } catch (err) { toast(err.message); }
});
$("#btn-stop").addEventListener("click", async () => {
  try {
    if (state.s?.controls.emergency_stop) { await api("/api/control/clear_emergency", { method: "POST" }); await refresh(); return; }
    if (!confirm("Emergency stop: halt all agent trading and cancel open orders? Positions are kept.")) return;
    await api("/api/control/emergency_stop", { method: "POST" }); await refresh();
  } catch (err) { toast(err.message); }
});

// ---------------------------------------------------------------- KPIs
function renderKPIs(s) {
  const a = s.account;
  const excess = a.benchmark_return != null ? a.return - a.benchmark_return : null;
  const usage = limitUsage(a.exposure, s.limits.max_gross_pct), drawdown = limitUsage(a.drawdown, s.limits.max_drawdown_pct);
  const track = (value) => `<div class="metric-track ${value >= 100 ? "bad" : value >= 80 ? "warn" : ""}" aria-hidden="true"><span style="width:${value}%"></span></div>`;
  const k = [
    ["Account equity", money(a.equity), `${money(a.cash)} available cash`, '<rect x="3" y="5" width="18" height="15" rx="3"/><path d="M3 9h18m-5 5h2"/>', ""],
    ["Net P&L", `<span class="${signCls(a.pnl)}">${money(a.pnl)}</span>`, `<span class="${signCls(a.return)}">${pct(a.return)}</span> since inception`, '<path d="m3 17 6-6 4 4 8-10m-6 0h6v6"/>', ""],
    ["Gross exposure", pct(a.exposure, 1, false), `${money(a.gross)} / ${pct(s.limits.max_gross_pct, 0, false)} limit`, '<path d="M4 20V10m5 10V4m6 16v-7m5 7V7"/>', track(usage)],
    ["Current drawdown", `<span class="${a.drawdown < 0 ? "down" : ""}">${pct(a.drawdown, 2, false)}</span>`, `Halt at −${(s.limits.max_drawdown_pct * 100).toFixed(1)}%`, '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>', track(drawdown)],
  ];
  $("#kpis").innerHTML = k.map(([l, v, sub, icon, meter]) => `<div class="kpi"><div class="label">${l}${outlineIcon(icon)}</div><div class="value">${v}</div><div class="sub">${sub}</div>${meter}</div>`).join("");
  $("#performance-stats").innerHTML = `
    <div class="performance-stat"><div class="label">vs. ${esc(a.benchmark_symbol)} buy &amp; hold</div><div class="value ${signCls(excess)}">${excess == null ? "–" : pct(excess)}</div><small>Since inception · percentage-point difference</small></div>
    <div class="performance-stat"><div class="label">Fees paid</div><div class="value">${money(a.fees_paid, 2)}</div><small>${s.counts.fills} fills · ${s.counts.orders} orders</small></div>
    <div class="performance-stat"><div class="label">Crypto exposure</div><div class="value">${money(a.crypto_gross)}</div><small>Equity high-water mark ${money(a.peak)}</small></div>`;
}

// ---------------------------------------------------------------- equity chart
function themedChartOptions() {
  return {
    layout: { background: { type: "solid", color: cssVar("--surface-1") }, textColor: cssVar("--text-secondary"), fontFamily: getComputedStyle(document.body).fontFamily, fontSize: 11 },
    grid: { vertLines: { visible: false }, horzLines: { color: cssVar("--border"), style: 2 } },
    rightPriceScale: { borderVisible: false }, timeScale: { borderVisible: false, timeVisible: true },
    crosshair: { mode: 0 },
    localization: { priceFormatter: (v) => Math.abs(v) >= 1000 ? "$" + Math.round(v).toLocaleString() : Math.abs(v) < 1 ? String(Number(v.toPrecision(4))) : v.toFixed(Math.abs(v) < 10 ? 3 : 2) },
  };
}
function ensureEquityChart() {
  if (state.charts.equity) return state.charts.equity;
  const el = $("#equity-chart");
  const chart = LightweightCharts.createChart(el, { ...themedChartOptions(), autoSize: true });
  const mk = (v) => chart.addLineSeries({ color: cssVar(v), lineWidth: 2, priceLineVisible: false, lastValueVisible: true });
  const area = () => ({ lineColor: cssVar("--series-1"), topColor: cssVar("--accent-soft"), bottomColor: "rgba(0,0,0,0)" });
  const acct = chart.addAreaSeries({ ...area(), lineWidth: 2, priceLineVisible: false, lastValueVisible: true });
  const c = { chart, acct, bench: mk("--series-2"), btc: mk("--series-3") };
  c.theme = () => { chart.applyOptions(themedChartOptions()); c.acct.applyOptions(area()); c.bench.applyOptions({ color: cssVar("--series-2") }); c.btc.applyOptions({ color: cssVar("--series-3") }); };
  chart.subscribeCrosshairMove((p) => {
    if (!p || !p.time) { $("#equity-tip").textContent = ""; return; }
    const g = (ser) => { const d = p.seriesData.get(ser); return d ? money(d.value) : "–"; };
    $("#equity-tip").textContent = `${dt(p.time)}   Account ${g(c.acct)}   ${state.s?.account.benchmark_symbol || "QQQ"} ${g(c.bench)}   BTC ${g(c.btc)}`;
  });
  state.charts.equity = c;
  return c;
}
async function loadEquity() {
  state.equityRows = await api("/api/equity");
  renderEquity();
}
function renderEquity() {
  const rows = equityInRange(state.equityRows, state.equityRange, Date.now() / 1000);
  const c = ensureEquityChart();
  const ser = (f) => rows.filter((r) => r[f] != null).map((r) => ({ time: Math.floor(r.ts), value: r[f] }));
  c.acct.setData(ser("equity")); c.bench.setData(ser("benchmark_equity")); c.btc.setData(ser("crypto_benchmark_equity"));
  $("#equity-tip").textContent = "";
  const note = $("#equity-range-note");
  note.classList.toggle("hidden", rows.length > 0);
  $("#equity-chart").classList.toggle("collapsed", rows.length === 0);
  note.textContent = !state.equityRows.length ? "No equity snapshots yet. The worker records the account once per minute." :
    "No snapshots in this period. Choose All to see the recorded history.";
  c.chart.timeScale().fitContent();
  const bs = state.s?.account.benchmark_symbol || "QQQ";
  $("#equity-legend").innerHTML = [["--series-1", "Account (net of costs)"], ["--series-2", `${bs} buy & hold`], ["--series-3", "BTC-USD buy & hold"]]
    .map(([v, l]) => `<span><span class="sw" style="background:var(${v})"></span>${esc(l)}</span>`).join("");
  $("#equity-since").textContent = rows.length ? `${day(rows[0].ts)} — ${day(rows[rows.length - 1].ts)}` : "No recorded marks in this period";
}
$("#equity-ranges").addEventListener("click", (event) => {
  const button = event.target.closest("[data-range]");
  if (!button) return;
  state.equityRange = button.dataset.range;
  $("#equity-ranges").querySelectorAll("button").forEach((item) => {
    item.classList.toggle("active", item === button); item.setAttribute("aria-pressed", String(item === button));
  });
  renderEquity();
});

// ---------------------------------------------------------------- desk view
function renderNow(s) {
  const a = s.agent || {};
  const nexts = (a.next_actions || []).slice(0, 4);
  $("#now").innerHTML = `
    <div class="now-activity act-${esc(a.activity || "starting")}">${esc(cap(a.activity || "starting"))}</div>
    <div class="now-detail">${esc(a.detail || "")} <span class="muted">· ${ago(a.updated_at)}</span></div>
    ${s.open_orders.length ? `<div class="now-orders"><b>${s.open_orders.length}</b> open order${s.open_orders.length > 1 ? "s" : ""} waiting for a fill</div>` : ""}
    <ul class="next-list">${nexts.map((n) => `<li><span>${esc(n.what)}</span><span class="muted num">${until(n.at) === "now" ? "now" : "in " + until(n.at)}</span></li>`).join("")}</ul>`;
}
const cap = (x) => x.charAt(0).toUpperCase() + x.slice(1);

// ---------------------------------------------------------------- pipeline: why the desk is (not) trading
function renderPipeline(s) {
  const p = s.pipeline;
  if (!p) return;
  const v = tradingVerdict(s);
  const live = p.live.strategies + p.live.probes;
  const steps = [
    ["Tested", p.finished, `${p.experiments} started`],
    ["Profitable on test", p.profitable, "net of costs"],
    ["Passed all gates", p.passed, "full allocation"],
    ["Live", live, `${p.live.probes} probe${p.live.probes === 1 ? "" : "s"} included`],
    ["Positions", s.positions.length, s.open_orders.length ? `${s.open_orders.length} order${s.open_orders.length > 1 ? "s" : ""} pending` : "open now"],
  ];
  const maxFail = Math.max(1, ...p.gate_failures.map((g) => g.n));
  const probe = p.probe;
  $("#pipeline-window").textContent = `last ${p.window_days} days`;
  $("#pipeline-body").innerHTML = `
    <div class="verdict ${esc(v.tone)}"><span class="verdict-dot" aria-hidden="true"></span><p>${esc(v.text)}</p></div>
    <ol class="funnel">${steps.map(([label, n, sub], i) => `<li class="${n > 0 ? "has" : ""}${i === steps.length - 1 && n > 0 ? " goal" : ""}">
      <span class="funnel-n num">${n}</span><span class="funnel-label">${label}</span><small>${esc(sub)}</small></li>`).join("")}</ol>
    <div class="pipeline-grid">
      <div><h3>Closest to trading</h3>
        ${p.candidates.length ? `<ul class="cands">${p.candidates.map((c) => `<li>
          <div class="cand-main"><a href="#" data-exp="${c.id}">#${c.id} ${esc(c.title)}</a>
            <small>${esc(c.venue)} · Sharpe ${num(c.sharpe)} · ${c.trades ?? "–"} trades · ${c.failed.length ? `${c.failed.length}/${c.n_checks} gates failed` : "all gates passed"}</small></div>
          <span class="cand-ret num ${signCls(c.test_return)}">${pct(c.test_return, 1)}</span>
          ${c.operator_blocker ? `<span class="muted cand-note">${esc(c.operator_blocker)}</span>` :
            `<button class="btn small ${c.test_return > 0 ? "primary" : ""}" data-probe="${c.id}" title="${esc(c.probe_blocker ? "Auto-probe skipped: " + c.probe_blocker : "Paper-trade this idea now with a small probe allocation")}">Start probe</button>`}</li>`).join("")}</ul>`
          : `<p class="muted">No finished experiments in this window.</p>`}</div>
      <div><h3>What blocks ideas</h3>
        ${p.gate_failures.length ? `<ul class="bars">${p.gate_failures.slice(0, 6).map((g) => `<li><span>${esc(gateLabel(g.gate))}</span>
          <span class="bar"><i style="width:${(g.n / maxFail * 100).toFixed(0)}%"></i></span><span class="num">${g.n}</span></li>`).join("")}</ul>`
          : `<p class="muted">No gate failures recorded.</p>`}
        <p class="probe-rule">${probe.enabled ? `<b>Probes on.</b> An idea that made more than ${pct(probe.min_test_return, 1)} on its sealed test gets ${pct(probe.allocation_pct, 0, false)} of equity for ${probe.lifetime_days} days (max ${probe.max_active} at once, ${probe.per_venue_per_day}/venue/day).`
          : `<b>Probes off.</b> Only ideas that pass every gate trade.`} <a href="#records" data-nav="system">Change</a></p></div>
    </div>`;
}
function evidenceLinks(ev) {
  return (ev || []).map((r) => {
    const [t, id] = String(r).split(":");
    if (t === "experiment") return `<a href="#" data-exp="${esc(id)}">${esc(r)}</a>`;
    if (t === "strategy") return `<a href="#" data-strat="${esc(id)}">${esc(r)}</a>`;
    if (t === "order") return `<a href="#" data-order="${esc(id)}">${esc(r)}</a>`;
    return esc(r);
  }).join(", ");
}
function renderBeliefs(s) {
  if (!s.beliefs.length) {
    $("#beliefs").innerHTML = `<div class="belief-placeholder"><span class="empty-symbol">${researchIcon}</span><div><b>Evidence before conviction.</b><p>No evidence-backed beliefs yet. A thesis only appears here when it cites an experiment, strategy, or trade record. Until then, the desk stays open-minded.</p></div></div>`;
    return;
  }
  $("#beliefs").innerHTML = s.beliefs.slice(0, 3).map((b) => `<div class="belief">
    <div><span class="conf">${esc(b.confidence)}</span>${rich(b.statement)}</div>
    <div class="evidence muted">Evidence: ${evidenceLinks(b.evidence)} · ${esc(b.venue || "")} · ${authorLabel(b.author)} · ${ago(b.ts)}</div></div>`).join("");
}

// ---------------------------------------------------------------- positions
const openPos = new Set();
function renderPositions(s) {
  const p = s.positions;
  $("#pos-summary").textContent = p.length ? `${p.length} position${p.length > 1 ? "s" : ""} · ${money(s.account.gross)}` : "All cash";
  if (!p.length) {
    $("#positions").innerHTML = `<div class="empty compact">${s.strategies.length ?
      "All cash. Live sleeves are waiting for their entry rules on the next bar close." :
      "All cash. Nothing is live yet; see the pipeline above for why."}</div>`;
    return;
  }
  const rows = p.map((r, i) => {
    const key = `${r.strategy_id}:${r.symbol}`;
    const open = openPos.has(key);
    return `<tr class="clickable" data-pos="${esc(key)}">
      <td><b>${esc(r.symbol)}</b><div class="muted" style="font-size:12px">${esc(r.venue)}</div></td>
      <td>${esc(r.strategy)}${r.probe ? ' <span class="probe-chip">probe</span>' : ""}</td>
      <td class="r num">${qty(r.qty)}</td>
      <td class="r num">${px(r.avg_cost)}</td>
      <td class="r num">${px(r.mark)}<div class="muted" style="font-size:11px">${esc((r.mark_basis || "").replace("_", " "))} · ${ago(r.mark_as_of)}</div></td>
      <td class="r num">${money(r.value)}</td>
      <td class="r num ${signCls(r.unrealized)}">${arrow(r.unrealized)}${money(r.unrealized)}<div style="font-size:11px">${pct(r.unrealized_pct)}</div></td>
      <td class="r num">${pct(r.weight, 1, false)}</td>
    </tr>
    ${open ? `<tr class="detail-row"><td colspan="8"><div class="detail-grid">
      <div><h4>Why I entered (recorded before the fill)</h4><div class="pre">${rich(r.entry_rationale || "–")}</div>
        ${r.entry_order_id ? `<div style="margin-top:6px"><a href="#" data-order="${r.entry_order_id}">order #${r.entry_order_id}</a></div>` : ""}</div>
      <div><h4>What would make me exit</h4><ul style="margin:0;padding-left:18px">${r.exit_conditions.map((x) => `<li>${rich(x)}</li>`).join("")}</ul>
        <h4 style="margin-top:10px">Exit plan</h4><div class="pre">${rich(r.exit_plan || "–")}</div>
        <div style="margin-top:8px"><button class="btn small" data-askpos="${esc(r.symbol)}">Ask about ${esc(r.symbol)}</button>
        <a href="#" data-strat="${r.strategy_id}" style="margin-left:8px">strategy record</a></div></div>
    </div></td></tr>` : ""}`;
  }).join("");
  $("#positions").innerHTML = `<table class="tbl"><thead><tr><th>Symbol</th><th>Strategy</th><th class="r">Qty</th><th class="r">Avg cost</th>
    <th class="r">Mark</th><th class="r">Value</th><th class="r">Unrealized</th><th class="r">Weight</th></tr></thead><tbody>${rows}</tbody></table>`;
}
$("#positions").addEventListener("click", (e) => {
  const ask = e.target.closest("[data-askpos]");
  if (ask) { e.stopPropagation(); askQuestion(`Why did you buy ${ask.dataset.askpos}, and what would make you sell it?`); return; }
  const tr = e.target.closest("[data-pos]");
  if (!tr || e.target.closest("a")) return;
  const k = tr.dataset.pos; openPos.has(k) ? openPos.delete(k) : openPos.add(k); renderPositions(state.s);
});

// ---------------------------------------------------------------- tabs
$("#tabs").addEventListener("click", (e) => {
  const t = e.target.closest("[data-tab]"); if (!t) return;
  selectTab(t.dataset.tab);
});
$("#tabs").addEventListener("keydown", (e) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
  const tabs = [...$("#tabs").querySelectorAll("[role=tab]")], index = tabs.indexOf(document.activeElement);
  if (index < 0) return;
  e.preventDefault();
  const next = e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1 :
    (index + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
  tabs[next].focus(); tabs[next].click();
});
async function renderTab() {
  const body = $("#tab-body");
  const version = ++state.tabVersion;
  if (state.charts.px) { state.charts.px.chart.remove(); state.charts.px = null; }
  body.setAttribute("aria-busy", "true");
  try {
    if (state.tab === "research") await renderResearch(body, version);
    if (state.tab === "strategies") await renderStrategies(body, version);
    if (state.tab === "orders") await renderOrders(body, version);
    if (state.tab === "market") await renderMarket(body, version);
    if (state.tab === "system") await renderSystem(body, version);
  } catch (err) { if (version === state.tabVersion) body.innerHTML = `<div class="empty">${esc(err.message)}</div>`; }
  finally { if (version === state.tabVersion) body.setAttribute("aria-busy", "false"); }
}

const GATE_LABEL = { validation_sharpe: "Validation", test_sharpe: "Test Sharpe", test_trades: "Too few trades",
  test_return_cost_x2: "2× costs", test_sharpe_delay_1bar: "1-bar delay", neighbor_positive_frac: "Nearby params",
  subperiod_positive_frac: "Sub-periods", top5_concentration: "Top-5 trades", deflated_psr: "Deflated PSR" };
const gateLabel = (n) => GATE_LABEL[n] || n.replaceAll("_", " ");
function gatesCell(e) {
  if (!e.gates) return `<span class="muted">${e.error ? "could not run" : "not evaluated"}</span>`;
  if (e.error && !e.gates) return `<span class="muted">could not run</span>`;
  const failed = e.gates?.failed || [], total = e.gates?.checks?.length || 9;
  if (!failed.length) return `<span class="up">all ${total} passed</span>`;
  const shown = failed.slice(0, 2);
  return `<span class="gate-count">${total - failed.length}/${total}</span><div class="chips">${shown.map((f) => `<span class="gchip">${esc(gateLabel(f))}</span>`).join("")}${failed.length > shown.length ? `<span class="gchip more" title="${esc(failed.slice(2).map(gateLabel).join(", "))}">+${failed.length - shown.length}</span>` : ""}</div>`;
}

async function renderResearch(body, version) {
  const ex = await api("/api/experiments");
  if (version !== state.tabVersion) return;
  if (!ex.length) { body.innerHTML = emptyState("The next idea starts here.", "No experiments yet. The first research cycle begins once the worker has market data. Every hypothesis will have a frozen test and a recorded decision."); return; }
  const passed = ex.filter((e) => e.status === "passed").length;
  body.innerHTML = `<div class="research-summary"><span class="summary-total"><b>${ex.length}</b> experiments in this view</span>
    <span class="summary-count"><span class="up">${passed}</span> passed</span><span class="summary-count">${ex.filter((e) => e.status === "failed").length} failed</span></div>
    <div class="tbl-scroll"><table class="tbl research-tbl"><thead><tr><th>#</th><th>Hypothesis</th><th>Status</th><th class="r">Test return</th>
    <th class="r">Sharpe</th><th class="r">Trades</th><th>Gates passed</th></tr></thead><tbody>
    ${ex.map((e) => `<tr class="clickable" data-exp="${e.id}"><td class="num">${e.id}</td><td class="hyp"><a href="#" data-exp="${e.id}">${esc(e.title)}</a><div class="muted" style="font-size:11.5px">${esc(e.venue)} · ${authorLabel(e.author)} · ${ago(e.created_at)}</div></td>
      <td><span class="status ${esc(e.status)}">${esc(e.status)}</span>${e.strategy_id ? `<div class="muted" style="font-size:11.5px;margin-top:4px">${e.strategy_probe ? "probe" : "strategy"} #${e.strategy_id}</div>` : ""}</td>
      <td class="r num ${signCls(e.test?.total_return)}">${pct(e.test?.total_return)}</td><td class="r num">${num(e.test?.sharpe)}</td>
      <td class="r num">${e.test?.n_trades ?? "–"}</td><td class="gates" style="font-size:12px">${gatesCell(e)}</td></tr>`).join("")}</tbody></table></div>
    <div class="research-note">Open an experiment to see its hypothesis, sealed test and gates, or to start it as a probe.</div>`;
}

async function renderStrategies(body, version) {
  const st = await api("/api/strategies");
  if (version !== state.tabVersion) return;
  if (!st.length) { body.innerHTML = emptyState("Nothing live yet.", "A strategy goes live when an experiment passes every gate. A probe goes live when an idea made money on its sealed test but missed a gate; you can also start one from any experiment."); return; }
  body.innerHTML = `<div class="tbl-scroll"><table class="tbl"><thead><tr><th>Strategy</th><th>Status</th><th class="r">Allocation</th><th class="r">Live return</th>
    <th class="r">Expected (test CAGR)</th><th class="r">Fills</th><th class="r">Avg slippage</th><th></th></tr></thead><tbody>
    ${st.map((s) => `<tr class="clickable" data-strat="${s.id}"><td><b><a href="#" data-strat="${s.id}">${esc(s.name)} v${s.version}</a></b>${s.probe ? ' <span class="probe-chip">probe</span>' : ""}<div class="muted" style="font-size:12px">${esc(s.venue)} · exp #${s.experiment_id}</div></td>
      <td><span class="status ${esc(s.status)}">${esc(s.status)}</span></td>
      <td class="r num">${money(s.allocation)}<div class="muted" style="font-size:11px">approved ${money(s.approved_allocation)}</div></td>
      <td class="r num ${signCls(s.live.return)}">${pct(s.live.return)}<div class="muted" style="font-size:11px">${s.live.days_live}d live</div></td>
      <td class="r num">${pct(s.expectations?.test?.cagr)}</td><td class="r num">${s.live.fills}</td>
      <td class="r num">${s.live.avg_slippage_bps == null ? "–" : num(s.live.avg_slippage_bps, 1) + " bp"}</td>
      <td>${s.status === "retired" ? "" : `<button class="btn small" data-retire="${s.id}">Retire</button>`}</td></tr>`).join("")}</tbody></table></div>`;
}

async function renderOrders(body, version) {
  const [orders, fills] = await Promise.all([api("/api/orders?limit=100"), api("/api/fills?limit=100")]);
  if (version !== state.tabVersion) return;
  body.innerHTML = `<div class="secondary" style="font-size:13px;margin-bottom:8px">An <b>intent</b> is recorded with its rationale before risk checks;
    a <b>fill</b> is a simulated execution against a live quote. They are never the same row.</div>
    <h4 class="muted" style="margin:6px 0">Orders</h4>
    ${orders.length ? `<div class="tbl-scroll"><table class="tbl"><thead><tr><th>#</th><th>Created</th><th>Side</th><th>Symbol</th><th class="r">Qty</th><th class="r">Filled</th>
      <th class="r">Avg px</th><th>Status</th><th>Note</th></tr></thead><tbody>${orders.map((o) => `<tr class="clickable" data-order="${o.id}"><td class="num"><a href="#" data-order="${o.id}">${o.id}</a></td>
      <td class="muted" style="white-space:nowrap">${dt(o.created_at)}</td><td>${esc(o.side)}</td><td>${esc(o.symbol)}</td><td class="r num">${qty(o.qty)}</td>
      <td class="r num">${qty(o.filled_qty)}</td><td class="r num">${px(o.avg_price)}</td><td><span class="status ${esc(o.status)}">${esc(o.status)}</span></td>
      <td class="secondary" style="font-size:12px">${esc(o.status_note || o.reason)}</td></tr>`).join("")}</tbody></table></div>` : `<div class="empty">No orders yet.</div>`}
    <h4 class="muted" style="margin:16px 0 6px">Fills</h4>
    ${fills.length ? `<div class="tbl-scroll"><table class="tbl"><thead><tr><th>Time</th><th>Side</th><th>Symbol</th><th class="r">Qty</th><th class="r">Price</th>
      <th class="r">Fee</th><th class="r">Spread</th><th class="r">Impact</th><th>Price basis</th></tr></thead><tbody>${fills.map((f) => `<tr><td class="muted" style="white-space:nowrap">${dt(f.ts)}</td>
      <td>${esc(f.side)}</td><td>${esc(f.symbol)}</td><td class="r num">${qty(f.qty)}</td><td class="r num">${px(f.price)}</td><td class="r num">${money(f.fee, 2)}</td>
      <td class="r num">${f.spread_bps == null ? "–" : num(f.spread_bps, 1) + " bp"}</td><td class="r num">${num(f.slippage_bps, 1)} bp</td>
      <td class="secondary" style="font-size:12px">${esc(f.price_basis)}${f.notes ? " · " + esc(f.notes) : ""}</td></tr>`).join("")}</tbody></table></div>` : `<div class="empty">No fills yet.</div>`}`;
}

async function renderMarket(body, version) {
  const u = await api("/api/universe");
  if (version !== state.tabVersion) return;
  const all = [...u.NASDAQ, ...u.CRYPTO];
  state.symbol = state.symbol || all[0];
  body.innerHTML = `<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:8px">
    <label class="muted" for="sym">Symbol</label><select class="sel" id="sym">${all.map((s) => `<option ${s === state.symbol ? "selected" : ""}>${esc(s)}</option>`).join("")}</select>
    <span id="quote" class="num secondary" style="font-size:13px"></span></div>
    <div class="chart-tip" id="px-tip" style="padding-left:0"></div><div class="chart-wrap" id="px-chart" style="height:340px"></div>
    <div class="muted" style="font-size:11px;margin-top:6px">Markers show this account's simulated fills. ${state.s?.mode.market_data === "synthetic" ? "Synthetic 15-minute bars for local/demo testing; these are not live market prices." : "Stocks: split/dividend-adjusted 15-minute bars (regular session). Crypto: Coinbase 15-minute bars."}</div>`;
  $("#sym").addEventListener("change", (e) => { state.symbol = e.target.value; renderTab(); });
  if (state.charts.px) { state.charts.px.chart.remove(); state.charts.px = null; }
  const chart = LightweightCharts.createChart($("#px-chart"), { ...themedChartOptions(), autoSize: true });
  const up = cssVar("--good-mark"), dn = cssVar("--critical-mark");
  const ser = chart.addCandlestickSeries({ upColor: up, downColor: dn, borderVisible: false, wickUpColor: up, wickDownColor: dn });
  state.charts.px = { chart, theme: () => chart.applyOptions(themedChartOptions()) };
  const [c, fills] = await Promise.all([api(`/api/candles?symbol=${encodeURIComponent(state.symbol)}&limit=500`), api(`/api/fills?symbol=${encodeURIComponent(state.symbol)}&limit=200`)]);
  if (version !== state.tabVersion) return;
  if (!c.bars.length) { $("#px-tip").textContent = "No bars stored for this symbol (data unavailable)."; return; }
  ser.setData(c.bars);
  // memecoins trade far below $0.01: give the candles enough decimals not to flatten at 0
  const lo = Math.min(...c.bars.map((b) => b.low));
  const prec = lo > 0 && lo < 1 ? Math.min(12, 3 - Math.floor(Math.log10(lo))) : 2;
  ser.applyOptions({ priceFormat: { type: "price", precision: prec, minMove: 10 ** -prec } });
  const times = c.bars.map((b) => b.time);
  const snap = (t) => { let lo = 0; for (let i = 0; i < times.length; i++) if (times[i] <= t) lo = i; return times[lo]; };
  ser.setMarkers(fills.filter((f) => f.ts >= times[0]).map((f) => ({ time: snap(f.ts), position: f.side === "buy" ? "belowBar" : "aboveBar",
    color: f.side === "buy" ? cssVar("--series-1") : cssVar("--series-2"), shape: f.side === "buy" ? "arrowUp" : "arrowDown", text: `${f.side} ${qty(f.qty)}` }))
    .sort((a, b) => a.time - b.time));
  chart.timeScale().fitContent();
  chart.subscribeCrosshairMove((p) => {
    const d = p && p.time ? p.seriesData.get(ser) : null;
    $("#px-tip").textContent = d ? `${dt(p.time)}  O ${px(d.open)}  H ${px(d.high)}  L ${px(d.low)}  C ${px(d.close)}` : "";
  });
  const q = c.quote;
  $("#quote").textContent = q ? `bid ${px(q.bid)} · ask ${px(q.ask)} · last ${px(q.last)} · ${esc(q.source)} · ${ago(q.ts)}` : "no live quote";
}

const LIMIT_FIELDS = [
  ["risk", "max_position_pct", "Max position (fraction of equity)"], ["risk", "max_gross_pct", "Max gross exposure"],
  ["risk", "max_crypto_pct", "Max crypto exposure"], ["risk", "max_strategy_pct", "Max per strategy"],
  ["risk", "max_total_allocation_pct", "Max total allocated"], ["risk", "max_order_usd", "Max order ($)"],
  ["risk", "daily_loss_limit_pct", "Daily loss limit"], ["risk", "max_drawdown_pct", "Max drawdown (halt)"],
  ["risk", "max_orders_per_hour", "Max orders / hour"], ["risk", "max_quote_age_sec", "Max quote age (s)"],
  ["risk", "max_participation", "Max % of ADV per order"], ["risk", "max_spread_bps", "Max usable spread (bp)"],
  ["budget", "monthly_usd", "LLM budget / month ($)"], ["budget", "daily_usd", "LLM budget / day ($)"],
  ["schedule", "crypto_research_hours", "Crypto research every (hours)"], ["schedule", "nasdaq_research_hours", "Stock research every (hours)"],
  ["schedule", "max_research_per_day", "Max experiments / day per venue"],
  ["schedule", "review_hour_utc", "Daily review hour (UTC)"],
  ["gates", "min_test_sharpe", "Gate: min test Sharpe"], ["gates", "min_val_sharpe", "Gate: min validation Sharpe"],
  ["gates", "min_test_trades", "Gate: min test trades"], ["gates", "min_deflated_psr", "Gate: min deflated PSR (0-1)"],
  ["gates", "min_cost_stress_return", "Gate: min return at 2x costs"], ["gates", "min_delay_stress_sharpe", "Gate: min Sharpe with 1-bar delay"],
  ["gates", "min_neighbor_positive_frac", "Gate: min share of positive neighbours"],
  ["gates", "min_subperiod_positive_frac", "Gate: min share of positive sub-periods"],
  ["gates", "max_top5_concentration", "Gate: max top-5 trade concentration"],
  ["research", "deflation_window_days", "Deflated PSR: count trials from last (days)"],
  ["probe", "enabled", "Probes enabled", "bool"], ["probe", "allocation_pct", "Probe size (fraction of equity)"],
  ["probe", "max_active", "Max probes at once"], ["probe", "per_venue_per_day", "Max new probes / venue / day"],
  ["probe", "min_test_return", "Probe: min sealed-test return"], ["probe", "lifetime_days", "Probe lifetime (days)"],
];
const LIMIT_GROUPS = [
  ["Risk", "Hard limits the risk engine applies to every order.", ["risk"]],
  ["Probes", "Small forward-paper trials of ideas that made money on their sealed test but missed a gate.", ["probe"]],
  ["Acceptance gates", "Every gate must pass for a full allocation.", ["gates", "research"]],
  ["AI budget & schedule", "", ["budget", "schedule"]],
];
async function renderSystem(body, version) {
  const sys = await api("/api/system");
  const calls = await api("/api/llm_calls?limit=15");
  if (version !== state.tabVersion) return;
  const v = (g, k) => sys.settings[g][k];
  body.innerHTML = `
    <div class="section"><h4>Operating limits (only you can change these; the agent cannot)</h4>
      <p class="secondary">Gate profile: <b>${esc(sys.gate_profile)}</b>.
        Exploratory admits paper experiments, not validated trading edges. Strict requires the causal DSL and stronger screening.
        Neither profile proves profitability. Current DSL requirement: ${sys.settings.gates.require_causal_dsl ? "on" : "off"}.</p>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
        <button class="btn" data-gate-preset="exploratory">Use exploratory gates</button>
        <button class="btn" data-gate-preset="strict">Use strict gates</button></div>
      <form id="limits">${LIMIT_GROUPS.map(([title, note, groups]) => `<fieldset class="limit-group"><legend>${esc(title)}</legend>
        ${note ? `<p class="muted limit-note">${esc(note)}</p>` : ""}<div class="form-grid">${LIMIT_FIELDS.filter(([g]) => groups.includes(g)).map(([g, k, l, type]) => type === "bool" ?
        `<div class="field check"><label for="limit-${g}-${k}"><input type="checkbox" id="limit-${g}-${k}" data-g="${g}" data-k="${k}" data-bool="1" ${v(g, k) ? "checked" : ""}> ${esc(l)}</label></div>` :
        `<div class="field"><label for="limit-${g}-${k}">${esc(cap(l.replace(/^(Gate|Probe): /, "")))}</label>
        <input id="limit-${g}-${k}" data-g="${g}" data-k="${k}" value="${esc(v(g, k))}" inputmode="decimal" required></div>`).join("")}</div></fieldset>`).join("")}
        <div style="margin-top:10px;display:flex;gap:8px;align-items:center"><button class="btn" type="submit">Save limits</button>
        <span class="muted" style="font-size:12px">Hard ceilings: position ≤ ${pct(sys.ceilings.ceiling_max_position_pct, 0, false)}, gross ≤ ${pct(sys.ceilings.ceiling_max_gross_pct, 0, false)} (no leverage),
        order ≤ ${money(sys.ceilings.ceiling_max_order_usd)}, LLM ≤ ${money(sys.ceilings.ceiling_monthly_llm_usd)}/mo.</span></div></form></div>
    <div class="section"><h4>Controls</h4><div style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="btn" data-ctl="research_nasdaq">Run NASDAQ research now</button><button class="btn" data-ctl="research_crypto">Run crypto research now</button>
      <button class="btn danger" data-ctl="flatten_all">Flatten all positions</button>
      ${state.s.controls.drawdown_halt ? `<button class="btn" data-ctl="reset_drawdown_halt">Reset drawdown halt</button>` : ""}
      ${state.s.controls.system_halt ? `<button class="btn" data-ctl="clear_system_halt">Clear ledger halt</button>` : ""}</div></div>
    <div class="section"><h4>Integrity & isolation</h4><div class="secondary" style="font-size:13px">
      Ledger rebuilt from fills: <b class="${sys.integrity.ok ? "up" : "down"}">${sys.integrity.ok ? "✓ matches" : "✗ MISMATCH"}</b> (cash ${money(sys.integrity.cash, 2)}).
      Research-code sandbox: ${esc(sys.sandbox.mode)}, env ${esc(sys.sandbox.env)}, network namespace ${sys.sandbox.network_namespace ? "yes" : "no"},
      ${sys.sandbox.mode === "local" && !sys.sandbox.local_supported ? "<b>Python sandbox unavailable: requires Linux/Docker.</b>" : ""}
      ${sys.sandbox.cpu_seconds}s CPU / ${sys.sandbox.memory_mb} MB. LLM: ${esc(sys.llm.demo ? "demo (no key)" : sys.llm.model + " · effort " + sys.llm.effort)},
      knowledge cutoff assumed ${esc(sys.llm.knowledge_cutoff)}.</div></div>
    <div class="section"><h4>Recent LLM calls (audit)</h4>${calls.length ? `<div class="tbl-scroll"><table class="tbl"><thead><tr><th>#</th><th>When</th><th>Purpose</th><th>Model</th><th class="r">Tokens in/out</th><th class="r">Cost</th><th>Status</th></tr></thead><tbody>
      ${calls.map((c) => `<tr class="clickable" data-llm="${c.id}"><td class="num">${c.id}</td><td class="muted">${ago(c.ts)}</td><td>${esc(c.purpose)}</td><td>${esc(c.served_model || c.model)}</td>
      <td class="r num">${c.input_tokens}/${c.output_tokens}</td><td class="r num">${money(c.cost_usd, 4)}</td><td>${esc(c.status)}</td></tr>`).join("")}</tbody></table></div>` : `<div class="muted">No LLM calls.</div>`}</div>
    <div class="section"><h4>Jobs</h4><div class="tbl-scroll"><table class="tbl"><thead><tr><th>Job</th><th>Last run</th><th>Status</th><th class="r">Runs</th><th>Error</th></tr></thead><tbody>
      ${sys.jobs.filter((j) => !j.name.includes(":behind") && !j.name.startsWith("closed_quotes")).map((j) => `<tr><td>${esc(j.name)}</td><td class="muted">${ago(j.last_started_at)}</td><td>${esc(j.last_status || "")}</td><td class="r num">${j.run_count}</td>
      <td class="secondary" style="font-size:12px">${esc(j.last_error || "")}</td></tr>`).join("")}</tbody></table></div></div>
    <div class="section"><h4>Audit log</h4>${sys.audit.slice(0, 15).map((a) => `<div style="font-size:12.5px;padding:3px 0"><span class="muted">${dt(a.ts)}</span> ${esc(a.actor)} · ${esc(a.action)} <span class="muted">${esc(JSON.stringify(a.detail) || "")}</span></div>`).join("") || `<div class="muted">Empty.</div>`}</div>`;
  $("#limits").addEventListener("submit", async (e) => {
    e.preventDefault();
    const groups = {};
    const inputs = [...e.target.querySelectorAll("input:not([data-bool])")];
    if (inputs.some((i) => !i.value.trim() || !Number.isFinite(Number(i.value)))) { toast("Every limit must be a finite number."); return; }
    e.target.querySelectorAll("input[data-bool]").forEach((i) => {
      if (i.checked !== Boolean(v(i.dataset.g, i.dataset.k))) (groups[i.dataset.g] ||= {})[i.dataset.k] = i.checked;
    });
    inputs.forEach((i) => {
      const val = Number(i.value); if (Number.isNaN(val)) return;
      if (val !== Number(v(i.dataset.g, i.dataset.k))) (groups[i.dataset.g] ||= {})[i.dataset.k] = val;
    });
    try { for (const [g, vals] of Object.entries(groups)) await api(`/api/settings/${g}`, { body: vals }); toast(Object.keys(groups).length ? "Limits saved" : "No changes"); refresh(); renderTab(); }
    catch (err) { toast(err.message); }
  });
}
$("#tab-body").addEventListener("click", async (e) => {
  const preset = e.target.closest("[data-gate-preset]");
  if (preset) {
    if (!confirm(`Apply ${preset.dataset.gatePreset} gates to future research? This replaces gate edits, not existing allocations.`)) return;
    try { await api(`/api/gate-presets/${preset.dataset.gatePreset}`, { method: "POST" }); toast("Gate preset saved"); renderTab(); }
    catch (err) { toast(err.message); }
    return;
  }
  const ctl = e.target.closest("[data-ctl]");
  if (ctl) {
    if (ctl.dataset.ctl === "flatten_all" && !confirm("Pause new entries, cancel pending orders, and close every position at the next usable quotes?")) return;
    try {
      ctl.disabled = true;
      await api(`/api/control/${ctl.dataset.ctl}`, { method: "POST" });
      const [m, d] = CTL_MSG[ctl.dataset.ctl] || ["Done"];
      toast(m, { tone: "good", detail: d }); refresh();
    } catch (err) { toast(err.message); } finally { setTimeout(() => { ctl.disabled = false; }, 3000); }
    return;
  }
  const ret = e.target.closest("[data-retire]");
  if (ret) {
    e.stopPropagation();
    if (confirm("Retire this strategy and close its positions?")) {
      try { await api(`/api/strategies/${ret.dataset.retire}/retire`, { method: "POST" }); await renderTab(); }
      catch (err) { toast(err.message); }
    }
    return;
  }
  const llm = e.target.closest("[data-llm]"); if (llm) return openLLM(llm.dataset.llm).catch((err) => toast(err.message));
});

// ---------------------------------------------------------------- drawers
let closeDrawer = null;
function drawer(title, html) {
  if (closeDrawer) closeDrawer();
  const previousFocus = document.activeElement;
  $("#drawer-root").innerHTML = `<div class="drawer-bg"></div><div class="drawer" role="dialog" aria-modal="true" aria-label="${esc(title)}"><div class="drawer-h"><h3>${esc(title)}</h3>
    <button class="btn small" id="drawer-close">Close</button></div><div class="drawer-b">${html}</div></div>`;
  const close = () => {
    if (state.charts.exp) { state.charts.exp.chart.remove(); state.charts.exp = null; }
    document.removeEventListener("keydown", onKey);
    $("#drawer-root").innerHTML = ""; $("#app").inert = false; closeDrawer = null;
    if (previousFocus?.isConnected) previousFocus.focus();
  };
  const onKey = (ev) => {
    if (ev.key === "Escape") close();
    if (ev.key === "Tab") {
      const items = [...$(".drawer").querySelectorAll("button,a[href],input,select,[tabindex='0']")];
      const first = items[0], last = items[items.length - 1];
      if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last?.focus(); }
      else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first?.focus(); }
    }
  };
  closeDrawer = close;
  $("#app").inert = true;
  $("#drawer-close").onclick = close; $(".drawer-bg").onclick = close;
  document.addEventListener("keydown", onKey); $("#drawer-close").focus();
}
function metricsTable(cols) {
  const rows = [["Net return", "total_return", pct], ["CAGR", "cagr", pct], ["Sharpe", "sharpe", (x) => num(x)], ["Max drawdown", "max_drawdown", pct],
    ["Ann. volatility", "ann_vol", (x) => pct(x, 1, false)], ["Trades", "n_trades", (x) => x ?? "–"], ["Win rate", "win_rate", (x) => pct(x, 0, false)],
    ["Exposure", "exposure", (x) => pct(x, 0, false)], ["Cost drag", "cost_drag", (x) => pct(x, 2, false)]];
  return `<div class="tbl-scroll"><table class="tbl"><thead><tr><th></th>${cols.map(([l]) => `<th class="r">${esc(l)}</th>`).join("")}</tr></thead><tbody>
    ${rows.map(([l, k, f]) => `<tr><td>${l}</td>${cols.map(([, m]) => `<td class="r num">${m ? f(m[k]) : "–"}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}
async function openExperiment(id) {
  const e = await api(`/api/experiments/${id}`);
  const h = e.hypothesis || {};
  const w = e.windows;
  const tl = w ? `<div class="timeline">${["train", "validation", "test"].map((k) => `<div class="${k === "test" ? "test" : ""}" style="flex:${w[k].bars}">
      ${k} · ${day(w[k].start)} – ${day(w[k].end)}</div>`).join("")}</div>` : "";
  const ch = e.challenge || {};
  const t = e.test || {};
  const g = e.gates;
  const dec = (e.decisions || []).slice(-1)[0];
  const spec = e.spec || {};
  drawer(`Experiment #${e.id}: ${h.title || ""}`, `
    <div class="section"><span class="status ${esc(e.status)}">${esc(e.status)}</span> <span class="muted">· ${esc(e.venue)} · created ${dt(e.created_at)} · frozen ${dt(e.frozen_at)} · by ${authorLabel(h.author)}</span>
      ${e.strategy ? ` · <a href="#" data-strat="${e.strategy.id}">strategy ${esc(e.strategy.name)} v${e.strategy.version} (${esc(e.strategy.status)})</a>` : ""}</div>
    ${e.test && !e.strategy && e.status !== "running" ? `<div class="rejudge-box"><div><b>Re-judge under current gates</b>
      <p>Re-checks the stored results against today's thresholds. No new backtest and no new look at the sealed test window. If every gate passes, the analyst makes a fresh decision.</p></div>
      <button class="btn" data-rejudge="${e.id}">Re-judge #${e.id}</button></div>
    <div class="rejudge-box probe-box"><div><b>Paper-trade it as a probe</b>
      <p>Starts a small, clearly-labelled forward test with ${pct(state.s?.pipeline?.probe.allocation_pct ?? 0.03, 0, false)} of equity for ${state.s?.pipeline?.probe.lifetime_days ?? 14} days.
      Uses the frozen rules and the normal risk checks. ${(e.test.total_return ?? 0) > 0 ? "It made money on its sealed test." : "<b>It lost money on its sealed test</b>, so expect the same unless conditions changed."}</p></div>
      <button class="btn primary" data-probe="${e.id}">Start probe</button></div>` : ""}
    <div class="section"><h4>Hypothesis (recorded before testing)</h4><div class="pre">${rich(h.rationale)}</div>
      <p><b>Prediction:</b> ${rich(h.prediction)}<br><b>Would drop it if:</b> ${rich(h.falsification)}</p></div>
    <div class="section"><h4>Rules</h4><div style="font-size:13px">Entry <code>${esc(spec.entry || "custom code")}</code><br>Exit <code>${esc(spec.exit || "custom code")}</code>
      ${spec.rank_by ? `<br>Rank by <code>${esc(spec.rank_by)}</code>, top ${spec.top_k}` : ""}<br>
      <span class="secondary">Universe ${esc((spec.universe || []).join(", "))} · grid ${esc(JSON.stringify(spec.params || {}))} → chosen on train <b>${esc(JSON.stringify(e.chosen_params || {}))}</b>
      · sizing ${esc(JSON.stringify(spec.sizing || {}))}${spec.stop_loss_pct ? ` · stop ${pct(spec.stop_loss_pct, 0, false)}` : ""}${spec.max_hold_bars ? ` · max hold ${spec.max_hold_bars} bars` : ""}</span>
      ${spec.kind === "code" ? `<pre class="pre"><code>${esc(spec.code)}</code></pre>` : ""}</div></div>
    ${e.error ? `<div class="alert critical"><span class="icon">!</span><div>${esc(e.error)}</div></div>` : ""}
    ${w ? `<div class="section"><h4>Chronological split (test window sealed until the spec was frozen)</h4>${tl}</div>` : ""}
    ${e.test ? `<div class="section"><h4>Results (net of fees, spread, impact)</h4>${metricsTable([["Train", e.train], ["Validation", e.validation], ["Test", t],
      ["Test ×2 costs", ch.test_cost_x2], ["Test +1 bar delay", ch.test_delay_1bar], [`${t.benchmark_symbol} B&H (test)`, t.benchmark_venue], ["Universe B&H (test)", t.benchmark_universe]])}
      <div class="muted" style="font-size:12px;margin-top:6px">PSR ${num(t.psr)} · deflated PSR ${num(t.deflated_psr)} vs. expected max Sharpe of ${e.leakage?.n_trials_for_deflation} zero-skill trial(s) (${num(t.sr_star_annualised)} annualised).</div></div>
    <div class="section"><h4>Test window: strategy vs. benchmark (growth of $1)</h4><div class="chart-tip" id="exp-tip" style="padding-left:0"></div><div class="chart-wrap" id="exp-chart" style="height:220px"></div>
      <div class="chart-legend" style="padding:0"><span><span class="sw" style="background:var(--series-1)"></span>Strategy (net)</span><span><span class="sw" style="background:var(--series-2)"></span>${esc(t.benchmark_symbol)} buy & hold</span></div></div>
    <div class="section"><h4>Acceptance gates (deterministic, operator-set)</h4>${(g?.checks || []).map((c) => `<div class="gate ${c.pass ? "pass" : "fail"}"><span class="mark">${c.pass ? "✓" : "✗"}</span>
      <div><b>${esc(gateLabel(c.name))}</b> <span class="num">${c.value == null ? "n/a" : num(c.value, 3)} ${esc(c.op)} ${num(c.threshold, 3)}</span><div class="muted" style="font-size:12px">${esc(c.why)}</div></div></div>`).join("")}</div>
    <div class="section"><h4>Challenge</h4><div class="secondary" style="font-size:13px">Neighbouring parameters positive: ${pct(ch.neighbor_positive_frac, 0, false)} (median Sharpe ${num(ch.neighbor_median_sharpe)}) ·
      pre-test sub-periods positive: ${pct(ch.subperiod_positive_frac, 0, false)} · top-5 trades share of pre-test profit: ${ch.top5_concentration_pre_test == null ? "n/a (no profit)" : pct(ch.top5_concentration_pre_test, 0, false)}</div>
      <div class="tbl-scroll" style="margin-top:6px"><table class="tbl"><thead><tr><th>Sub-period</th><th class="r">Return</th><th class="r">Sharpe</th><th class="r">Trades</th></tr></thead><tbody>
      ${(ch.subperiods || []).map((s) => `<tr><td>${day(s.start)} – ${day(s.end)}</td><td class="r num ${signCls(s.total_return)}">${pct(s.total_return)}</td><td class="r num">${num(s.sharpe)}</td><td class="r num">${s.n_trades}</td></tr>`).join("")}</tbody></table></div></div>
    <div class="section"><h4>Data limitations & possible leakage</h4><ul class="notes secondary" style="font-size:13px;padding-left:18px">${(e.leakage?.notes || []).map((n) => `<li>${esc(n)}</li>`).join("")}</ul></div>` : ""}
    ${dec ? `<div class="section"><h4>Decision: ${esc(dec.kind)} (${authorLabel(dec.author)}${dec.llm_call_id ? `, <a href="#" data-llm="${dec.llm_call_id}">inputs</a>` : ""})</h4><div class="pre">${rich(dec.rationale)}</div>
      ${(dec.details?.lessons || []).length ? `<h4 style="margin-top:10px">Recorded lessons</h4><ul style="padding-left:18px">${dec.details.lessons.map((l) => `<li>${rich(l)}</li>`).join("")}</ul>` : ""}</div>` : ""}`);
  if (e.equity_curve?.test?.ts?.length) {
    const chart = LightweightCharts.createChart($("#exp-chart"), { ...themedChartOptions(), autoSize: true });
    const a = chart.addLineSeries({ color: cssVar("--series-1"), lineWidth: 2, priceLineVisible: false });
    const b = chart.addLineSeries({ color: cssVar("--series-2"), lineWidth: 2, priceLineVisible: false });
    const toS = (c) => c.ts.map((tt, i) => ({ time: tt, value: c.v[i] })).filter((p, i, arr) => i === 0 || p.time > arr[i - 1].time);
    a.setData(toS(e.equity_curve.test)); b.setData(toS(e.equity_curve.test_benchmark)); chart.timeScale().fitContent();
    chart.subscribeCrosshairMove((p) => { const x = p?.time ? p.seriesData.get(a) : null, y = p?.time ? p.seriesData.get(b) : null;
      $("#exp-tip").textContent = x ? `${day(p.time)}  strategy ${num(x.value, 3)}  benchmark ${y ? num(y.value, 3) : "–"}` : ""; });
    state.charts.exp = { chart, theme: () => chart.applyOptions(themedChartOptions()) };
  }
}
async function openStrategy(id) {
  const s = await api(`/api/strategies/${encodeURIComponent(id)}`);
  const sp = s.spec || {};
  drawer(`Strategy ${s.name} v${s.version}`, `
    <div class="section"><span class="status ${esc(s.status)}">${esc(s.status)}</span>${s.probe ? ' <span class="probe-chip">probe</span>' : ""} <span class="muted">· ${esc(s.venue)} · approved ${dt(s.created_at)} · from <a href="#" data-exp="${s.experiment_id}">experiment #${s.experiment_id}</a></span></div>
    <div class="section"><h4>Thesis (recorded at approval)</h4><div class="pre">${rich(s.thesis)}</div></div>
    <div class="section"><h4>Exit plan & invalidation</h4><div class="pre">${rich(s.exit_plan)}</div><div class="secondary" style="font-size:13px;margin-top:6px">${esc(JSON.stringify(s.invalidation))}</div></div>
    <div class="section"><h4>Rules (frozen)</h4><div style="font-size:13px">Entry <code>${esc(sp.entry || "code")}</code><br>Exit <code>${esc(sp.exit || "code")}</code><br>
      <span class="secondary">params ${esc(JSON.stringify(sp.fixed_params))} · universe ${esc((sp.universe || []).join(", "))}</span></div></div>
    <div class="section"><h4>Live vs expected</h4>${metricsTable([["Test (expected)", s.expectations?.test]])}
      <div class="secondary" style="font-size:13px;margin-top:8px">Live: ${pct(s.live.return)} over ${s.live.days_live} days on ${money(s.allocation)} · realised ${money(s.live.realized, 2)} ·
      unrealised ${money(s.live.unrealized, 2)} · fees ${money(s.live.fees, 2)} · avg slippage ${s.live.avg_slippage_bps ?? "–"} bp · avg spread ${s.live.avg_spread_bps ?? "–"} bp</div></div>
    <div class="section"><h4>Reviews</h4>${(s.reviews || []).map((r) => `<div style="padding:8px 0;border-top:1px solid var(--border)"><b>${esc(r.kind)}</b> <span class="muted">${dt(r.ts)} · ${authorLabel(r.author)}</span>
      <div class="pre secondary" style="font-size:13px">${rich(r.rationale)}</div></div>`).join("") || `<div class="muted">No reviews yet (daily).</div>`}</div>`);
}
async function openOrder(id) {
  const o = await api(`/api/orders/${encodeURIComponent(id)}`);
  const fills = o.fills;
  drawer(`Order #${o.id}: ${o.side} ${o.symbol}`, `
    <div class="section"><span class="status ${esc(o.status)}">${esc(o.status)}</span> <span class="muted">· ${esc(o.client_order_id)} · origin ${esc(o.origin)}</span></div>
    <div class="section"><h4>Rationale (written ${dt(o.created_at)}, before any fill)</h4><div class="pre">${rich(o.rationale)}</div></div>
    <div class="section"><h4>Risk engine</h4><div class="secondary" style="font-size:13px">${esc(o.risk_check?.reason || o.status_note || "")}</div>
      ${(o.risk_check?.checks || []).map((c) => `<div class="gate ${c.pass ? "pass" : "fail"}"><span class="mark">${c.pass ? "✓" : "✗"}</span><div><b>${esc(c.check)}</b> <span class="muted">${esc(c.detail)}</span></div></div>`).join("")}</div>
    <div class="section"><h4>Execution</h4><div class="secondary" style="font-size:13px">Qty ${qty(o.qty)} · eligible ${dt(o.eligible_at)} · expires ${dt(o.expires_at)} · filled ${qty(o.filled_qty)} @ ${px(o.avg_price)} · fees ${money(o.fees, 2)}</div>
      ${fills.map((f) => `<div style="font-size:13px;margin-top:6px">Fill ${dt(f.ts)}: ${qty(f.qty)} @ ${px(f.price)} (bid ${px(f.ref_bid)} / ask ${px(f.ref_ask)}; ${esc(f.price_basis)}; impact ${num(f.slippage_bps, 1)} bp; fee ${money(f.fee, 2)})</div>`).join("")}</div>`);
}
async function openLLM(id) {
  const c = await api(`/api/llm_calls/${id}`);
  drawer(`LLM call #${c.id}: ${c.purpose}`, `<div class="section secondary" style="font-size:13px">${esc(c.served_model || c.model)} · ${dt(c.ts)} · ${c.input_tokens} in / ${c.output_tokens} out
    (${c.cache_read_tokens} cached) · ${money(c.cost_usd, 4)} · ${esc(c.status)} ${esc(c.error || "")}</div>
    <div class="section"><h4>Inputs given to the model</h4><pre class="pre" style="font-size:12px">${esc(c.request?.user || "")}</pre></div>
    <div class="section"><h4>Response</h4><pre class="pre" style="font-size:12px">${esc(c.response || "")}</pre></div>`);
}
document.addEventListener("click", async (e) => {
  const pr = e.target.closest("[data-probe]");
  if (pr) {
    e.preventDefault(); e.stopPropagation();
    if (!confirm(`Paper-trade experiment #${pr.dataset.probe} as a probe? It gets a small allocation and the normal risk checks.`)) return;
    pr.disabled = true;
    try {
      await api(`/api/control/probe_${pr.dataset.probe}`, { method: "POST" });
      toast(`Starting probe for #${pr.dataset.probe}`, { tone: "good", detail: "The worker starts it within ~15 seconds; it trades from the next bar close." });
      if (closeDrawer) closeDrawer();
      setTimeout(refresh, 16000);
    } catch (err) { toast(err.message); pr.disabled = false; }
    return;
  }
  const rj = e.target.closest("[data-rejudge]");
  if (rj) {
    if (!confirm(`Re-judge experiment #${rj.dataset.rejudge} under the current gates? Only the gate check and decision run again.`)) return;
    rj.disabled = true;
    try {
      await api(`/api/control/rejudge_${rj.dataset.rejudge}`, { method: "POST" });
      toast(`Re-judging experiment #${rj.dataset.rejudge}`, { tone: "good", detail: "Starts within ~15 seconds. You'll get a notification with the decision." });
      if (closeDrawer) closeDrawer();
    } catch (err) { toast(err.message); rj.disabled = false; }
    return;
  }
  const a = e.target.closest("[data-exp],[data-strat],[data-order],[data-llm]");
  if (!a || a.closest("#tab-body [data-llm]") && !a.matches("a")) return;
  if (a.dataset.exp) { e.preventDefault(); openExperiment(a.dataset.exp).catch((err) => toast(err.message)); }
  else if (a.dataset.strat) { e.preventDefault(); openStrategy(a.dataset.strat).catch((err) => toast(err.message)); }
  else if (a.dataset.order) { e.preventDefault(); openOrder(a.dataset.order).catch((err) => toast(err.message)); }
  else if (a.dataset.llm && a.matches("a")) { e.preventDefault(); openLLM(a.dataset.llm).catch((err) => toast(err.message)); }
});

// ---------------------------------------------------------------- journal
// Each chip covers a group of entry kinds: "Research" is every step of the research loop.
const KINDS = [["research", "Research", ["observation", "research", "hypothesis", "test", "decision"]],
  ["trades", "Trades", ["intent", "order", "fill"]], ["review", "Reviews", ["review"]], ["risk", "Risk", ["risk"]],
  ["data", "Data", ["data"]], ["system", "System", ["system"]]];
const activeKinds = () => new Set(KINDS.filter(([k]) => state.filter.has(k)).flatMap(([, , ks]) => ks));
function renderFilters() {
  $("#filters").innerHTML = `<button class="fchip ${state.filter.size ? "" : "on"}" data-k="" aria-pressed="${!state.filter.size}">All</button>` +
    KINDS.map(([k, l]) => `<button class="fchip ${state.filter.has(k) ? "on" : ""}" data-k="${k}" aria-pressed="${state.filter.has(k)}">${l}</button>`).join("");
}
$("#filters").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-k]"); if (!b) return;
  if (!b.dataset.k) state.filter.clear(); else state.filter.has(b.dataset.k) ? state.filter.delete(b.dataset.k) : state.filter.add(b.dataset.k);
  renderFilters(); renderFeed();
  if (state.filter.size) {  // older matching entries may be beyond the 200 most recent ones loaded at start
    try {
      const rows = await api(`/api/journal?limit=200&kind=${[...activeKinds()].join(",")}`);
      const have = new Set(state.journal.map((j) => j.id));
      state.journal = state.journal.concat(rows.filter((r) => !have.has(r.id))).sort((x, y) => y.id - x.id);
      renderFeed();
    } catch (err) { console.error(err); }
  }
});
function entryHTML(j, fresh) {
  const refs = j.refs || {};
  const links = [];
  if (refs.experiment_id) links.push(`<a href="#" data-exp="${refs.experiment_id}">experiment #${refs.experiment_id}</a>`);
  if (refs.strategy_id) links.push(`<a href="#" data-strat="${refs.strategy_id}">strategy #${refs.strategy_id}</a>`);
  if (refs.order_id) links.push(`<a href="#" data-order="${refs.order_id}">order #${refs.order_id}</a>`);
  if (j.llm_call_id) links.push(`<a href="#" data-llm="${j.llm_call_id}">model inputs</a>`);
  const long = (j.body || "").length > 420;
  return `<div class="entry k-${esc(j.kind)} ${fresh ? "fresh" : ""}" data-jid="${j.id}"><div class="entry-meta"><span class="kind ${esc(j.kind)}">${esc(j.kind === "intent" ? "intent · not a fill" : j.kind)}</span>
    <span class="author">${authorLabel(j.author)}</span><span title="${esc(new Date(j.ts * 1000).toISOString())}">${dt(j.ts)}</span></div>
    <div class="entry-title">${rich(j.title)}</div><div class="entry-body ${long ? "clamp" : ""}">${rich(j.body)}</div>
    <div class="entry-refs">${long ? `<a href="#" data-more>more</a>` : ""}${links.join("")}</div></div>`;
}
function renderFeed(freshIds = new Set()) {
  const kinds = activeKinds();
  const items = state.journal.filter((j) => !state.filter.size || kinds.has(j.kind));
  $("#feed").innerHTML = items.length ? items.slice(0, 300).map((j) => entryHTML(j, freshIds.has(j.id))).join("") : `<div class="empty">No journal entries${state.filter.size ? " for this filter" : ""} yet.</div>`;
}
$("#feed").addEventListener("click", (e) => {
  const m = e.target.closest("[data-more]"); if (!m) return; e.preventDefault();
  const body = m.closest(".entry").querySelector(".entry-body"); body.classList.toggle("clamp"); m.textContent = body.classList.contains("clamp") ? "more" : "less";
});
async function loadJournal() {
  const rows = await api("/api/journal?limit=200");
  state.journal = rows; state.lastId = rows.length ? rows[0].id : 0; renderFeed();
  connectStream();
}
function connectStream() {
  if (state.es) state.es.close();
  const es = new EventSource(`/api/stream?after_id=${state.lastId}`);
  state.es = es;
  es.addEventListener("journal", (ev) => {
    const j = JSON.parse(ev.data);
    if (j.id <= state.lastId) return;
    state.lastId = j.id; state.journal.unshift(j); state.journal.length = Math.min(state.journal.length, 1000);
    renderFeed(new Set([j.id]));
    notifyFromJournal(j);
    if (["fill", "decision", "review", "risk", "test"].includes(j.kind)) refresh();
    if (["test", "decision"].includes(j.kind) && state.tab === "research" && !$("#drawer-root").innerHTML) renderTab();
  });
  es.onopen = () => { $("#live-badge").className = "badge ok"; $("#live-badge").innerHTML = '<span class="dot"></span>live'; };
  es.onerror = () => { $("#live-badge").className = "badge bad"; $("#live-badge").innerHTML = '<span class="dot"></span>reconnecting'; };
}

// ---------------------------------------------------------------- ask
const SUGGEST = [["Current research ↗", "What are you researching right now?"], ["Why cash? ↗", "Why are you holding cash?"], ["Latest lesson ↗", "What did your last failed test teach you?"]];
$("#suggest").innerHTML = SUGGEST.map(([label, q]) => `<button class="fchip" data-q="${esc(q)}">${esc(label)}</button>`).join("");
$("#suggest").addEventListener("click", (e) => { const b = e.target.closest("[data-q]"); if (b) askQuestion(b.dataset.q); });
$("#ask-form").addEventListener("submit", (e) => { e.preventDefault(); const q = $("#ask-input").value.trim(); if (q) askQuestion(q); });
async function askQuestion(q) {
  $("#ask-input").value = q;
  $("#answer").innerHTML = `<div class="q">${esc(q)}</div><div class="muted">Looking through the records…</div>`;
  try {
    const a = await api("/api/ask", { body: { question: q } });
    const cites = (a.citations || []).map((c) => {
      const [t, id] = c.split(":");
      return t === "experiment" ? `<a href="#" data-exp="${esc(id)}">${esc(c)}</a>` : t === "strategy" ? `<a href="#" data-strat="${esc(id)}">${esc(c)}</a>` :
        t === "order" ? `<a href="#" data-order="${esc(id)}">${esc(c)}</a>` : esc(c);
    }).join(", ");
    $("#answer").innerHTML = `<div class="q">${esc(q)}</div><div class="pre" style="margin-top:4px">${rich(a.answer)}</div>
      <div class="muted" style="font-size:12px;margin-top:6px">${authorLabel(a.author)} · sources: ${cites || "none"}${a.unknowns ? ` · ${esc(a.unknowns)}` : ""}</div>`;
  } catch (err) { $("#answer").innerHTML = `<div class="down">${esc(err.message)}</div>`; }
}

// ---------------------------------------------------------------- main loop
async function refresh() {
  try {
    const s = await api("/api/state"); state.s = s;
    state.lastRefresh = Date.now() / 1000; $("#connection-alert").classList.add("hidden");
    renderBadges(s); renderAlerts(s); renderKPIs(s); renderPipeline(s); renderNow(s); renderBeliefs(s); renderPositions(s);
  } catch (err) {
    if (err.message !== "login required") {
      $("#desk-health").className = "desk-health bad";
      $("#desk-health").innerHTML = '<span class="health-indicator"></span><div><b>Connection interrupted</b><small>Displayed values may be out of date</small></div>';
      $("#connection-alert").textContent = `Dashboard data is not updating: ${err.message}. Last success: ${state.lastRefresh ? dt(state.lastRefresh) : "never"}. Retrying automatically.`;
      $("#connection-alert").classList.remove("hidden");
    }
  }
}
async function boot() {
  if (state.booting) return;
  state.booting = true;
  stopUpdates();
  try {
  const sess = await fetch("/api/session").then((r) => r.json()).catch(() => ({ authenticated: false }));
  if (!sess.authenticated) { showLogin(); return; }
  $("#btn-logout").classList.toggle("hidden", !sess.password_required);
  $("#login-view").classList.add("hidden");
  $("#app").classList.remove("hidden");
  renderFilters();
  await refresh();
  const safe = (fn) => () => fn().catch((err) => { if (err.message !== "login required") toast(err.message); });
  await Promise.all([safe(loadEquity)(), safe(loadJournal)(), renderTab()]);
  if ($("#app").classList.contains("hidden")) return;
  state.timers.push(setInterval(refresh, 10000), setInterval(safe(loadEquity), 60000),
    setInterval(() => { if (["research", "strategies", "orders"].includes(state.tab) && !$("#drawer-root").innerHTML) renderTab(); }, 30000));
  } finally { state.booting = false; }
}
boot();

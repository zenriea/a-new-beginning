// QuantDesk UI. Vanilla JS; every server string is escaped before it touches the DOM.
import { changeDirection, deskHealth, equityInRange, formatLimit, friendlyGates, fromDisplay, gateCount, gateLabel,
  limitUsage, LIMIT_FIELDS, sameValue, toDisplay, tradingVerdict, UNIT_SUFFIX } from "./ui-model.mjs?v=20261119-console";

const $ = (s, el = document) => el.querySelector(s);
const state = { s: null, journal: [], lastId: 0, filter: new Set(), tab: "research", charts: {}, symbol: null, es: null,
  timers: [], booting: false, tabVersion: 0, lastRefresh: null, equityRows: [], equityRange: "all", equityDecimals: 0, tabKey: null };

// ---------------------------------------------------------------- helpers
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const rich = (v) => friendlyGates(esc(v)).replace(/`([^`\n]{1,300})`/g, "<code>$1</code>");
// Objects from the records (grids, sizing, invalidation, audit detail) read as key = value pairs, not JSON.
function kv(obj) {
  if (obj == null || (typeof obj === "object" && !Object.keys(obj).length)) return `<span class="muted">none</span>`;
  if (typeof obj !== "object") return esc(obj);
  return `<span class="kv">${Object.entries(obj).map(([k, v]) => `<span><span class="kv-k">${esc(k.replaceAll("_", " "))}</span> ${
    esc(Array.isArray(v) ? v.join(", ") : v && typeof v === "object" ? Object.entries(v).map(([a, b]) => `${a} ${b}`).join(", ") : v)}</span>`).join("")}</span>`;
}
const money = (v, d = 0) => v == null || isNaN(v) ? "–" : (v < 0 ? "−$" : "$") + Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const pct = (v, d = 2, sign = true) => v == null || isNaN(v) ? "–" : (sign && v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v * 100).toFixed(d) + "%";
const num = (v, d = 2) => v == null || isNaN(v) ? "–" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: Math.min(d, 2) > 0 && Math.abs(v) < 1000 ? Math.min(d, 2) : 0 });
const qty = (v) => v == null ? "–" : Number.isInteger(v) ? v.toLocaleString() : Number(v).toLocaleString(undefined, { maximumFractionDigits: 6 });
const px = (v) => v == null ? "–" : Number(v).toLocaleString(undefined, { maximumFractionDigits: v < 0.01 ? 10 : v < 1 ? 6 : v < 100 ? 4 : 2, minimumFractionDigits: 2 });
const signCls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
// Drawn marks in the rail's stroke style, never text glyphs.
const svgMark = (d, cls = "") => `<svg class="mark-svg ${cls}" viewBox="0 0 16 16" aria-hidden="true"><path d="${d}"/></svg>`;
const MARK = { up: svgMark("M8 3.5 13 11H3z", "fill"), down: svgMark("M8 12.5 3 5h10z", "fill"), pass: svgMark("m3.5 8.5 3 3 6-7"), fail: svgMark("m4.5 4.5 7 7m0-7-7 7") };
const arrow = (v) => (v > 0 ? MARK.up : v < 0 ? MARK.down : "");
const dt = (ts) => ts ? new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }) : "–";
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
// The caller states the tone; errors go through fail() so a success message is never painted red.
function toast(msg, opts = {}) {
  const tone = opts.tone || "info";
  let stack = $("#toasts");
  if (!stack) { stack = document.createElement("div"); stack.id = "toasts"; stack.className = "toast-stack"; stack.setAttribute("aria-live", "polite"); document.body.appendChild(stack); }
  if (opts.key) stack.querySelectorAll(`.toast[data-key="${opts.key}"]`).forEach((old) => old.remove());
  const t = document.createElement("div"); t.className = `toast ${tone}`;
  if (opts.key) t.dataset.key = opts.key;
  if (tone === "bad") t.setAttribute("role", "alert");
  t.innerHTML = `<span class="toast-dot" aria-hidden="true"></span><div class="toast-text"><b></b>${opts.detail ? "<small></small>" : ""}</div><button class="toast-x" aria-label="Dismiss">${MARK.fail}</button>`;
  t.querySelector("b").textContent = msg;
  if (opts.detail) t.querySelector("small").textContent = opts.detail;
  if (opts.exp) { t.classList.add("clickable"); t.dataset.exp = opts.exp; }
  const remove = () => { t.classList.add("out"); setTimeout(() => t.remove(), 200); };
  t.querySelector(".toast-x").onclick = (e) => { e.stopPropagation(); remove(); };
  stack.appendChild(t);
  while (stack.children.length > 4) stack.firstChild.remove();
  setTimeout(remove, opts.ms || (tone === "bad" ? 9000 : opts.detail ? 8000 : 4000));
}
const fail = (err, detail) => toast(err?.message || String(err), { tone: "bad", detail });

// A confirmation that states consequences. `focus: "confirm"` only where acting is the safe direction
// (stopping trading); everything that adds risk opens with Cancel focused.
function confirmDialog({ title, body = "", confirm, tone = "primary", focus = "cancel" }) {
  return new Promise((resolve) => {
    const d = document.createElement("dialog");
    d.className = `confirm-dialog ${tone}`;
    d.setAttribute("aria-labelledby", "confirm-title");
    d.innerHTML = `<form method="dialog"><h2 id="confirm-title"></h2><div class="confirm-body">${body}</div>
      <div class="confirm-actions"><button class="btn" value="cancel">Cancel</button><button class="btn ${tone === "danger" ? "danger solid" : "primary"}" value="ok"></button></div></form>`;
    d.querySelector("h2").textContent = title;
    d.querySelector("[value=ok]").textContent = confirm;
    d.addEventListener("close", () => { resolve(d.returnValue === "ok"); d.remove(); });
    document.body.appendChild(d);
    d.showModal();
    d.querySelector(focus === "confirm" ? "[value=ok]" : "[value=cancel]").focus();
  });
}
const icon = {
  alert: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 8v5m0 3.5v.01"/><path d="M10.3 3.9 2.6 17.2A2 2 0 0 0 4.3 20h15.4a2 2 0 0 0 1.7-2.8L13.7 3.9a2 2 0 0 0-3.4 0Z"/></svg>',
  pause: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5v14M15 5v14"/></svg>',
  info: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-8.5v.01"/></svg>',
  stop: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="2"/></svg>',
};

// Journal entries worth interrupting the operator for (research outcomes, trades, risk).
function notifyFromJournal(j) {
  if (j.author === "operator") return;  // the operator's own action already got its confirmation
  const t = j.title || "", refs = j.refs || {};
  const research = j.kind === "research" || j.kind === "decision" || (j.kind === "test" && /^Re-judged/.test(t));
  const failed = j.kind === "system" && /failed|paused|could not/i.test(t);
  if (!(research || failed || j.kind === "fill" || j.kind === "risk")) return;
  const tone = j.kind === "risk" || failed ? "bad" : /^(Allocating|New (strategy|probe) live)/.test(t) || j.kind === "fill" ? "good" : "info";
  const body = friendlyGates(j.body || "");
  toast(friendlyGates(t), { tone, detail: body.slice(0, 160) + (body.length > 160 ? "…" : ""), exp: refs.experiment_id });
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
  catch (err) { fail(err); }
});

// ---------------------------------------------------------------- theme
function applyTheme(t) {
  if (t) document.documentElement.setAttribute("data-theme", t); else document.documentElement.removeAttribute("data-theme");
  const dark = t === "dark", btn = $("#btn-theme");
  btn.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme"); btn.title = btn.getAttribute("aria-label");
  btn.innerHTML = dark ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>'
    : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';
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
const clock24 = (ts, seconds = false) => new Date(ts * 1000).toLocaleTimeString(undefined,
  { hour: "2-digit", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}), hourCycle: "h23" });
function renderBadges(s) {
  // Mode badges (paper, demo, synthetic) and anything in trouble always show; routine status folds away on phones.
  const b = [];
  const badge = (cls, text, { quiet = false, dot = true, title = "" } = {}) =>
    b.push(`<span class="badge ${cls}${quiet ? " quiet" : ""}"${title ? ` title="${esc(title)}"` : ""}>${dot ? '<span class="dot"></span>' : ""}${text}</span>`);
  badge("paper", "PAPER · no real money", { dot: false });
  if (s.mode.llm === "demo") badge("warn", "DEMO analyst · no AI configured");
  else badge("ok", `AI: ${esc(s.mode.llm.split(" (")[0])}`, { quiet: true, title: `AI analyst: ${s.mode.llm}` });
  if (s.mode.market_data === "synthetic") badge("warn", "SYNTHETIC market data");
  badge(s.worker.alive ? "ok" : "bad", `Worker ${s.worker.alive ? "running" : "offline"}`, { quiet: s.worker.alive });
  const n = s.sessions.NASDAQ;
  badge(n.open ? "ok" : "", `NASDAQ ${n.open ? "open · closes in " + until(n.closes_at) : "closed · opens in " + (n.opens_at ? until(n.opens_at) : "?")}`, { quiet: true });
  badge("ok", "Crypto 24/7", { quiet: true });
  const bud = s.budget, broke = bud.day_remaining <= 0 || bud.month_remaining <= 0;
  badge(broke ? "bad" : "", `AI spend $${bud.month_spent.toFixed(2)} / $${bud.monthly_cap}`,
    { quiet: !broke, dot: broke, title: `AI spend this month (today $${bud.day_spent.toFixed(2)} of $${bud.daily_cap})` });
  const quiet = b.filter((x) => x.includes(" quiet")).length;
  const expanded = $("#badges").classList.contains("expanded");
  $("#badges").innerHTML = b.join("") + (quiet ? `<button type="button" class="badge badge-more" aria-expanded="${expanded}">${expanded ? "Less" : `+${quiet} status`}</button>` : "");
  const c = s.controls;
  $("#btn-pause").innerHTML = `<span>${c.paused ? "Resume" : "Pause"}<span class="lbl-long"> agent</span></span>`;
  $("#btn-pause").setAttribute("aria-label", c.paused ? "Resume agent" : "Pause agent");
  $("#btn-pause").setAttribute("aria-pressed", String(c.paused));
  // Resuming does nothing while the stop is on; say so instead of offering it.
  $("#btn-pause").disabled = Boolean(c.emergency_stop);
  $("#btn-pause").title = c.emergency_stop ? "Clear the emergency stop first" : "";
  $("#btn-stop").classList.toggle("hidden", Boolean(c.emergency_stop));
  $("#btn-clear-stop").classList.toggle("hidden", !c.emergency_stop);
  $("#nav-research-count").textContent = s.counts.experiments.toLocaleString();
  const strategies = s.pipeline?.live.strategies || 0, live = strategies + (s.pipeline?.live.probes || 0);
  $("#nav-live-count").textContent = live ? `${live} live` : "idle";
  $("#nav-live-count").classList.toggle("on", strategies > 0);
  $("#nav-live-count").classList.toggle("probe", live > 0 && !strategies);
  const health = deskHealth(s);
  $("#desk-health").className = `desk-health ${health.tone}`;
  $("#desk-health").innerHTML = `<span class="health-indicator"></span><div><b>${esc(health.label)}</b><small>Updated ${esc(clock24(Date.now() / 1000, true))}</small></div>`;
  $("#desk-health").title = health.label;
}
$("#badges").addEventListener("click", (e) => {
  if (!e.target.closest(".badge-more")) return;
  $("#badges").classList.toggle("expanded");
  if (state.s) renderBadges(state.s);
  $("#badges .badge-more")?.focus();
});
function renderAlerts(s) {
  const a = [];
  const c = s.controls;
  if (c.emergency_stop) {
    const when = c.emergency_stop_at ? `since ${clock24(c.emergency_stop_at, true)} (${ago(c.emergency_stop_at)})` : "";
    const n = c.emergency_cancelled;
    a.push(["critical", icon.stop, `<b>Emergency stop is on ${when}.</b> ${n != null ? `${n} open order${n === 1 ? " was" : "s were"} cancelled. ` : ""}
      No new research, orders or fills. Positions are held, not sold.
      Clear it with the button at the top of the page.
      <div class="alert-actions"><button class="btn small danger" data-ctl="flatten_all">Flatten all positions…</button></div>`]);
  } else if (c.paused) a.push(["warning", icon.pause, "<b>The agent is paused.</b> No new research or positions. Exits, fills and risk monitoring continue."]);
  if (c.drawdown_halt) a.push(["critical", icon.alert, `<b>Max drawdown limit reached.</b> New risk is halted until you reset it in Limits &amp; system.`]);
  if (c.system_halt) a.push(["critical", icon.alert, "<b>Ledger integrity check failed at startup.</b> New buys are blocked. See Limits &amp; system."]);
  if (!s.worker.alive) a.push(["critical", icon.alert, "<b>The background worker is not running,</b> so nothing is being researched or traded. Check the worker service."]);
  if (!s.mode.alpaca_configured && s.mode.market_data !== "synthetic") a.push(["warning", icon.alert, "<b>NASDAQ data is not configured</b> (Alpaca keys missing). NASDAQ research and trading are stopped; crypto continues."]);
  if (s.data.problems.length) a.push(["warning", icon.alert, `<b>Data unavailable or stale:</b> ${s.data.problems.slice(0, 6).map((d) => esc(d.key)).join(", ")}${s.data.problems.length > 6 ? "…" : ""}. Dependent actions are stopped.`]);
  if (s.mode.llm === "demo" && !demoAcknowledged()) a.push(["info", icon.info, `<button type="button" class="alert-x" data-ack-demo aria-label="Hide the demo notice; the DEMO badge stays">Got it</button><b>Demo mode.</b> A deterministic rules engine stands in for the AI analyst; its entries are labelled “Rules engine”. Research, tests and trading are real computations.
    <details class="alert-more"><summary>Connect an AI provider</summary>Set <code>QD_LLM_PROVIDER</code>, <code>QD_LLM_API_KEY</code> and <code>QD_LLM_MODEL</code> (or <code>ANTHROPIC_API_KEY</code>) in <code>.env</code>, then restart the worker.</details>`]);
  const open = $("#alerts .alert-more")?.open;
  $("#alerts").innerHTML = a.map(([k, i, t]) => `<div class="alert ${k}"><span class="icon">${i}</span><div>${t}</div></div>`).join("");
  if (open && $("#alerts .alert-more")) $("#alerts .alert-more").open = true;
}
async function control(action) {
  return api(`/api/control/${action}`, { method: "POST" });
}
$("#btn-pause").addEventListener("click", async () => {
  const pausing = !state.s?.controls.paused;
  try {
    await control(pausing ? "pause" : "resume");
    toast(pausing ? "Agent paused" : "Agent resumed", { key: "control", tone: pausing ? "info" : "good",
      detail: pausing ? "No new research or positions. Exits, fills and risk monitoring continue." : "Research and trading schedules are active again." });
    await refresh();
  } catch (err) { fail(err); }
});
$("#btn-stop").addEventListener("click", async () => {
  const orders = state.s?.open_orders.length ?? 0;
  const ok = await confirmDialog({ title: "Stop all agent trading?", tone: "danger", confirm: "Stop trading", focus: "confirm",
    body: `<ul class="confirm-list"><li>Halts research, new orders and fills.</li><li>Cancels ${orders} open order${orders === 1 ? "" : "s"}.</li>
      <li>Keeps every position. Use Flatten all afterwards to close them.</li></ul>` });
  if (!ok) return;
  try {
    const r = await control("emergency_stop");
    toast("Emergency stop is on", { key: "control", tone: "bad", ms: 12000,
      detail: `${clock24(Date.now() / 1000, true)} · ${r.cancelled ?? 0} open order${r.cancelled === 1 ? "" : "s"} cancelled · positions held.` });
    await refresh();
    window.scrollTo({ top: 0, behavior: "instant" });
  } catch (err) { fail(err, "The stop may not be active. Check the status above and try again."); }
});
async function clearEmergency() {
  const ok = await confirmDialog({ title: "Clear the emergency stop?", confirm: "Clear stop",
    body: `<p>Trading can resume after this. The agent stays <b>paused</b> until you press Resume agent, and positions are unchanged.</p>` });
  if (!ok) return;
  try {
    await control("clear_emergency");
    toast("Emergency stop cleared", { key: "control", detail: "The agent is still paused. Press Resume agent when you're ready." });
    await refresh();
  } catch (err) { fail(err); }
}
$("#btn-clear-stop").addEventListener("click", clearEmergency);
// The demo notice can be hidden once; the DEMO badge keeps the mode visible.
function demoAcknowledged() { try { return localStorage.getItem("qd-demo-ack") === "1"; } catch { return false; } }
document.addEventListener("click", (e) => {
  if (!e.target.closest("[data-ack-demo]")) return;
  try { localStorage.setItem("qd-demo-ack", "1"); } catch {}
  if (state.s) renderAlerts(state.s);
});

// ---------------------------------------------------------------- KPIs
function renderKPIs(s) {
  const a = s.account;
  // With nothing invested the four tiles only say "$0": shrink them to a strip so the verdict leads.
  $("#kpis").classList.toggle("compact", !a.gross && !s.positions.length && !s.open_orders.length);
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
    layout: { background: { type: "solid", color: cssVar("--surface-1") }, textColor: cssVar("--text-secondary"), fontFamily: cssVar("--mono") || "monospace", fontSize: 12, attributionLogo: false },
    grid: { vertLines: { visible: false }, horzLines: { color: cssVar("--border"), style: 2 } },
    rightPriceScale: { borderVisible: false },
    timeScale: { borderVisible: false, timeVisible: true,
      // 24-hour times; a day change reads "Nov 19", never a bare day number
      tickMarkFormatter: (t, type) => { const d = new Date(t * 1000);
        return type <= 1 ? d.toLocaleDateString(undefined, { month: "short", year: "numeric" })
          : type === 2 ? d.toLocaleDateString(undefined, { month: "short", day: "numeric" })
          : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" }); } },
    crosshair: { mode: 0 },
    // Enough decimals for the visible range: a flat $100,000 account must not show "$100,000" four times.
    localization: { priceFormatter: (v) => Math.abs(v) >= 1000
      ? "$" + v.toLocaleString(undefined, { minimumFractionDigits: state.equityDecimals, maximumFractionDigits: state.equityDecimals })
      : Math.abs(v) < 1 ? String(Number(v.toPrecision(4))) : v.toFixed(Math.abs(v) < 10 ? 3 : 2) },
  };
}
function ensureEquityChart() {
  if (state.charts.equity) return state.charts.equity;
  const el = $("#equity-chart");
  const chart = LightweightCharts.createChart(el, { ...themedChartOptions(), autoSize: true });
  // Benchmarks are muted dashed context; only the account carries a last-value label (no label collisions).
  const mk = (v, style) => chart.addLineSeries({ color: cssVar(v), lineWidth: 1.5, lineStyle: style, priceLineVisible: false, lastValueVisible: false });
  // The account line is ink; it takes the reserved live cyan only while capital is actually deployed.
  const area = () => { const live = (state.s?.positions.length || 0) > 0;
    return { lineColor: cssVar(live ? "--live" : "--text-primary"), topColor: live ? cssVar("--live-soft") : "rgba(0,0,0,0)", bottomColor: "rgba(0,0,0,0)" }; };
  const bench = mk("--series-2", 2), btc = mk("--series-3", 1);  // dashed QQQ, dotted BTC  // added first so the account line draws on top
  const acct = chart.addAreaSeries({ ...area(), lineWidth: 2.5, priceLineVisible: false, lastValueVisible: true });
  const c = { chart, acct, bench, btc };
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
  const values = rows.flatMap((r) => [r.equity, r.benchmark_equity, r.crypto_benchmark_equity]).filter((v) => v != null);
  const spread = values.length ? Math.max(...values) - Math.min(...values) : 0;
  const decimals = spread < 20 ? 2 : spread < 200 ? 1 : 0;
  if (decimals !== state.equityDecimals) { state.equityDecimals = decimals; state.charts.equity?.chart.applyOptions(themedChartOptions()); }
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
  // leave room so the first time label is not clipped at the left edge
  requestAnimationFrame(() => {
    const ts = c.chart.timeScale(), range = ts.getVisibleLogicalRange();
    // pad by pixels, not a share of the range, so narrow phones keep the first label whole
    const perPx = (range?.to - range?.from) / Math.max(1, $("#equity-chart").clientWidth - 70);
    if (range) ts.setVisibleLogicalRange({ from: range.from - Math.max(1, perPx * 30), to: range.to + Math.max(1, perPx * 18) });
  });
  const bs = state.s?.account.benchmark_symbol || "QQQ";
  const live = (state.s?.positions.length || 0) > 0;
  c.acct.applyOptions({ lineColor: cssVar(live ? "--live" : "--text-primary"), topColor: live ? cssVar("--live-soft") : "rgba(0,0,0,0)" });
  $("#equity-legend").innerHTML = [[live ? "--live" : "--text-primary", "Account (net of costs)", ""], ["--series-2", `${bs} buy & hold`, "dashed"], ["--series-3", "BTC-USD buy & hold", "dotted"]]
    .map(([v, l, k]) => `<span><span class="sw ${k}" style="--sw:var(${v})"></span>${esc(l)}</span>`).join("") +
    `<a class="chart-credit" href="https://www.tradingview.com/lightweight-charts/" target="_blank" rel="noopener">Charts: TradingView Lightweight Charts</a>`;
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
    ["Live", live, p.live.probes ? `${p.live.probes} probe${p.live.probes === 1 ? "" : "s"} included` : "strategies only"],
    ["Positions", s.positions.length, s.open_orders.length ? `${s.open_orders.length} order${s.open_orders.length > 1 ? "s" : ""} pending` : "open now"],
  ];
  const maxFail = Math.max(1, ...p.gate_failures.map((g) => g.n));
  const probe = p.probe;
  $("#pipeline-window").textContent = `last ${p.window_days} days`;
  renderCheckin(s, v);
  $("#pipeline-body").innerHTML = `
    <ol class="funnel">${steps.map(([label, n, sub], i) => `<li class="${n > 0 ? "has" : ""}${i === steps.length - 1 && n > 0 ? " goal" : ""}">
      <span class="funnel-n num">${n}</span><span class="funnel-label">${label}</span><small>${esc(sub)}</small></li>`).join("")}</ol>
    <div class="pipeline-grid">
      <div><h3>Closest to trading</h3>
        ${p.candidates.length ? `<ul class="cands">${p.candidates.map((c) => `<li>
          <div class="cand-main"><a href="#" data-exp="${c.id}">#${c.id} ${esc(c.title)}</a>
            <small>${esc(c.venue)} · Sharpe ${num(c.sharpe)} · ${c.trades ?? "–"} trades · gates ${gateCount(c.n_checks, c.failed.length)}</small></div>
          <span class="cand-ret num ${signCls(c.test_return)}">${pct(c.test_return, 1)}</span>
          ${c.operator_blocker ? `<span class="muted cand-note">${esc(c.operator_blocker)}</span>` :
            `<button class="btn small ${c.test_return > 0 ? "primary" : ""}" data-probe="${c.id}" title="${esc(c.probe_blocker ? "Auto-probe skipped: " + c.probe_blocker : "Paper-trade this idea now with a small probe allocation")}">Start probe…</button>`}</li>`).join("")}</ul>`
          : `<p class="muted">No finished experiments in this window.</p>`}</div>
      <div><h3>What blocks ideas</h3>
        ${p.gate_failures.length ? `<ul class="bars">${p.gate_failures.slice(0, 6).map((g) => `<li><span>${esc(gateLabel(g.gate))}</span>
          <span class="bar"><i style="width:${(g.n / maxFail * 100).toFixed(0)}%"></i></span><span class="num">${g.n}</span></li>`).join("")}</ul>`
          : `<p class="muted">No gate failures recorded.</p>`}
        <p class="probe-rule">${probe.enabled ? `<b>Probes on.</b> An idea that made more than ${pct(probe.min_test_return, 1)} on its sealed test gets ${pct(probe.allocation_pct, 0, false)} of equity for ${probe.lifetime_days} days (max ${probe.max_active} at once, ${probe.per_venue_per_day}/venue/day).`
          : `<b>Probes off.</b> Only ideas that pass every gate trade.`} <a href="#records" data-nav="system">Change</a></p></div>
    </div>`;
}
// Phone check-in: is it trading, why, what happened last, what happens next. Shown above the KPIs on narrow screens.
function renderCheckin(s = state.s, v = s && tradingVerdict(s)) {
  if (!s) return;
  const last = state.journal[0];
  const next = (s.agent?.next_actions || [])[0];
  $("#checkin").className = `checkin ${esc(v.tone)}`;
  $("#checkin").innerHTML = `<div class="verdict ${esc(v.tone)}"><span class="verdict-dot" aria-hidden="true"></span><p>${esc(v.text)}</p></div>
    <dl class="checkin-rows">
      ${last ? `<div><dt>Latest</dt><dd><a href="#journal" data-nav="journal">${rich(last.title)}</a> <span class="muted num">${ago(last.ts)}</span></dd></div>` : ""}
      ${next ? `<div><dt>Next</dt><dd>${esc(next.what)} <span class="muted num">${until(next.at) === "now" ? "now" : "in " + until(next.at)}</span></dd></div>` : ""}
    </dl>`;
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
      <td><button type="button" class="row-toggle" data-pos-toggle aria-expanded="${open}" aria-label="${open ? "Hide" : "Show"} why ${esc(r.symbol)} was bought and its exit rules">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg><b>${esc(r.symbol)}</b></button><div class="muted cell-sub">${esc(r.venue)}</div></td>
      <td>${esc(r.strategy)}${r.probe ? ' <span class="probe-chip">probe</span>' : ""}</td>
      <td class="r num">${qty(r.qty)}</td>
      <td class="r num">${px(r.avg_cost)}</td>
      <td class="r num">${px(r.mark)}<div class="muted cell-sub">${esc((r.mark_basis || "").replace("_", " "))} · ${ago(r.mark_as_of)}</div></td>
      <td class="r num">${money(r.value)}</td>
      <td class="r num ${signCls(r.unrealized)}">${arrow(r.unrealized)}${money(r.unrealized)}<div class="cell-sub">${pct(r.unrealized_pct)}</div></td>
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
  if (e.target.closest("[data-pos-toggle]")) $(`#positions [data-pos="${CSS.escape(k)}"] [data-pos-toggle]`)?.focus();
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
async function renderTab({ quiet = false } = {}) {
  const body = $("#tab-body");
  const version = ++state.tabVersion;
  if (!quiet) state.tabKey = null;
  if (state.charts.px) { state.charts.px.chart.remove(); state.charts.px = null; }
  body.setAttribute("aria-busy", "true");
  try {
    if (state.tab === "research") await renderResearch(body, version, quiet);
    if (state.tab === "strategies") await renderStrategies(body, version, quiet);
    if (state.tab === "orders") await renderOrders(body, version, quiet);
    if (state.tab === "market") await renderMarket(body, version);
    if (state.tab === "system") await renderSystem(body, version);
  } catch (err) { if (version === state.tabVersion) body.innerHTML = `<div class="empty">${esc(err.message)}</div>`; }
  finally { if (version === state.tabVersion) body.setAttribute("aria-busy", "false"); }
}

function gatesCell(e) {
  if (!e.gates) return `<span class="muted">${e.error ? "could not run" : "not evaluated"}</span>`;
  const failed = e.gates?.failed || [], total = e.gates?.checks?.length || 9;
  if (!failed.length) return `<span class="passed-text">${gateCount(total, 0)}</span>`;
  const shown = failed.slice(0, 2), rest = failed.slice(2);
  return `<span class="gate-count">${gateCount(total, failed.length)}</span><div class="chips" aria-label="Failed gates: ${esc(failed.map(gateLabel).join(", "))}">${
    shown.map((f) => `<span class="gchip">${esc(gateLabel(f))}</span>`).join("")}${rest.length ? `<span class="gchip more">+${rest.length} more</span>` : ""}</div>`;
}
// Experiment statuses in plain words. "failed" means it missed a gate, not that the code broke.
const EXP_STATUS = { failed: "Missed gates", passed: "Passed gates", running: "Running", error: "Could not run", interrupted: "Interrupted" };
const expStatus = (st) => `<span class="status plain ${esc(st)}">${esc(EXP_STATUS[st] || st)}</span>`;
// Skip a background re-render when nothing changed, so a table the operator is reading does not jump.
function unchanged(data, version, quiet) {
  const key = state.tab + ":" + JSON.stringify(data);
  if (quiet && key === state.tabKey) return true;
  if (version === state.tabVersion) state.tabKey = key;
  return false;
}

async function renderResearch(body, version, quiet) {
  const ex = await api("/api/experiments");
  if (version !== state.tabVersion || unchanged(ex, version, quiet)) return;
  if (!ex.length) { body.innerHTML = emptyState("The next idea starts here.", "No experiments yet. The first research cycle begins once the worker has market data. Every hypothesis will have a frozen test and a recorded decision."); return; }
  const passed = ex.filter((e) => e.status === "passed").length;
  body.innerHTML = `<div class="research-summary"><span class="summary-total"><b>${ex.length}</b> experiments in this view</span>
    <span class="summary-count"><span class="${passed ? "up" : ""}">${passed}</span> passed gates</span><span class="summary-count">${ex.filter((e) => e.status === "failed").length} missed gates</span></div>
    <div class="tbl-scroll"><table class="tbl research-tbl"><thead><tr><th>#</th><th>Hypothesis</th><th>Status</th><th class="r">Test return</th>
    <th class="r">Sharpe</th><th class="r">Trades</th><th>Gates</th></tr></thead><tbody>
    ${ex.map((e) => `<tr class="clickable" data-exp="${e.id}"><td class="num id-cell">#${e.id}</td><td class="hyp"><a href="#" data-exp="${e.id}">${esc(e.title)}</a><div class="muted cell-sub">${esc(e.venue)} · ${authorLabel(e.author)} · ${ago(e.created_at)}</div></td>
      <td data-label="Status">${expStatus(e.status)}${e.strategy_id ? `<div class="cell-sub"><span class="${e.strategy_probe ? "probe-chip" : "strategy-chip"}">${e.strategy_probe ? `probe #${e.strategy_id} · forward test` : `strategy #${e.strategy_id}`}</span></div>` : ""}</td>
      <td class="r num ${signCls(e.test?.total_return)}" data-label="Test return">${pct(e.test?.total_return)}</td><td class="r num" data-label="Sharpe">${num(e.test?.sharpe)}</td>
      <td class="r num" data-label="Trades">${e.test?.n_trades ?? "–"}</td><td class="gates" data-label="Gates">${gatesCell(e)}</td></tr>`).join("")}</tbody></table></div>
    <div class="research-note">Open an experiment to see its hypothesis, sealed test and gates, or to start it as a probe.</div>`;
}

async function renderStrategies(body, version, quiet) {
  const st = await api("/api/strategies");
  if (version !== state.tabVersion || unchanged(st, version, quiet)) return;
  if (!st.length) { body.innerHTML = emptyState("Nothing live yet.", "A strategy goes live when an experiment passes every gate. A probe goes live when an idea made money on its sealed test but missed a gate; you can also start one from any experiment."); return; }
  body.innerHTML = `<div class="tbl-scroll"><table class="tbl"><thead><tr><th>Strategy</th><th>Holding now</th><th>Status</th><th class="r">Allocation</th><th class="r">Live return</th>
    <th class="r">Expected (test CAGR)</th><th class="r">Fills</th><th class="r">Avg slippage</th><th></th></tr></thead><tbody>
    ${st.map((s) => `<tr class="clickable" data-strat="${s.id}"><td><b><a href="#" data-strat="${s.id}">${esc(s.name)} v${s.version}</a></b>${s.probe ? ' <span class="probe-chip">probe</span>' : ""}<div class="muted" style="font-size:12px">${esc(s.venue)} · exp #${s.experiment_id}</div>
        <div class="sym-list" title="${esc(s.symbols.map((x) => x.symbol).join(", "))}">Trades ${symbolSummary(s.symbols)}</div></td>
      <td>${holdingCell(s.symbols)}</td>
      <td><span class="status ${esc(s.status)}">${esc(s.status)}</span></td>
      <td class="r num">${money(s.allocation)}<div class="muted cell-sub">approved ${money(s.approved_allocation)}</div></td>
      <td class="r num ${signCls(s.live.return)}">${pct(s.live.return)}<div class="muted cell-sub">${s.live.days_live}d live</div></td>
      <td class="r num">${pct(s.expectations?.test?.cagr)}</td><td class="r num">${s.live.fills}</td>
      <td class="r num">${s.live.avg_slippage_bps == null ? "–" : num(s.live.avg_slippage_bps, 1) + " bp"}</td>
      <td>${s.status === "retired" ? "" : `<button class="btn small danger" data-retire="${s.id}">Retire…</button>`}</td></tr>`).join("")}</tbody></table></div>`;
}

function symbolSummary(syms, max = 4) {
  const names = syms.map((x) => x.symbol);
  return esc(names.slice(0, max).join(", ")) + (names.length > max ? ` <span class="muted">+${names.length - max} more</span>` : "");
}
function holdingCell(syms) {
  const held = syms.filter((x) => x.qty > 0), pend = syms.filter((x) => x.pending);
  if (!held.length && !pend.length) return `<span class="muted">cash</span>`;
  return held.map((x) => `<div><b>${esc(x.symbol)}</b> <span class="num muted">${qty(x.qty)} · ${money(x.value)}</span></div>`).join("") +
    pend.filter((x) => !(x.qty > 0 && x.pending === "buy")).map((x) => `<div class="muted">${esc(x.pending)} ${esc(x.symbol)} pending</div>`).join("");
}
async function renderOrders(body, version, quiet) {
  const [orders, fills] = await Promise.all([api("/api/orders?limit=100"), api("/api/fills?limit=100")]);
  if (version !== state.tabVersion || unchanged([orders, fills], version, quiet)) return;
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
    <div class="muted cell-sub" style="margin-top:6px">Markers show this account's simulated fills. ${state.s?.mode.market_data === "synthetic" ? "Synthetic 15-minute bars for local/demo testing; these are not live market prices." : "Stocks: split/dividend-adjusted 15-minute bars (regular session). Crypto: Coinbase 15-minute bars."}</div>`;
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

const LIMIT_GROUPS = [
  ["Risk", "Hard limits the risk engine applies to every order.", ["risk"]],
  ["Probes", "Small forward-paper trials of ideas that made money on their sealed test but missed a gate. Not evidence of an edge.", ["probe"]],
  ["Acceptance gates", "Every gate must pass for a full allocation.", ["gates", "research"]],
  ["AI budget & schedule", "Estimated spend is checked before every model call.", ["budget", "schedule"]],
];
const fieldId = (f) => `limit-${f.g}-${f.k}`;
const localReviewHour = (utcHour) => {
  const d = new Date(); d.setUTCHours(utcHour, 0, 0, 0);
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
};
function limitField(f, value, bounds) {
  const id = fieldId(f);
  if (f.unit === "bool") return `<div class="field check" data-field="${f.g}.${f.k}"><label for="${id}"><input type="checkbox" id="${id}" data-g="${f.g}" data-k="${f.k}" data-bool="1" ${value ? "checked" : ""}> ${esc(f.label)}</label>
    <div class="hint" id="${id}-hint">Currently ${value ? "on" : "off"}</div></div>`;
  const [lo, hi] = bounds || [];
  const range = bounds ? `${formatLimit(lo, f.unit)} – ${formatLimit(hi, f.unit)}` : "";
  const hint = [f.hint, range && `allowed ${range}`, f.unit === "hour" && `${localReviewHour(value)} your time`].filter(Boolean).join(" · ");
  const suffix = f.unit === "usd" ? "" : f.unit === "hour" ? "UTC" : UNIT_SUFFIX[f.unit] || "";
  return `<div class="field" data-field="${f.g}.${f.k}"><label for="${id}">${esc(f.label)}</label>
    <div class="input-unit${f.unit === "usd" ? " prefix" : ""}">${f.unit === "usd" ? '<span class="unit" aria-hidden="true">$</span>' : ""}
      <input id="${id}" type="text" inputmode="decimal" autocomplete="off" spellcheck="false" data-g="${f.g}" data-k="${f.k}"
        value="${esc(toDisplay(value, f.unit))}" aria-describedby="${id}-hint ${id}-err">
      ${suffix ? `<span class="unit" aria-hidden="true">${esc(suffix)}</span>` : ""}</div>
    <div class="hint" id="${id}-hint" data-base="${esc(hint)}">${esc(hint)}</div><div class="field-err" id="${id}-err"></div></div>`;
}
async function renderSystem(body, version) {
  const [sys, calls] = await Promise.all([api("/api/system"), api("/api/llm_calls?limit=15")]);
  if (version !== state.tabVersion) return;
  state.sys = sys;
  const v = (g, k) => sys.settings[g][k];
  const auditDetail = (d) => d == null ? "" : typeof d === "object" ? kv(d) : esc(d);
  body.innerHTML = `
    <div class="section"><h4>Operating limits</h4>
      <p class="secondary section-lead">Only you can change these; the agent cannot. Gate profile: <b>${esc(sys.gate_profile)}</b>.
        Exploratory admits paper experiments, not validated trading edges. Strict requires the causal DSL and stronger screening.
        Neither profile proves profitability. Causal DSL required: ${sys.settings.gates.require_causal_dsl ? "yes" : "no"}.</p>
      <div class="button-row"><button class="btn" data-gate-preset="exploratory">Use exploratory gates…</button>
        <button class="btn" data-gate-preset="strict">Use strict gates…</button></div>
      <form id="limits" novalidate>${LIMIT_GROUPS.map(([title, note, groups]) => `<fieldset class="limit-group"><legend>${esc(title)}</legend>
        ${note ? `<p class="muted limit-note">${esc(note)}</p>` : ""}<div class="form-grid">${LIMIT_FIELDS.filter((f) => groups.includes(f.g))
          .map((f) => limitField(f, v(f.g, f.k), sys.bounds?.[`${f.g}.${f.k}`])).join("")}</div></fieldset>`).join("")}
        <div class="form-bar"><button class="btn primary" type="submit" id="limits-save" disabled>Review changes</button>
          <button class="btn ghost" type="button" id="limits-reset" disabled>Discard</button>
          <span class="muted form-status" id="limits-status" role="status">No unsaved changes.</span></div>
        <p class="muted ceilings">Hard ceilings set in config.py: position ≤ ${pct(sys.ceilings.ceiling_max_position_pct, 0, false)}, gross ≤ ${pct(sys.ceilings.ceiling_max_gross_pct, 0, false)} (no leverage),
          order ≤ ${money(sys.ceilings.ceiling_max_order_usd)}, AI ≤ ${money(sys.ceilings.ceiling_monthly_llm_usd)}/month.</p></form></div>
    <div class="section"><h4>Controls</h4>
      <div class="control-groups"><div><p class="muted control-label">Research</p><div class="button-row">
        <button class="btn" data-ctl="research_nasdaq">Run NASDAQ research now</button><button class="btn" data-ctl="research_crypto">Run crypto research now</button></div></div>
      <div><p class="muted control-label">Positions and halts</p><div class="button-row">
        <button class="btn danger" data-ctl="flatten_all">Flatten all positions…</button>
        ${state.s.controls.drawdown_halt ? `<button class="btn" data-ctl="reset_drawdown_halt">Reset drawdown halt…</button>` : ""}
        ${state.s.controls.system_halt ? `<button class="btn" data-ctl="clear_system_halt">Clear ledger halt…</button>` : ""}</div></div></div></div>
    <div class="section"><h4>Integrity & isolation</h4><div class="secondary small-text">
      Ledger rebuilt from fills: <b class="${sys.integrity.ok ? "passed-text" : "err"}">${sys.integrity.ok ? "matches" : "MISMATCH"}</b> (cash ${money(sys.integrity.cash, 2)}).
      Research-code sandbox: ${esc(sys.sandbox.mode)}, env ${esc(sys.sandbox.env)}, network namespace ${sys.sandbox.network_namespace ? "yes" : "no"},
      ${sys.sandbox.mode === "local" && !sys.sandbox.local_supported ? "<b>Python sandbox unavailable: requires Linux/Docker.</b>" : ""}
      ${sys.sandbox.cpu_seconds}s CPU / ${sys.sandbox.memory_mb} MB. LLM: ${esc(sys.llm.demo ? "demo (no key)" : sys.llm.model + " · effort " + sys.llm.effort)},
      knowledge cutoff assumed ${esc(sys.llm.knowledge_cutoff)}.</div></div>
    <div class="section"><h4>Recent LLM calls (audit)</h4>${calls.length ? `<div class="tbl-scroll"><table class="tbl"><thead><tr><th>#</th><th>When</th><th>Purpose</th><th>Model</th><th class="r">Tokens in/out</th><th class="r">Cost</th><th>Status</th></tr></thead><tbody>
      ${calls.map((c) => `<tr class="clickable" data-llm="${c.id}"><td class="num"><a href="#" data-llm="${c.id}">${c.id}</a></td><td class="muted">${ago(c.ts)}</td><td>${esc(c.purpose)}</td><td>${esc(c.served_model || c.model)}</td>
      <td class="r num">${c.input_tokens}/${c.output_tokens}</td><td class="r num">${money(c.cost_usd, 4)}</td><td>${esc(c.status)}</td></tr>`).join("")}</tbody></table></div>` : `<div class="muted">No LLM calls yet.</div>`}</div>
    <div class="section"><h4>Jobs</h4><div class="tbl-scroll"><table class="tbl"><thead><tr><th>Job</th><th>Last run</th><th>Status</th><th class="r">Runs</th><th>Error</th></tr></thead><tbody>
      ${sys.jobs.filter((j) => !j.name.includes(":behind") && !j.name.startsWith("closed_quotes")).map((j) => `<tr><td>${esc(j.name)}</td><td class="muted">${ago(j.last_started_at)}</td><td>${esc(j.last_status || "")}</td><td class="r num">${j.run_count}</td>
      <td class="secondary small-text">${esc(j.last_error || "")}</td></tr>`).join("")}</tbody></table></div></div>
    <div class="section"><h4>Audit log</h4><ul class="audit">${sys.audit.slice(0, 15).map((a) => `<li><span class="muted num">${dt(a.ts)}</span> <span>${esc(a.actor)} · ${esc(a.action)}</span> ${auditDetail(a.detail)}</li>`).join("") || `<li class="muted">Empty.</li>`}</ul></div>`;
  const form = $("#limits");
  form.addEventListener("input", () => updateLimitDraft(form));
  form.addEventListener("change", () => updateLimitDraft(form));
  $("#limits-reset").addEventListener("click", () => renderTab());
  form.addEventListener("submit", (e) => { e.preventDefault(); saveLimits(form); });
}
// Reads the form against the stored values: which fields changed, which are invalid, and how.
function readLimits(form) {
  const sys = state.sys, changes = [], errors = [];
  for (const f of LIMIT_FIELDS) {
    const input = form.querySelector(`#${fieldId(f)}`);
    if (!input) continue;
    const before = sys.settings[f.g][f.k];
    let after;
    if (f.unit === "bool") after = input.checked;
    else {
      const parsed = fromDisplay(input.value, f.unit, sys.bounds?.[`${f.g}.${f.k}`]);
      if (parsed.error) { errors.push({ f, input, msg: parsed.error }); continue; }
      after = parsed.value;
    }
    if (!sameValue(before, after)) changes.push({ f, before, after, dir: changeDirection(f, before, after) });
  }
  return { changes, errors };
}
function setFieldError(input, msg) {
  const field = input.closest(".field");
  field.classList.toggle("invalid", Boolean(msg));
  input.setAttribute("aria-invalid", String(Boolean(msg)));
  field.querySelector(".field-err").textContent = msg || "";
}
function updateLimitDraft(form) {
  const { changes, errors } = readLimits(form);
  form.querySelectorAll(".field").forEach((el) => { el.classList.remove("changed", "loosens"); });
  form.querySelectorAll("input:not([data-bool])").forEach((i) => setFieldError(i, ""));
  for (const c of changes) {
    const field = form.querySelector(`[data-field="${c.f.g}.${c.f.k}"]`), hint = field.querySelector(".hint");
    field.classList.add("changed"); if (c.dir === "loosens") field.classList.add("loosens");
    hint.textContent = `was ${formatLimit(c.before, c.f.unit)}${c.dir === "loosens" ? " · loosens a control" : ""}`;
  }
  form.querySelectorAll(".field:not(.changed) .hint").forEach((h) => {
    if (h.dataset.base != null) h.textContent = h.dataset.base;
    else { const box = h.closest(".field").querySelector("input"); h.textContent = `Currently ${box.checked ? "on" : "off"}`; }
  });
  for (const e of errors) setFieldError(e.input, e.msg);
  const loosen = changes.filter((c) => c.dir === "loosens").length;
  $("#limits-save").disabled = !changes.length && !errors.length;
  $("#limits-reset").disabled = !changes.length && !errors.length;
  $("#limits-status").textContent = errors.length ? `${errors.length} field${errors.length > 1 ? "s need" : " needs"} a valid value.` :
    changes.length ? `${changes.length} unsaved change${changes.length > 1 ? "s" : ""}${loosen ? `, ${loosen} loosening a control` : ""}.` : "No unsaved changes.";
}
async function saveLimits(form) {
  const { changes, errors } = readLimits(form);
  updateLimitDraft(form);
  if (errors.length) { errors[0].input.focus(); return; }
  if (!changes.length) return;
  const row = (c) => `<li class="${c.dir}"><span>${esc(c.f.label)}</span><span class="num">${esc(formatLimit(c.before, c.f.unit))} → <b>${esc(formatLimit(c.after, c.f.unit))}</b></span>${
    c.dir === "loosens" ? '<span class="dir-tag">loosens</span>' : c.dir === "tightens" ? '<span class="dir-tag quiet">tightens</span>' : ""}</li>`;
  const loosen = changes.filter((c) => c.dir === "loosens");
  const ok = await confirmDialog({
    title: `Save ${changes.length} change${changes.length > 1 ? "s" : ""} to operating limits?`, confirm: loosen.length ? "Save, including loosening" : "Save changes",
    tone: loosen.length ? "danger" : "primary",
    body: `${loosen.length ? `<p class="confirm-warn">${loosen.length} change${loosen.length > 1 ? "s loosen" : " loosens"} a risk control or gate. They apply to the next order and the next research decision.</p>` : ""}
      <ul class="change-list">${changes.map(row).join("")}</ul><p class="muted">Saved together: either every change applies or none does.</p>`,
  });
  if (!ok) return;
  const body = {};
  for (const c of changes) (body[c.f.g] ||= {})[c.f.k] = c.after;
  $("#limits-save").disabled = true;
  try {
    await api("/api/settings", { body });
    toast(`Saved ${changes.length} limit change${changes.length > 1 ? "s" : ""}`, { tone: "good", detail: changes.map((c) => `${c.f.label} ${formatLimit(c.after, c.f.unit)}`).join(" · ") });
    refresh(); renderTab();
  } catch (err) {
    // Server messages name the setting ("risk.max_gross_pct=3 outside …"); put the message on that field.
    const m = /\b([a-z_]+)\.([a-z0-9_]+)\b/.exec(err.message);
    const f = m && LIMIT_FIELDS.find((x) => x.g === m[1] && x.k === m[2]);
    const input = f && form.querySelector(`#${fieldId(f)}`);
    if (input) { setFieldError(input, `Not saved: ${err.message.replace(/^[^\s]+\s*/, "")}`); input.focus(); }
    fail(err, "Nothing was saved. Fix the field and review again.");
    $("#limits-save").disabled = false;
  }
}
const CONTROL_CONFIRM = {
  flatten_all: () => ({ title: "Close every position?", tone: "danger", confirm: "Flatten all",
    body: `<ul class="confirm-list"><li>Pauses new entries and cancels pending orders.</li><li>Sells ${state.s?.positions.length ?? 0} position${state.s?.positions.length === 1 ? "" : "s"} at the next usable quotes (NASDAQ orders wait for the session).</li><li>Exits keep retrying until flat.</li></ul>` }),
  reset_drawdown_halt: () => ({ title: "Reset the drawdown halt?", confirm: "Reset halt", body: "<p>New positions are allowed again within the current limits.</p>" }),
  clear_system_halt: () => ({ title: "Clear the ledger halt?", confirm: "Clear halt", body: "<p>Only clears if the ledger now passes its integrity check.</p>" }),
};
document.addEventListener("click", async (e) => {
  const ctl = e.target.closest("[data-ctl]");
  if (!ctl) return;
  const action = ctl.dataset.ctl;
  if (CONTROL_CONFIRM[action] && !(await confirmDialog(CONTROL_CONFIRM[action]()))) return;
  try {
    ctl.disabled = true;
    await control(action);
    const [m, d] = CTL_MSG[action] || ["Done"];
    toast(m, { tone: "good", detail: d }); refresh();
  } catch (err) { fail(err); } finally { setTimeout(() => { ctl.disabled = false; }, 3000); }
});
$("#tab-body").addEventListener("click", async (e) => {
  const preset = e.target.closest("[data-gate-preset]");
  if (preset) {
    const name = preset.dataset.gatePreset, values = state.sys?.gate_presets?.[name] || {};
    const changes = LIMIT_FIELDS.filter((f) => f.g === "gates" && f.k in values && !sameValue(state.sys.settings.gates[f.k], values[f.k]))
      .map((f) => ({ f, before: state.sys.settings.gates[f.k], after: values[f.k], dir: changeDirection(f, state.sys.settings.gates[f.k], values[f.k]) }));
    const loosen = changes.filter((c) => c.dir === "loosens").length;
    const ok = await confirmDialog({ title: `Use ${name} gates?`, confirm: `Use ${name} gates`, tone: loosen ? "danger" : "primary",
      body: `<p>Applies to future research decisions; existing allocations are unchanged. Replaces any gate edits.</p>${changes.length ? `<ul class="change-list">${changes.map((c) =>
        `<li class="${c.dir}"><span>${esc(c.f.label)}</span><span class="num">${esc(formatLimit(c.before, c.f.unit))} → <b>${esc(formatLimit(c.after, c.f.unit))}</b></span>${c.dir === "loosens" ? '<span class="dir-tag">loosens</span>' : ""}</li>`).join("")}</ul>` : "<p class=\"muted\">Your gates already match this preset.</p>"}` });
    if (!ok) return;
    try { await api(`/api/gate-presets/${name}`, { method: "POST" }); toast(`Using ${name} gates`, { tone: "good" }); renderTab(); }
    catch (err) { fail(err); }
    return;
  }
  const ret = e.target.closest("[data-retire]");
  if (ret) {
    e.stopPropagation();
    if (await confirmDialog({ title: "Retire this strategy?", tone: "danger", confirm: "Retire and close positions",
      body: "<p>Cancels its queued entries and sells its holdings at the next usable quotes. A retired strategy never trades again.</p>" })) {
      try { await api(`/api/strategies/${ret.dataset.retire}/retire`, { method: "POST" }); toast("Strategy retired", { detail: "Its positions are being closed." }); await renderTab(); }
      catch (err) { fail(err); }
    }
    return;
  }
  const llm = e.target.closest("[data-llm]"); if (llm) return openLLM(llm.dataset.llm).catch(fail);
});

// ---------------------------------------------------------------- drawers
let closeDrawer = null;
function drawer(title, html, hash = "") {
  if (closeDrawer) closeDrawer({ keepHash: true });
  const previousFocus = document.activeElement;
  $("#drawer-root").innerHTML = `<div class="drawer-bg"></div><div class="drawer" role="dialog" aria-modal="true" aria-label="${esc(title)}"><div class="drawer-h"><h3>${esc(title)}</h3>
    <button class="btn small" id="drawer-close">Close</button></div><div class="drawer-b">${html}</div></div>`;
  // Each record drawer has its own address (#experiment-2), so it can be bookmarked or sent.
  if (hash && location.hash !== hash) history.replaceState(null, "", hash);
  const close = ({ keepHash = false } = {}) => {
    if (!keepHash && location.hash) history.replaceState(null, "", location.pathname + location.search);
    if (state.charts.exp) { state.charts.exp.chart.remove(); state.charts.exp = null; }
    document.removeEventListener("keydown", onKey);
    $("#drawer-root").innerHTML = ""; $("#app").inert = false; closeDrawer = null;
    if (previousFocus?.isConnected) previousFocus.focus();
  };
  const onKey = (ev) => {
    if (document.querySelector("dialog[open]")) return;  // a confirmation on top owns the keyboard
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
  $("#drawer-close").onclick = () => close(); $(".drawer-bg").onclick = () => close();
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
  const parts = ["train", "validation", "test"];
  const tl = w ? `<div class="timeline" aria-hidden="true">${parts.map((k) => `<div class="${k}" style="flex:${w[k].bars}"></div>`).join("")}</div>
    <ul class="timeline-legend">${parts.map((k) => `<li class="${k}"><span class="sw"></span><b>${cap(k)}${k === "test" ? " (sealed)" : ""}</b>
      <span class="muted">${day(w[k].start)} – ${day(w[k].end)} · ${w[k].bars.toLocaleString()} bars</span></li>`).join("")}</ul>` : "";
  const ch = e.challenge || {};
  const t = e.test || {};
  const g = e.gates;
  const dec = (e.decisions || []).slice(-1)[0];
  const spec = e.spec || {};
  drawer(`Experiment #${e.id}: ${h.title || ""}`, `
    <div class="section">${expStatus(e.status)} <span class="muted">· ${esc(e.venue)} · created ${dt(e.created_at)} · frozen ${dt(e.frozen_at)} · by ${authorLabel(h.author)}</span>
      ${e.strategy ? ` · <a href="#" data-strat="${e.strategy.id}">${e.strategy.probe ? "probe" : "strategy"} ${esc(e.strategy.name)} v${e.strategy.version} (${esc(e.strategy.probe && e.strategy.status === "active" ? "forward test" : e.strategy.status)})</a>` : ""}</div>
    ${dec ? `<div class="section"><h4>Decision: ${esc(dec.kind)} (${authorLabel(dec.author)}${dec.llm_call_id ? `, <a href="#" data-llm="${dec.llm_call_id}">inputs</a>` : ""})</h4><div class="pre">${rich(dec.rationale)}</div>
      ${(dec.details?.lessons || []).length ? `<h4 style="margin-top:10px">Recorded lessons</h4><ul style="padding-left:18px">${dec.details.lessons.map((l) => `<li>${rich(l)}</li>`).join("")}</ul>` : ""}</div>` : ""}
    ${e.test && !e.strategy && e.status !== "running" ? `<div class="rejudge-box"><div><b>Re-judge under current gates</b>
      <p>Re-checks the stored results against today's thresholds. No new backtest and no new look at the sealed test window. If every gate passes, the analyst makes a fresh decision.</p></div>
      <button class="btn" data-rejudge="${e.id}">Re-judge #${e.id}</button></div>
    <div class="rejudge-box probe-box"><div><b>Paper-trade it as a probe</b>
      <p>Starts a small, clearly-labelled forward test with ${pct(state.s?.pipeline?.probe.allocation_pct ?? 0.03, 0, false)} of equity for ${state.s?.pipeline?.probe.lifetime_days ?? 14} days.
      Uses the frozen rules and the normal risk checks. ${(e.test.total_return ?? 0) > 0 ? "It made money on its sealed test." : "<b>It lost money on its sealed test</b>, so expect the same unless conditions changed."}</p></div>
      <button class="btn ${(e.test.total_return ?? 0) > 0 ? "primary" : ""}" data-probe="${e.id}" data-lost="${(e.test.total_return ?? 0) > 0 ? 0 : 1}">Start probe…</button></div>` : ""}
    <div class="section"><h4>Hypothesis (recorded before testing)</h4><div class="pre">${rich(h.rationale)}</div>
      <p><b>Prediction:</b> ${rich(h.prediction)}<br><b>Would drop it if:</b> ${rich(h.falsification)}</p></div>
    <div class="section"><h4>Rules</h4><dl class="spec">
      <dt>Entry</dt><dd><code>${esc(spec.entry || "custom code")}</code></dd><dt>Exit</dt><dd><code>${esc(spec.exit || "custom code")}</code></dd>
      ${spec.rank_by ? `<dt>Rank by</dt><dd><code>${esc(spec.rank_by)}</code>, top ${esc(spec.top_k)}</dd>` : ""}
      <dt>Universe</dt><dd>${esc((spec.universe || []).join(", ")) || "–"}</dd>
      <dt>Parameter grid</dt><dd>${kv(spec.params)}</dd><dt>Chosen on train</dt><dd><b>${kv(e.chosen_params)}</b></dd>
      <dt>Sizing</dt><dd>${kv(spec.sizing)}${spec.stop_loss_pct ? ` · stop ${pct(spec.stop_loss_pct, 0, false)}` : ""}${spec.max_hold_bars ? ` · max hold ${spec.max_hold_bars} bars` : ""}</dd></dl>
      ${spec.kind === "code" ? `<pre class="pre"><code>${esc(spec.code)}</code></pre>` : ""}</div>
    ${e.error ? `<div class="alert critical"><span class="icon">${icon.alert}</span><div>${esc(e.error)}</div></div>` : ""}
    ${w ? `<div class="section"><h4>Chronological split (test window sealed until the spec was frozen)</h4>${tl}</div>` : ""}
    ${e.test ? `<div class="section"><h4>Results (net of fees, spread, impact)</h4>${metricsTable([["Train", e.train], ["Validation", e.validation], ["Test", t],
      ["Test ×2 costs", ch.test_cost_x2], ["Test +1 bar delay", ch.test_delay_1bar], [`${t.benchmark_symbol} B&H (test)`, t.benchmark_venue], ["Universe B&H (test)", t.benchmark_universe]])}
      <div class="muted cell-sub" style="margin-top:6px">PSR ${num(t.psr)} · deflated PSR ${num(t.deflated_psr)} vs. expected max Sharpe of ${e.leakage?.n_trials_for_deflation} zero-skill trial(s) (${num(t.sr_star_annualised)} annualised).</div>
      ${(e.leakage?.n_trials_for_deflation ?? 0) <= 1 ? `<p class="caveat">Deflation counted only ${e.leakage?.n_trials_for_deflation ?? 0} trial, so it barely discounts luck here. Treat deflated PSR as an upper bound.</p>` : ""}
      ${w && (w.test.end - w.test.start) < 90 * 86400 ? `<p class="caveat">CAGR is annualised from a ${Math.max(1, Math.round((w.test.end - w.test.start) / 86400))}-day test window; short windows exaggerate it. Net return is the measured result.</p>` : ""}</div>
    <div class="section"><h4>Test window: strategy vs. benchmark (growth of $1)</h4><div class="chart-tip" id="exp-tip" style="padding-left:0"></div><div class="chart-wrap" id="exp-chart" style="height:220px"></div>
      <div class="chart-legend" style="padding:0"><span><span class="sw" style="--sw:var(--text-primary)"></span>Strategy (net)</span><span><span class="sw dashed" style="--sw:var(--series-2)"></span>${esc(t.benchmark_symbol)} buy & hold</span></div></div>
    <div class="section"><h4>Acceptance gates: ${g?.checks ? gateCount(g.checks.length, g.checks.filter((c) => !c.pass).length) : "not evaluated"}</h4>${(g?.checks || []).map((c) => `<div class="gate ${c.pass ? "pass" : "fail"}"><span class="mark">${c.pass ? MARK.pass : MARK.fail}</span>
      <div><b>${esc(gateLabel(c.name))}</b> <span class="num">${c.value == null ? "n/a" : c.name === "test_trades" ? Math.round(c.value) : num(c.value, 3)} ${esc(c.op)} ${c.name === "test_trades" ? Math.round(c.threshold) : num(c.threshold, 3)}</span><div class="muted cell-sub">${rich(c.why)}</div></div></div>`).join("")}</div>
    <div class="section"><h4>Challenge</h4><div class="secondary" style="font-size:13px">Neighbouring parameters positive: ${pct(ch.neighbor_positive_frac, 0, false)} (median Sharpe ${num(ch.neighbor_median_sharpe)}) ·
      pre-test sub-periods positive: ${pct(ch.subperiod_positive_frac, 0, false)} · top-5 trades share of pre-test profit: ${ch.top5_concentration_pre_test == null ? "n/a (no profit)" : pct(ch.top5_concentration_pre_test, 0, false)}</div>
      <div class="tbl-scroll" style="margin-top:6px"><table class="tbl"><thead><tr><th>Sub-period</th><th class="r">Return</th><th class="r">Sharpe</th><th class="r">Trades</th></tr></thead><tbody>
      ${(ch.subperiods || []).map((s) => `<tr><td>${day(s.start)} – ${day(s.end)}</td><td class="r num ${signCls(s.total_return)}">${pct(s.total_return)}</td><td class="r num">${num(s.sharpe)}</td><td class="r num">${s.n_trades}</td></tr>`).join("")}</tbody></table></div></div>
    <div class="section"><h4>Data limitations & possible leakage</h4><ul class="notes secondary" style="font-size:13px;padding-left:18px">${(e.leakage?.notes || []).map((n) => `<li>${esc(n)}</li>`).join("")}</ul></div>` : ""}`, `#experiment-${e.id}`);
  if (e.equity_curve?.test?.ts?.length) {
    const chart = LightweightCharts.createChart($("#exp-chart"), { ...themedChartOptions(), autoSize: true });
    const b = chart.addLineSeries({ color: cssVar("--series-2"), lineWidth: 1.5, lineStyle: 2, priceLineVisible: false, lastValueVisible: false });
    const a = chart.addLineSeries({ color: cssVar("--text-primary"), lineWidth: 2, priceLineVisible: false });
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
    <div class="section"><h4>What it trades (${s.symbols.length} symbol${s.symbols.length === 1 ? "" : "s"})</h4>
      <div class="sym-grid">${s.symbols.map((x) => `<div class="sym ${x.qty > 0 ? "held" : ""}"><b>${esc(x.symbol)}</b>
        <span>${x.qty > 0 ? `${qty(x.qty)} @ ${px(x.avg_cost)} · ${money(x.value)} <span class="${signCls(x.unrealized)}">${money(x.unrealized)}</span>` : "not held"}</span>
        ${x.pending ? `<span class="sym-pend">${esc(x.pending)} order pending</span>` : ""}</div>`).join("")}</div>
      <div class="muted" style="font-size:12px;margin-top:8px">The rules are checked for every symbol on each 15-minute bar close; it buys whichever symbols signal, sized from its ${money(s.allocation)} sleeve.</div></div>
    <div class="section"><h4>Thesis (recorded at approval)</h4><div class="pre">${rich(s.thesis)}</div></div>
    <div class="section"><h4>Exit plan & invalidation</h4><div class="pre">${rich(s.exit_plan)}</div><div class="secondary small-text" style="margin-top:6px">Invalidation: ${kv(s.invalidation)}</div></div>
    <div class="section"><h4>Rules (frozen)</h4><div style="font-size:13px">Entry <code>${esc(sp.entry || "code")}</code><br>Exit <code>${esc(sp.exit || "code")}</code><br>
      <span class="secondary">params ${kv(sp.fixed_params)}</span></div></div>
    <div class="section"><h4>Live vs expected</h4>${metricsTable([["Test (expected)", s.expectations?.test]])}
      <div class="secondary" style="font-size:13px;margin-top:8px">Live: ${pct(s.live.return)} over ${s.live.days_live} days on ${money(s.allocation)} · realised ${money(s.live.realized, 2)} ·
      unrealised ${money(s.live.unrealized, 2)} · fees ${money(s.live.fees, 2)} · avg slippage ${s.live.avg_slippage_bps ?? "–"} bp · avg spread ${s.live.avg_spread_bps ?? "–"} bp</div></div>
    <div class="section"><h4>Reviews</h4>${(s.reviews || []).map((r) => `<div style="padding:8px 0;border-top:1px solid var(--border)"><b>${esc(r.kind)}</b> <span class="muted">${dt(r.ts)} · ${authorLabel(r.author)}</span>
      <div class="pre secondary" style="font-size:13px">${rich(r.rationale)}</div></div>`).join("") || `<div class="muted">No reviews yet (daily).</div>`}</div>`, `#strategy-${s.id}`);
}
async function openOrder(id) {
  const o = await api(`/api/orders/${encodeURIComponent(id)}`);
  const fills = o.fills;
  drawer(`Order #${o.id}: ${o.side} ${o.symbol}`, `
    <div class="section"><span class="status ${esc(o.status)}">${esc(o.status)}</span> <span class="muted">· ${esc(o.client_order_id)} · origin ${esc(o.origin)}</span></div>
    <div class="section"><h4>Rationale (written ${dt(o.created_at)}, before any fill)</h4><div class="pre">${rich(o.rationale)}</div></div>
    <div class="section"><h4>Risk engine</h4><div class="secondary" style="font-size:13px">${esc(o.risk_check?.reason || o.status_note || "")}</div>
      ${(o.risk_check?.checks || []).map((c) => `<div class="gate ${c.pass ? "pass" : "fail"}"><span class="mark">${c.pass ? MARK.pass : MARK.fail}</span><div><b>${esc(c.check)}</b> <span class="muted">${esc(c.detail)}</span></div></div>`).join("")}</div>
    <div class="section"><h4>Execution</h4><div class="secondary" style="font-size:13px">Qty ${qty(o.qty)} · eligible ${dt(o.eligible_at)} · expires ${dt(o.expires_at)} · filled ${qty(o.filled_qty)} @ ${px(o.avg_price)} · fees ${money(o.fees, 2)}</div>
      ${fills.map((f) => `<div style="font-size:13px;margin-top:6px">Fill ${dt(f.ts)}: ${qty(f.qty)} @ ${px(f.price)} (bid ${px(f.ref_bid)} / ask ${px(f.ref_ask)}; ${esc(f.price_basis)}; impact ${num(f.slippage_bps, 1)} bp; fee ${money(f.fee, 2)})</div>`).join("")}</div>`, `#order-${o.id}`);
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
    const probe = state.s?.pipeline?.probe || {}, cand = (state.s?.pipeline?.candidates || []).find((c) => String(c.id) === pr.dataset.probe);
    const lost = cand ? cand.test_return <= 0 : pr.dataset.lost === "1";
    if (!(await confirmDialog({ title: `Paper-trade #${pr.dataset.probe} as a probe?`, confirm: "Start probe",
      body: `<ul class="confirm-list"><li>Allocates ${pct(probe.allocation_pct ?? 0.03, 0, false)} of equity for ${probe.lifetime_days ?? 14} days, with the normal risk checks.</li>
        ${lost ? "<li><b>It lost money on its sealed test</b>, so expect the same unless conditions changed.</li>" : ""}
        <li>A probe collects a forward paper record. It is not evidence of an edge.</li></ul>` }))) return;
    pr.disabled = true;
    try {
      await api(`/api/control/probe_${pr.dataset.probe}`, { method: "POST" });
      toast(`Starting probe for #${pr.dataset.probe}`, { tone: "good", detail: "The worker starts it within ~15 seconds; it trades from the next bar close." });
      if (closeDrawer) closeDrawer();
      setTimeout(refresh, 16000);
    } catch (err) { fail(err); pr.disabled = false; }
    return;
  }
  const rj = e.target.closest("[data-rejudge]");
  if (rj) {
    if (!(await confirmDialog({ title: `Re-judge experiment #${rj.dataset.rejudge}?`, confirm: "Re-judge",
      body: "<p>Only the gate check and the decision run again, under today's thresholds. No new backtest and no new look at the sealed test window.</p>" }))) return;
    rj.disabled = true;
    try {
      await api(`/api/control/rejudge_${rj.dataset.rejudge}`, { method: "POST" });
      toast(`Re-judging experiment #${rj.dataset.rejudge}`, { tone: "good", detail: "Starts within ~15 seconds. You'll get a notification with the decision." });
      if (closeDrawer) closeDrawer();
    } catch (err) { fail(err); rj.disabled = false; }
    return;
  }
  const a = e.target.closest("[data-exp],[data-strat],[data-order],[data-llm]");
  if (!a || a.closest("#tab-body [data-llm]") && !a.matches("a")) return;
  if (a.dataset.exp) { e.preventDefault(); openExperiment(a.dataset.exp).catch(fail); }
  else if (a.dataset.strat) { e.preventDefault(); openStrategy(a.dataset.strat).catch(fail); }
  else if (a.dataset.order) { e.preventDefault(); openOrder(a.dataset.order).catch(fail); }
  else if (a.dataset.llm && a.matches("a")) { e.preventDefault(); openLLM(a.dataset.llm).catch(fail); }
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
  state.journal = rows; state.lastId = rows.length ? rows[0].id : 0; renderFeed(); renderCheckin();
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
    renderCheckin();
    notifyFromJournal(j);
    if (["fill", "decision", "review", "risk", "test"].includes(j.kind)) refresh();
    if (["test", "decision"].includes(j.kind) && state.tab === "research" && !$("#drawer-root").innerHTML) renderTab();
  });
  es.onopen = () => { $("#live-badge").className = "badge ok"; $("#live-badge").innerHTML = '<span class="dot"></span>live'; };
  es.onerror = () => { $("#live-badge").className = "badge bad"; $("#live-badge").innerHTML = '<span class="dot"></span>reconnecting'; };
}

// ---------------------------------------------------------------- ask
const SUGGEST = [["Current research", "What are you researching right now?"], ["Why cash?", "Why are you holding cash?"], ["Latest lesson", "What did your last failed test teach you?"]];
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
  } catch (err) { $("#answer").innerHTML = `<div class="err">${esc(err.message)}</div>`; }
}

// ---------------------------------------------------------------- main loop
// Strips that scroll sideways on phones show a fade on the side that has more items.
function edgeCue(el) {
  if (!el) return;
  const update = () => {
    el.classList.toggle("more-left", el.scrollLeft > 4);
    el.classList.toggle("more-right", el.scrollLeft + el.clientWidth < el.scrollWidth - 4);
  };
  el.addEventListener("scroll", update, { passive: true });
  window.addEventListener("resize", update);
  update();
}
edgeCue($(".sidebar")); edgeCue($("#tabs"));
// Figures inside prose (counts, money, times, percentages) render in the mono face; words stay in the UI face.
// Tokens touching a slash ("24/7") are names, not measurements, and stay in the UI face.
const FIGURE = /(^|[\s(·,:])([−+-]?[#$]?\d[\d,.:]*(?:%| ?bp|[hmsd×])?)(?=$|[\s),·;.:]|,\s)/g;
const SKIP = "code, pre, script, style, svg, input, textarea, select, .num, .badge, .kpi .value, .funnel-n, .entry-title, .verdict, h2, h3, h4";
function monoFigures(root) {
  if (!root || root.nodeType !== 1 || root.closest?.(SKIP)) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, { acceptNode: (n) =>
    !/\d/.test(n.nodeValue) || n.parentElement?.closest(SKIP) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT });
  const nodes = []; while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const n of nodes) {
    const html = esc(n.nodeValue).replace(FIGURE, (m, lead, fig) => `${lead}<span class="num">${fig}</span>`);
    if (html === esc(n.nodeValue)) continue;
    const span = document.createElement("span"); span.innerHTML = html; n.replaceWith(...span.childNodes);
  }
}
new MutationObserver((records) => records.forEach((r) => r.addedNodes.forEach((n) => monoFigures(n.nodeType === 3 ? n.parentElement : n))))
  .observe(document.body, { childList: true, subtree: true });
// #experiment-2, #strategy-1 and #order-5 open that record's drawer.
function openFromHash() {
  const m = /^#(experiment|strategy|order)-(\d+)$/.exec(location.hash);
  if (!m) return;
  ({ experiment: openExperiment, strategy: openStrategy, order: openOrder })[m[1]](m[2]).catch(fail);
}
window.addEventListener("hashchange", openFromHash);
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
  const safe = (fn) => () => fn().catch((err) => { if (err.message !== "login required") fail(err); });
  await Promise.all([safe(loadEquity)(), safe(loadJournal)(), renderTab()]);
  if ($("#app").classList.contains("hidden")) return;
  openFromHash();
  state.timers.push(setInterval(refresh, 10000), setInterval(safe(loadEquity), 60000),
    setInterval(() => { if (["research", "strategies", "orders"].includes(state.tab) && !$("#drawer-root").innerHTML) renderTab({ quiet: true }); }, 30000));
  } finally { state.booting = false; }
}
boot();

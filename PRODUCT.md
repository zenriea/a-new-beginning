# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- **Operator (primary):** the owner who deploys QuantDesk (Railway, Docker or a VPS) and runs it. They watch what the autonomous analyst is doing, review its hypotheses, experiments and trades, start probes, set limits and gates, and use Pause / Emergency stop. They use it on desktop and phone about equally: deep reviews of research records, drawers and limits at a desk, and frequent short check-ins on a phone ("is it trading, why or why not, is anything wrong").
- **Viewers (secondary):** people the operator shows results to. They look at outcomes and the reasoning behind them but never change settings. *Open decision:* the app currently has a single operator password and no read-only viewer role, so viewers today see the operator's screen or screenshots.

## Product Purpose

QuantDesk is an autonomous AI research and paper-trading desk for NASDAQ-listed (Alpaca) US stocks/ETFs and liquid crypto (Coinbase). The AI analyst forms testable hypotheses, backtests them with realistic costs on a sealed chronological test window, stress-tests the results, and allocates **virtual** capital ($100,000 start) only when deterministic gates pass. It then trades through an independent risk engine and reviews live results against expectations.

Success means **finding an edge**: ideas that survive sealed tests and forward paper trading well enough to eventually justify real capital. The dashboard exists so the operator can tell, quickly and honestly, whether that is happening and why.

## Positioning

The AI proposes, but deterministic machinery decides: a frozen spec before the one-time sealed test, cost and robustness challenges, Deflated Sharpe, operator-set gates the agent cannot loosen, and an independent risk engine. Every step is journaled with evidence, and answers to questions cite stored records. The claim is honest, auditable paper evidence, not hype about returns.

## Operating Context

- Research loop: Observe → Investigate → Test → Challenge → Decide → Trade → Review, on 15-minute bars; research every ~2 h per venue; daily reviews.
- NASDAQ regular session with the XNAS calendar (holidays, early closes); crypto 24/7. Free-plan stock data arrives ~16 min late.
- Analyst modes: a configured LLM provider (e.g. Gemini, OpenRouter, Anthropic, Ollama) or the deterministic **DEMO rules engine**. Data modes: live providers or **SYNTHETIC**.
- A safe local preview (`python -m quantdesk.preview`, `Start Local Preview.cmd`) uses a separate database and makes no paid AI calls.
- Live journal over server-sent events; "Ask the desk" Q&A answered only from records.

## Capabilities and Constraints

- **Paper trading only.** No code path sends orders to a broker; the UI must never imply real money is at risk.
- Operator-only actions behind authentication with CSRF: limits and gates (clamped to hard ceilings in `config.py`), probes, re-judge, pause, emergency stop, flatten.
- Default exploratory gates admit paper experiments but **do not establish a statistically validated edge**; strict gates are available. Probes collect forward records and are not evidence of an edge. The UI must keep these distinctions visible.
- Costs are always shown: P&L is after simulated costs; AI spend is shown against a budget.
- Stack: Python API (`quantdesk/api/app.py`) serving a static vanilla HTML/CSS/JS frontend (`web/`), vendored Lightweight Charts, Hanken Grotesk and JetBrains Mono from Google Fonts (visual system in DESIGN.md). Light and dark themes exist, with the preference saved locally.
- Times shown in 24-hour format.
- Terminology: intent vs. fill, sleeve, probe, incubation, sealed test window, gates, venue (NASDAQ / crypto), benchmark (QQQ, BTC-USD).

## Brand Commitments

- Name: **QuantDesk**, subtitle "Paper desk" / "AI research & paper trading". Existing rising-line logo mark.
- Voice: plain, precise and candid. It states what is *not* proven (e.g. "Probes are not evidence of an edge"), labels demo and synthetic modes explicitly, and does not hype.

## Evidence on Hand

- `README.md`: full description of the agent, safeguards, deployment and what was verified (automated test suite; UI checked in Chromium light/dark and at 390 px).
- `docs/workspace-light.png`: current dashboard screenshot.
- No real track record, live-edge results, users, testimonials or performance claims exist. Future work must not invent returns, Sharpe figures or "proven" edges.

## Product Principles

1. **Honesty over optimism.** Show failed gates, costs, drawdowns and "nothing worth testing" as clearly as wins; never present unvalidated results as an edge.
2. **Every number has a why.** Each trade, belief and decision links back to the experiment, rule values or record that produced it.
3. **Status at a glance, depth on demand.** A phone check-in answers "is it trading, why, is anything wrong" within seconds; desktop review reaches the full audit trail.
4. **The operator stays in control.** Limits, gates and kill switches are unambiguous and always reachable; the agent can never loosen them.
5. **Paper is always visible.** Paper, demo and synthetic modes are never hidden or hard to see.

## Accessibility & Inclusion

No product-specific standard has been set. Existing practice to keep: skip link, labelled navigation, light/dark themes, no horizontal scroll at 390 px.

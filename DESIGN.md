---
name: QuantDesk
description: Dark-first working console for an autonomous paper-trading research desk.
colors:
  graphite-ground: "#0d1117"
  panel: "#151a22"
  panel-raised: "#1b212b"
  panel-pressed: "#232a35"
  seam: "#262c36"
  seam-strong: "#353c48"
  ink: "#e6edf3"
  ink-secondary: "#b1bac4"
  ink-muted: "#8b949e"
  keyword-blue: "#79c0ff"
  keyword-blue-bright: "#a5d6ff"
  live-cyan: "#39c5cf"
  string-green: "#56d364"
  string-green-mark: "#3fb950"
  warning-amber: "#e3b341"
  warning-amber-mark: "#d29922"
  destructive-red: "#ff7b72"
  destructive-red-mark: "#f85149"
  on-danger: "#ffffff"
  number-violet: "#c678dd"
  benchmark-grey: "#9aa4b2"
  benchmark-grey-dim: "#6b7686"
typography:
  headline:
    fontFamily: "Hanken Grotesk, ui-sans-serif, system-ui, sans-serif"
    fontSize: "22px"
    fontWeight: 600
    lineHeight: 1.35
    letterSpacing: "-0.015em"
  title:
    fontFamily: "Hanken Grotesk, ui-sans-serif, system-ui, sans-serif"
    fontSize: "18px"
    fontWeight: 650
    lineHeight: 1.3
    letterSpacing: "-0.01em"
  body:
    fontFamily: "Hanken Grotesk, ui-sans-serif, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.55
    letterSpacing: "0em"
  body-dense:
    fontFamily: "Hanken Grotesk, ui-sans-serif, system-ui, sans-serif"
    fontSize: "13.5px"
    fontWeight: 400
    lineHeight: 1.65
  panel-title:
    fontFamily: "Hanken Grotesk, ui-sans-serif, system-ui, sans-serif"
    fontSize: "12.5px"
    fontWeight: 650
    letterSpacing: "0.09em"
  label:
    fontFamily: "Hanken Grotesk, ui-sans-serif, system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 500
    lineHeight: 1.5
    fontFeature: "tnum"
  figure-lg:
    fontFamily: "JetBrains Mono, ui-monospace, SF Mono, Menlo, monospace"
    fontSize: "24px"
    fontWeight: 500
    lineHeight: 1.1
    letterSpacing: "-0.03em"
    fontFeature: "tnum"
  figure:
    fontFamily: "JetBrains Mono, ui-monospace, SF Mono, Menlo, monospace"
    fontSize: "17px"
    fontWeight: 500
    letterSpacing: "-0.02em"
    fontFeature: "tnum"
  figure-sm:
    fontFamily: "JetBrains Mono, ui-monospace, SF Mono, Menlo, monospace"
    fontSize: "12px"
    fontWeight: 400
    letterSpacing: "-0.01em"
    fontFeature: "tnum"
rounded:
  tag: "4px"
  control: "5px"
  panel: "6px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "20px"
  2xl: "24px"
  3xl: "32px"
components:
  button:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "8px 14px"
  button-hover:
    backgroundColor: "{colors.panel-raised}"
  button-primary:
    backgroundColor: "{colors.keyword-blue}"
    textColor: "{colors.graphite-ground}"
    rounded: "{rounded.control}"
    padding: "8px 14px"
  button-primary-hover:
    backgroundColor: "{colors.keyword-blue-bright}"
  button-ghost:
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "8px 14px"
  button-danger:
    textColor: "{colors.destructive-red}"
    rounded: "{rounded.control}"
    padding: "8px 14px"
  button-danger-hover:
    backgroundColor: "{colors.destructive-red-mark}"
    textColor: "{colors.on-danger}"
  button-danger-solid:
    backgroundColor: "{colors.destructive-red-mark}"
    textColor: "{colors.on-danger}"
    rounded: "{rounded.control}"
  tag:
    backgroundColor: "{colors.panel-raised}"
    textColor: "{colors.ink-secondary}"
    typography: "{typography.label}"
    rounded: "{rounded.tag}"
    padding: "4px 10px"
  tag-paper:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    typography: "{typography.figure-sm}"
    rounded: "{rounded.tag}"
    padding: "4px 10px"
  panel:
    backgroundColor: "{colors.panel}"
    rounded: "{rounded.panel}"
    padding: "18px 20px 20px"
  input:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "8px 11px"
  nav-item:
    textColor: "{colors.ink-secondary}"
    rounded: "{rounded.control}"
    padding: "9px 12px"
    height: "40px"
  nav-item-active:
    backgroundColor: "{colors.panel-raised}"
    textColor: "{colors.keyword-blue}"
---

# Design System: QuantDesk

## Overview

**Creative North Star: "The Night-Shift Console"**

QuantDesk is a working console, not a showcase. Graphite panels sit on a near-black ground, divided by 1px hairline seams; there is almost no decoration, and colour is borrowed from code syntax and spent only on state. The operator reads one verdict line, then the live and probe state, then figures that always carry their costs and limits beside them. Density is high and calm: small tracked-capital panel titles, mono figures in fixed columns, tags rather than pills.

The system is dark-first (the house theme is dark by default) with a full "daylight" variant that keeps the same seams, roles and hierarchy on pale grey ground. Depth comes from tonal steps between ground, panel and raised panel, never from resting shadows. The one theatrical gesture is the kill cell: the emergency stop stands alone behind a seam at the very end of the top bar, set apart by empty space, drawn as a dashed red outline that fills only when deliberately hovered or focused. Every other destructive control (Flatten all, Retire) wears the same dashed outline.

It rejects the soft SaaS dashboard: glow backgrounds, gradient tiles, pill-shaped everything and shadowed round cards.

**Key Characteristics:**
- Graphite tonal layering with 1px seams instead of shadows.
- Syntax-derived state colour; neutral ink for everything that is not state, gains and losses included.
- Green only for passed evidence; routine "all is well" is Keyword Blue.
- One reserved cyan for capital that is live right now.
- Hanken Grotesk for words, JetBrains Mono for figures, even figures inside sentences.
- Tight 4/5/6px corners: tags, controls, panels.
- Destructive controls outlined and dashed until intent is shown.

## Colors

A graphite neutral ladder carries the page; saturated syntax hues appear only where something has a state.

### Primary
- **Keyword Blue** (keyword-blue): actions, links, focus rings, active nav and tab, and every routine "fine" signal: the healthy-desk indicator, healthy status-tag dots, the environment dot, the agent's "working" pulse, success toasts, hypothesis, research, decision and review entries in the journal, buy markers on price charts, and the sealed test segment of the train/validation/test bar. **Keyword Blue Bright** (keyword-blue-bright) is its hover step on primary buttons.

### Secondary
- **Live Cyan** (live-cyan): capital that is live right now, and nothing else. It draws the account equity line (with a faint area fill) only while positions are open, the "Live" funnel cell, and the live count in the rail. A 13% tint (live-soft) backs the live funnel cell.
- **Number Violet** (number-violet): probes. Probe chips, the probe verdict, probe counts and test entries in the journal. Probes are activity, never success, so they never borrow green.

### Tertiary (state)
- **String Green** (string-green, mark string-green-mark): passed evidence and nothing else. The passed-gates count and a ledger that reconciles (the one green text style), passed gate marks, passed and active status tags, strategy chips, and the check-in verdict when a strategy that passed every gate is live.
- **Warning Amber** (warning-amber, mark warning-amber-mark): demo and synthetic modes, running or pending work, limit changes that loosen, honesty caveats.
- **Destructive Red** (destructive-red, mark destructive-red-mark): failed gates, risk entries, errors, halted desk, and destructive controls. Each state hue has a text value and a brighter or deeper "mark" value for dots, bars and fills, plus a 9 to 14% tint for backgrounds. **On Danger** (on-danger) is the text and icon colour on a filled red surface (hovered danger button, confirm button, critical alert icon) in both themes.
- **Benchmarks:** muted greys, never a hue. Benchmark Grey (benchmark-grey) draws the equity benchmark (QQQ by default) dashed, and sell markers on price charts; Benchmark Grey Dim (benchmark-grey-dim) draws the BTC-USD benchmark dotted, and the dots of intent, order and fill entries in the journal.

### Neutral
- **Graphite Ground** (graphite-ground): page, rail and top bar background.
- **Panel** (panel): cards, inputs, drawers, dialogs.
- **Panel Raised** (panel-raised): hover fills, tags, segmented-control wells, detail rows.
- **Panel Pressed** (panel-pressed): active button, empty metric tracks, the train segment.
- **Seam / Seam Strong** (seam, seam-strong): every divider and panel edge; strong seams for control borders, table header rules and the kill-cell divider.
- **Ink / Ink Secondary / Ink Muted** (ink, ink-secondary, ink-muted): primary text and the non-live account line; secondary copy and panel titles; metadata, labels and empty states.

The daylight theme redefines the same token names (ground #f4f5f7, panel #ffffff, seam #d6dae0, ink #11151b, keyword blue #1f5fd1, live #0b7a84, benchmarks #5c6573 and #8a929e); the full map lives in the sidecar.

### Named Rules
**The Reserved Ink Rule.** Live Cyan marks capital that is live right now and nothing else. If no sleeve is live, no cyan appears on the screen.

**The State-Only Colour Rule.** Direction is carried by sign and drawn SVG marks, not colour: positive and negative returns and P&L both render in Ink. Hue is spent on state (passed, probe, warning, failed, live), never on decoration.

**The Green Is Evidence Rule.** Green means a gate was passed, nothing weaker. Healthy services, recorded decisions and successful saves are Keyword Blue; the PAPER tag is neutral ink.

**The Probe Is Not Proof Rule.** Probes are violet everywhere they appear. They never take green.

## Typography

**Body Font:** Hanken Grotesk (with ui-sans-serif, system-ui)
**Label/Mono Font:** JetBrains Mono (with ui-monospace, SF Mono, Menlo)

Both load from Google Fonts as variable weight ranges (400 to 700), so the 550 and 650 steps used throughout are real weights, not synthesised.

**Character:** A clear, slightly warm grotesk for prose and controls, paired with a developer mono that carries every number, timestamp, unit and code-like tag. The pairing reads as a terminal that learned typesetting.

### Hierarchy
- **Headline** (600, 22px, 1.35): the verdict line in the check-in band; 17px on phones. The largest text on the desk.
- **Title** (650, 18px): drawer and dialog headings; 20px for the agent's current activity.
- **Body** (400, 14px, 1.55): default copy. Dense panels run at 13.5px with 1.65 to 1.7 leading; prose blocks cap at 75ch.
- **Panel title** (650, 12.5px, 0.09em tracking, uppercase, Ink Secondary): every panel header. Table headers, section headings in drawers and rail group labels use the same small tracked capitals at 12px.
- **Label** (500, 12px, tabular numerals): metadata, hints, tag and status text (550 in tags). 12px is the floor; nothing is set smaller.
- **Figures** (JetBrains Mono, 500, tabular numerals, negative tracking): 24px funnel counts, 19 to 22px account values, 17px compact and summary stats, 12 to 13.5px inline figures and units.

### Named Rules
**The Every Figure Is Mono Rule.** Every number, money value, percentage, timestamp, count and unit is set in JetBrains Mono with tabular numerals, so columns never move and values change in place. Elements that hold only figures (values, table cells, axes, timestamps, the chart readout) are mono whole; inside prose, a render-time pass wraps each figure in mono and leaves the words in Hanken Grotesk.

**The Names Stay In Words Rule.** Figures that are part of a name stay in the UI face: tokens joined by a slash ("24/7") and text inside status and mode tags, which keep Hanken Grotesk with tabular numerals. The PAPER tag is the one mono tag.

## Layout

A fixed 232px left rail, a sticky 64px top bar (desk health at the left; then, at the right, the theme toggle, Pause, and the kill cell, which always ends the bar), a status strip of mode tags, then a two-column grid: a fluid main column and a 380px journal column (340px between 1261 and 1480px) that sticks below the top bar at full viewport height. Content caps at 1920px. Gutters are 32px on desktop and 16px on phones; columns and stacked panels are separated by 24px (16px on phones).

Spacing steps on 4px: 4, 8, 12, 16, 20, 24, 32. Panel headers are 48px tall with 20px side padding; panel bodies pad 18px 20px 20px.

Responsive behaviour: at 1260px the journal drops below the main column. At 900px the rail becomes a horizontally scrolling strip with fade masks on the side that has more. At 640px the top bar holds desk health, theme, Pause and Stop on one row with 44px targets, quiet status tags collapse behind a "+N status" tag, the account strip goes 2 by 2, the funnel wraps 3 over 2 with seams between every cell and its chevrons hidden, and the research table becomes stacked rows. Container queries on the main column (760px, 680px) reflow the desk pair, pipeline grid and account strip independently of the viewport.

## Elevation & Depth

Flat by default. Resting surfaces have no shadow (the shadow token is `none`); depth is tonal: ground, then panel, then raised panel, each bounded by a 1px seam. Active nav items and selected segments use an inset 1px seam ring rather than a lift. Shadow appears only on surfaces that float above the page; the sign-in card is an ordinary flat panel.

### Shadow Vocabulary
- **Overlay** (`box-shadow: 0 16px 48px rgba(0,0,0,.55)`; daylight `rgba(17,21,27,.18)`): drawer, confirmation dialog, toasts. Behind modal surfaces the page dims with a near-black scrim (62%).
- **Focus halo** (`box-shadow: 0 0 0 4px` blue tint at 12%): text inputs and unit inputs on focus, alongside a Keyword Blue border.

### Named Rules
**The Seams Not Shadows Rule.** A panel is separated from the page by a 1px seam and one tonal step. If a resting surface needs a shadow to be seen, the tonal step is wrong.

## Shapes

Three tight radii and nothing rounder: 4px for tags, statuses, counts and filter chips; 5px for buttons, inputs, nav items and small wells; 6px for panels, alerts, dialogs and the funnel. Focus outlines are 2px Keyword Blue, offset 2px, at 4px radius. Only indicator dots are circles. Strips of related cells (the account strip, the funnel, the timeline bar) share one outer border and divide internally by seams, clipped to the outer radius. Dashed strokes are a meaning, not a decoration: destructive controls, intents (not yet fills) in the journal, the dashed and dotted benchmark lines, and empty-state frames.

## Components

### Buttons
Quiet, rectangular, deliberate.
- **Shape:** tight corners (5px), 1px Seam Strong border, 8px 14px padding, 13px at 550 weight.
- **Default:** Panel fill, Ink text; hover steps to Panel Raised with a muted border; active to Panel Pressed.
- **Primary:** Keyword Blue fill with Graphite Ground text; hover to Keyword Blue Bright. Used for the one forward action in a form or dialog.
- **Ghost:** no border or fill until hover (theme toggle, sign out).
- **Danger:** transparent with a dashed red border and Destructive Red text; on hover or keyboard focus it fills solid red with On Danger text and the border turns solid. Used for Emergency stop, Flatten all and Retire (small size, label ends in an ellipsis because it opens a confirmation).
- **Danger solid:** filled red, only as the confirm button inside a destructive dialog.
- **Small:** 5px 10px, 12.5px.

### Tags
Tags, not pills.
- **Style:** 4px radius, Panel Raised fill, 1px seam, 12px Hanken Grotesk at 550 with tabular numerals, a 6px status dot where state applies (Keyword Blue when healthy). Status tags take a state tint and state text: green for passed and active, amber for running and submitted, red for rejected, errored and retired (retired is also struck through); failed experiments stay neutral with only a red dot. The PAPER tag is outlined in Ink Muted on Panel, set in mono capitals at 600 in Ink, and always shown first.
- **Gate chips:** failed gates are outlined in a red seam, not filled.
- **Journal kind tags:** uppercase mono at 0.04em, tinted from the entry's state hue; intents are dashed and unfilled.
- **Filter chips:** outlined; the selected chip takes a blue tint and blue text.

### Cards / Containers
- **Corner Style:** 6px.
- **Background:** Panel on Graphite Ground.
- **Shadow Strategy:** none; see Elevation & Depth.
- **Border:** 1px Seam; headers divide from bodies by a seam.
- **Internal Padding:** 18px 20px 20px; 14px 16px 18px on phones.

### Inputs / Fields
- **Style:** Panel fill, 1px Seam Strong border, 5px radius; limit fields set values in mono and attach units in a seamed suffix or prefix cell.
- **Focus:** border turns Keyword Blue with a 4px blue tint halo.
- **States:** a changed value turns the border and hint blue; a loosening change turns them amber; an invalid value turns the border red with a 12px red error line.

### Navigation
- **Rail:** 40px items, 13.5px at 500, Ink Secondary with 18px stroked icons; hover fills Panel Raised; the active item takes Keyword Blue text, a Panel Raised fill and an inset seam ring. Counts sit right-aligned in 4px mono tags, cyan when live and violet when probing.
- **Tabs and range controls:** a seamed well of Panel Raised; the active segment lifts to Panel with Keyword Blue text and an inset Seam Strong ring.
- **Mobile:** the rail becomes a single scrolling row without labels or counts.

### Check-in band (Signature)
The first answer on every screen: one verdict line at headline size beside "Latest" and "Next" rows, inside a 6px panel tinted 8% toward its state hue (blue working, green clear, amber warning, red halted, violet probing) with a matching state seam.

### Kill cell (Signature)
The emergency stop lives alone at the far right of the top bar, after the theme toggle and Pause, behind a 1px Seam Strong divider with 16px of empty space on each side, using the dashed danger button at 650 weight. Clear emergency stop replaces it in place when the desk is stopped. Every destructive action opens a confirmation dialog that states exactly what will and will not happen.

### Funnel (Signature)
The pipeline's five stages as one seam-divided strip: a single 6px outer border, 1px seams between cells, and a small chevron (a 7px rotated square with two Seam Strong edges) sitting on each seam to show flow. Each cell holds a 24px mono count (Ink Muted at zero, Ink once it has items), a 12.5px stage label and a 12px note. The last cell, Live, takes the live-soft tint and a Live Cyan count.

### Equity chart
The account line is Ink at 2.5px and the only series with a last-value label; it turns Live Cyan, with a faint cyan area, only while positions are open. Benchmarks are 1.5px muted greys, QQQ dashed and BTC-USD dotted, with no last-value labels. Axes and the crosshair readout are mono at 12px; only dashed horizontal seam gridlines; ticks read 24-hour times, a short date ("Nov 19") where the day changes. The library logo is off; the legend ends with a muted credit link to TradingView Lightweight Charts.

### Journal entry
A 1px seam timeline down the left with a 9px state dot per entry (blue for hypothesis, research, decision and review; violet for tests; dim grey for intents, orders and fills; red for risk; amber for data); meta row with a mono kind tag, author and mono time, a 14px title at 600, and secondary body text that clamps with a fade and expands in place.

## Do's and Don'ts

### Do:
- **Do** separate surfaces with a 1px seam and one tonal step (graphite-ground, panel, panel-raised).
- **Do** set every figure, unit, timestamp and count in JetBrains Mono with tabular numerals.
- **Do** keep Live Cyan for capital that is live right now; draw the account line in Ink when nothing is live.
- **Do** colour probes violet everywhere and show costs and limits beside the numbers they qualify.
- **Do** draw destructive controls (Emergency stop, Flatten all, Retire) as dashed red outlines that fill only on hover or focus, isolated from neighbouring controls, and confirm them in a dialog.
- **Do** use Keyword Blue for routine health, recorded decisions and successful actions.
- **Do** draw benchmarks as muted grey lines, dashed for the equity benchmark and dotted for BTC, without last-value labels.
- **Do** keep radii at 4px (tags), 5px (controls) and 6px (panels).
- **Do** keep the PAPER tag visible on every screen in both themes.
- **Do** cancel or strike through retired and stopped items rather than removing them.

### Don't:
- **Don't** use glow backgrounds, gradient tiles, pill shapes or shadowed round cards.
- **Don't** put a resting shadow on a panel; overlays (drawer, dialog, toast) are the only lifted surfaces.
- **Don't** colour gains and losses green and red; sign and drawn marks carry direction.
- **Don't** use green for probes, routine status, journal decisions, success toasts, or anything not affirmed by a passed gate.
- **Don't** wrap figures that are part of a name ("24/7") or tag text in the mono face.
- **Don't** set any text below 12px.
- **Don't** use a radius above 6px or a fully rounded shape for anything except indicator dots.

---
version: 1
slug: "web-index-html"
primary_target: "web/index.html"
related_targets: []
---

# Surface: operator dashboard (web/index.html)

Mode: Operate. Audience: the self-hosting operator (desktop reviews and frequent phone check-ins); viewers shown results second. Job: know in seconds whether the desk is trading, why, and whether anything is wrong; then audit records and adjust limits safely. Constraints: paper/demo/synthetic always visible; costs always shown; no invented performance claims; standard web controls only.

## Direction contract

THESIS: A dark-first working console for an autonomous research desk: graphite panels divided by hairline seams, syntax-derived colour used only for state, and the kill switch standing alone. It refuses the soft SaaS dashboard (glow backgrounds, gradient tiles, pill everything, shadowed round cards).

OWN-WORLD: Ground #0d1117, panels #151a22, elevated #1b212b, seams #262c36, text #e6edf3, muted #8b949e. State accents from syntax: keyword blue for actions and links, string green for passed evidence only, number violet for probes, warning amber, destructive red; one reserved cyan ink marks capital that is live right now and nothing else. Hanken Grotesk for UI text, JetBrains Mono for every figure, panel titles in small tracked capitals. 4–6px radii, 1px seams instead of shadows, tags not pills. A light "daylight" variant keeps the same seams and roles.

STORY: The operator opens the desk, reads one verdict line and the live/probe state, sees costs and limits beside every number, drills into any record for the full audit trail, and changes limits or stops trading through deliberate, isolated controls.

FIRST VIEWPORT: Standard left rail (brand, workspace and operations nav) and top bar (desk health, Pause, then the isolated kill cell holding Emergency stop). Below: mode tags, then the verdict panel spanning the main column (verdict sentence, latest event, next action), the compact account strip, then the pipeline panel; the journal panel runs the right column. Signature move: the kill cell, a seam-bounded cell set apart by empty space whose dashed red outline fills only on hover/focus, the same treatment for Flatten all and Retire.

FORM: Catalog challenger "dark-first developer console" (digital-design-canon-dark-first-developer-console), chosen by the user over assigned grounded candidate 7 (technical drawing sheet). seed:a7ec68ebbe2cfc95. Raises kept from declined challengers: one reserved ink for live capital (orienteering map); nothing disappears, it cancels (ticket wallet); one fixed scale across train/validation/test/live (botanical folio); columns never move, values change in place (split-flap board).

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

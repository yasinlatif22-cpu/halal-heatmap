# Mirsad visual identity

Mirsad is an auditable equity screening research product. It should feel suitable for a financial analyst: clear at a glance, detailed on inspection, and explicit about uncertainty. It is an automated AAOIFI-based screen, not a fatwa, certification or investment recommendation.

## Direction

Full visual overhaul, preserving the static HTML/CSS/JavaScript stack, existing URLs, data contracts and screening rules. A financial research aesthetic implemented in native CSS, without claiming to implement a third-party design system.

Design variance 4, motion intensity 2, visual density 7 in the research workspace and 4 on the landing page. Restrained transitions support navigation; no decorative animation or gradients.

## Identity and palette

The name remains Mirsad. A thin teal square encloses a typographic M. The wordmark uses a modern system sans. Equity research identifies the product category, not a claim of professional endorsement.

Graphite canvas #101820; panel #16212b; lifted surface #1d2a35; primary ink #edf1f3; secondary ink #a7b6c2; subtle ink #95a4b1; accent #78c9b0. Semantic chart fills: pass #245f52, needs review #715d36, fail #6d3e47, insufficient data #35424f. Status is also communicated through words, glyphs and hatching.

## Typography and geometry

System sans for navigation, headings and explanatory content. Self-hosted Mirsad Mono for numerical records, tickers and financial values. Tabular figures align data. Sentence case is the default; small uppercase labels identify metadata sparingly. Desktop heading 32px in the workspace and larger editorial type on the landing page. Small text uses adequate contrast; no condensed font in the active interface.

Use 4px spacing increments, 32px desktop gutters and 20px mobile gutters. Controls use 4px corners; grouped status metrics use 6px corners. Tables and charts remain square. Avoid repetitive cards and ornamental shadows; a shadow distinguishes the research overlay from the map.

## Workspaces

Market map: sector-grouped treemap sized by stored spot market capitalization and coloured by exported status. Status summary is also a filter. Every number comes from the exported records.

Screener: full-width sortable table with company, market cap, status, near-limit flag, interest-income basis and last-close price change. Download and watchlist controls remain functional.

Changes: full-width review of valid stored status changes and index events.

Company research: focused right panel, separate from filters and list. Verdict, reason and key facts precede ratio values and headroom. Ratio bars visualize stored ratios against stored limits; they never decide a verdict. Filing evidence, input provenance, overrides and XBRL facts remain accessible.

Filters: dedicated narrow panel. Search always remains available. Navigation indicates the active workspace. Closing hidden content returns keyboard focus to a visible control.

## Responsive and state requirements

The navigation wraps on smaller screens. Status metrics become a two-column grid. Research panels occupy the full viewport width on phones; wide tables scroll locally rather than widening the page. Touch targets are at least 44px. Reduced motion disables transitions. Errors explain the failed operation and provide a reload action. Empty states distinguish no matching records from failed data loading. Stale exported data is always labelled.

## Verification

Check desktop and mobile screenshots, keyboard search and filters, detail records, watchlist, CSV and links. Preserve contrast tests and screening tests. Update assertions tied to the retired terminal design rather than weakening behavioral checks. Never imply live market data or historical backtest validity when the underlying export does not support it.

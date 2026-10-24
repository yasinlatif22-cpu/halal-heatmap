# Equity Screener

Configurable fundamental screens and a heatmap of S&P 500 companies, built from SEC filings with an audit trail.
See README.md for what it does and how to run it. This file is the short working guide for contributors and coding agents.

## Working rules

- Every threshold and rule parameter lives in `config.yaml` or a profile in `profiles/`. Nothing in `src/` hardcodes one.
- A missing required input is unavailable or `insufficient_data`. Never default to pass, and never estimate a figure.
- Metrics are ratios of reported figures; a metric is `None` when an input is missing or a denominator is not positive.
- Criteria are evaluated by `criteria.py` and `web/tool/criteria.js`; both must agree on `tests/criteria_cases.json`.
- Point in time: read only filings made on or before the screen date, and never the screen date's own price.
- The config hash is the methodology version. `fundamentals:` and fetch settings are outside it; anything else is methodology.
- SQLite rows are append-only. A bad run is marked superseded, not deleted.
- EDGAR requests send the User-Agent from `SEC_USER_AGENT` (hard error if unset) and stay under 10 requests per second.
- All network access is in `src/equity_screener/sources/`. Everything else is pure and tested offline.

## Commands

```bash
pip install -e ".[dev]" && pytest -q && ruff check src tests scripts
python scripts/make_demo_data.py --out web/data    # synthetic demo export, no network
```

## History

The project began as a single fixed screen. Its original spec is in `docs/reference-screen-spec.md`, the design record in
`docs/design-record.md`, and the original README in `docs/reference-screen.md`.

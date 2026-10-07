# Mirsad

The product is named Mirsad in user-facing text. The repo slug, the Python package, the CLI, the data branch, the workflow
names, SEC_USER_AGENT and the watchlist storage key are deliberately still halal-heatmap, to be renamed later in a separate step.

A static web tool: a treemap of S&P 500 stocks coloured by the result of an AAOIFI-based Shariah
screen, with status changes flagged, updated automatically. This file is the working spec and the
record of decisions. It is an automated screen, not a fatwa and not investment advice.

## Status

- Phase 1 (screening engine + unit tests): built, under review.
- Phase 2 (change detection + point-in-time): built, under review.
- Phase 3 (heatmap UI): built, under review.
- Phase 4 (automation): not started.
  Stop after each phase for review. Do not start a phase without being asked.

## Working rules

- Every threshold and rule parameter lives in `config.yaml`. Nothing in `src/` hardcodes one;
  `tests/test_config.py` enforces this.
- A missing required input means `insufficient_data`. Never default to pass.
- Borderline business cases go to `needs_review`, never auto-pass.
- A false pass matters more than a false fail. When in doubt, choose the conservative reading.
- Every verdict must be auditable: ticker, screen date, filing used, each input, each ratio,
  status, reason, and every XBRL fact used are stored.
- EDGAR requests send the User-Agent from the `SEC_USER_AGENT` environment variable (hard error
  if unset, never hardcoded) and stay under 10 requests per second (configured at 8).
- SQLite rows are append-only. A bad run is marked superseded, not deleted.
- Rules added as safety checks may only turn a pass into `insufficient_data`; they never move a
  fail or a needs_review. Tests prove this for each one.
- Screening functions are pure. All network access is in `src/halal_heatmap/sources/`.
- A screen for a date reads only filings filed on or before it and closes of trading days before
  it. The screen date's own price is never read: it may be an unfinished trading day.
- Stored history only moves forward: a date before the latest stored screen is refused.
  `--no-store` looks at a past date without storing it, but is not a valid historical backtest:
  the constituent list, SIC codes and GICS sub-industries are today's values, not that date's.
- A screen identical to the one already stored for its date adds no row; a run that adds no row
  is not recorded.

## Commands

```
.venv/bin/pytest -q                                   # offline unit tests
.venv/bin/ruff check src tests
.venv/bin/halal-heatmap screen AAPL MSFT --facts      # audit records for some tickers
.venv/bin/halal-heatmap screen --quiet                # full S&P 500 into data/screens.db
.venv/bin/halal-heatmap update                        # what automation runs: screen whatever is due
.venv/bin/halal-heatmap changes [TICKER] [--since D]  # stored status changes and index events
.venv/bin/halal-heatmap supersede-run 4 --reason "…"  # mark a stored run invalid, re-measure changes
.venv/bin/halal-heatmap export                        # write web/data/*.json from the latest valid screens
python3 -m http.server 8000 --directory web           # then open http://localhost:8000
```

Python 3.10 in a plain `.venv` (no uv on this machine). `data/*.db` and `.cache/` are not tracked.

## Statuses and precedence

`pass`, `fail`, `needs_review`, `insufficient_data`, decided in this order:

1. Business screen fail -> `fail`
2. Any required input missing -> `insufficient_data`
3. Any financial ratio fails -> `fail`
4. Business screen needs_review -> `needs_review` (an active override may resolve it)
5. Otherwise `pass`
6. Pass-only checks, each of which can turn a pass into `insufficient_data`:
   total-assets bound, debt plausibility, out-of-date balance sheet (see Newer filings below).

## Screens

Financial (AAOIFI Shari'ah Standard No. 21, to be verified by maintainer; see the table below):

| Ratio | Limit | Operator |
|---|---|---|
| interest-bearing debt / market cap | 30% | strict `<` |
| cash + interest-bearing securities / market cap | 30% | strict `<` |
| impure income / revenue | 5% | strict `<` |

The operator is configurable per screen. Impure income is interest income only.

Market cap denominator: the 12-month average drives the verdict. Spot and the 36-month average
are always computed and stored with their own would-be verdicts. `denominator_disagreement`
marks any name whose three verdicts differ. Headroom to each limit is stored, and a passing
ratio within 10% of its limit (relative) is flagged `near_threshold`. The receivables /
illiquid-asset screen is intentionally omitted.

Business: fail for alcohol, tobacco, gambling, conventional banks and insurers, weapons makers
(SIC 348x, 3795); needs_review for the borderline groups in `config.yaml`, matched on GICS
sub-industry or EDGAR SIC code. Aerospace & Defense stays needs_review. Two captive-finance
rules send an otherwise passing business to needs_review: four manufacturer sub-industries
(autos, motorcycles, farm machinery, construction machinery and heavy trucks), and any company
whose financing receivables exceed 5% of total assets.

## Data

- Constituents: Wikipedia S&P 500 list. Prices: yfinance behind a `PriceSource` interface.
- Financials: SEC EDGAR companyfacts, point-in-time (`filed <= screen date`). Trailing 12
  months = fiscal year + year to date - prior year to date.
- The filing's own XBRL instance is read when companyfacts falls short: it has company-specific
  tags and facts reported with dimensions, which companyfacts drops. A 10-Q or 10-K that EDGAR lists
  but companyfacts does not serve yet is read the same way for its balance sheet (see Newer filings).
- `predecessors` in `config.yaml` maps a successor CIK to the CIKs the same company filed under
  before a holding-company reorganisation (Exxon, Bunge, Ferguson). A merger is not a
  reorganisation: Paramount Skydance is deliberately not linked to Paramount Global.

## Decisions, in the order they were made

Inputs
- Numerators (debt, cash and securities, interest income, interest expense) use the alternative
  tag set with the largest total, so overlaps overstate rather than understate.
- Missing debt counts as 0 only when total liabilities is reported and interest expense is
  absent or below 0.1% of revenue. Otherwise `insufficient_data`.
- Debt plausibility: interest expense above 25% of tagged debt means debt looks understated.
  Pass-only.
- Investment tags absent: treated as 0, recorded ("investment tags absent, treated as 0") and
  flagged `investments_assumed_zero`. Not made a required input.

Overlaps between tags (debt and cash), config `inputs.<name>.within` and `prefer`
- A tag that is part of a total reported with it is left out of the sum, so nothing is counted
  twice. `within` rules name the container (`inside`) and the contained tags (`contains`). A
  contained tag is left out only when its container is at least as large at the same period. If
  the container is smaller, the contained tag cannot be part of it: both are kept and the audit
  record says so (`debt_kept`).
- Debt rules, each checked against a filing: `DebtCurrent` is inside `LongTermDebt` and inside
  `DebtAndCapitalLeaseObligations` (both include current maturities; NVDA, UPS). Finance leases,
  current and noncurrent, are inside `DebtAndCapitalLeaseObligations` and
  `LongTermDebtAndCapitalLeaseObligations`. `CommercialPaperNoncurrent` is inside
  `LongTermDebtAndCapitalLeaseObligations` (MAR). Checked for UPS and MAR only; the others rest on
  the tag labels and need review.
- A `prefer` rule is not a containment rule. The broad available-for-sale debt total can include
  cash equivalents already in the cash line, but it also covers lines the balance-sheet line does
  not. So a `prefer` rule drops the broad total only when its excess over the balance-sheet line
  (`DebtSecuritiesCurrent` or `ShortTermInvestments`) fits inside the cash line named in
  `covered_by`. A larger excess keeps both, as before (Phase 1 test).
- Cash: `LongTermInvestments` may include equity investments, which are not interest-bearing. It is
  counted as reported and the audit record notes it. Not excluded without a tag that separates it.

Newer filings
- EDGAR lists a 10-Q or 10-K before companyfacts serves its figures. A periodic filing from the last
  3 days that companyfacts lacks is noted (`filing_lag`). Older ones that companyfacts still lacks
  are now read from their XBRL instance (`instance_lookback_days`, 400), so the balance sheet can
  come from the newest filing. Before this, the screen silently used the older balance sheet.
- A newer periodic report, for a later period than the balance sheet in use, whose balance sheet
  is not usable is `insufficient_data`, with its accession, period and filing date. An older
  balance sheet may stand in only when it is no older than `filings.max_fallback_age_days` at the
  screen date. Default 0: never.
- Staleness is relative to the company, not a flat age. The next report's expected period is the
  period the company reported one year earlier, followed by the one after it, moved forward a
  year. It is due after that company's longest observed filing lag, plus `overdue_grace_days`
  (45). If nothing is listed by then, the balance sheet is `insufficient_data`. A company with no
  such history uses `default_cadence_days` (91) after its balance sheet. This replaced the flat
  200-day rule.

Interest income, tried in this order, with the basis recorded on every result
1. Gross tags over trailing 12 months from companyfacts. `InterestIncomeOperating` and
   `InvestmentIncomeNonoperating` are accepted as gross. Net "other income" lines are never used.
2. The same from the filing's own XBRL, including three company-specific tag names.
3. Annual fallback: the latest annual figure, at most 450 days old at the screen date, flagged.
4. Sums over dimension members, only on listed axes and with at least two members; supports a
   pass only below 50% of the limit.
5. `InvestmentIncomeNet`, basis `net_investment_income`; supports a pass only below 50% of the
   limit, otherwise `insufficient_data` (even above 5%). A negative figure is `insufficient_data`.
6. Nothing disclosed: basis `upper_bound_no_disclosure`. Bound = (cash + interest-bearing
   securities) x a 5% yield ceiling. If bound / revenue clears the limit it can support a pass;
   otherwise `insufficient_data`, never a fail. When investments are also assumed zero, a pass
   must instead clear total assets x 5% / revenue. Must be clearly labelled in the UI.

Finding: most companies do not tag interest income anywhere, so dimensional parsing recovered
only a handful of names. The SEC Financial Statement Data Sets are built from the same XBRL and
are not expected to add anything (not verified by download).

Market cap and shares
- Share counts come from the filing cover page, stepped by filing date and adjusted for splits.
- Multi-class companies: per-class counts from the cover page, each listed class priced at its
  own symbol, unlisted classes priced at the largest listed class. Tickers of one company
  (GOOGL/GOOG, FOX/FOXA, NWS/NWSA) share one company-level market cap and verdict.
- Per-ticker exception list for unlisted classes with no economic rights: Ares.
- Share-count sanity check: a count more than 5x from every neighbouring filing after split
  adjustment is rejected and the nearest valid count applies; no valid count means
  `insufficient_data`. A change above 1.5x between consecutive counts is flagged for review
  (Honeywell) and changes no verdict.

Out-of-date balance sheet (pass-only)
- An 8-K reporting a spin-off or major disposition, filed after the latest balance sheet date,
  turns a pass into `insufficient_data` until the next 10-Q or 10-K (Corteva).
- The spot divergence flag is set when spot and the 12-month average market cap differ by more
  than 1.5x. When it is set and the spot verdict is not a pass, a pass becomes `insufficient_data`.

History and change detection (`runner.py`, `changes.py`)
- `update` screens every constituent when the monthly screen is due: no valid full run on or
  after the latest monthly date (day 1), so a missed day is made up by the next run. Otherwise it
  screens only the companies that are due: first screen, config hash changed, an earlier fetch
  failed (retry), a new periodic filing, a new 8-K carrying an events item, or an override that
  now applies or has expired.
- New filings are found in the EDGAR submissions list by comparing it with the watermark stored
  on the last valid result: the latest filing date read and every accession of that date.
- EDGAR lists a filing before companyfacts serves its figures. A periodic filing from the last
  3 days that companyfacts does not hold is noted (`filing_lag`) and left out of the watermark,
  so the company stays due until the figures arrive.
- A status change is measured against the previous valid result of the same ticker and stored in
  `status_changes` with old and new status and reason, each ratio that crossed its limit (before
  and after: ratio, limit, operator, numerator, denominator), the cause, and every other factor
  that differed. Superseding a run hides the changes measured from or to it and measures the
  next valid result of each of its tickers again.
- The config hash is the methodology version. It covers everything in `config.yaml` that can
  change a verdict (thresholds and operators, market cap settings, tag lists, business rules,
  override expiry, events, filing and period rules, predecessors) and leaves out `edgar`,
  `schedule`, `constituents`, `filings.companyfacts_lag_days` and `near_threshold`, which only
  sets a flag. A changed margin re-screens nothing: each flag is refreshed the next time its
  company is screened. A new key is hashed unless it is added to `OPERATIONAL_KEYS` in
  `config.py`; a test fails until a new section is classified.
- Cause, first that applies: `methodology_change` (config hash), a data source failing or
  recovering (`other`), `override_added` / `override_expired` / `override_removed`, `event_8k`,
  `new_filing` (reported figures changed), `price_move` (a market cap ratio crossed, a market cap
  appeared or vanished, or spot diverged), `other`. `price_move` comes before `new_filing` when
  the market cap alone carries every crossed ratio over its limit.
- Index additions and removals are stored in `index_events`, from the difference between
  consecutive constituent lists. A company that joins, or rejoins, gets no status change.

Overrides
- `overrides.yaml`, in git. An override resolves needs_review only, must carry reason, reviewer
  and date, and expires after 365 days.
- Tesla: pass, "financing assets 0.16% of total assets; interest income 1.68% of revenue",
  maintainer, 2026-10-05.

Process
- Re-screen on a new 10-Q/10-K and on a fixed monthly date. History is forward-only from launch.
- Treemap shows all constituents coloured by status, sized by spot market cap, with a toggle to
  daily price change.
- The repository may be public.

Site (phase 3)
- The page computes no verdicts. `export` reads only the latest valid result of each ticker and
  writes `screens.json`, `changes.json` and `meta.json` to `web/data/` (not tracked).
- Near-threshold flags are recomputed at export under the current margin, with the same rule the
  screen uses (`near_threshold_flag`). They are set only on pass and needs_review.
- Daily price change is the derived percent change between the two closes before the screen date
  (the screen date's own close is never read). Only the change and its date are exported.
  `--no-prices` skips it.
- Lower confidence means no interest income was disclosed: the two upper-bound bases. Net
  investment income, annual figures and partial sums are labelled, not marked lower confidence.
- Reviewers appear as "Reviewer N", and their names are scrubbed from every text field.
- A status-change side panel and the click-through audit record read `changes.json` and
  `screens.json`. Changes measured from superseded runs are not exported.

## Launch checklist (start of Phase 4)

The database built during development holds several valid runs for the same dates under
different config hashes. History must start from one clean run.

0. The real database must be re-screened with these fixes before launch. Its latest run (id 7)
   predates the overlap, newer-filing and due-date rules, and the config hash has changed since it
   was written. Do not carry its rows into the launch history.
1. Archive the current database as a backup: `mv data/screens.db data/screens.pre-launch.bak.db`.
   `*.db` is in `.gitignore`; check `git status` shows nothing new.
2. Confirm `config.yaml` and `overrides.yaml` are the launch versions and committed.
3. Run `.venv/bin/halal-heatmap update` once. With no database it creates a fresh one and
   screens every constituent: this is the first row of history for each ticker.
4. Check the run: one row in `runs` with scope `full`, about 500 results, no `source:` errors,
   `halal-heatmap changes` shows 0 status changes and 0 index events.
5. Run `update` again and confirm "nothing due" and no new rows. Only then schedule it.

## To verify against the current AAOIFI standard (maintainer)

| # | Item | Value | Check |
|---|---|---|---|
| 1 | Debt / market cap | 30% | value, and that the denominator is market cap |
| 2 | Cash + interest-bearing securities / market cap | 30% | value, and what the numerator covers |
| 3 | Impure income / revenue | 5% | value, and revenue vs total income |
| 4 | Operator | strict `<` | the standard may say "does not exceed" (`<=`) |
| 5 | Market cap averaging | 12-month | whether the standard names a period |
| 6 | Prohibited activities | see config | any others to add |
| 7 | Business tolerance | none | whether the 5% covers minor prohibited revenue |
| 8 | Receivables screen | omitted | confirm none is required |

Project rules, not from the standard (all in `config.yaml`): near-threshold margin 10%;
zero-debt tolerance 0.1% of revenue; debt plausibility 25%; override expiry 365 days; annual
fallback age 450 days; dimensional and net caps 50% of the limit; yield ceiling 5%; financing
receivables 5% of assets; spot divergence 1.5x; share-count reject 5x and flag 1.5x; monthly
screen on day 1; companyfacts lag 3 days.

## Known gaps

- Captive finance arms that use non-standard tags (GM, PACCAR, Deere) are caught only by the
  sub-industry rule.
- A fiscal-year change or a predecessor/successor split leaves no 12-month revenue figure until
  the next annual report (Ferguson, Paramount Skydance).
- Companies that file thousands of reports a year may have less than 36 months of 10-Qs in the
  EDGAR "recent" list; older pages are not read.
- The business screen cannot see revenue mix inside a harmless SIC code.
- yfinance is unofficial and may break or be throttled.
- SIC code, GICS sub-industry and the constituent list are read as they are today, not as they
  were on the screen date, so a `--no-store` look-back is not a historical backtest.
- A screen run after the close still uses the previous trading day's close, one day behind.
- History is kept per ticker: a ticker rename shows as one removal and one addition.
- A cause is read from the two stored records, not from re-running the screen with one input
  changed at a time, so when several inputs change together the named cause is the likeliest one.
- Overlap rules: the debt containment rules were checked against filings for UPS, MAR and NVDA
  only. The finance-lease and current-maturity exclusions for other companies rest on tag labels.
  The value safeguard catches a container smaller than the tag it contains. It cannot catch a
  container that excludes an item its label suggests it includes.
- On 5 October 2026 EDGAR listed 62 periodic reports (filed 59 to 75 days earlier) that companyfacts
  did not serve. Their XBRL instances hold the balance sheet, so the screen now reads them, but why
  companyfacts lacks them is not known and should be checked.
- LEN's newest 10-Q tags total assets only by segment, with no undimensioned fact, so it is
  `insufficient_data`. Whether the consolidated total is available elsewhere is not checked.

# Halal Heatmap

A static web tool: a treemap of S&P 500 stocks coloured by the result of an AAOIFI-based Shariah
screen, with status changes flagged, updated automatically. This file is the working spec and the
record of decisions. It is an automated screen, not a fatwa and not investment advice.

## Status

- Phase 1 (screening engine + unit tests): built, under review.
- Phase 2 (change detection + point-in-time), Phase 3 (heatmap UI), Phase 4 (automation): not started.
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

## Commands

```
.venv/bin/pytest -q                                   # offline unit tests
.venv/bin/ruff check src tests
.venv/bin/halal-heatmap screen AAPL MSFT --facts      # audit records for some tickers
.venv/bin/halal-heatmap screen --quiet                # full S&P 500 into data/screens.db
.venv/bin/halal-heatmap supersede-run 4 --reason "…"  # mark a stored run invalid
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
   total-assets bound, debt plausibility, out-of-date balance sheet.

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
  tags and facts reported with dimensions, which companyfacts drops.
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
receivables 5% of assets; spot divergence 1.5x; share-count reject 5x and flag 1.5x.

## Known gaps

- Captive finance arms that use non-standard tags (GM, PACCAR, Deere) are caught only by the
  sub-industry rule.
- A fiscal-year change or a predecessor/successor split leaves no 12-month revenue figure until
  the next annual report (Ferguson, Paramount Skydance).
- Companies that file thousands of reports a year may have less than 36 months of 10-Qs in the
  EDGAR "recent" list; older pages are not read.
- The business screen cannot see revenue mix inside a harmless SIC code.
- yfinance is unofficial and may break or be throttled.

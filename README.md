# Mirsad

Mirsad is a static page showing every S&P 500 stock coloured by the result of an AAOIFI-based Shariah screen,
with status changes recorded and flagged. The screen is updated automatically from SEC filings and
daily prices, and every verdict keeps the trail needed to check it.

> **Read this first.** This is an automated screen, not a fatwa, not a certification and not investment
> advice. The thresholds and the method come from secondary sources and have not been verified against
> the AAOIFI primary text (Shari'ah Standard No. 21). Public data can be wrong, late or incomplete, and
> a company marked `pass` has not been certified as permissible by anyone. Consult a qualified scholar
> before relying on any result, and read the audit record for any company you care about.

## Reading the page

Each stock has one of four statuses:

| Status | Meaning |
|---|---|
| `pass` | Every input was available and every ratio is within its limit. |
| `fail` | A business screen failed, or a financial ratio is over its limit. |
| `needs_review` | A borderline business case. Not passed automatically. A reviewer may resolve it with a recorded override. |
| `insufficient_data` | A required input is missing or out of date. Never treated as a pass. |

They are decided in this order: business fail, then missing input, then a financial ratio fail, then
business needs_review, then pass. Pass-only checks (below) can turn a pass into `insufficient_data`.

Tiles are sized by spot market cap. Two colourings are offered: status, and the last-close price change.
The last-close price change is the move between the two closes before the screen date, and it is only offered
when the export includes price data.

## Methodology

### Financial screens

Based on AAOIFI Shari'ah Standard No. 21. The values, operators and the market cap denominator are
still to be verified against the current standard (see *Known limitations*).

| Ratio | Limit | Operator | Numerator |
|---|---|---|---|
| Interest-bearing debt / market cap | 30% | `<` (strict) | Debt, from the widest non-overlapping set of tags |
| Cash and interest-bearing securities / market cap | 30% | `<` (strict) | Cash plus interest-bearing securities, same rule |
| Impure income / revenue | 5% | `<` (strict) | Interest income only |

The operator and limit for each ratio live in `config.yaml` under `screens`. A passing ratio within
10% of its limit (relative) is flagged `near_threshold`.

### Business screen

Fails outright for alcohol, tobacco, gambling, conventional banks and insurers, and weapons makers.
Matches are made on the GICS sub-industry or on the SEC SIC code. A set of borderline groups
(restaurants and food retail, pork, adult entertainment, conventional finance, aerospace and defence,
and others) goes to `needs_review`. Aerospace and defence always stays `needs_review`.

Two captive-finance rules send an otherwise passing company to `needs_review`:

- Four manufacturer sub-industries whose lending arm reports interest as revenue: autos, motorcycles,
  farm machinery, and construction machinery and heavy trucks.
- Any company whose financing receivables are above 5% of total assets.

The full list of groups and codes is in `config.yaml` under `business.rules`.

### Market cap

- **Verdicts use the 12-month average** of daily market cap (`market_cap.driving`). The spot value
  and the 36-month average are computed and stored with their own would-be verdicts, so a disagreement
  between them is visible.
- An average needs a close on at least 90% of the expected trading days (`min_coverage`). Otherwise
  that average is not available.
- Market cap is closing price times reported shares. Shares come from the filing cover page, stepped by
  filing date and adjusted for splits. A count more than 5x away from every neighbouring filing is
  rejected; a change above 1.5x between filings is flagged for review.
- **Multi-class companies**: each listed class is priced at its own symbol. Unlisted classes are priced
  at the largest listed class. Tickers of one company share one market cap and one verdict.
- **Spot divergence**: when spot and the 12-month average differ by more than 1.5x, the company is
  flagged. If the spot verdict is not a pass, the pass becomes `insufficient_data`.

### Point in time

A screen for a date reads only filings filed on or before that date, and closes of trading days before
it. The screen date's own close is never read, because it may be an unfinished trading day. Stored
history only moves forward.

### Inputs and their fallbacks

- Debt and cash use the alternative tag set with the largest total, so overlaps overstate rather than
  understate. Known double counts are removed by containment rules, checked against filings for UPS,
  MAR and NVDA.
- Missing debt counts as zero only when total liabilities is reported and interest expense is below
  0.1% of revenue. Otherwise the company is `insufficient_data`.
- Debt is treated as understated, and the pass becomes `insufficient_data`, when interest expense
  exceeds 25% of the tagged debt.
- **Interest income** is taken from the most direct disclosure available, and the basis is recorded on
  every result: gross tags over the trailing 12 months, then the filing's own XBRL, then the latest
  annual figure (at most 450 days old), then dimensional members, then net investment income. Where no
  interest income is disclosed at all, the result uses an upper bound: cash and securities times a 5%
  yield ceiling. That bound can support a pass; it can never support a fail. Every basis except disclosed
  gross interest is labelled lower confidence on the page.
- **Newer filings**: a 10-Q or 10-K that EDGAR lists before companyfacts serves it is read from the
  filing's own XBRL. A newer periodic report with no usable balance sheet makes the company
  `insufficient_data`. An older balance sheet is never used in its place (`filings.max_fallback_age_days`
  is 0).
- **Staleness** is measured against each company's own filing cadence, not a flat age. A balance sheet is
  out of date once the next report is 45 days overdue. An 8-K that reports a spin-off or major disposition
  makes the balance sheet out of date until the next periodic report.

### Overrides

`overrides.yaml` resolves a `needs_review` only. Each override carries a reason, a reviewer and a date,
and expires after 365 days. An override never moves a `fail`, and it never changes a `pass`.

Reviewers are not named. Enter `maintainer` as the reviewer in `overrides.yaml`: that is what the stored
records carry. The export shows `maintainer` for every reviewer, and it also removes any name or email
that appears in stored text, as a second check.

## Automation

`.github/workflows/screen.yml` runs on weekdays after the close, with extra midday runs in the earnings
months. Each run calls `halal-heatmap update`, which screens:

- every constituent on the monthly date (the first day of the month, or the next run if that day was
  missed), or
- otherwise only the companies with something new: a first screen, a changed config, a failed earlier
  fetch, a new periodic filing, a new 8-K with an events item, or an override that started or expired.

Then the **publish gates** run. If any fails, the job fails and nothing from that run is exported or
deployed:

| Gate | Limit (`config.yaml`, `publish`) |
|---|---|
| Source errors (excluding filings EDGAR lists that companyfacts does not serve yet) | 2% of the constituent list |
| Closes that could not be read | 0 |
| Companies removed from the constituent list since the last snapshot | 5 |
| Status changes | 5% of the constituent list; accept by hand with the `accept_status_changes` input |

Also warned, not stopped: more than 10% of the constituent list with a filing that EDGAR lists but
companyfacts does not serve yet (`publish.max_lagging_share`). A batch of recent filings is normal; a
large, lasting one points at companyfacts.

The last-close price change is exported only when every company's closes were read. A missing close stops
the export, so the site never shows partial prices.

Order of steps: restore the database, screen, check the gates, export, save the database, deploy. The
database is saved only after the export succeeds, so a run that cannot be published leaves nothing
behind.

Tests run on every push and pull request (`.github/workflows/test.yml`). They are offline.

Action versions are pinned to commit SHAs.

### Database storage: the `data` branch

The database is not in `main`. It lives on an orphan branch named `data`, as one file, `screens.db.gz`.
Each run downloads it, screens, and uploads a new copy only if the run stored rows. The branch history is
the backup: any earlier copy can be recovered with `git show <commit>:screens.db.gz`.

Set up once, by hand, after reviewing the first run's output:

1. Run the workflow manually with `start_new_database` set. It creates the `data` branch with the first
   database. It is refused if the branch already exists, so it cannot wipe history by accident. Without
   this input, a missing branch stops the run.
2. Later scheduled runs restore from the branch.

Concurrency: the workflow has one concurrency group, `halal-heatmap-publish`, with `cancel-in-progress`
off. Two runs never overlap, so two runs never write the branch at once.

Squashing the branch history later, to keep the repository small:

1. Disable the workflow in the Actions tab, so no run starts.
2. `git fetch origin data`
3. `git worktree add --orphan -b data-squashed ../halal-heatmap-data`
4. `git show origin/data:screens.db.gz > ../halal-heatmap-data/screens.db.gz`
5. `git -C ../halal-heatmap-data add screens.db.gz`
6. `git -C ../halal-heatmap-data commit -m "Squash data history"`
7. `git push --force origin data-squashed:data`
8. `git worktree remove ../halal-heatmap-data && git branch -D data-squashed`
9. Re-enable the workflow.

After a squash, the old commits are gone from the branch, so the backup is the one copy you keep. Do this
only when you are happy to lose the older copies.

### Secrets and permissions

- `SEC_USER_AGENT`: a repository secret, sent to EDGAR as the User-Agent. Use a contact address, for example
  `halal-heatmap/0.1 (you@example.com)`. The program refuses to run without it.
- The workflow's default permissions are read-only. The screen job can write contents, and that is needed
  only to push the data branch.

## Running it yourself

```
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q                                  # offline unit tests
.venv/bin/ruff check src tests

export SEC_USER_AGENT="halal-heatmap/0.1 (you@example.com)"
.venv/bin/halal-heatmap screen AAPL MSFT --facts      # audit records for some tickers
.venv/bin/halal-heatmap update                        # what the schedule runs
.venv/bin/halal-heatmap changes [TICKER] [--since D]  # status changes and index events
.venv/bin/halal-heatmap export --no-prices            # site data, without the last-close price change
python3 -m http.server 8000 --directory web           # landing page at http://localhost:8000, the tool at http://localhost:8000/tool/
```

`data/*.db` and `.cache/` are not tracked. The first `update` builds the database from scratch and
screens every constituent. Expect it to download a few gigabytes of EDGAR company facts.

## Changing a threshold or a rule, and re-running

1. Edit `config.yaml`. Every threshold, operator, tag list and business rule is there, and no code in
   `src/` hardcodes one. `tests/test_config.py` enforces this.
2. Run the tests: `.venv/bin/pytest -q`.
3. Run `halal-heatmap update`. Every result stores a hash of the methodology (the config, minus the
   settings that cannot change a verdict). A changed methodology makes every company due, and each
   resulting status change is recorded with its cause, `methodology_change`.
4. Run `halal-heatmap export`.

Settings that only control how data is fetched (`edgar`, `prices`, `schedule`, `constituents`), the
publish gates (`publish`), and the near-threshold margin do not change the hash.

Operators are `<` or `<=`. Changing an operator is a methodology change, like any threshold.

To review a status change that a gate held back, use `halal-heatmap changes --since YYYY-MM-DD`. If the
change is real, run the workflow manually with `accept_status_changes` set. The other gates still apply.

A bad run is marked superseded with `halal-heatmap supersede-run ID --reason "…"`. Rows are never deleted.

## Known limitations

- **Lower-confidence interest income.** Most companies do not tag interest income anywhere. Only interest
  income disclosed as a gross figure is standard. Every other basis is labelled lower confidence on the page:
  an upper bound where no interest income is disclosed (which can only support a pass), net investment
  income, annual fallbacks, and partial or dimensional sums.
- **Unverified rules.** The receivables and illiquid-asset screen is omitted on purpose. The
  finance-lease and current-maturity overlap rules are checked against filings only for UPS, MAR and NVDA.
  For other companies they rest on tag labels. The business rules are the project's reading of the
  standard.
- **Filing lag.** EDGAR lists a filing before companyfacts serves its figures. Such filings are
  reported on each run as lagging, and the company is screened again once its figures arrive. Some
  filings may not be served for longer; their balance sheets are read from the filing's XBRL instead.
- **Companyfacts gaps.** Some companies' newest filings are not served by companyfacts at all.
  Why is not known.
- **Fiscal-year changes and reorganisations.** A fiscal-year change, or a predecessor and successor split,
  leaves no 12-month revenue until the next annual report. A merger is not treated as a reorganisation.
- **Unofficial prices.** Prices come from yfinance, which is unofficial. It may break or be throttled. A
  throttled fetch is retried with backoff, and if the closes are still missing the run is not published.
- **Timing.** A run after the close still uses the previous trading day's close, one day behind.
- **History is per ticker.** A ticker rename shows as one removal and one addition.
- **No historical backtest.** The constituent list, SIC codes and GICS sub-industries are today's values.
  A `--no-store` look at a past date is not a backtest.
- **Business screen.** It cannot see revenue mix inside a harmless SIC code.
- **Causes are inferred.** A status change's cause is read from the two stored records. When several
  inputs change together, the named cause is the likeliest one, not a proven one.

## Verifying against the current standard

These items are still open. None has been verified against the AAOIFI primary text yet.

| # | Item | Current value | Last verified (by, date) |
|---|---|---|---|
| 1 | Debt / market cap limit and denominator | 30%, market cap | none yet |
| 2 | Cash and interest-bearing securities / market cap | 30% | none yet |
| 3 | Impure income / revenue (revenue vs total income) | 5% | none yet |
| 4 | Operator (the standard may say "does not exceed") | strict `<` | none yet |
| 5 | Market cap averaging period | 12-month | none yet |
| 6 | Other prohibited activities to add | see `config.yaml` | none yet |
| 7 | Business tolerance for minor prohibited revenue | none | none yet |
| 8 | Receivables screen | omitted | none yet |

Project rules that are not from the standard, all in `config.yaml`: near-threshold margin 10%, zero-debt
tolerance 0.1% of revenue, debt plausibility 25%, override expiry 365 days, annual fallback 450 days,
dimensional and net caps at 50% of the limit, yield ceiling 5%, financing receivables 5% of assets,
spot divergence 1.5x, share-count rejection 5x and flag 1.5x, monthly screen on day 1, companyfacts lag
3 days, overdue grace 45 days, default cadence 91 days.

## Data sources

- Constituents: the Wikipedia list of S&P 500 companies.
- Filings and financial facts: SEC EDGAR, companyfacts and each filing's own XBRL.
- Prices: yfinance.

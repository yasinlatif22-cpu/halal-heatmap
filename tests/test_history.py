"""Phase 2: point-in-time screens, what triggers a screen, and status changes over time.

Every test runs over one fixture world whose sources always return their whole history,
including what lies after the screen date. Nothing here touches the network.
"""

import json
from datetime import date

import pytest

from conftest import companyfacts, fact, trading_days
from halal_heatmap.changes import crossings, diff_results
from halal_heatmap.cli import format_change
from halal_heatmap.config import parse_config
from halal_heatmap.overrides import Override
from halal_heatmap.pipeline import screen_constituent
from halal_heatmap.runner import (
    ForwardOnlyError,
    detect_due,
    monthly_due,
    run_screen,
    scheduled_date,
    supersede_run,
    update,
)
from halal_heatmap.screen.engine import result_to_record
from halal_heatmap.sources import SourceError
from halal_heatmap.sources.prices import PriceHistory
from halal_heatmap.sources.wikipedia import Constituent
from halal_heatmap.store import RESULT_COLUMNS, Store

HORIZON = date(2026, 12, 31)  # the fixture world's sources know everything up to here
PERIODIC = (  # key, form, period end, filed, quarters of the year reported
    ("k25", "10-K", "2025-12-31", "2026-02-10", 0),
    ("q1", "10-Q", "2026-03-31", "2026-04-30", 1),
    ("q2", "10-Q", "2026-06-30", "2026-07-30", 2),
    ("q3", "10-Q", "2026-09-30", "2026-10-30", 3),
)
BALANCE = {  # market cap is 100 shares x $100 = 10,000 unless a price path says otherwise
    "Assets": 5000,
    "Liabilities": 2000,
    "LongTermDebt": 2000,
    "CashAndCashEquivalentsAtCarryingValue": 1500,
    "ShortTermInvestments": 500,
}
INCOME = {"Revenues": 1000, "InvestmentIncomeInterest": 10, "InterestExpense": 20}  # per quarter
SPIN = "On August 19, 2026, the Company completed the separation of its Seed Business into Vylor Inc."
OVERRIDES = {"OVR": Override("OVR", "pass", "defence revenue under 1%", "YL", date(2026, 5, 20))}


def flat(day):
    return 100.0


def falls(day):  # 100 until 10 July 2026, then 70: the 12-month average sinks slowly
    return 100.0 if day < date(2026, 7, 10) else 70.0


class Company:
    def __init__(self, ticker, cik, *, sic="3674", price=flat, base=None, quarters=None):
        self.constituent = Constituent(ticker, f"{ticker} Corp", "Information Technology", "Semiconductors", cik)
        self.sic, self.price = sic, price
        self.rows = []  # submissions rows: accession, form, filed, items
        self.texts = {}
        usd = {tag: [] for tag in (*BALANCE, *INCOME)}
        shares = [fact("2023-01-20", 100, "2023-01-30", accn=f"{ticker}-old")]
        for key, form, end, filed, quarter in PERIODIC:
            accn = f"{ticker}-{key}"
            self.rows.append((accn, form, filed, ""))
            values = {**BALANCE, **(base or {}), **((quarters or {}).get(key) or {})}
            for tag in BALANCE:
                usd[tag].append(fact(end, values[tag], filed, form=form, accn=accn))
            for tag, amount in INCOME.items():
                if quarter == 0:
                    usd[tag].append(fact(end, 4 * amount, filed, start="2025-01-01", form=form, accn=accn))
                else:  # year to date, with last year's comparative in the same filing
                    usd[tag].append(fact(end, quarter * amount, filed, start="2026-01-01", accn=accn))
                    last_year = end.replace("2026", "2025")
                    usd[tag].append(fact(last_year, quarter * amount, filed, start="2025-01-01", accn=accn))
            shares.append(fact(filed, 100, filed, form=form, accn=accn))
        self.usd, self.shares = usd, shares

    @property
    def ticker(self):
        return self.constituent.ticker

    def file(self, key, form, filed, items="", *, text=None, facts=None):
        """Add a later filing: a restatement (`facts`: tag -> (period end, value)) or a current report."""
        accn = f"{self.ticker}-{key}"
        self.rows.append((accn, form, filed, items))
        if text:
            self.texts[accn] = text
        for tag, (end, value) in (facts or {}).items():
            self.usd[tag].append(fact(end, value, filed, form=form, accn=accn))
        return self

    def doc(self, as_of=HORIZON):
        def visible(items):
            return [item for item in items if item["filed"] <= as_of.isoformat()]

        usd = {tag: visible(items) for tag, items in self.usd.items()}
        return companyfacts(usd, {"dei:EntityCommonStockSharesOutstanding": visible(self.shares)})

    def submissions(self, as_of=HORIZON):
        rows = [row for row in self.rows if row[2] <= as_of.isoformat()]
        recent = {
            "accessionNumber": [r[0] for r in rows],
            "form": [r[1] for r in rows],
            "filingDate": [r[2] for r in rows],
            "primaryDocument": [f"{r[0]}.htm" for r in rows],
            "items": [r[3] for r in rows],
        }
        return {"sic": self.sic, "filings": {"recent": recent}}


class World:
    """Filing and price sources in one. `known_until` cuts the world off at a date, to compare a
    screen that could not have seen the future with one that was handed it."""

    def __init__(self, companies, known_until=HORIZON):
        self.companies = {c.ticker: c for c in companies}
        self.by_cik = {c.constituent.cik: c for c in companies}
        self.known_until = known_until
        self.down = set()  # tickers whose companyfacts cannot be fetched

    def until(self, day):
        return World(self.companies.values(), day)

    def company_facts(self, cik):
        if self.by_cik[cik].ticker in self.down:
            raise SourceError("EDGAR down")
        return self.by_cik[cik].doc(self.known_until)

    def submissions(self, cik):
        return self.by_cik[cik].submissions(self.known_until)

    def filing_text(self, cik, accession, primary_document):
        return self.by_cik[cik].texts[accession]

    def filing_instance(self, cik, accession, primary_document):
        raise SourceError(f"no XBRL instance document in filing {accession}")

    def history(self, ticker, start, end):  # ignores `end` on purpose
        price = self.companies[ticker].price
        return PriceHistory([(day, price(day)) for day in trading_days(start, self.known_until)], [])

    def constituents(self, *tickers):
        return [self.companies[t].constituent for t in tickers]


BEFORE = ("DEBT", "GONE", "METH", "OVR", "PASS", "PRIC", "RSTD", "SPIN")
AFTER = ("DEBT", "METH", "NEWC", "OVR", "PASS", "PRIC", "RSTD", "SPIN")  # 1 September: NEWC replaces GONE


@pytest.fixture
def world():
    return World(
        [
            Company("PASS", 1),
            Company("DEBT", 2, quarters={"q2": {"LongTermDebt": 3500}}),
            Company("PRIC", 3, price=falls, base={"LongTermDebt": 2900}),
            Company("OVR", 4, sic="3812"),
            Company("SPIN", 5).file("spin", "8-K", "2026-08-20", "8.01,9.01", text=SPIN),
            Company("METH", 6, base={"CashAndCashEquivalentsAtCarryingValue": 2200}),
            Company("RSTD", 7).file("q1a", "10-Q/A", "2026-06-20", facts={"LongTermDebt": ("2026-03-31", 3200)}),
            Company("GONE", 8),
            Company("NEWC", 9, sic="6021"),
        ]
    )


@pytest.fixture
def cfg(raw):
    raw["overrides"]["expiry_days"] = 120  # the OVR override of 20 May expires after 17 September
    return parse_config(raw)


@pytest.fixture
def stricter(raw):
    raw["overrides"]["expiry_days"] = 120
    raw["screens"]["cash"]["threshold"] = 0.25
    return parse_config(raw)


@pytest.fixture
def store():
    store = Store(":memory:")
    yield store
    store.close()


def record_for(world, cfg, ticker, as_of, overrides=OVERRIDES):
    result, used = screen_constituent(
        world.companies[ticker].constituent, as_of, cfg, world, world, overrides.get(ticker), trigger="test"
    )
    return result_to_record(result, cfg), used


def rows(store, table):
    return store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def table_sizes(store):
    return {t: rows(store, t) for t in ("runs", "screen_results", "input_facts", "status_changes", "index_events")}


def screen(world, cfg, store, as_of, tickers=None, listed=BEFORE, **kwargs):
    listed = world.constituents(*listed)
    return run_screen(as_of, cfg, listed, world, world, OVERRIDES, store, tickers=tickers, **kwargs)


# --- 1. point-in-time ---


@pytest.mark.parametrize(
    "as_of",
    [date(2026, 4, 29), date(2026, 5, 4), date(2026, 6, 19), date(2026, 6, 22), date(2026, 7, 29), date(2026, 8, 21)],
)
def test_the_future_never_reaches_a_screen(world, cfg, as_of):
    """Handing a screen everything filed and priced after its date changes nothing in its record."""
    for ticker in world.companies:
        with_future, facts_with_future = record_for(world, cfg, ticker, as_of)
        without, facts_without = record_for(world.until(as_of), cfg, ticker, as_of)
        assert with_future == without, ticker
        assert facts_with_future == facts_without
        assert all(u.fact.filed <= as_of for u in facts_with_future)


def test_a_filing_dated_after_the_screen_date_is_not_used(world, cfg):
    record, used = record_for(world, cfg, "DEBT", date(2026, 7, 29))  # the Q2 report is filed the next day
    assert (record["filing_accession"], record["period_end"], record["debt"]) == ("DEBT-q1", "2026-03-31", 2000)
    assert (record["latest_filing_date"], record["latest_filing_accessions"]) == ("2026-04-30", '["DEBT-q1"]')
    assert record["status"] == "pass" and "DEBT-q2" not in {u.fact.accession for u in used}
    record, _ = record_for(world, cfg, "DEBT", date(2026, 7, 30))
    assert (record["filing_accession"], record["debt"], record["status"]) == ("DEBT-q2", 3500, "fail")


def test_prices_are_closes_of_completed_trading_days_only(world, cfg):
    """PRIC falls to 70 on Friday 10 July. The source returns every later price whatever it is asked."""
    record, _ = record_for(world, cfg, "PRIC", date(2026, 7, 10))
    assert record["mcap_spot"] == record["mcap_avg_12m"] == pytest.approx(10000)
    assert record["mcap_spot_end"] == record["mcap_avg_12m_end"] == "2026-07-09"
    weekend, _ = record_for(world, cfg, "PRIC", date(2026, 7, 11))
    assert (weekend["mcap_spot"], weekend["mcap_spot_end"]) == (pytest.approx(7000), "2026-07-10")


def test_an_unfinished_price_for_the_screen_date_is_ignored(world, cfg):
    """A run during market hours gets a price for today that is not a close and will change."""
    today = date(2026, 6, 1)  # a Monday

    class Live(World):
        def __init__(self, companies, quote):
            super().__init__(companies, today)
            self.quote = quote

        def history(self, ticker, start, end):
            closes = super().history(ticker, start, end).closes
            assert closes[-1][0] == today
            return PriceHistory([*closes[:-1], (today, self.quote)], [])

    closed, _ = record_for(world.until(date(2026, 5, 29)), cfg, "PASS", today)  # nothing after Friday's close
    morning, _ = record_for(Live(world.companies.values(), 55.0), cfg, "PASS", today)
    afternoon, _ = record_for(Live(world.companies.values(), 140.0), cfg, "PASS", today)
    assert morning == afternoon == closed
    assert (morning["mcap_spot"], morning["mcap_spot_end"]) == (pytest.approx(10000), "2026-05-29")
    assert all(morning[f"mcap_{name}_end"] < today.isoformat() for name in ("spot", "avg_12m", "avg_36m"))


def test_an_unfinished_price_does_not_add_a_row_on_a_same_day_rerun(world, cfg, store):
    today = date(2026, 6, 1)
    quote = [55.0]
    plain = world.history
    world.history = lambda ticker, start, end: PriceHistory(
        [(day, quote[0] if day == today else close) for day, close in plain(ticker, start, end).closes], []
    )
    assert screen(world, cfg, store, today).saved == 8
    quote[0] = 140.0
    again = screen(world, cfg, store, today)
    assert (again.saved, again.unchanged, again.run_id) == (0, 8, None)


def test_an_event_or_override_dated_after_the_screen_date_is_not_applied(world, cfg):
    assert record_for(world, cfg, "SPIN", date(2026, 8, 19))[0]["post_balance_sheet_event"] == ""
    assert record_for(world, cfg, "SPIN", date(2026, 8, 20))[0]["status"] == "insufficient_data"
    assert record_for(world, cfg, "OVR", date(2026, 5, 19))[0]["override_state"] == "none"
    assert record_for(world, cfg, "OVR", date(2026, 5, 20))[0]["override_state"] == "active"


def test_a_restatement_never_replaces_what_an_earlier_screen_used(world, cfg, store):
    early, late = date(2026, 5, 4), date(2026, 6, 22)  # the 10-Q/A restating Q1 debt is filed on 20 June
    screen(world, cfg, store, early, ["RSTD"])
    first = dict(store.latest_result("RSTD"))
    assert (first["debt"], first["status"]) == (2000, "pass")

    screen(world, cfg, store, late, ["RSTD"])
    restated = store.latest_result("RSTD")
    assert (restated["debt"], restated["status"], restated["filing_accession"]) == (3200, "fail", "RSTD-q1")
    debt_facts = [f for f in store.facts_for(restated["id"]) if f["input"] == "debt"]
    assert [(f["accession"], f["form"], f["filed"], f["value"]) for f in debt_facts] == [
        ("RSTD-q1a", "10-Q/A", "2026-06-20", 3200)
    ]

    # The earlier row and its facts are untouched, and screening its date again today gives it back exactly.
    kept = dict(store.conn.execute("SELECT * FROM screen_results WHERE id = ?", (first["id"],)).fetchone())
    assert kept == first
    assert [f["accession"] for f in store.facts_for(first["id"]) if f["input"] == "debt"] == ["RSTD-q1"]
    again, _ = record_for(world, cfg, "RSTD", early)
    assert all(again[name] == first[name] for name in RESULT_COLUMNS if name != "trigger")


def test_stored_history_only_moves_forward(world, cfg, store):
    screen(world, cfg, store, date(2026, 6, 1), ["PASS"])
    with pytest.raises(ForwardOnlyError, match="2026-06-01"):
        screen(world, cfg, store, date(2026, 5, 4), ["PASS"])
    assert rows(store, "screen_results") == 1
    assert screen(world, cfg, None, date(2026, 5, 4), ["PASS"]).counts == {"pass": 1}  # looking back is fine


# --- 2. what triggers a screen ---


def test_same_date_reruns_add_no_rows(world, cfg, store):
    as_of = date(2026, 5, 4)
    first = screen(world, cfg, store, as_of)
    assert (first.saved, first.unchanged) == (8, 0) and first.run_id is not None
    before = table_sizes(store)
    for trigger in ("manual", "monthly"):
        again = screen(world, cfg, store, as_of, trigger=trigger)
        assert (again.run_id, again.saved, again.unchanged, again.screened) == (None, 0, 8, 8)
    assert update(as_of, cfg, world.constituents(*BEFORE), world, world, OVERRIDES, store).screened == 0
    assert table_sizes(store) == before


def test_a_rerun_stores_only_what_differs(world, cfg, store):
    as_of = date(2026, 5, 4)
    world.down.add("DEBT")
    screen(world, cfg, store, as_of)
    failed = store.latest_result("DEBT")
    assert (failed["status"], failed["source_failed"], failed["latest_filing_date"]) == (
        "insufficient_data",
        1,
        "2026-04-30",
    )
    world.down.clear()
    due, _ = detect_due(as_of, cfg, world.constituents(*BEFORE), world, OVERRIDES, store)
    assert due == {"DEBT": "retry"}
    again = update(as_of, cfg, world.constituents(*BEFORE), world, world, OVERRIDES, store)
    assert (again.saved, again.unchanged) == (1, 0) and rows(store, "screen_results") == 9
    assert [(c["ticker"], c["new_status"], c["cause"], c["cause_detail"]) for c in again.changes] == [
        ("DEBT", "pass", "other", "a data source recovered")
    ]


def test_new_filings_are_detected_from_the_submissions_list(world, cfg, store):
    listed = world.constituents(*BEFORE)
    screen(world, cfg, store, date(2026, 6, 1))
    assert not monthly_due(date(2026, 6, 19), cfg, store)
    assert detect_due(date(2026, 6, 19), cfg, listed, world, OVERRIDES, store) == ({}, [])
    assert detect_due(date(2026, 6, 20), cfg, listed, world, OVERRIDES, store)[0] == {"RSTD": "new_filing"}

    summary = update(date(2026, 6, 22), cfg, listed, world, world, OVERRIDES, store)
    assert (summary.due, summary.saved) == ({"RSTD": "new_filing"}, 1)
    assert store.latest_result("RSTD")["trigger"] == "new_filing"
    assert detect_due(date(2026, 6, 23), cfg, listed, world, OVERRIDES, store)[0] == {}
    # Every company files its Q2 report on 30 July.
    assert set(detect_due(date(2026, 7, 30), cfg, listed, world, OVERRIDES, store)[0].values()) == {"new_filing"}


def test_a_second_filing_on_the_same_day_is_still_new(world, cfg, store):
    listed = world.constituents("PASS")
    screen(world, cfg, store, date(2026, 6, 1), listed=("PASS",))
    world.companies["PASS"].file("amend", "10-K/A", "2026-04-30")  # same day as the Q1 report already read
    assert detect_due(date(2026, 6, 2), cfg, listed, world, OVERRIDES, store)[0] == {"PASS": "new_filing"}


def test_only_current_reports_with_an_events_item_trigger_a_screen(world, cfg, store):
    listed = world.constituents("PASS", "SPIN")
    screen(world, cfg, store, date(2026, 8, 3), listed=("PASS", "SPIN"))
    world.companies["PASS"].file("earnings", "8-K", "2026-08-10", "2.02,9.01")
    assert detect_due(date(2026, 8, 19), cfg, listed, world, OVERRIDES, store)[0] == {}
    assert detect_due(date(2026, 8, 20), cfg, listed, world, OVERRIDES, store)[0] == {"SPIN": "event_filing"}


def test_a_filing_not_yet_in_companyfacts_is_not_counted_as_read(world, cfg, store):
    """EDGAR lists a filing before its figures are served. The screen must come back for them."""
    listed = world.constituents("DEBT")
    as_of = date(2026, 7, 30)
    company = world.companies["DEBT"]
    full, all_shares = company.usd, company.shares
    company.usd = {tag: [f for f in items if f["accn"] != "DEBT-q2"] for tag, items in full.items()}
    company.shares = [f for f in all_shares if f["accn"] != "DEBT-q2"]
    screen(world, cfg, store, as_of, listed=("DEBT",))
    lagging = store.latest_result("DEBT")
    assert (lagging["status"], lagging["filing_accession"], lagging["latest_filing_date"]) == (
        "pass",
        "DEBT-q1",
        "2026-04-30",
    )
    assert "DEBT-q2" in json.loads(lagging["input_notes"])["filing_lag"]
    assert detect_due(as_of, cfg, listed, world, OVERRIDES, store)[0] == {"DEBT": "new_filing"}

    company.usd, company.shares = full, all_shares
    summary = update(as_of, cfg, listed, world, world, OVERRIDES, store)
    assert [(c["old_status"], c["new_status"], c["cause"]) for c in summary.changes] == [("pass", "fail", "new_filing")]
    assert detect_due(as_of, cfg, listed, world, OVERRIDES, store)[0] == {}


def test_overrides_and_config_changes_make_a_company_due(world, cfg, stricter, store):
    listed = world.constituents(*BEFORE)
    screen(world, cfg, store, date(2026, 5, 4))
    assert detect_due(date(2026, 5, 19), cfg, listed, world, OVERRIDES, store)[0] == {}
    assert detect_due(date(2026, 5, 20), cfg, listed, world, OVERRIDES, store)[0] == {"OVR": "override_change"}
    assert detect_due(date(2026, 5, 20), cfg, listed, world, {}, store)[0] == {}
    assert set(detect_due(date(2026, 5, 5), stricter, listed, world, OVERRIDES, store)[0].values()) == {
        "methodology_change"
    }
    new = [*listed, world.companies["NEWC"].constituent]
    assert detect_due(date(2026, 5, 5), cfg, new, world, OVERRIDES, store)[0] == {"NEWC": "first_screen"}


def test_monthly_date_and_catch_up(world, cfg, store):
    assert scheduled_date(date(2026, 6, 1), 1) == date(2026, 6, 1)
    assert scheduled_date(date(2026, 6, 30), 1) == date(2026, 6, 1)
    assert scheduled_date(date(2026, 3, 14), 15) == date(2026, 2, 15)
    assert scheduled_date(date(2026, 1, 5), 15) == date(2025, 12, 15)
    assert scheduled_date(date(2026, 2, 28), 31) == date(2026, 2, 28)  # a short month uses its last day
    assert scheduled_date(date(2026, 3, 30), 31) == date(2026, 2, 28)

    listed = world.constituents(*BEFORE)
    assert monthly_due(date(2026, 5, 4), cfg, store)
    screen(world, cfg, store, date(2026, 5, 4), ["PASS"])  # a partial run does not count
    assert monthly_due(date(2026, 5, 4), cfg, store)
    first = update(date(2026, 5, 4), cfg, listed, world, world, OVERRIDES, store)
    assert set(first.due.values()) == {"monthly"} and first.saved == 7 and first.unchanged == 1
    assert not monthly_due(date(2026, 5, 31), cfg, store)
    assert monthly_due(date(2026, 6, 1), cfg, store)
    # The first of June was missed: the next run makes it up.
    late = update(date(2026, 6, 3), cfg, listed, world, world, OVERRIDES, store)
    assert late.saved == 8 and not monthly_due(date(2026, 6, 30), cfg, store)
    assert store.conn.execute("SELECT trigger, scope FROM runs WHERE id = ?", (late.run_id,)).fetchone()[:] == (
        "monthly",
        "full",
    )
    supersede_run(store, late.run_id, "bad run")
    assert monthly_due(date(2026, 6, 3), cfg, store)  # a superseded run does not count either


# --- 3 and 4. status changes and their causes ---


def test_a_change_records_the_ratio_that_crossed_with_before_and_after(world, cfg, store):
    screen(world, cfg, store, date(2026, 7, 29), ["DEBT"])
    summary = screen(world, cfg, store, date(2026, 7, 31), ["DEBT"])
    (change,) = store.status_changes("DEBT")
    assert dict(change).items() >= summary.changes[0].items()
    assert (change["old_status"], change["new_status"], change["cause"]) == ("pass", "fail", "new_filing")
    assert (change["previous_screen_date"], change["screen_date"]) == ("2026-07-29", "2026-07-31")
    assert change["old_reason"].startswith("all screens passed")
    assert "debt / market cap (avg_12m) 35.00% is not < 30.00%" in change["new_reason"]
    assert "figures changed with filing DEBT-q2: debt" in change["cause_detail"]
    (crossing,) = json.loads(change["crossings"])
    assert (crossing["ratio"], crossing["direction"]) == ("debt", "breached")
    assert crossing["before"] == {
        "ratio": pytest.approx(0.20),
        "threshold": 0.30,
        "operator": "<",
        "passed": True,
        "numerator": 2000,
        "denominator": "market cap (avg_12m)",
        "denominator_value": pytest.approx(10000),
    }
    assert crossing["after"]["ratio"] == pytest.approx(0.35) and crossing["after"]["numerator"] == 3500
    text = format_change(change)
    assert "DEBT  pass -> fail  cause: new_filing" in text
    assert "debt / market cap (avg_12m) breached its limit: 20.00% (limit < 30.00%) -> 35.00%" in text


def test_no_change_row_without_a_status_change(world, cfg, store):
    screen(world, cfg, store, date(2026, 5, 4))
    summary = screen(world, cfg, store, date(2026, 6, 1))
    assert summary.saved == 8 and [c["ticker"] for c in summary.changes] == ["OVR"]
    assert rows(store, "status_changes") == 1


def test_changes_skip_superseded_runs(world, cfg, raw, store):
    raw["screens"]["debt"]["threshold"] = 0.10
    broken = parse_config(raw)
    screen(world, cfg, store, date(2026, 6, 1), ["PASS", "DEBT"])
    bad = screen(world, broken, store, date(2026, 7, 1), ["PASS", "DEBT"])
    assert [(c["ticker"], c["new_status"], c["cause"]) for c in bad.changes] == [
        ("PASS", "fail", "methodology_change"),
        ("DEBT", "fail", "methodology_change"),
    ]
    assert supersede_run(store, bad.run_id, "debt limit mistyped") == []
    assert store.status_changes() == [] and rows(store, "status_changes") == 2  # kept, never read

    # The next screen is compared with June, not with the superseded July run.
    good = screen(world, cfg, store, date(2026, 7, 31), ["PASS", "DEBT"])
    assert [(c["ticker"], c["previous_screen_date"], c["old_status"], c["new_status"]) for c in good.changes] == [
        ("DEBT", "2026-06-01", "pass", "fail")
    ]
    assert [c["ticker"] for c in store.status_changes()] == ["DEBT"]


def test_superseding_a_run_measures_later_changes_again(world, cfg, store):
    """pass (June), fail (July, bad), fail (August): once July is superseded August is a change from June."""
    screen(world, cfg, store, date(2026, 6, 1), ["DEBT", "PASS"])
    july = screen(world, cfg, store, date(2026, 7, 31), ["DEBT", "PASS"])
    august = screen(world, cfg, store, date(2026, 8, 3), ["DEBT", "PASS"])
    assert len(july.changes) == 1 and august.changes == []
    (again,) = supersede_run(store, july.run_id, "wrong inputs")
    assert (again["ticker"], again["previous_screen_date"]) == ("DEBT", "2026-06-01")
    assert again["screen_date"] == "2026-08-03"
    (change,) = store.status_changes()
    assert (change["old_status"], change["new_status"], change["run_id"]) == ("pass", "fail", august.run_id)


def base_pair(world, cfg):
    old, _ = record_for(world, cfg, "PASS", date(2026, 8, 3))
    new, _ = record_for(world, cfg, "PASS", date(2026, 8, 4))
    return old, {**new, "status": "fail"}


def test_causes_are_told_apart(world, cfg):
    old, new = base_pair(world, cfg)
    assert diff_results(old, {**new, "status": "pass"}) is None

    def cause(old_changes=None, **new_changes):
        change = diff_results({**old, **(old_changes or {})}, {**new, **new_changes})
        return change["cause"], [f["cause"] for f in json.loads(change["factors"])]

    assert cause() == ("other", ["other"])
    assert cause(config_hash="abc", debt=9000.0)[0] == "methodology_change"
    assert cause(override_state="active", override_id="X@2026-08-04")[0] == "override_added"
    assert cause({"override_state": "active", "override_id": "X@1"}, override_state="expired")[0] == "override_expired"
    assert cause({"override_state": "active", "override_id": "X@1"})[0] == "override_removed"
    event = dict(post_balance_sheet_event="separation reported in 8-K", stale_balance_sheet=1)
    assert cause(**event) == ("event_8k", ["event_8k"])
    assert cause(old_changes=event, **event)[0] == "other"  # the same event again explains nothing new
    assert cause(filing_accession="new", debt=3500.0, debt_ratio_avg_12m=0.35) == ("new_filing", ["new_filing"])
    # Same figures, market cap down a third: the price alone carried debt across the limit.
    cheaper = dict(mcap_avg_12m=6000.0, debt_ratio_avg_12m=2000 / 6000)
    assert cause(**cheaper) == ("price_move", ["price_move"])
    assert cause(filing_accession="new", **cheaper) == ("price_move", ["price_move", "new_filing"])
    # Slightly more debt and a much lower market cap: still the price.
    assert cause(debt=2100.0, **{**cheaper, "debt_ratio_avg_12m": 0.35})[0] == "price_move"
    # Debt up enough to fail at the old market cap: the filing.
    assert cause(debt=3500.0, mcap_avg_12m=9000.0, debt_ratio_avg_12m=3500 / 9000) == (
        "new_filing",
        ["new_filing", "price_move"],
    )
    assert cause(spot_diverges=1, mcap_spot=5000.0) == ("price_move", ["price_move"])
    assert cause(source_failed=1, debt=None, mcap_avg_12m=None) == ("other", ["other"])
    assert cause(sic="6021")[0] == "other"


def test_a_ratio_without_a_value_is_not_a_crossing(world, cfg):
    old, new = base_pair(world, cfg)
    assert crossings(old, {**new, "debt_ratio_avg_12m": None}) == []
    (limit_moved,) = crossings(old, {**new, "cash_threshold": 0.15})
    assert (limit_moved["ratio"], limit_moved["before"]["threshold"], limit_moved["after"]["threshold"]) == (
        "cash",
        0.30,
        0.15,
    )


def test_index_changes_are_events_not_status_changes(world, cfg, store):
    screen(world, cfg, store, date(2026, 8, 3))
    assert store.index_events() == []  # the first list is the starting point, not 500 additions
    summary = screen(world, cfg, store, date(2026, 9, 1), listed=AFTER)
    events = [(e["event_date"], e["kind"], e["ticker"], e["previous_snapshot_date"]) for e in store.index_events()]
    assert events == [("2026-09-01", "added", "NEWC", "2026-08-03"), ("2026-09-01", "removed", "GONE", "2026-08-03")]
    assert [e["ticker"] for e in summary.index_events] == ["NEWC", "GONE"]
    assert "NEWC" not in {c["ticker"] for c in store.status_changes()}
    assert store.latest_result("NEWC")["status"] == "fail"
    assert store.latest_result("GONE")["screen_date"] == "2026-08-03"  # no longer screened
    screen(world, cfg, store, date(2026, 9, 1), listed=AFTER)
    assert rows(store, "index_events") == 2


def test_a_company_that_rejoins_starts_a_new_history(world, cfg, store):
    screen(world, cfg, store, date(2026, 6, 1), listed=("PASS", "DEBT"))
    screen(world, cfg, store, date(2026, 7, 1), listed=("PASS",))
    back = screen(world, cfg, store, date(2026, 8, 3), listed=("PASS", "DEBT"))  # DEBT now fails
    assert [(e["kind"], e["ticker"]) for e in back.index_events] == [("added", "DEBT")]
    assert back.changes == [] and store.latest_result("DEBT")["status"] == "fail"


# --- 5. replay ---

TIMELINE = (  # screen date, constituent list, strict config?, why companies were due, rows stored
    ("2026-05-04", BEFORE, False, {"monthly": 8}, 8),
    ("2026-05-05", BEFORE, False, {}, 0),
    ("2026-05-21", BEFORE, False, {"override_change": 1}, 1),  # the OVR override is dated 20 May
    ("2026-06-01", BEFORE, False, {"monthly": 8}, 8),
    ("2026-06-22", BEFORE, False, {"new_filing": 1}, 1),  # RSTD restates Q1 debt in a 10-Q/A
    ("2026-07-01", BEFORE, False, {"monthly": 8}, 8),
    ("2026-07-31", BEFORE, False, {"new_filing": 8}, 8),  # Q2 reports
    ("2026-08-03", BEFORE, False, {"monthly": 8}, 8),
    ("2026-08-21", BEFORE, False, {"event_filing": 1}, 1),  # SPIN completes a separation
    ("2026-09-01", AFTER, False, {"monthly": 8}, 8),  # PRIC's average market cap has sunk; index changes
    ("2026-09-15", AFTER, True, {"methodology_change": 8}, 8),  # cash limit cut from 30% to 25%
    ("2026-09-18", AFTER, True, {"override_change": 1}, 1),  # the OVR override has expired
    ("2026-10-01", AFTER, True, {"monthly": 8}, 8),
    ("2026-11-02", AFTER, True, {"monthly": 8}, 8),  # Q3 reports
)
EXPECTED_CHANGES = [  # screen date, ticker, old status, new status, cause, ratios that crossed
    ("2026-05-21", "OVR", "needs_review", "pass", "override_added", []),
    ("2026-06-22", "RSTD", "pass", "fail", "new_filing", [("debt", "breached", 0.20, 0.32)]),
    ("2026-07-31", "DEBT", "pass", "fail", "new_filing", [("debt", "breached", 0.20, 0.35)]),
    ("2026-07-31", "RSTD", "fail", "pass", "new_filing", [("debt", "cleared", 0.32, 0.20)]),
    ("2026-08-21", "SPIN", "pass", "insufficient_data", "event_8k", []),
    ("2026-09-01", "PRIC", "pass", "fail", "price_move", [("debt", "breached", 0.295, 0.303)]),
    ("2026-09-15", "METH", "pass", "fail", "methodology_change", [("cash", "breached", 0.27, 0.27)]),
    ("2026-09-18", "OVR", "pass", "needs_review", "override_expired", []),
    ("2026-11-02", "DEBT", "fail", "pass", "new_filing", [("debt", "cleared", 0.35, 0.20)]),
    ("2026-11-02", "SPIN", "insufficient_data", "pass", "new_filing", []),
]


def test_replay_over_the_fixture_timeline(world, cfg, stricter, raw, store):
    """Run the scheduled entry point day by day, as automation will, and read the history back."""
    raw["overrides"]["expiry_days"] = 120
    raw["screens"]["debt"]["threshold"] = 0.10
    broken = parse_config(raw)
    for day, listed, strict, due, stored in TIMELINE:
        as_of = date.fromisoformat(day)
        constituents = world.constituents(*listed)
        if day == "2026-08-03":  # a run with a mistyped limit is stored first, then marked superseded
            bad = run_screen(as_of, broken, constituents, world, world, OVERRIDES, store)
            assert len(bad.changes) == 7  # everything but DEBT, which already failed
            supersede_run(store, bad.run_id, "debt limit mistyped as 10%")
        config = stricter if strict else cfg
        summary = update(as_of, config, constituents, world, world, OVERRIDES, store)
        counted = {}
        for reason in summary.due.values():
            counted[reason] = counted.get(reason, 0) + 1
        assert (counted, summary.saved) == (due, stored), day
        assert not summary.errors, summary.errors
        # Running the same day again, by either entry point, adds nothing.
        sizes = table_sizes(store)
        assert update(as_of, config, constituents, world, world, OVERRIDES, store).saved == 0
        again = run_screen(as_of, config, constituents, world, world, OVERRIDES, store, tickers=sorted(summary.due))
        assert (again.run_id, again.unchanged) == (None, stored)
        assert table_sizes(store) == sizes, day

    changes = [
        (
            c["screen_date"],
            c["ticker"],
            c["old_status"],
            c["new_status"],
            c["cause"],
            [
                (x["ratio"], x["direction"], round(x["before"]["ratio"], 3), round(x["after"]["ratio"], 3))
                for x in json.loads(c["crossings"])
            ],
        )
        for c in store.status_changes()
    ]
    assert changes == EXPECTED_CHANGES
    assert rows(store, "status_changes") == len(EXPECTED_CHANGES) + 7  # the superseded run's seven stay on file
    assert [(e["event_date"], e["kind"], e["ticker"]) for e in store.index_events()] == [
        ("2026-09-01", "added", "NEWC"),
        ("2026-09-01", "removed", "GONE"),
    ]
    final = {t: store.latest_result(t)["status"] for t in AFTER}
    assert final == {
        "DEBT": "pass",
        "METH": "fail",
        "NEWC": "fail",
        "OVR": "needs_review",
        "PASS": "pass",
        "PRIC": "fail",
        "RSTD": "pass",
        "SPIN": "pass",
    }
    # Every stored result used only what was public on its screen date.
    leaked = store.conn.execute(
        "SELECT COUNT(*) FROM input_facts f JOIN screen_results r ON r.id = f.result_id WHERE f.filed > r.screen_date"
    ).fetchone()[0]
    assert leaked == 0
    late = store.conn.execute(
        "SELECT COUNT(*) FROM screen_results WHERE filing_date > screen_date OR mcap_spot_end >= screen_date "
        "OR mcap_avg_12m_end >= screen_date OR latest_filing_date > screen_date"
    ).fetchone()[0]
    assert late == 0
    assert store.conn.execute("SELECT COUNT(*) FROM runs WHERE superseded = 1").fetchone()[0] == 1


def test_closes_on_or_after_the_screen_date_are_dropped_before_any_use():
    """Market caps filter by date themselves; this keeps every other reader of the closes safe too."""
    from halal_heatmap.pipeline import _completed

    history = PriceHistory([(date(2026, 7, 9), 100.0), (date(2026, 7, 10), 70.0)], [(date(2026, 8, 1), 2.0)])
    assert _completed(history, date(2026, 7, 10)) == PriceHistory([(date(2026, 7, 9), 100.0)], history.splits)

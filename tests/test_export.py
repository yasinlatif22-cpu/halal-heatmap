"""Phase 3: the static site's data. Reads stored records only, so every test here works from
the Phase 2 fixture world and nothing touches the network."""

import re
from datetime import date

import pytest

from halal_heatmap.config import parse_config
from halal_heatmap.export import _interest_basis, build_site, daily_changes, reviewer_labels, scrub_text, write_site
from halal_heatmap.overrides import Override
from halal_heatmap.runner import run_screen, supersede_run
from halal_heatmap.sources import SourceError
from halal_heatmap.sources.prices import PriceHistory
from halal_heatmap.store import Store
from test_history import Company, World

EMAIL_PATTERN = r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
REVIEWER = "Ada Example"
OVR = {
    "OVR": Override("OVR", "pass", f"cleared with {REVIEWER}; mail ada@example.org", REVIEWER, date(2026, 5, 20))
}
ALL = ["DEBT", "NEAR", "OVR", "PASS"]


@pytest.fixture
def world():
    return World(
        [
            Company("PASS", 1),
            Company("DEBT", 2, quarters={"q2": {"LongTermDebt": 3500}}),
            Company("NEAR", 3, base={"LongTermDebt": 2900}),  # debt is 29% of market cap: inside the 10% band
            Company("OVR", 4, sic="3812"),  # weapons: needs_review, so the override applies
        ]
    )


@pytest.fixture
def cfg(raw):
    return parse_config(raw)


@pytest.fixture
def store():
    store = Store(":memory:")
    yield store
    store.close()


def screen(world, cfg, store, as_of, tickers, overrides=OVR):
    constituents = world.constituents(*ALL)
    return run_screen(as_of, cfg, constituents, world, world, overrides, store, tickers=tickers)


def screen_of(site, ticker):
    return next(s for s in site["screens.json"]["screens"] if s["ticker"] == ticker)


class Prices:
    """Closes by ticker. Returns every close it holds, including the screen date's own, on purpose."""

    def __init__(self, closes, failing=()):
        self.closes, self.failing, self.asked = closes, set(failing), []

    def history(self, ticker, start, end):
        self.asked.append((ticker, start, end))
        if ticker in self.failing:
            raise SourceError(f"no prices for {ticker}")
        return PriceHistory(self.closes.get(ticker, []), [])


def test_the_export_shows_the_latest_valid_screen_of_each_stock(world, cfg, store):
    screen(world, cfg, store, date(2026, 6, 1), ALL)
    later = screen(world, cfg, store, date(2026, 8, 3), ["PASS"])
    site = build_site(store, cfg, OVR)
    assert screen_of(site, "PASS")["screen_date"] == "2026-08-03"
    assert screen_of(site, "DEBT")["screen_date"] == "2026-06-01"
    assert site["meta.json"]["counts"]["screened_before_latest_date"] == 3

    supersede_run(store, later.run_id, "bad run")
    site = build_site(store, cfg, OVR)
    assert screen_of(site, "PASS")["screen_date"] == "2026-06-01"
    assert site["meta.json"]["run_ids"] == [1]


def test_changes_from_superseded_runs_are_not_exported(world, cfg, raw, store):
    screen(world, cfg, store, date(2026, 6, 1), ["DEBT"])
    raw["screens"]["debt"]["threshold"] = 0.10
    broken = parse_config(raw)
    bad = screen(world, broken, store, date(2026, 8, 3), ["DEBT"])
    assert [c["new_status"] for c in build_site(store, broken, OVR)["changes.json"]["changes"]] == ["fail"]

    supersede_run(store, bad.run_id, "debt limit mistyped")
    assert build_site(store, cfg, OVR)["changes.json"]["changes"] == []


def test_near_threshold_is_recomputed_with_the_current_margin(world, cfg, raw, store):
    screen(world, cfg, store, date(2026, 6, 1), ALL)
    assert screen_of(build_site(store, cfg, OVR), "NEAR")["near_threshold"] is True

    raw["near_threshold"]["margin"] = 0.02  # the 29% debt ratio is no longer within 2% of the 30% limit
    tight = parse_config(raw)
    site = build_site(store, tight, OVR)
    near = screen_of(site, "NEAR")
    assert near["near_threshold"] is False and near["status"] == "pass"  # the verdict does not move
    assert near["ratios"]["debt"]["near_threshold"] is False
    assert site["meta.json"]["near_threshold"] == {"mode": "relative", "margin": 0.02}


def test_near_is_only_flagged_where_the_verdict_could_still_be_a_pass(world, cfg, store):
    screen(world, cfg, store, date(2026, 6, 1), ALL)
    site = build_site(store, cfg, OVR)
    for screen_row in site["screens.json"]["screens"]:
        if screen_row["near_threshold"]:
            assert screen_row["status"] in ("pass", "needs_review")


def test_reviewer_names_and_emails_never_reach_any_exported_file(world, cfg, store, tmp_path):
    screen(world, cfg, store, date(2026, 6, 1), ALL)
    site = build_site(store, cfg, OVR)
    write_site(site, tmp_path)
    written = "".join(p.read_text(encoding="utf-8") for p in tmp_path.iterdir())
    assert REVIEWER not in written and "Ada" not in written and "Example" not in written
    assert "ada@example.org" not in written
    assert re.search(EMAIL_PATTERN, written) is None  # override ids contain "@" but no address
    assert screen_of(site, "OVR")["override"]["reviewer"] == "Reviewer 1"
    assert screen_of(site, "OVR")["status"] == "pass"

    # The stored record does carry the name, so the scrub is doing real work.
    assert REVIEWER in store.latest_result("OVR")["reason"]


def test_reviewer_labels_are_neutral_and_stable(world, cfg, store):
    overrides = {
        "A": Override("A", "pass", "x", "First Person", date(2026, 5, 1)),
        "B": Override("B", "fail", "y", "Second Person", date(2026, 5, 2)),
        "C": Override("C", "pass", "z", "First Person", date(2026, 5, 3)),
    }
    assert reviewer_labels(overrides) == {"First Person": "Reviewer 1", "Second Person": "Reviewer 2"}


def test_scrubbing_matches_whole_words_only():
    labels = {"YL": "Reviewer 1"}
    assert scrub_text("YL signed off; XYL and YLE are other words", labels) == (
        "Reviewer 1 signed off; XYL and YLE are other words"
    )
    assert scrub_text("manual override by Ada Example on 2026-05-20: x", {}) == (
        "manual override by a reviewer on 2026-05-20: x"
    )


def test_daily_change_is_the_derived_move_before_the_screen_date(world, cfg, store):
    screen(world, cfg, store, date(2026, 8, 3), ["PASS"])
    prices = Prices(
        {"PASS": [(date(2026, 7, 31), 100.0), (date(2026, 8, 1), 110.0), (date(2026, 8, 3), 500.0)]}
    )
    site = build_site(store, cfg, OVR, prices)
    assert screen_of(site, "PASS")["daily_change"] == {"date": "2026-08-01", "pct": pytest.approx(0.10)}
    assert site["meta.json"]["daily_change"]["included"] is True
    # Only the derived change is asked for, and it never reaches the screen date's own close.
    (ticker, start, end) = prices.asked[0]
    assert ticker == "PASS" and end < date(2026, 8, 3)


def test_a_failed_price_fetch_leaves_the_change_empty_and_is_reported(world, cfg, store):
    screen(world, cfg, store, date(2026, 8, 3), ["PASS", "DEBT"])
    site = build_site(store, cfg, OVR, Prices({}, failing={"DEBT"}))
    assert screen_of(site, "DEBT")["daily_change"] is None
    assert site["meta.json"]["daily_change"]["failed"] == ["DEBT"]


def test_no_prices_means_no_price_fields_are_filled(world, cfg, store):
    screen(world, cfg, store, date(2026, 8, 3), ["PASS"])
    site = build_site(store, cfg, OVR, None)
    assert screen_of(site, "PASS")["daily_change"] is None
    assert site["meta.json"]["daily_change"]["included"] is False


def test_daily_changes_returns_only_the_percent_and_its_date():
    prices = Prices({"X": [(date(2026, 7, 30), 50.0), (date(2026, 7, 31), 55.0)]})
    changes, failed = daily_changes(prices, [("X", date(2026, 8, 3))])
    assert changes == {"X": {"date": "2026-07-31", "pct": pytest.approx(0.10)}}
    assert failed == []


def test_each_stock_carries_its_full_audit_record(world, cfg, store):
    screen(world, cfg, store, date(2026, 8, 3), ["PASS"])
    site = build_site(store, cfg, OVR)
    row = screen_of(site, "PASS")
    assert len(row["facts"]) == len(store.facts_for(store.latest_result("PASS")["id"])) > 0
    assert set(row["ratios"]) == {"debt", "cash", "impure_income"}
    for ratio in row["ratios"].values():
        assert {"ratio", "limit", "operator", "passed", "headroom", "headroom_rel", "near_threshold"} <= set(ratio)
    assert set(row["market_cap"]["denominators"]) == {"spot", "avg_12m", "avg_36m"}
    assert row["market_cap"]["driving"] == "avg_12m"
    assert row["filing"]["accession"] and row["filing"]["url"].startswith("https://www.sec.gov/Archives/edgar/data/")
    assert row["reason"] and row["status"] == "pass"


def test_a_config_other_than_the_stored_one_is_flagged(world, cfg, raw, store):
    screen(world, cfg, store, date(2026, 6, 1), ["PASS"])
    assert build_site(store, cfg, OVR)["meta.json"]["config_matches"] is True
    raw["screens"]["debt"]["threshold"] = 0.31
    meta = build_site(store, parse_config(raw), OVR)["meta.json"]
    assert meta["config_matches"] is False and meta["current_config_hash"] != meta["config_hash"]


def test_undisclosed_interest_is_the_lower_confidence_basis():
    row = {
        "interest_income_basis": "upper_bound_no_disclosure",
        "interest_income_bound_base": "total_assets",
        "interest_income": None,
        "interest_income_source": None,
        "interest_income_tags": "",
        "interest_income_dimensional": 0,
        "interest_income_annual": 0,
        "interest_income_period_end": None,
        "interest_income_filing_date": None,
        "interest_income_accession": None,
        "interest_income_upper_bound": 500.0,
        "interest_income_yield_ceiling": 0.05,
        "interest_income_max_ratio": None,
    }
    basis = _interest_basis(row)
    assert basis["key"] == "upper_bound_total_assets" and basis["confidence"] == "lower"
    assert basis["upper_bound"] == {"value": 500.0, "yield_ceiling": 0.05, "base": "total_assets"}
    row["interest_income_basis"] = "ttm"
    row["interest_income_bound_base"] = None
    row["interest_income"] = 40.0
    row["interest_income_source"] = "companyfacts"
    row["interest_income_tags"] = "us-gaap:InvestmentIncomeInterest, us-gaap:InterestIncomeOperating"
    basis = _interest_basis(row)
    assert basis["key"] == "disclosed" and basis["confidence"] == "standard"
    assert basis["tags"] == ["us-gaap:InvestmentIncomeInterest", "us-gaap:InterestIncomeOperating"]


def _basis_row(**changes):
    row = {
        "interest_income_basis": "ttm",
        "interest_income_bound_base": None,
        "interest_income": 40.0,
        "interest_income_source": "companyfacts",
        "interest_income_tags": "us-gaap:InvestmentIncomeInterest",
        "interest_income_dimensional": 0,
        "interest_income_annual": 0,
        "interest_income_period_end": None,
        "interest_income_filing_date": None,
        "interest_income_accession": None,
        "interest_income_upper_bound": None,
        "interest_income_yield_ceiling": None,
        "interest_income_max_ratio": None,
    }
    row.update(changes)
    return row


@pytest.mark.parametrize(
    "changes, key, confidence",
    [
        ({}, "disclosed", "standard"),
        ({"interest_income_dimensional": 1}, "disclosed_partial", "lower"),
        (
            {"interest_income_basis": "annual_fallback", "interest_income_annual": 1,
             "interest_income_period_end": "2025-12-31"},
            "annual_fallback",
            "lower",
        ),
        (
            {"interest_income_basis": "net_investment_income", "interest_income_tags": "us-gaap:InvestmentIncomeNet"},
            "net_investment_income",
            "lower",
        ),
        (
            {"interest_income_basis": "upper_bound_no_disclosure", "interest_income": None,
             "interest_income_bound_base": "cash_and_securities", "interest_income_upper_bound": 9.0},
            "upper_bound_cash",
            "lower",
        ),
        (
            {"interest_income_basis": "upper_bound_no_disclosure", "interest_income": None,
             "interest_income_bound_base": "total_assets", "interest_income_upper_bound": 9.0},
            "upper_bound_total_assets",
            "lower",
        ),
        ({"interest_income_basis": None, "interest_income": None}, "none", "none"),
    ],
)
def test_every_basis_but_disclosed_gross_interest_is_lower_confidence(changes, key, confidence):
    basis = _interest_basis(_basis_row(**changes))
    assert (basis["key"], basis["confidence"]) == (key, confidence)

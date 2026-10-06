"""Publish gates: a run that fails one is not exported or deployed. The pure rules are tested on
plain facts; the integration tests run the real screen and export against the fixture world, and
nothing here touches the network."""

import json
from datetime import date
from pathlib import Path

import pytest

from halal_heatmap.cli import main
from halal_heatmap.config import parse_config
from halal_heatmap.gates import check_prices, check_run, check_warnings
from halal_heatmap.runner import run_screen, update
from halal_heatmap.sources import SourceError
from halal_heatmap.store import Store
from test_export import ALL, OVR, Prices, world  # noqa: F401  (fixtures and fixture helpers)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def publish(raw):
    return parse_config(raw).publish


def facts(**changes):
    """A clean full run of 500 companies, then the changes under test."""
    base = {
        "as_of": "2026-08-03",
        "constituents": 500,
        "previous_constituents": 500,
        "errors": [],
        "lagging": [],
        "source_failures": {},
        "status_changes": 0,
        "screened": 500,
    }
    base.update(changes)
    return base


def test_a_clean_run_passes(publish):
    assert check_run(facts(), publish) == []


def test_an_empty_constituent_list_fails(publish):
    failures = check_run(facts(constituents=0), publish)
    assert failures == ["no constituents were read"]


# --- error notes -------------------------------------------------------------------------------


def test_error_share_at_the_limit_passes(publish):
    errors = [f"T{i}: filing: boom" for i in range(10)]  # 10 of 500 is exactly 2%
    assert check_run(facts(errors=errors), publish) == []


def test_error_share_above_the_limit_fails(publish):
    errors = [f"T{i}: filing: boom" for i in range(11)]
    failures = check_run(facts(errors=errors), publish)
    assert len(failures) == 1 and "11 source errors" in failures[0]


def test_lagging_filings_are_not_errors(publish):
    # Sixty filings EDGAR lists and companyfacts does not serve yet: expected for a few days.
    assert check_run(facts(lagging=[f"T{i}" for i in range(60)]), publish) == []


# --- price failures (strict) -------------------------------------------------------------------


def test_one_price_failure_fails_even_when_the_error_share_is_fine(publish):
    failures = check_run(
        facts(errors=["T1: prices: no prices returned"], source_failures={"prices": 1}), publish
    )
    assert failures == ["closes could not be read for 1 companies, above the limit of 0"]


def test_no_price_failures_is_the_only_passing_price_count(publish):
    assert check_run(facts(source_failures={"prices": 0, "filing": 3}), publish) == []


def test_daily_price_gate_fails_on_any_missing_close(publish):
    assert check_prices(0, publish) == []
    assert check_prices(1, publish) and "daily price change" in check_prices(1, publish)[0]


# --- constituent list ---------------------------------------------------------------------------


def test_a_drop_at_the_limit_passes_and_past_it_fails(publish):
    assert check_run(facts(constituents=495), publish) == []  # five left: at the limit
    failures = check_run(facts(constituents=494), publish)  # six left
    assert failures == ["constituent list fell from 500 to 494, by more than the limit of 5"]


def test_growth_and_the_first_snapshot_are_not_drops(publish):
    assert check_run(facts(constituents=510, previous_constituents=500), publish) == []
    assert check_run(facts(previous_constituents=None), publish) == []


# --- status swings ------------------------------------------------------------------------------


def test_status_changes_at_the_limit_pass(publish):
    assert check_run(facts(status_changes=25), publish) == []  # 25 of 500 is 5%


def test_status_changes_above_the_limit_fail_and_name_the_way_out(publish):
    failures = check_run(facts(status_changes=26), publish)
    assert len(failures) == 1
    assert "26 status changes" in failures[0] and "accept option" in failures[0]


def test_a_daily_run_is_measured_against_the_list_not_what_was_screened(publish):
    # Two changes among 3 screened companies would be 67%; against 500 companies it is 0.4%.
    assert check_run(facts(screened=3, status_changes=2), publish) == []


def test_accepting_status_changes_lifts_only_that_gate(publish):
    swings = facts(status_changes=60)
    assert check_run(swings, publish, accept_status_changes=True) == []
    # Accepting never relaxes the other gates.
    mixed = facts(status_changes=60, errors=[f"T{i}: filing: boom" for i in range(11)])
    failures = check_run(mixed, publish, accept_status_changes=True)
    assert len(failures) == 1 and "source errors" in failures[0]
    priced = facts(status_changes=60, source_failures={"prices": 1})
    failures = check_run(priced, publish, accept_status_changes=True)
    assert len(failures) == 1 and "closes could not be read" in failures[0]
    dropped = facts(status_changes=60, constituents=480)
    failures = check_run(dropped, publish, accept_status_changes=True)
    assert len(failures) == 1 and "constituent list fell" in failures[0]


def test_every_failed_gate_is_reported_together(publish):
    failures = check_run(
        facts(
            constituents=480,
            errors=[f"T{i}: filing: boom" for i in range(40)],
            source_failures={"prices": 2},
            status_changes=100,
        ),
        publish,
    )
    assert len(failures) == 4


# --- the run's facts, from a real screen ---------------------------------------------------------


class FlakyPrices:
    """The fixture world's prices, with some tickers failing as yfinance does when throttled."""

    def __init__(self, inner, failing):
        self.inner, self.failing = inner, set(failing)

    def history(self, ticker, start, end):
        if ticker in self.failing:
            raise SourceError(f"no prices for {ticker}")
        return self.inner.history(ticker, start, end)


def test_a_real_run_reports_its_facts_and_a_failed_close(world, raw):  # noqa: F811
    cfg = parse_config(raw)
    store = Store(":memory:")
    constituents = world.constituents(*ALL)
    flaky = FlakyPrices(world, failing={"DEBT"})
    summary = run_screen(date(2026, 8, 3), cfg, constituents, world, flaky, OVR, store)
    got = summary.facts()
    assert got["constituents"] == 4 and got["previous_constituents"] is None
    assert got["source_failures"].get("prices") == 1
    assert any(error.startswith("DEBT: prices") for error in got["errors"])
    assert check_run(got, cfg.publish) != []  # the failed close is enough to stop the publish
    store.close()


def test_a_removal_is_counted_as_a_drop_and_as_an_index_event(world, raw):  # noqa: F811
    cfg = parse_config(raw)
    store = Store(":memory:")
    run_screen(date(2026, 8, 3), cfg, world.constituents(*ALL), world, world, OVR, store)
    smaller = world.constituents("DEBT", "NEAR", "OVR")  # PASS leaves the list
    summary = run_screen(date(2026, 8, 4), cfg, smaller, world, world, OVR, store)
    got = summary.facts()
    assert got["previous_constituents"] == 4 and got["constituents"] == 3
    assert got["index_events"] == 1
    store.close()


def test_update_facts_describe_a_run_that_stored_nothing(world, raw):  # noqa: F811
    cfg = parse_config(raw)
    store = Store(":memory:")
    constituents = world.constituents(*ALL)
    update(date(2026, 8, 3), cfg, constituents, world, world, OVR, store)  # first full run
    summary = update(date(2026, 8, 3), cfg, constituents, world, world, OVR, store)  # same day again
    got = summary.facts()
    assert got["stored"] is False and got["run_id"] is None
    assert check_run(got, cfg.publish) == []
    store.close()


# --- the commands ----------------------------------------------------------------------------------


def test_check_run_exits_nonzero_and_says_why(tmp_path, capsys):
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps(facts(source_failures={"prices": 1}, errors=["T: prices: x"])))
    code = main(["check-run", str(summary)])
    assert code == 1
    assert "gate failed: closes could not be read for 1 companies" in capsys.readouterr().err


def test_check_run_passes_a_clean_run(tmp_path, capsys):
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps(facts()))
    assert main(["check-run", str(summary)]) == 0
    assert "gates passed" in capsys.readouterr().out


def test_check_run_can_accept_status_changes(tmp_path):
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps(facts(status_changes=60)))
    assert main(["check-run", str(summary)]) == 1
    assert main(["check-run", str(summary), "--accept-status-changes"]) == 0


def export_args(db, out):
    return ["export", "--db", str(db), "--out", str(out), "--config", str(ROOT / "config.yaml"),
            "--overrides", str(ROOT / "overrides.yaml")]


def test_export_writes_nothing_when_a_close_is_missing(world, raw, tmp_path, monkeypatch, capsys):  # noqa: F811
    db = tmp_path / "screens.db"
    out = tmp_path / "web-data"
    cfg = parse_config(raw)
    store = Store(str(db))
    run_screen(date(2026, 8, 3), cfg, world.constituents(*ALL), world, world, OVR, store)
    store.close()
    monkeypatch.setattr("halal_heatmap.cli._prices_from", lambda cfg: FlakyPrices(Prices({}), failing={"DEBT"}))
    assert main(export_args(db, out)) == 1
    assert "nothing written" in capsys.readouterr().err
    assert not out.exists()


def test_export_writes_the_site_when_every_close_is_read(world, raw, tmp_path, monkeypatch):  # noqa: F811
    db = tmp_path / "screens.db"
    out = tmp_path / "web-data"
    cfg = parse_config(raw)
    store = Store(str(db))
    run_screen(date(2026, 8, 3), cfg, world.constituents(*ALL), world, world, OVR, store)
    store.close()
    monkeypatch.setattr("halal_heatmap.cli._prices_from", lambda cfg: Prices({}))
    assert main(export_args(db, out)) == 0
    assert (out / "screens.json").exists() and (out / "meta.json").exists()



# --- warnings: noted, never a reason to stop ---------------------------------------------------


def test_lagging_share_at_the_warning_level_is_quiet(publish):
    assert check_warnings(facts(lagging=[f"T{i}" for i in range(50)]), publish) == []  # 10% exactly


def test_lagging_share_above_the_warning_level_warns_without_failing(publish):
    lagging = facts(lagging=[f"T{i}" for i in range(51)])
    warnings = check_warnings(lagging, publish)
    assert len(warnings) == 1 and "51 of 500 companies (10.2%)" in warnings[0]
    assert check_run(lagging, publish) == []  # a warning does not stop the publish


def test_check_run_prints_the_warning_and_still_passes(tmp_path, capsys):
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps(facts(lagging=[f"T{i}" for i in range(80)])))
    assert main(["check-run", str(summary)]) == 0
    assert "warning: 80 of 500 companies" in capsys.readouterr().err

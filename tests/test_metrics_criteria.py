"""General metrics and configurable criteria: arithmetic, missing-data handling, profile validation."""

import json
from pathlib import Path

import pytest

from equity_screener.criteria import FAIL, INSUFFICIENT, PASS, ProfileError, evaluate, load_profiles, parse_profile
from equity_screener.metrics import METRIC_KEYS, compute_metrics, metric

FIGURES = {
    "market_cap": 1000.0,
    "revenue": 500.0,
    "gross_profit": 250.0,
    "operating_income": 100.0,
    "net_income": 60.0,
    "stockholders_equity": 400.0,
    "total_assets": 900.0,
    "total_liabilities": 500.0,
    "debt": 200.0,
    "cash_and_securities": 150.0,
    "interest_expense": 10.0,
    "interest_income": 5.0,
    "operating_cash_flow": 90.0,
    "capital_expenditures": 30.0,
}


def test_metric_arithmetic():
    m = compute_metrics(FIGURES)
    assert m["gross_margin"] == pytest.approx(0.5)
    assert m["operating_margin"] == pytest.approx(0.2)
    assert m["net_margin"] == pytest.approx(0.12)
    assert m["roe"] == pytest.approx(0.15)
    assert m["roa"] == pytest.approx(60 / 900)
    assert m["debt_to_equity"] == pytest.approx(0.5)
    assert m["debt_to_market_cap"] == pytest.approx(0.2)
    assert m["interest_coverage"] == pytest.approx(10.0)
    assert m["fcf_margin"] == pytest.approx(60 / 500)
    assert m["fcf_yield"] == pytest.approx(0.06)
    assert m["price_to_earnings"] == pytest.approx(1000 / 60)
    assert m["price_to_book"] == pytest.approx(2.5)
    assert m["interest_income_to_revenue"] == pytest.approx(0.01)


def test_every_metric_has_a_formula_and_a_description():
    assert set(compute_metrics(FIGURES)) == set(METRIC_KEYS)
    assert all(metric(k).description for k in METRIC_KEYS)


def test_missing_inputs_give_none_not_zero():
    m = compute_metrics({"market_cap": 1000.0, "revenue": 500.0})
    assert m["net_margin"] is None and m["roe"] is None and m["fcf_yield"] is None


@pytest.mark.parametrize("equity", [0.0, -50.0])
def test_non_positive_equity_leaves_equity_ratios_undefined(equity):
    m = compute_metrics({**FIGURES, "stockholders_equity": equity})
    assert m["roe"] is None and m["debt_to_equity"] is None and m["price_to_book"] is None


def test_non_positive_earnings_leave_pe_undefined_but_keep_yield():
    m = compute_metrics({**FIGURES, "net_income": -10.0})
    assert m["price_to_earnings"] is None
    assert m["earnings_yield"] < 0


def test_capex_sign_does_not_matter():
    assert compute_metrics({**FIGURES, "capital_expenditures": -30.0})["fcf_yield"] == pytest.approx(0.06)


def test_zero_interest_expense_leaves_coverage_undefined():
    assert compute_metrics({**FIGURES, "interest_expense": 0.0})["interest_coverage"] is None


PROFILE = {
    "name": "t",
    "criteria": [
        {"metric": "roe", "op": ">", "value": 0.1},
        {"metric": "debt_to_equity", "op": "<=", "value": 0.5},
    ],
}


def test_evaluate_pass_fail_and_boundary():
    p = parse_profile(PROFILE, "t")
    assert evaluate(p, {"roe": 0.15, "debt_to_equity": 0.5}).status == PASS  # <= includes the limit
    assert evaluate(p, {"roe": 0.10, "debt_to_equity": 0.1}).status == FAIL  # > excludes the limit


def test_missing_metric_is_insufficient_but_a_failure_wins():
    p = parse_profile(PROFILE, "t")
    assert evaluate(p, {"roe": 0.2, "debt_to_equity": None}).status == INSUFFICIENT
    assert evaluate(p, {"roe": 0.01, "debt_to_equity": None}).status == FAIL


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "t", "criteria": [{"metric": "nope", "op": ">", "value": 1}]},
        {"name": "t", "criteria": [{"metric": "roe", "op": "~", "value": 1}]},
        {"name": "t", "criteria": [{"metric": "roe", "op": ">", "value": "high"}]},
        {"name": "t", "criteria": []},
        {"criteria": [{"metric": "roe", "op": ">", "value": 1}]},
    ],
)
def test_bad_profiles_are_rejected(bad):
    with pytest.raises(ProfileError):
        parse_profile(bad, "t")


def test_shipped_profiles_load():
    profiles = load_profiles(Path(__file__).parent.parent / "profiles")
    assert {"quality", "value", "balance-sheet-strength", "conservative-capital-structure"} <= set(profiles)


def test_shared_cases_agree_with_the_javascript_implementation():
    """web/tool/criteria.js is checked against these same cases by tests/criteria_cases.json."""
    cases = json.loads((Path(__file__).parent / "criteria_cases.json").read_text())
    for case in cases:
        got = evaluate(parse_profile(case["profile"], "case"), case["metrics"]).status
        assert got == case["expected"], case["name"]

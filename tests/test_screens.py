"""Financial ratios, the zero-debt rule and the business screen."""

import pytest

from halal_heatmap.config import DebtPlausibility, NearThreshold, Threshold, ZeroDebt
from halal_heatmap.screen.business import screen_business
from halal_heatmap.screen.financial import DebtResolution, check_debt_plausibility, evaluate_ratio, resolve_debt

NEAR = NearThreshold("relative", 0.10)
ZERO = ZeroDebt(0.001, 460)
PLAUSIBLE = DebtPlausibility(0.25)


@pytest.mark.parametrize(
    "numerator, operator, passed",
    [
        (299.9, "<", True),
        (300.0, "<", False),  # exactly at the limit fails under strict <
        (300.1, "<", False),
        (300.0, "<=", True),  # and passes under <=
        (300.1, "<=", False),
    ],
)
def test_threshold_boundary(numerator, operator, passed):
    result = evaluate_ratio("debt", numerator, 1000.0, Threshold(0.30, operator), NEAR)
    assert result.passed is passed


def test_headroom_values():
    result = evaluate_ratio("debt", 240.0, 1000.0, Threshold(0.30, "<"), NEAR)
    assert result.ratio == pytest.approx(0.24)
    assert result.headroom == pytest.approx(0.06)
    assert result.headroom_rel == pytest.approx(0.20)
    assert result.near_threshold is False


@pytest.mark.parametrize(
    "numerator, near",
    [(269.0, False), (270.0, True), (299.0, True), (300.0, False), (350.0, False)],
)
def test_near_threshold_relative_margin(numerator, near):
    # Within 10% of a 30% limit means a passing ratio at or above 27%.
    result = evaluate_ratio("debt", numerator, 1000.0, Threshold(0.30, "<"), NEAR)
    assert result.near_threshold is near


def test_near_threshold_absolute_margin():
    absolute = NearThreshold("absolute", 0.10)
    assert evaluate_ratio("debt", 199.0, 1000.0, Threshold(0.30, "<"), absolute).near_threshold is False
    assert evaluate_ratio("debt", 201.0, 1000.0, Threshold(0.30, "<"), absolute).near_threshold is True


@pytest.mark.parametrize("numerator, denominator", [(None, 1000.0), (10.0, None), (10.0, 0.0), (10.0, -5.0)])
def test_ratio_is_not_computed_without_inputs(numerator, denominator):
    assert evaluate_ratio("debt", numerator, denominator, Threshold(0.30, "<"), NEAR) is None


def test_reported_debt_is_used_as_is():
    assert resolve_debt(50.0, None, None, True, None, ZERO).value == 50.0


def test_zero_debt_when_no_interest_expense_reported():
    result = resolve_debt(None, 400.0, None, False, 1000.0, ZERO)
    assert (result.value, result.assumed_zero) == (0, True)
    assert "treated as 0" in result.note


def test_zero_debt_when_interest_expense_is_tiny():
    result = resolve_debt(None, 400.0, 0.5, True, 1000.0, ZERO)  # 0.05% of revenue
    assert (result.value, result.assumed_zero) == (0, True)


@pytest.mark.parametrize(
    "total_liabilities, interest_expense, reported, revenue",
    [
        (None, None, False, 1000.0),  # no total liabilities
        (400.0, 1.0, True, 1000.0),  # interest expense exactly at the limit is not below it
        (400.0, 20.0, True, 1000.0),  # material interest expense
        (400.0, None, True, 1000.0),  # interest expense reported but not sizeable
        (400.0, 0.5, True, None),  # no revenue to size it against
    ],
)
def test_missing_debt_stays_missing_when_any_condition_fails(total_liabilities, interest_expense, reported, revenue):
    result = resolve_debt(None, total_liabilities, interest_expense, reported, revenue, ZERO)
    assert result.value is None and result.assumed_zero is False


@pytest.mark.parametrize(
    "sic, gics, outcome, category",
    [
        ("3571", "Technology Hardware, Storage & Peripherals", "pass", None),
        ("6021", "Diversified Banks", "fail", "conventional_banking"),
        ("2111", "Tobacco", "fail", "tobacco"),
        ("3571", "Casinos & Gaming", "fail", "gambling"),  # either classification is enough to fail
        ("2082", "Soft Drinks & Non-alcoholic Beverages", "fail", "alcohol"),
        ("3760", "Aerospace & Defense", "needs_review", "weapons"),
        ("5812", "Restaurants", "needs_review", "alcohol"),
        ("7011", "Hotels, Resorts & Cruise Lines", "needs_review", "alcohol"),
        ("2011", "Packaged Foods & Meats", "needs_review", "pork"),
        ("7372", "Movies & Entertainment", "needs_review", "adult_entertainment"),
        ("6324", "Managed Health Care", "needs_review", "conventional_insurance"),
        ("3523", "Agricultural & Farm Machinery", "needs_review", "captive_finance"),
        ("3711", "Automobile Manufacturers", "needs_review", "captive_finance"),
        ("3531", "Construction Machinery & Heavy Transportation Equipment", "needs_review", "captive_finance"),
        ("7990", "Casinos & Gaming", "fail", "gambling"),  # fail outranks review
    ],
)
def test_business_screen(cfg, sic, gics, outcome, category):
    result = screen_business(sic, gics, cfg.business)
    assert (result.outcome, result.category) == (outcome, category)


@pytest.mark.parametrize("sic, gics", [(None, "Semiconductors"), ("", "Semiconductors"), ("3674", None), ("3674", " ")])
def test_missing_classification_never_passes(cfg, sic, gics):
    assert screen_business(sic, gics, cfg.business).outcome == "insufficient_data"


def test_fail_rule_still_applies_with_one_classification_missing(cfg):
    assert screen_business(None, "Tobacco", cfg.business).outcome == "fail"


def reported(value):
    return DebtResolution(value, False, "")


@pytest.mark.parametrize(
    "debt, interest_expense, implausible",
    [
        (1000.0, 50.0, False),  # 5%
        (1000.0, 250.0, False),  # exactly at the limit is still plausible
        (1000.0, 251.0, True),
        (133.0, 849.0, True),  # Marriott before the tag fix: 638%
    ],
)
def test_debt_plausibility_limit(debt, interest_expense, implausible):
    check = check_debt_plausibility(reported(debt), interest_expense, 10000.0, PLAUSIBLE, ZERO)
    assert check.implausible is implausible
    assert check.implied_rate == pytest.approx(interest_expense / debt)


def test_debt_tagged_zero_with_real_interest_expense_is_implausible():
    assert check_debt_plausibility(reported(0.0), 50.0, 10000.0, PLAUSIBLE, ZERO).implausible
    assert not check_debt_plausibility(reported(0.0), 5.0, 10000.0, PLAUSIBLE, ZERO).implausible  # 0.05% of revenue
    assert check_debt_plausibility(reported(0.0), 5.0, None, PLAUSIBLE, ZERO).implausible


@pytest.mark.parametrize(
    "debt, interest_expense",
    [
        (DebtResolution(None, False, "missing"), 50.0),  # nothing to check
        (DebtResolution(0, True, "assumed zero"), 50.0),  # the zero-debt rule already judged this
        (DebtResolution(100.0, False, ""), None),  # no interest expense to compare with
        (DebtResolution(100.0, False, ""), 0.0),
    ],
)
def test_debt_plausibility_is_not_judged_without_both_figures(debt, interest_expense):
    check = check_debt_plausibility(debt, interest_expense, 10000.0, PLAUSIBLE, ZERO)
    assert (check.implausible, check.implied_rate) == (False, None)

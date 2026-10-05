import dataclasses
from datetime import date, timedelta

import pytest

from conftest import SCREEN_DATE, caps, make_inputs
from halal_heatmap.config import DENOMINATORS, ConfigError, TagRef, parse_config
from halal_heatmap.facts import Fact
from halal_heatmap.overrides import Override
from halal_heatmap.screen.engine import result_to_record, screen
from halal_heatmap.store import RESULT_COLUMNS

REVIEW = dict(sic="5812", gics_sub_industry="Restaurants")
BANK = dict(sic="6021", gics_sub_industry="Diversified Banks")


def override(decision="pass", days_ago=30, extra=0):
    return Override("TEST", decision, "segment note reviewed", "YL", SCREEN_DATE - timedelta(days=days_ago), extra)


def test_clean_company_passes(cfg):
    result = screen(make_inputs(), cfg)
    assert result.status == "pass"
    assert result.driving == "avg_12m"
    assert not result.denominator_disagreement and not result.near_threshold


@pytest.mark.parametrize(
    "changes, fragment",
    [
        (dict(debt=300.0), "debt / market cap (avg_12m) 30.00% is not < 30.00%"),
        (dict(cash_and_securities=450.0), "cash / market cap"),
        (dict(interest_income=50.0), "impure_income / revenue 5.00% is not < 5.00%"),
    ],
)
def test_each_ratio_can_fail(cfg, changes, fragment):
    result = screen(make_inputs(**changes), cfg)
    assert result.status == "fail"
    assert fragment in result.reason


def test_operator_is_configurable_per_screen(raw):
    raw["screens"]["debt"]["operator"] = "<="
    cfg = parse_config(raw)
    assert screen(make_inputs(debt=300.0), cfg).status == "pass"
    assert screen(make_inputs(cash_and_securities=300.0), cfg).status == "fail"


@pytest.mark.parametrize(
    "changes, fragment",
    [
        (dict(cash_and_securities=None), "cash_and_securities"),
        (dict(interest_income=None, cash_and_securities=None), "interest_income"),
        (dict(interest_income=None, cash_and_securities=1000.0, market_caps=caps(5000, 5000, 5000)), "upper bound"),
        (dict(revenue=None), "revenue"),
        (dict(revenue=0.0), "revenue is not positive"),
        (dict(filing=None), "no balance sheet filing"),
        (dict(debt=None), "debt"),  # interest expense is material, so no zero-debt assumption
        (dict(debt=None, total_liabilities=None, interest_expense=None, interest_expense_reported=False), "debt"),
        (dict(sic=None), "classification missing: sic"),
        (dict(gics_sub_industry=None), "classification missing: gics_sub_industry"),
        (dict(market_caps=caps(avg_12m=None)), "market cap avg_12m"),
    ],
)
def test_any_missing_required_input_is_insufficient_data(cfg, changes, fragment):
    result = screen(make_inputs(**changes), cfg)
    assert result.status == "insufficient_data"
    assert fragment in result.reason


def test_an_old_balance_sheet_is_fine_until_the_next_report_is_overdue(cfg):
    """Staleness follows the company's own cadence, not a flat age: a 200+ day old balance sheet passes
    while its next periodic report is not yet due."""
    old = dataclasses.replace(make_inputs().filing, period_end=SCREEN_DATE - timedelta(days=201))
    assert screen(make_inputs(filing=old, balance_sheet_due=SCREEN_DATE), cfg).status == "pass"
    result = screen(make_inputs(filing=old, balance_sheet_due=SCREEN_DATE - timedelta(days=1)), cfg)
    assert result.status == "insufficient_data" and "no periodic filing listed" in result.reason


def test_missing_debt_treated_as_zero_only_when_corroborated(cfg):
    result = screen(make_inputs(debt=None, interest_expense=None, interest_expense_reported=False), cfg)
    assert result.status == "pass"
    record = result_to_record(result, cfg)
    assert record["debt"] == 0 and record["debt_assumed_zero"] == 1
    assert "treated as 0" in record["debt_note"]


def test_precedence_business_fail_beats_everything(cfg):
    result = screen(make_inputs(**BANK, revenue=None, debt=900.0), cfg)
    assert result.status == "fail"
    assert "business screen" in result.reason


def test_precedence_missing_data_beats_ratio_failure(cfg):
    assert screen(make_inputs(debt=900.0, revenue=None), cfg).status == "insufficient_data"


def test_precedence_ratio_failure_beats_needs_review(cfg):
    assert screen(make_inputs(**REVIEW, debt=900.0), cfg).status == "fail"


def test_precedence_missing_data_beats_needs_review(cfg):
    assert screen(make_inputs(**REVIEW, revenue=None), cfg).status == "insufficient_data"


def test_borderline_business_is_needs_review_not_pass(cfg):
    result = screen(make_inputs(**REVIEW), cfg)
    assert result.status == "needs_review"
    assert "Restaurants" in result.reason


def test_configured_denominator_drives_the_verdict(raw, cfg):
    # Debt 250: 31% of the 12-month average, 25% of spot, 20% of the 36-month average.
    inputs = make_inputs(debt=250.0, market_caps=caps(spot=1000.0, avg_12m=800.0, avg_36m=1250.0))
    result = screen(inputs, cfg)
    assert result.status == "fail" and result.driving == "avg_12m"
    assert {name: result.outcomes[name].status for name in DENOMINATORS} == {
        "spot": "pass",
        "avg_12m": "fail",
        "avg_36m": "pass",
    }
    assert result.denominator_disagreement
    assert "[denominator: avg_12m]" in result.reason

    for name, expected in (("spot", "pass"), ("avg_36m", "pass")):
        raw["market_cap"]["driving"] = name
        other = screen(inputs, parse_config(raw))
        assert (other.status, other.driving) == (expected, name)
        assert other.denominator_disagreement


def test_all_three_denominators_are_recorded(cfg):
    inputs = make_inputs(debt=250.0, market_caps=caps(spot=1000.0, avg_12m=800.0, avg_36m=1250.0))
    record = result_to_record(screen(inputs, cfg), cfg)
    assert record["mcap_spot"] == 1000.0 and record["mcap_avg_12m"] == 800.0 and record["mcap_avg_36m"] == 1250.0
    assert record["debt_ratio_spot"] == pytest.approx(0.25)
    assert record["debt_ratio_avg_12m"] == pytest.approx(0.3125)
    assert record["debt_ratio_avg_36m"] == pytest.approx(0.20)
    assert record["driving_denominator"] == "avg_12m"
    assert record["denominator_disagreement"] == 1


def test_unavailable_non_driving_denominator_is_null_and_not_a_disagreement(cfg):
    result = screen(make_inputs(market_caps=caps(avg_36m=None)), cfg)
    record = result_to_record(result, cfg)
    assert result.status == "pass" and not result.denominator_disagreement
    assert record["status_avg_36m"] is None and record["mcap_avg_36m"] is None
    assert record["status_spot"] == "pass"


def test_unavailable_driving_denominator_does_not_fall_back(cfg):
    result = screen(make_inputs(market_caps=caps(avg_12m=None)), cfg)
    record = result_to_record(result, cfg)
    assert result.status == "insufficient_data"
    assert record["status_avg_12m"] == "insufficient_data" and record["status_spot"] == "pass"


def test_headroom_and_near_threshold_are_recorded(cfg):
    record = result_to_record(screen(make_inputs(debt=280.0, interest_income=10.0), cfg), cfg)
    assert record["status"] == "pass"
    assert record["debt_headroom"] == pytest.approx(0.02)
    assert record["debt_headroom_rel"] == pytest.approx(0.02 / 0.30)
    assert record["debt_near"] == 1 and record["cash_near"] == 0 and record["impure_income_near"] == 0
    assert record["near_threshold"] == 1
    assert record["impure_income_headroom"] == pytest.approx(0.04)
    assert (record["debt_threshold"], record["debt_operator"]) == (0.30, "<")


@pytest.mark.parametrize(
    "inputs",
    [
        dict(debt=280.0, interest_income=10.0),  # pass, near the debt limit
        dict(debt=320.0),  # fail
        dict(debt=280.0, **REVIEW),  # needs_review
        dict(debt=280.0, revenue=None),  # insufficient_data
        dict(debt=280.0, **BANK),  # business fail
    ],
)
def test_near_threshold_settings_only_move_the_flags(raw, cfg, inputs):
    """Why near_threshold is left out of the config hash: no margin changes a status."""
    flags = ("near_threshold", "debt_near", "cash_near", "impure_income_near")
    base = result_to_record(screen(make_inputs(**inputs), cfg), cfg)
    for mode, margin in (("relative", 0.001), ("relative", 0.99), ("absolute", 0.001), ("absolute", 0.29)):
        raw["near_threshold"] = {"mode": mode, "margin": margin}
        other = parse_config(raw)
        assert other.hash == cfg.hash
        record = result_to_record(screen(make_inputs(**inputs), other), other)
        assert {k: v for k, v in record.items() if k not in flags} == {k: v for k, v in base.items() if k not in flags}


def test_active_override_resolves_needs_review(cfg):
    passed = screen(make_inputs(**REVIEW, override=override("pass")), cfg)
    assert passed.status == "pass" and "manual override by YL" in passed.reason
    assert result_to_record(passed, cfg)["override_id"] == passed.inputs.override.id
    assert screen(make_inputs(**REVIEW, override=override("fail")), cfg).status == "fail"


def test_override_expires(cfg):
    last_day = screen(make_inputs(**REVIEW, override=override(days_ago=365)), cfg)
    assert last_day.status == "pass"
    expired = screen(make_inputs(**REVIEW, override=override(days_ago=366)), cfg)
    assert expired.status == "needs_review" and "override expired" in expired.reason
    assert expired.override_state == "expired"


def test_override_only_resolves_needs_review(cfg):
    assert screen(make_inputs(**BANK, override=override("pass")), cfg).status == "fail"
    assert screen(make_inputs(**REVIEW, debt=900.0, override=override("pass")), cfg).status == "fail"
    assert screen(make_inputs(**REVIEW, revenue=None, override=override("pass")), cfg).status == "insufficient_data"
    untouched = screen(make_inputs(override=override("fail")), cfg)
    assert untouched.status == "pass" and untouched.override_state == "none"


def test_override_dated_after_the_screen_is_ignored(cfg):
    future = Override("TEST", "pass", "later review", "YL", SCREEN_DATE + timedelta(days=1))
    assert screen(make_inputs(**REVIEW, override=future), cfg).status == "needs_review"


def test_override_impure_income_counts_toward_the_ratio(cfg):
    result = screen(make_inputs(**REVIEW, override=override("pass", extra=60.0)), cfg)
    assert result.status == "fail" and "impure_income" in result.reason
    assert result_to_record(result, cfg)["override_impure_income"] == 60.0


def test_record_matches_storage_schema(cfg):
    record = result_to_record(screen(make_inputs(), cfg), cfg)
    assert set(record) == set(RESULT_COLUMNS)
    assert record["screen_date"] == SCREEN_DATE.isoformat()
    assert record["filing_date"] == date(2026, 7, 30).isoformat()


IMPLAUSIBLE = dict(debt=10.0, interest_expense=50.0)  # 500% implied rate


def test_implausible_debt_turns_pass_into_insufficient_data(cfg):
    result = screen(make_inputs(**IMPLAUSIBLE), cfg)
    record = result_to_record(result, cfg)
    assert result.status == "insufficient_data" and "debt not reliable" in result.reason
    assert record["debt_implausible"] == 1 and record["debt_implied_rate"] == pytest.approx(5.0)
    assert record["debt"] == 10.0  # the tagged figure is still recorded


def test_implausible_debt_also_blocks_an_override_pass(cfg):
    result = screen(make_inputs(**REVIEW, **IMPLAUSIBLE, override=override("pass")), cfg)
    assert result.status == "insufficient_data"


@pytest.mark.parametrize(
    "changes, status",
    [
        (BANK, "fail"),
        (dict(cash_and_securities=450.0), "fail"),
        (REVIEW, "needs_review"),
        (dict(revenue=None), "insufficient_data"),
    ],
)
def test_implausible_debt_changes_no_status_other_than_pass(cfg, changes, status):
    with_flag = screen(make_inputs(**{**IMPLAUSIBLE, **changes}), cfg)
    without = screen(make_inputs(**changes), cfg)
    assert with_flag.status == without.status == status
    assert with_flag.reason == without.reason
    assert result_to_record(with_flag, cfg)["debt_implausible"] == 1


def test_plausible_debt_is_recorded_with_its_implied_rate(cfg):
    record = result_to_record(screen(make_inputs(), cfg), cfg)  # 5 / 100
    assert record["status"] == "pass" and record["debt_implausible"] == 0
    assert record["debt_implied_rate"] == pytest.approx(0.05)


def test_annual_interest_income_is_flagged_in_the_record(cfg):
    annual = Fact(
        TagRef.parse("InvestmentIncomeInterest"),
        "USD",
        1.0,
        date(2025, 1, 1),
        date(2025, 12, 31),
        "k-2025",
        "10-K",
        date(2026, 2, 10),
    )
    result = screen(make_inputs(interest_income_fallback=annual), cfg)
    record = result_to_record(result, cfg)
    assert result.status == "pass" and "annual figure to 2025-12-31, filed 2026-02-10" in result.reason
    assert record["interest_income_basis"] == "annual_fallback"
    assert (record["interest_income_period_end"], record["interest_income_filing_date"]) == ("2025-12-31", "2026-02-10")
    assert record["interest_income_accession"] == "k-2025"
    plain = result_to_record(screen(make_inputs(), cfg), cfg)
    assert plain["interest_income_basis"] == "ttm" and plain["interest_income_filing_date"] is None


def test_investments_assumed_zero_is_recorded(cfg):
    note = "investment tags absent, treated as 0"
    record = result_to_record(screen(make_inputs(investments_assumed_zero=True, cash_note=note), cfg), cfg)
    assert record["status"] == "pass"
    assert record["investments_assumed_zero"] == 1 and record["cash_note"] == note
    assert result_to_record(screen(make_inputs(), cfg), cfg)["investments_assumed_zero"] == 0


# Upper bound on undisclosed interest income: cash and securities x the configured yield ceiling.


def undisclosed(cash, **changes):
    """No interest income disclosed; market cap 10,000 so the cash ratio never interferes."""
    return make_inputs(
        interest_income=None,
        cash_and_securities=cash,
        market_caps=caps(10000.0, 10000.0, 10000.0),
        notes={"interest_income": "no usable tag for: interest_income"},
        **changes,
    )


def test_upper_bound_supports_a_pass_and_is_labelled(cfg):
    result = screen(undisclosed(400.0), cfg)  # 400 x 5% = 20, which is 2% of revenue 1000
    assert result.status == "pass"
    assert "upper bound" in result.reason and "no interest income disclosed" in result.reason
    record = result_to_record(result, cfg)
    assert record["interest_income_basis"] == "upper_bound_no_disclosure"
    assert record["interest_income"] is None  # nothing was disclosed, and the record says so
    assert record["interest_income_upper_bound"] == record["impure_income"] == pytest.approx(20.0)
    assert record["interest_income_yield_ceiling"] == 0.05
    assert record["impure_ratio"] == pytest.approx(0.02)
    assert record["impure_income_headroom"] == pytest.approx(0.03)


@pytest.mark.parametrize(
    "cash, status",
    [(999.0, "pass"), (1000.0, "insufficient_data"), (1001.0, "insufficient_data"), (2500.0, "insufficient_data")],
)
def test_upper_bound_boundary_follows_the_impure_income_limit(cfg, cash, status):
    # 1000 x 5% = 50 is exactly 5% of revenue, which is not < 5%.
    result = screen(undisclosed(cash), cfg)
    assert result.status == status
    if status != "pass":
        assert "interest_income" in result.reason and "upper bound" in result.reason
        assert "no usable tag" in result.reason


def test_upper_bound_never_produces_a_fail(cfg):
    # A bound above the limit proves nothing about the true figure.
    result = screen(undisclosed(2500.0), cfg)
    assert result.status == "insufficient_data"
    assert result_to_record(result, cfg)["impure_ratio"] == pytest.approx(0.125)


def test_upper_bound_follows_the_configured_operator_and_ceiling(raw):
    raw["screens"]["impure_income"]["operator"] = "<="
    assert screen(undisclosed(1000.0), parse_config(raw)).status == "pass"
    raw["screens"]["impure_income"]["operator"] = "<"
    raw["interest_income_sources"]["upper_bound"]["yield_ceiling"] = 0.08
    cfg = parse_config(raw)
    assert screen(undisclosed(600.0), cfg).status == "pass"  # 48 / 1000
    assert screen(undisclosed(650.0), cfg).status == "insufficient_data"  # 52 / 1000
    assert result_to_record(screen(undisclosed(600.0), cfg), cfg)["interest_income_yield_ceiling"] == 0.08
    del raw["interest_income_sources"]["upper_bound"]["yield_ceiling"]
    with pytest.raises(ConfigError, match="yield_ceiling"):
        parse_config(raw)


def test_upper_bound_needs_cash_and_revenue(cfg):
    assert screen(undisclosed(None), cfg).status == "insufficient_data"
    result = screen(undisclosed(400.0, revenue=None), cfg)
    assert result.status == "insufficient_data" and "interest_income" in result.reason


def test_upper_bound_is_not_used_when_interest_income_is_disclosed(cfg):
    record = result_to_record(screen(make_inputs(interest_income=1.0), cfg), cfg)
    assert record["interest_income_basis"] == "ttm"
    assert record["interest_income_upper_bound"] is None and record["interest_income_yield_ceiling"] is None
    assert record["impure_income"] == 1.0
    # A disclosed figure above the limit still fails; the bound cannot rescue it.
    assert screen(make_inputs(interest_income=60.0, cash_and_securities=10.0), cfg).status == "fail"


def test_upper_bound_does_not_replace_a_capped_net_figure(cfg):
    inputs = make_inputs(
        interest_income=30.0,
        interest_income_kind="net_investment_income",
        interest_income_max_share_of_limit=0.5,
        cash_and_securities=10.0,
    )
    assert screen(inputs, cfg).status == "insufficient_data"


def test_upper_bound_does_not_rescue_other_outcomes(cfg):
    assert screen(undisclosed(400.0, **BANK), cfg).status == "fail"
    assert screen(undisclosed(400.0, **REVIEW), cfg).status == "needs_review"
    assert screen(undisclosed(400.0, debt=4000.0), cfg).status == "fail"
    # Cash ratio 40% would fail, but the bound of 20% leaves interest income missing, and missing comes first.
    assert screen(undisclosed(4000.0), cfg).status == "insufficient_data"
    assert screen(undisclosed(400.0, debt=None), cfg).status == "insufficient_data"
    understated = undisclosed(400.0, debt=10.0, interest_expense=50.0)
    assert screen(understated, cfg).status == "insufficient_data"  # the debt plausibility check still applies


def test_upper_bound_near_the_limit_is_flagged(cfg):
    result = screen(undisclosed(950.0), cfg)  # 4.75% of revenue
    assert result.status == "pass" and result.near_threshold
    assert result_to_record(result, cfg)["impure_income_near"] == 1


def test_override_impure_income_is_added_to_the_upper_bound(cfg):
    inputs = undisclosed(400.0, override=override(extra=35), **REVIEW)  # bound 20 + 35 = 5.5%
    result = screen(inputs, cfg)
    assert result.status == "insufficient_data" and result.impure_income == pytest.approx(55.0)


def test_bound_uses_total_assets_when_investments_are_assumed_zero(cfg):
    inputs = undisclosed(100.0, investments_assumed_zero=True, total_assets=800.0)  # 800 x 5% = 40 -> 4%
    result = screen(inputs, cfg)
    record = result_to_record(result, cfg)
    assert result.status == "pass" and "upper bound from total assets" in result.reason
    assert record["interest_income_bound_base"] == "total_assets"
    assert record["interest_income_upper_bound"] == pytest.approx(40.0)
    # The cash-based bound would have passed (0.5%), but it is not trusted here.
    over = screen(dataclasses.replace(inputs, total_assets=1000.0), cfg)
    assert over.status == "insufficient_data" and "total assets" in over.reason
    missing = screen(dataclasses.replace(inputs, total_assets=None), cfg)
    assert missing.status == "insufficient_data"
    assert "no total assets" in missing.reason
    # It only ever takes a pass away: a failing or borderline company keeps its verdict.
    assert screen(dataclasses.replace(inputs, total_assets=1000.0, debt=4000.0), cfg).status == "fail"
    review = screen(dataclasses.replace(inputs, total_assets=1000.0, **REVIEW), cfg)
    assert review.status == "needs_review"
    assert result_to_record(review, cfg)["interest_income_bound_base"] == "cash_and_securities"
    resolved = screen(dataclasses.replace(inputs, total_assets=1000.0, override=override(), **REVIEW), cfg)
    assert resolved.status == "insufficient_data"  # an override cannot pass it either


def test_bound_stays_on_cash_when_an_investment_tag_is_reported(cfg):
    record = result_to_record(screen(undisclosed(400.0, total_assets=5000.0), cfg), cfg)
    assert record["status"] == "pass" and record["interest_income_bound_base"] == "cash_and_securities"
    assert record["interest_income_upper_bound"] == pytest.approx(20.0)


@pytest.mark.parametrize("receivables, status", [(49.0, "pass"), (50.0, "pass"), (51.0, "needs_review")])
def test_financing_receivables_threshold(cfg, receivables, status):
    result = screen(make_inputs(total_assets=1000.0, financing_receivables=receivables), cfg)
    assert result.status == status
    if status == "needs_review":
        assert result.business.category == "captive_finance" and "5.1% of total assets" in result.reason


def test_financing_receivables_rule_only_touches_a_passing_business(raw, cfg):
    lender = dict(total_assets=1000.0, financing_receivables=400.0)
    assert screen(make_inputs(**lender, **BANK), cfg).business.category == "conventional_banking"
    assert screen(make_inputs(**lender, **REVIEW), cfg).business.category == "alcohol"
    assert screen(make_inputs(**lender, sic=None), cfg).status == "insufficient_data"
    assert screen(make_inputs(financing_receivables=400.0), cfg).status == "pass"  # no total assets, no finding
    resolved = screen(make_inputs(**lender, override=override()), cfg)
    assert resolved.status == "pass" and "manual override" in resolved.reason
    raw["business"]["financing_receivables"]["max_share_of_assets"] = 0.5
    assert screen(make_inputs(**lender), parse_config(raw)).status == "pass"


@pytest.mark.parametrize(
    "spot, avg_12m, avg_36m, factor, flagged",
    [
        (1000.0, 1000.0, 1000.0, 1.0, False),
        (1000.0, 1400.0, 1000.0, 1.4, False),
        (1000.0, 1500.0, 1000.0, 1.5, False),  # exactly at the factor is not beyond it
        (1000.0, 1600.0, 1000.0, 1.6, True),
        (1600.0, 1000.0, 1200.0, 1.6, True),  # either direction
        (1000.0, 1100.0, 2000.0, 1.1, False),  # only the 12-month average is compared
    ],
)
def test_spot_divergence_flag(cfg, spot, avg_12m, avg_36m, factor, flagged):
    record = result_to_record(screen(make_inputs(market_caps=caps(spot, avg_12m, avg_36m)), cfg), cfg)
    assert record["spot_divergence"] == pytest.approx(factor) and record["spot_diverges"] == int(flagged)


def test_spot_divergence_needs_spot_and_an_average(cfg):
    record = result_to_record(screen(make_inputs(market_caps=caps(None, 1000.0, 1000.0)), cfg), cfg)
    assert record["spot_divergence"] is None and record["spot_diverges"] == 0
    record = result_to_record(screen(make_inputs(market_caps=caps(1000.0, 1000.0, None)), cfg), cfg)
    assert record["spot_divergence"] == pytest.approx(1.0)


# Out-of-date balance sheet rules. Both may only turn a pass into insufficient_data.

EVENT = "separation reported in 8-K filed 2026-10-01 (0001755672-26-000028)"
SPOT_FAILS = caps(300.0, 1000.0, 1000.0)  # spot is 3.3x below the average; debt 100 / 300 fails on spot


def test_event_after_the_balance_sheet_withdraws_a_pass(cfg):
    result = screen(make_inputs(post_balance_sheet_event=EVENT), cfg)
    assert result.status == "insufficient_data"
    assert "balance sheet is out of date" in result.reason and "8-K filed 2026-10-01" in result.reason
    record = result_to_record(result, cfg)
    assert record["stale_balance_sheet"] == 1 and record["post_balance_sheet_event"] == EVENT
    assert record["status_avg_12m"] == "insufficient_data"
    assert record["debt_ratio_avg_12m"] == pytest.approx(0.10)  # the ratios are still on record


def test_diverging_spot_with_a_different_spot_verdict_withdraws_a_pass(cfg):
    result = screen(make_inputs(market_caps=SPOT_FAILS), cfg)
    assert result.status == "insufficient_data"
    assert "3.33x away" in result.reason and "spot verdict is fail" in result.reason
    record = result_to_record(result, cfg)
    assert (record["stale_balance_sheet"], record["spot_diverges"], record["status_spot"]) == (1, 1, "fail")


def test_diverging_spot_that_also_passes_changes_nothing(cfg):
    result = screen(make_inputs(market_caps=caps(600.0, 1000.0, 1000.0)), cfg)  # 1.67x apart, spot debt 16.7%
    record = result_to_record(result, cfg)
    assert result.status == "pass" and (record["spot_diverges"], record["stale_balance_sheet"]) == (1, 0)


def test_spot_failure_without_divergence_changes_nothing(cfg):
    result = screen(make_inputs(debt=250.0, market_caps=caps(800.0, 1000.0, 1000.0)), cfg)  # 1.25x, spot debt 31%
    record = result_to_record(result, cfg)
    assert result.status == "pass" and record["denominator_disagreement"] == 1
    assert (record["spot_diverges"], record["stale_balance_sheet"]) == (0, 0)


@pytest.mark.parametrize(
    "changes, status",
    [
        (dict(debt=400.0), "fail"),
        (dict(cash_and_securities=400.0), "fail"),
        (dict(interest_income=60.0), "fail"),
        (BANK, "fail"),
        (REVIEW, "needs_review"),
        (dict(revenue=None), "insufficient_data"),
        (dict(override=override("fail"), **REVIEW), "fail"),
    ],
)
@pytest.mark.parametrize("stale", [dict(post_balance_sheet_event=EVENT), dict(market_caps=SPOT_FAILS)])
def test_stale_balance_sheet_rules_never_move_a_fail_or_needs_review(cfg, changes, status, stale):
    plain = screen(make_inputs(**changes), cfg)
    flagged = screen(make_inputs(**{**changes, **stale}), cfg)
    assert plain.status == flagged.status == status
    assert not flagged.stale_balance_sheet
    if "post_balance_sheet_event" in stale:
        assert flagged.reason == plain.reason  # nothing about the verdict changes, only the recorded event
        assert result_to_record(flagged, cfg)["post_balance_sheet_event"] == EVENT


@pytest.mark.parametrize("stale", [dict(post_balance_sheet_event=EVENT), dict(market_caps=SPOT_FAILS)])
def test_stale_balance_sheet_rules_also_withdraw_an_override_pass(cfg, stale):
    result = screen(make_inputs(override=override(), **REVIEW, **stale), cfg)
    assert result.status == "insufficient_data" and result.stale_balance_sheet


def test_share_count_flags_are_recorded_and_change_no_verdict(cfg):
    note = "share count changed 2.00x between filings of 2026-04-23 and 2026-07-23 (634 to 317); review"
    for changes, status in ((dict(), "pass"), (dict(debt=400.0), "fail"), (REVIEW, "needs_review")):
        flagged = make_inputs(share_count_jump=True, share_counts_rejected=1, share_count_note=note, **changes)
        result = screen(flagged, cfg)
        assert result.status == status
        record = result_to_record(result, cfg)
        assert (record["share_count_jump"], record["share_counts_rejected"]) == (1, 1)
        assert record["share_count_note"] == note


def test_override_restores_a_reviewed_company_with_its_reason_on_record(cfg):
    """Tesla: needs_review on sub-industry alone, restored by a dated, reasoned override."""
    reason = "financing assets 0.16% of total assets; interest income 1.68% of revenue"
    auto = dict(sic="3711", gics_sub_industry="Automobile Manufacturers")
    assert screen(make_inputs(**auto), cfg).status == "needs_review"
    tesla = Override("TEST", "pass", reason, "maintainer", SCREEN_DATE)
    result = screen(make_inputs(override=tesla, **auto), cfg)
    assert result.status == "pass" and reason in result.reason and "captive_finance" in result.reason
    old = dataclasses.replace(tesla, date=SCREEN_DATE - timedelta(days=366))  # 12-month expiry
    assert screen(make_inputs(override=old, **auto), cfg).status == "needs_review"

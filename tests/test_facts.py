from datetime import date, timedelta

import pytest

from conftest import companyfacts, fact
from halal_heatmap.config import TagRef
from halal_heatmap.facts import (
    CompanyFacts,
    find_anchor,
    has_recent_facts,
    instant_fact,
    latest_annual_fact,
    resolve_input,
    share_counts,
    ttm_facts,
    ttm_value,
)

Q2_END = date(2026, 6, 30)
AS_OF = date(2026, 10, 1)


def view(doc, cfg, as_of=AS_OF):
    return CompanyFacts(doc, forms=cfg.filings.forms, as_of=as_of)


def tag(name):
    return TagRef.parse(name)


def test_anchor_is_latest_balance_sheet_from_its_original_filing(cfg):
    doc = companyfacts(
        {
            "Assets": [
                fact("2025-12-31", 900, "2026-02-10", form="10-K", accn="k1"),
                fact("2025-12-31", 900, "2026-07-30", accn="q2"),  # comparative column in the 10-Q
                fact(Q2_END, 1000, "2026-07-30", accn="q2"),
                fact(Q2_END, 1001, "2026-09-01", form="10-Q/A", accn="q2a"),
            ]
        }
    )
    anchor = find_anchor(view(doc, cfg), cfg.filings.balance_sheet_anchor)
    assert (anchor.end, anchor.accession, anchor.filed) == (Q2_END, "q2", date(2026, 7, 30))


def test_facts_filed_after_the_screen_date_are_invisible(cfg):
    doc = companyfacts(
        {
            "Assets": [
                fact("2026-03-31", 950, "2026-04-30", accn="q1"),
                fact(Q2_END, 1000, "2026-07-30", accn="q2"),
            ]
        }
    )
    assert find_anchor(view(doc, cfg, date(2026, 7, 29)), cfg.filings.balance_sheet_anchor).accession == "q1"
    assert find_anchor(view(doc, cfg, date(2026, 7, 30)), cfg.filings.balance_sheet_anchor).accession == "q2"


def test_restated_value_is_used_only_once_it_is_filed(cfg):
    doc = companyfacts(
        {
            "LongTermDebt": [
                fact(Q2_END, 500, "2026-07-30", accn="q2"),
                fact(Q2_END, 650, "2026-09-01", form="10-Q/A", accn="q2a"),
            ]
        }
    )
    assert instant_fact(view(doc, cfg, date(2026, 8, 15)), tag("LongTermDebt"), Q2_END).value == 500
    assert instant_fact(view(doc, cfg), tag("LongTermDebt"), Q2_END).value == 650


def test_other_form_types_are_ignored(cfg):
    doc = companyfacts({"LongTermDebt": [fact(Q2_END, 500, "2026-07-30", form="8-K")]})
    assert instant_fact(view(doc, cfg), tag("LongTermDebt"), Q2_END) is None


def test_ttm_from_an_annual_fact(cfg):
    doc = companyfacts({"Revenues": [fact("2025-12-31", 4000, "2026-02-10", start="2025-01-01", form="10-K")]})
    parts = ttm_facts(view(doc, cfg), tag("Revenues"), date(2025, 12, 31), cfg.periods)
    assert [role for role, _ in parts] == ["annual"]
    assert ttm_value(parts) == 4000


def quarterly_revenue():
    return [
        fact("2025-12-31", 4000, "2026-02-10", start="2025-01-01", form="10-K", accn="k"),
        fact("2025-06-30", 1800, "2025-07-30", start="2025-01-01", accn="q2-25"),
        fact("2025-06-30", 950, "2025-07-30", start="2025-04-01", accn="q2-25"),
        fact(Q2_END, 2300, "2026-07-30", start="2026-01-01", accn="q2-26"),
        fact(Q2_END, 1200, "2026-07-30", start="2026-04-01", accn="q2-26"),
    ]


def test_ttm_is_fiscal_year_plus_ytd_minus_prior_ytd(cfg):
    doc = companyfacts({"Revenues": quarterly_revenue()})
    parts = ttm_facts(view(doc, cfg), tag("Revenues"), Q2_END, cfg.periods)
    assert [role for role, _ in parts] == ["fiscal_year", "ytd", "prior_ytd"]
    assert ttm_value(parts) == 4000 + 2300 - 1800


def test_ttm_handles_52_53_week_fiscal_years(cfg):
    doc = companyfacts(
        {
            "Revenues": [
                fact("2025-09-27", 4000, "2025-10-31", start="2024-09-29", form="10-K"),
                fact("2025-03-29", 2100, "2025-05-02", start="2024-09-29"),
                fact("2026-03-28", 2400, "2026-05-01", start="2025-09-28"),
            ]
        }
    )
    parts = ttm_facts(view(doc, cfg), tag("Revenues"), date(2026, 3, 28), cfg.periods)
    assert ttm_value(parts) == 4000 + 2400 - 2100


@pytest.mark.parametrize("drop", ["k", "q2-25", "q2-26"])
def test_ttm_is_unavailable_when_a_piece_is_missing(cfg, drop):
    doc = companyfacts({"Revenues": [f for f in quarterly_revenue() if f["accn"] != drop]})
    assert ttm_facts(view(doc, cfg), tag("Revenues"), Q2_END, cfg.periods) is None


def test_debt_sums_components_and_records_the_facts(cfg):
    doc = companyfacts(
        {
            "LongTermDebtNoncurrent": [fact(Q2_END, 400, "2026-07-30")],
            "LongTermDebtCurrent": [fact(Q2_END, 50, "2026-07-30")],
            "CommercialPaper": [fact(Q2_END, 25, "2026-07-30")],
            "FinanceLeaseLiability": [fact(Q2_END, 10, "2026-07-30")],
        }
    )
    resolved = resolve_input(view(doc, cfg), cfg.inputs["debt"], Q2_END, cfg.periods)
    assert resolved.value == 485
    assert {str(u.fact.tag) for u in resolved.used} == {
        "us-gaap:LongTermDebtNoncurrent",
        "us-gaap:LongTermDebtCurrent",
        "us-gaap:CommercialPaper",
        "us-gaap:FinanceLeaseLiability",
    }


def test_largest_alternative_wins_for_numerators(cfg):
    # A stray partial value under LongTermDebt must not hide the real total (seen live on MAR).
    doc = companyfacts(
        {
            "LongTermDebt": [fact(Q2_END, 23, "2026-07-30")],
            "LongTermDebtAndCapitalLeaseObligations": [fact(Q2_END, 16455, "2026-07-30")],
            "LongTermDebtAndCapitalLeaseObligationsCurrent": [fact(Q2_END, 460, "2026-07-30")],
        }
    )
    resolved = resolve_input(view(doc, cfg), cfg.inputs["debt"], Q2_END, cfg.periods)
    assert resolved.value == 16915
    assert "us-gaap:LongTermDebt" not in {str(u.fact.tag) for u in resolved.used}


def test_first_alternative_wins_for_revenue(cfg):
    doc = companyfacts(
        {
            "Revenues": [fact("2025-12-31", 4000, "2026-02-10", start="2025-01-01", form="10-K")],
            "RevenueFromContractWithCustomerExcludingAssessedTax": [
                fact("2025-12-31", 9999, "2026-02-10", start="2025-01-01", form="10-K")
            ],
        }
    )
    resolved = resolve_input(view(doc, cfg), cfg.inputs["revenue"], date(2025, 12, 31), cfg.periods)
    assert resolved.value == 4000


def test_debt_is_missing_when_no_component_is_reported(cfg):
    doc = companyfacts({"LongTermDebt": [fact("2026-03-31", 450, "2026-04-30")]})  # only an older period
    resolved = resolve_input(view(doc, cfg), cfg.inputs["debt"], Q2_END, cfg.periods)
    assert resolved.value is None and resolved.missing


def test_cash_requires_cash_but_not_investments(cfg):
    spec = cfg.inputs["cash_and_securities"]
    only_cash = companyfacts({"CashAndCashEquivalentsAtCarryingValue": [fact(Q2_END, 70, "2026-07-30")]})
    assert resolve_input(view(only_cash, cfg), spec, Q2_END, cfg.periods).value == 70
    only_investments = companyfacts({"ShortTermInvestments": [fact(Q2_END, 30, "2026-07-30")]})
    resolved = resolve_input(view(only_investments, cfg), spec, Q2_END, cfg.periods)
    assert resolved.value is None and resolved.missing == ("cash",)
    both = companyfacts(
        {
            "CashAndCashEquivalentsAtCarryingValue": [fact(Q2_END, 70, "2026-07-30")],
            "ShortTermInvestments": [fact(Q2_END, 30, "2026-07-30")],
            "MarketableSecuritiesNoncurrent": [fact(Q2_END, 15, "2026-07-30")],
        }
    )
    assert resolve_input(view(both, cfg), spec, Q2_END, cfg.periods).value == 115


def test_recent_facts_detection(cfg):
    spec = cfg.inputs["interest_expense"]
    recent = companyfacts({"InterestExpense": [fact("2025-12-31", 9, "2026-02-10", start="2025-01-01", form="10-K")]})
    assert has_recent_facts(view(recent, cfg), spec, Q2_END, cfg.zero_debt.interest_expense_lookback_days)
    old = companyfacts({"InterestExpense": [fact("2023-12-31", 9, "2024-02-10", start="2023-01-01", form="10-K")]})
    assert not has_recent_facts(view(old, cfg), spec, Q2_END, cfg.zero_debt.interest_expense_lookback_days)


def test_share_counts_use_one_count_per_filing(cfg):
    doc = companyfacts(
        shares={
            "dei:EntityCommonStockSharesOutstanding": [
                fact("2026-01-31", 1000, "2026-02-10", form="10-K", accn="k"),
                fact("2026-07-20", 990, "2026-07-30", accn="q2"),
            ]
        }
    )
    share_tag, counts = share_counts(view(doc, cfg), cfg.shares.tags, cfg.shares.recent_days)
    assert str(share_tag.tag) == "dei:EntityCommonStockSharesOutstanding"
    assert [(c.effective, c.basis, c.shares) for c in counts] == [
        (date(2026, 2, 10), date(2026, 1, 31), 1000),
        (date(2026, 7, 30), date(2026, 7, 20), 990),
    ]


def test_share_counts_fall_back_when_the_cover_tag_is_stale(cfg):
    doc = companyfacts(
        shares={
            "dei:EntityCommonStockSharesOutstanding": [fact("2024-01-31", 1000, "2024-02-10", form="10-K")],
            "WeightedAverageNumberOfSharesOutstandingBasic": [
                fact(Q2_END, 985, "2026-07-30", start="2026-01-01", accn="q2"),
                fact(Q2_END, 980, "2026-07-30", start="2026-04-01", accn="q2"),
                fact("2025-06-30", 1010, "2026-07-30", start="2025-04-01", accn="q2"),
            ],
        }
    )
    share_tag, counts = share_counts(view(doc, cfg), cfg.shares.tags, cfg.shares.recent_days)
    assert share_tag.tag.name == "WeightedAverageNumberOfSharesOutstandingBasic"
    assert [(c.shares, c.basis) for c in counts] == [(980, date(2026, 7, 30))]


def test_no_share_counts(cfg):
    assert share_counts(view(companyfacts(), cfg), cfg.shares.tags, cfg.shares.recent_days) == (None, [])


ANNUAL_ONLY = {
    "InvestmentIncomeInterestAndDividend": [
        fact("2024-12-31", 30, "2025-02-10", start="2024-01-01", form="10-K", accn="k-24"),
        fact("2025-12-31", 40, "2026-02-10", start="2025-01-01", form="10-K", accn="k-25"),
    ]
}


def test_interest_income_falls_back_to_the_latest_annual_figure(cfg):
    resolved = resolve_input(view(companyfacts(ANNUAL_ONLY), cfg), cfg.inputs["interest_income"], Q2_END, cfg.periods)
    assert resolved.value == 40
    assert (resolved.annual_fallback.accession, resolved.annual_fallback.filed) == ("k-25", date(2026, 2, 10))
    assert [u.role for u in resolved.used] == ["annual_fallback"]


def test_trailing_figure_is_preferred_over_the_annual_fallback(cfg):
    doc = companyfacts({"InvestmentIncomeInterest": quarterly_revenue(), **ANNUAL_ONLY})
    resolved = resolve_input(view(doc, cfg), cfg.inputs["interest_income"], Q2_END, cfg.periods)
    assert resolved.value == 4500 and resolved.annual_fallback is None


def test_annual_fallback_respects_the_max_age_and_the_screen_date(cfg):
    spec = cfg.inputs["interest_income"]
    doc = companyfacts(ANNUAL_ONLY)
    on_limit = date(2025, 12, 31) + timedelta(days=450)
    assert resolve_input(view(doc, cfg, on_limit), spec, Q2_END, cfg.periods).value == 40
    too_old = resolve_input(view(doc, cfg, on_limit + timedelta(days=1)), spec, Q2_END, cfg.periods)
    assert too_old.value is None and too_old.missing == ("interest_income",)
    # Before the FY2025 10-K is filed only FY2024 is visible, and by then it is too old.
    early = view(doc, cfg, date(2026, 2, 9))
    assert latest_annual_fact(early, tag("InvestmentIncomeInterestAndDividend"), Q2_END, cfg.periods, 450).value == 30
    assert latest_annual_fact(early, tag("InvestmentIncomeInterestAndDividend"), Q2_END, cfg.periods, 400) is None


def test_annual_fallback_never_uses_a_net_other_income_tag(cfg):
    net_tags = {
        "NonoperatingIncomeExpense": ANNUAL_ONLY["InvestmentIncomeInterestAndDividend"],
        "OtherNonoperatingIncomeExpense": ANNUAL_ONLY["InvestmentIncomeInterestAndDividend"],
        "InterestIncomeExpenseNonoperatingNet": ANNUAL_ONLY["InvestmentIncomeInterestAndDividend"],
        "InterestIncomeExpenseNet": ANNUAL_ONLY["InvestmentIncomeInterestAndDividend"],
    }
    resolved = resolve_input(view(companyfacts(net_tags), cfg), cfg.inputs["interest_income"], Q2_END, cfg.periods)
    assert resolved.value is None
    configured = {t.name for t in cfg.inputs["interest_income"].all_tags()}
    assert not any("Net" in name or "Expense" in name for name in configured)


def test_other_duration_inputs_have_no_annual_fallback(cfg):
    doc = companyfacts({"Revenues": ANNUAL_ONLY["InvestmentIncomeInterestAndDividend"]})
    assert resolve_input(view(doc, cfg), cfg.inputs["revenue"], Q2_END, cfg.periods).value is None


def test_absent_optional_components_are_reported(cfg):
    spec = cfg.inputs["cash_and_securities"]
    only_cash = companyfacts({"CashAndCashEquivalentsAtCarryingValue": [fact(Q2_END, 70, "2026-07-30")]})
    resolved = resolve_input(view(only_cash, cfg), spec, Q2_END, cfg.periods)
    assert resolved.absent_optional == ("short_term_investments", "long_term_investments")
    partial = companyfacts(
        {
            "CashAndCashEquivalentsAtCarryingValue": [fact(Q2_END, 70, "2026-07-30")],
            "ShortTermInvestments": [fact(Q2_END, 30, "2026-07-30")],
        }
    )
    assert resolve_input(view(partial, cfg), spec, Q2_END, cfg.periods).absent_optional == ("long_term_investments",)

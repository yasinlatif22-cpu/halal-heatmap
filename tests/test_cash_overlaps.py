"""Regression tests for cash_and_securities tags that overlap the cash line.

USD millions, as each filing reports them for the period:
  NVDA 10-Q, 26 July 2026: cash 22,443; AvailableForSaleSecuritiesDebtSecurities 46,900 (includes the
    cash-equivalent debt already in cash); DebtSecuritiesCurrent 34,143 (the marketable debt line, which
    with cash makes the filing's 56,586 of cash, cash equivalents and marketable debt).
  MSFT 10-K, 30 June 2026: cash 20,935; AvailableForSaleSecuritiesDebtSecurities 71,084 (includes debt
    investments already in cash and in long-term investments); ShortTermInvestments 55,908 (the balance-sheet
    line); LongTermInvestments 36,348 ("equity and other investments", which includes equity investments).
"""

from datetime import date

from conftest import companyfacts, fact
from halal_heatmap.facts import CompanyFacts, resolve_input

M = 1_000_000
END = date(2026, 6, 30)


def cash(usd, cfg, as_of=date(2026, 7, 31)):
    tags = {tag: [fact(END, value * M, as_of, form="10-Q", accn="acc")] for tag, value in usd.items()}
    view = CompanyFacts(companyfacts(tags), forms=cfg.filings.forms, as_of=as_of)
    return resolve_input(view, cfg.inputs["cash_and_securities"], END, cfg.periods)


def test_nvda_available_for_sale_total_is_not_added_to_cash_twice(cfg):
    resolved = cash(
        {
            "CashAndCashEquivalentsAtCarryingValue": 22_443,
            "AvailableForSaleSecuritiesDebtSecurities": 46_900,
            "DebtSecuritiesCurrent": 34_143,
        },
        cfg,
    )
    assert resolved.value == 56_586 * M  # the filing's cash, cash equivalents and marketable debt
    assert resolved.left_out == ("us-gaap:AvailableForSaleSecuritiesDebtSecurities",)


def test_msft_available_for_sale_total_is_replaced_by_the_balance_sheet_line(cfg):
    resolved = cash(
        {
            "CashAndCashEquivalentsAtCarryingValue": 20_935,
            "AvailableForSaleSecuritiesDebtSecurities": 71_084,
            "ShortTermInvestments": 55_908,
            "LongTermInvestments": 36_348,
        },
        cfg,
    )
    assert resolved.value == 113_191 * M  # cash + short-term + equity and other investments, as filed
    assert resolved.left_out == ("us-gaap:AvailableForSaleSecuritiesDebtSecurities",)
    # Long-term investments stay in, as reported: the note tells the reader they may include equity.
    assert {u.component for u in resolved.used} >= {"cash", "short_term_investments", "long_term_investments"}


def test_a_broad_total_stays_when_its_excess_does_not_fit_in_cash(cfg):
    # Excess 2,500 is more than the 500 cash line can hold, so the broad total is not dropped.
    resolved = cash(
        {
            "CashAndCashEquivalentsAtCarryingValue": 500,
            "ShortTermInvestments": 100,
            "AvailableForSaleSecuritiesDebtSecurities": 2_600,
        },
        cfg,
    )
    assert resolved.value == 3_100 * M
    assert resolved.left_out == ()

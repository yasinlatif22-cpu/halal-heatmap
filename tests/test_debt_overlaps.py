"""Regression tests for debt tags that are parts of a total reported with them.

Values are from each company's filing for the period, in USD millions, as the filing reports them:
  NVDA 10-Q, 26 July 2026: LongTermDebt 33,366 (the 32,366 long-term line plus 1,000 short-term);
    DebtCurrent 1,000 is inside it.
  MAR 10-Q, 30 June 2026: noncurrent long-term debt 16,455 (includes 1,242 commercial paper and 110
    finance leases, per the debt note's 16,915 total less the 460 current portion); current portion 460
    is outside 16,455.
  UPS 10-Q, 30 June 2026: DebtAndCapitalLeaseObligations 24,484 includes DebtCurrent 634 and
    FinanceLeaseLiability 1,190.
  MSFT 10-K, 30 June 2026: no tag that contains another, so nothing is left out.
"""

from datetime import date

from conftest import companyfacts, fact
from halal_heatmap.facts import CompanyFacts, resolve_input

M = 1_000_000


def view(usd, cfg, as_of):
    tags = {
        tag: [fact(end, value * M, as_of, form="10-Q", accn="acc") for end, value in items]
        for tag, items in usd.items()
    }
    return CompanyFacts(companyfacts(tags), forms=cfg.filings.forms, as_of=as_of)


def debt(usd, cfg, end, as_of):
    return resolve_input(view(usd, cfg, as_of), cfg.inputs["debt"], end, cfg.periods)


def test_nvda_short_term_debt_is_not_counted_twice(cfg):
    end = date(2026, 7, 26)
    resolved = debt(
        {"LongTermDebt": [(end, 33_366)], "DebtCurrent": [(end, 1_000)]}, cfg, end, date(2026, 8, 1)
    )
    assert resolved.value == 33_366 * M
    assert resolved.left_out == ("us-gaap:DebtCurrent",)
    assert [str(u.fact.tag) for u in resolved.used] == ["us-gaap:LongTermDebt"]


def test_mar_commercial_paper_and_leases_are_inside_noncurrent_debt(cfg):
    end = date(2026, 6, 30)
    resolved = debt(
        {
            "LongTermDebtAndCapitalLeaseObligations": [(end, 16_455)],
            "LongTermDebtAndCapitalLeaseObligationsCurrent": [(end, 460)],  # outside 16,455: summed
            "CommercialPaperNoncurrent": [(end, 1_242)],
            "FinanceLeaseLiability": [(end, 110)],
        },
        cfg,
        end,
        date(2026, 7, 15),
    )
    assert resolved.value == 16_915 * M  # the debt note's total, not 18,267
    assert set(resolved.left_out) == {"us-gaap:CommercialPaperNoncurrent", "us-gaap:FinanceLeaseLiability"}


def test_ups_current_portion_and_leases_are_inside_its_total(cfg):
    end = date(2026, 6, 30)
    resolved = debt(
        {
            "DebtAndCapitalLeaseObligations": [(end, 24_484)],  # includes the 634 current portion and 1,190 leases
            "LongTermDebtAndCapitalLeaseObligations": [(end, 23_850)],
            "DebtCurrent": [(end, 634)],
            "FinanceLeaseLiability": [(end, 1_190)],
        },
        cfg,
        end,
        date(2026, 7, 31),
    )
    assert resolved.value == 24_484 * M
    assert set(resolved.left_out) == {"us-gaap:DebtCurrent", "us-gaap:FinanceLeaseLiability"}


def test_msft_tags_that_do_not_overlap_are_both_counted(cfg):
    end = date(2026, 6, 30)
    resolved = debt(
        {"DebtInstrumentCarryingAmount": [(end, 46_136)], "FinanceLeaseLiability": [(end, 66_594)]},
        cfg,
        end,
        date(2026, 7, 31),
    )
    assert resolved.value == 112_730 * M
    assert resolved.left_out == ()


def test_a_contained_tag_counts_when_its_total_is_not_reported(cfg):
    end = date(2026, 7, 26)
    resolved = debt({"DebtCurrent": [(end, 1_000)]}, cfg, end, date(2026, 8, 1))
    assert resolved.value == 1_000 * M
    assert resolved.left_out == ()


def test_commercial_paper_counts_without_its_noncurrent_total(cfg):
    end = date(2026, 6, 30)
    resolved = debt({"CommercialPaperNoncurrent": [(end, 1_242)]}, cfg, end, date(2026, 7, 15))
    assert resolved.value == 1_242 * M
    assert resolved.left_out == ()


def test_a_container_smaller_than_the_contained_tag_keeps_both(cfg):
    """A LongTermDebt that excludes current maturities cannot hold DebtCurrent: keep both, and say so."""
    end = date(2026, 7, 26)
    resolved = debt({"LongTermDebt": [(end, 5_000)], "DebtCurrent": [(end, 6_000)]}, cfg, end, date(2026, 8, 1))
    assert resolved.value == 11_000 * M
    assert resolved.left_out == ()
    assert resolved.kept == ("us-gaap:DebtCurrent",)


def test_finance_lease_alternatives_are_left_out_with_the_lease_tag(cfg):
    """Noncurrent and current finance leases are alternatives to FinanceLeaseLiability: all of them go."""
    end = date(2026, 6, 30)
    resolved = debt(
        {
            "DebtAndCapitalLeaseObligations": [(end, 24_484)],
            "FinanceLeaseLiabilityNoncurrent": [(end, 1_000)],
            "FinanceLeaseLiabilityCurrent": [(end, 190)],
        },
        cfg,
        end,
        date(2026, 7, 31),
    )
    assert resolved.value == 24_484 * M
    assert set(resolved.left_out) == {"us-gaap:FinanceLeaseLiabilityNoncurrent", "us-gaap:FinanceLeaseLiabilityCurrent"}

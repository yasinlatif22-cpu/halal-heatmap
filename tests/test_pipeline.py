"""End to end over fake sources: fetch, normalise, screen, store, read back."""

from datetime import date

import pytest

from conftest import companyfacts, fact, trading_days
from halal_heatmap.cli import format_record
from halal_heatmap.pipeline import screen_constituent
from halal_heatmap.screen.engine import result_to_record
from halal_heatmap.sources import SourceError
from halal_heatmap.sources.prices import PriceHistory
from halal_heatmap.sources.wikipedia import Constituent
from halal_heatmap.store import Store

AS_OF = date(2026, 10, 1)
Q2_END = "2026-06-30"
COMPANY = Constituent("TEST", "Test Corp", "Information Technology", "Semiconductors", 1234)


def duration(tag_values):
    """Annual, prior year-to-date and current year-to-date facts giving a known trailing total."""
    annual, prior_ytd, ytd = tag_values
    return [
        fact("2025-12-31", annual, "2026-02-10", start="2025-01-01", form="10-K", accn="k"),
        fact("2025-06-30", prior_ytd, "2025-07-30", start="2025-01-01", accn="q2-25"),
        fact(Q2_END, ytd, "2026-07-30", start="2026-01-01", accn="q2-26"),
    ]


def document(**overrides):
    usd = {
        "Assets": [fact(Q2_END, 5000, "2026-07-30", accn="q2-26")],
        "Liabilities": [fact(Q2_END, 2000, "2026-07-30", accn="q2-26")],
        "LongTermDebt": [fact(Q2_END, 2000, "2026-07-30", accn="q2-26")],
        "CashAndCashEquivalentsAtCarryingValue": [fact(Q2_END, 1500, "2026-07-30", accn="q2-26")],
        "ShortTermInvestments": [fact(Q2_END, 500, "2026-07-30", accn="q2-26")],
        "Revenues": duration((4000, 1800, 2300)),  # trailing 4500
        "InvestmentIncomeInterest": duration((40, 18, 23)),  # trailing 45
        "InterestExpense": duration((80, 40, 40)),
    }
    usd.update(overrides)
    usd = {tag: items for tag, items in usd.items() if items is not None}
    shares = {
        "dei:EntityCommonStockSharesOutstanding": [
            fact("2023-07-20", 100, "2023-07-30", accn="q2-23"),
            fact("2026-07-20", 100, "2026-07-30", accn="q2-26"),
        ]
    }
    return companyfacts(usd, shares)


class Filings:
    def __init__(self, doc, sic="3674", fail=False):
        self.doc, self.sic, self.fail = doc, sic, fail

    def company_facts(self, cik):
        if self.fail:
            raise SourceError("EDGAR down")
        return self.doc

    def submissions(self, cik):
        return {"sic": self.sic}


class Prices:
    def __init__(self, close=100.0, start=date(2023, 9, 1), fail=False):
        self.close, self.start, self.fail = close, start, fail

    def history(self, ticker, start, end):
        if self.fail:
            raise SourceError("no prices")
        return PriceHistory([(day, self.close) for day in trading_days(self.start, end)], [])


def run(cfg, doc=None, prices=None, sic="3674", as_of=AS_OF, filings=None):
    filings = filings or Filings(doc or document(), sic)
    return screen_constituent(COMPANY, as_of, cfg, filings, prices or Prices(), trigger="test")


def test_passing_company_end_to_end(cfg):
    result, used = run(cfg)  # market cap 100 shares x $100 = 10,000
    record = result_to_record(result, cfg)
    assert record["status"] == "pass"
    assert record["filing_accession"] == "q2-26" and record["filing_date"] == "2026-07-30"
    assert record["period_end"] == Q2_END
    assert (record["debt"], record["cash_and_securities"], record["revenue"], record["impure_income"]) == (
        2000,
        2000,
        4500,
        45,
    )
    assert record["mcap_spot"] == record["mcap_avg_12m"] == record["mcap_avg_36m"] == pytest.approx(10000)
    assert record["debt_ratio_avg_12m"] == pytest.approx(0.20)
    assert record["impure_ratio"] == pytest.approx(0.01)
    assert {u.input for u in used} == {
        "debt",
        "cash_and_securities",
        "total_assets",
        "total_liabilities",
        "revenue",
        "interest_income",
        "interest_expense",
        "shares_outstanding",
    }


def test_debt_above_limit_fails(cfg):
    doc = document(LongTermDebt=[fact(Q2_END, 3000, "2026-07-30", accn="q2-26")])
    result, _ = run(cfg, doc)
    assert result.status == "fail" and "debt" in result.reason


def test_screen_before_the_filing_uses_older_data_or_none(cfg):
    result, _ = run(cfg, as_of=date(2026, 7, 29))  # the only balance sheet is filed the next day
    assert result.status == "insufficient_data"
    assert result.inputs.filing is None


@pytest.mark.parametrize("tag", ["Revenues", "CashAndCashEquivalentsAtCarryingValue"])
def test_missing_tag_is_insufficient_data(cfg, tag):
    result, _ = run(cfg, document(**{tag: None}))
    assert result.status == "insufficient_data"


def test_undisclosed_interest_income_is_bounded_from_cash(cfg):
    result, used = run(cfg, document(InvestmentIncomeInterest=None))  # 2000 x 5% = 100; 100 / 4500 = 2.2%
    record = result_to_record(result, cfg)
    assert record["status"] == "pass" and record["interest_income_basis"] == "upper_bound_no_disclosure"
    assert record["interest_income"] is None and record["impure_income"] == pytest.approx(100)
    assert not [u for u in used if u.input == "interest_income"]
    assert "NO INTEREST INCOME DISCLOSED" in format_record(record, used)
    assert "interest income upper bound" in format_record(record, used)


def test_undisclosed_interest_income_with_too_much_cash_is_insufficient_data(cfg):
    def record_for(cash):
        doc = document(
            InvestmentIncomeInterest=None,
            CashAndCashEquivalentsAtCarryingValue=[fact(Q2_END, cash, "2026-07-30", accn="q2-26")],
        )
        # 1000 shares x $100 = market cap 100,000, so the cash ratio stays far below its limit.
        doc["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"] = [
            fact("2023-07-20", 1000, "2023-07-30", accn="q2-23"),
            fact("2026-07-20", 1000, "2026-07-30", accn="q2-26"),
        ]
        return result_to_record(run(cfg, doc)[0], cfg)

    assert record_for(2500)["status"] == "pass"  # (2500 + 500) x 5% = 150; 150 / 4500 = 3.3%
    record = record_for(4100)  # 4600 x 5% = 230; 230 / 4500 = 5.1%
    assert record["status"] == "insufficient_data" and "upper bound" in record["reason"]
    assert record["interest_income_basis"] == "upper_bound_no_disclosure"


@pytest.mark.parametrize("tag", ["InterestIncomeOperating", "InvestmentIncomeNonoperating"])
def test_added_gross_tags_are_used_and_recorded(cfg, tag):
    doc = document(InvestmentIncomeInterest=None, **{tag: duration((40, 18, 23))})
    record = result_to_record(run(cfg, doc)[0], cfg)
    assert (record["interest_income"], record["interest_income_basis"]) == (45, "ttm")
    assert record["interest_income_tags"] == f"us-gaap:{tag}"


def test_missing_debt_with_interest_expense_is_insufficient_data(cfg):
    result, _ = run(cfg, document(LongTermDebt=None))
    assert result.status == "insufficient_data" and "debt" in result.reason


def test_missing_debt_without_interest_expense_is_zero(cfg):
    result, _ = run(cfg, document(LongTermDebt=None, InterestExpense=None))
    assert result.status == "pass" and result.debt.assumed_zero


def test_missing_debt_without_total_liabilities_is_insufficient_data(cfg):
    result, _ = run(cfg, document(LongTermDebt=None, InterestExpense=None, Liabilities=None))
    assert result.status == "insufficient_data"


def test_source_failures_never_pass(cfg):
    for kwargs in (dict(filings=Filings(document(), fail=True)), dict(prices=Prices(fail=True))):
        result, _ = run(cfg, **kwargs)
        assert result.status == "insufficient_data"


def test_short_price_history_blocks_only_the_long_average(cfg):
    result, _ = run(cfg, prices=Prices(start=date(2025, 6, 1)))
    record = result_to_record(result, cfg)
    assert record["status"] == "pass"
    assert record["mcap_avg_36m"] is None and record["status_avg_36m"] is None


def test_business_rules_apply_from_sic_and_gics(cfg):
    assert run(cfg, sic="6021")[0].status == "fail"
    assert run(cfg, sic="3812")[0].status == "needs_review"
    assert run(cfg, sic=None)[0].status == "insufficient_data"


def test_results_are_stored_append_only_and_readable(cfg, tmp_path):
    store = Store(tmp_path / "db" / "screens.db")
    run_id = store.start_run(AS_OF.isoformat(), cfg.hash)
    store.save_constituents(AS_OF.isoformat(), [COMPANY])
    result, used = run(cfg)
    record = result_to_record(result, cfg)
    first = store.save_result(run_id, record, used)
    second = store.save_result(run_id, record, used)
    store.finish_run(run_id, {"pass": 2}, [])

    assert first != second
    assert store.conn.execute("SELECT COUNT(*) FROM screen_results").fetchone()[0] == 2
    row = store.latest_result("TEST")
    for key, value in record.items():
        assert row[key] == pytest.approx(value) if isinstance(value, float) else row[key] == value
    facts = store.facts_for(first)
    assert len(facts) == len(used)
    debt = next(f for f in facts if f["input"] == "debt")
    assert (debt["tag"], debt["value"], debt["accession"], debt["filed"]) == (
        "us-gaap:LongTermDebt",
        2000,
        "q2-26",
        "2026-07-30",
    )
    with pytest.raises(ValueError, match="schema"):
        store.save_result(run_id, {**record, "extra": 1}, used)
    store.close()


def test_audit_record_prints(cfg):
    result, used = run(cfg)
    text = format_record(result_to_record(result, cfg), used)
    assert "status: pass" in text and "drives verdict" in text and "us-gaap:LongTermDebt" in text


# --- cash-rich companies. Market cap is 10,000 throughout, so the 30% cash limit is 3,000. ---


def q2(value):
    return [fact(Q2_END, value, "2026-07-30", accn="q2-26")]


def cash_doc(**tags):
    base = dict(CashAndCashEquivalentsAtCarryingValue=None, ShortTermInvestments=None)
    return document(**{**base, **{tag: q2(value) for tag, value in tags.items()}})


def cash_record(cfg, **tags):
    result, used = run(cfg, cash_doc(**tags))
    return result_to_record(result, cfg), used


def test_cash_rich_alphabet_style_counts_marketable_securities(cfg):
    record, used = cash_record(cfg, CashAndCashEquivalentsAtCarryingValue=560, MarketableSecuritiesCurrent=1870)
    assert record["status"] == "pass"
    assert record["cash_and_securities"] == 2430 and record["cash_ratio_avg_12m"] == pytest.approx(0.243)
    assert record["investments_assumed_zero"] == 0
    assert record["cash_note"] == "long_term_investments tags absent, treated as 0"
    assert "us-gaap:MarketableSecuritiesCurrent" in {str(u.fact.tag) for u in used}


def test_cash_rich_apple_style_sums_cash_current_and_noncurrent_securities(cfg):
    record, _ = cash_record(
        cfg,
        CashAndCashEquivalentsAtCarryingValue=400,
        MarketableSecuritiesCurrent=230,
        MarketableSecuritiesNoncurrent=840,
    )
    assert record["cash_and_securities"] == 1470 and record["cash_note"] == ""
    assert record["status"] == "pass"


def test_cash_rich_nvidia_style_debt_securities_tag_is_counted(cfg):
    record, _ = cash_record(cfg, CashAndCashEquivalentsAtCarryingValue=224, DebtSecuritiesCurrent=341)
    assert record["cash_and_securities"] == 565


def test_cash_rich_company_fails_once_securities_are_included(cfg):
    # Cash alone is 15% of market cap; with securities it is 33%.
    record, _ = cash_record(
        cfg, CashAndCashEquivalentsAtCarryingValue=1500, ShortTermInvestments=1000, MarketableSecuritiesNoncurrent=800
    )
    assert record["status"] == "fail" and "cash / market cap (avg_12m) 33.00%" in record["reason"]


def test_cash_exactly_at_the_limit_fails(cfg):
    record, _ = cash_record(cfg, CashAndCashEquivalentsAtCarryingValue=2000, ShortTermInvestments=1000)
    assert record["status"] == "fail" and record["cash_ratio_avg_12m"] == pytest.approx(0.30)


def test_cash_only_company_passes_with_the_assumed_zero_flag(cfg):
    record, _ = cash_record(cfg, CashAndCashEquivalentsAtCarryingValue=2900)
    assert record["status"] == "pass"
    assert record["investments_assumed_zero"] == 1
    assert record["cash_note"] == "investment tags absent, treated as 0"
    assert record["cash_near"] == 1 and record["near_threshold"] == 1  # 29% is within 10% of the limit
    text = format_record(record, [])
    assert "investments assumed zero" in text and "investment tags absent, treated as 0" in text


def test_assumed_zero_flag_is_kept_when_the_company_fails(cfg):
    record, _ = cash_record(cfg, CashAndCashEquivalentsAtCarryingValue=3100)
    assert record["status"] == "fail" and record["investments_assumed_zero"] == 1


def test_small_securities_tag_cannot_hide_a_large_securities_book(cfg):
    # A partial figure under ShortTermInvestments next to the full available-for-sale total.
    record, _ = cash_record(
        cfg,
        CashAndCashEquivalentsAtCarryingValue=500,
        ShortTermInvestments=100,
        AvailableForSaleSecuritiesDebtSecurities=2600,
    )
    assert record["cash_and_securities"] == 3100 and record["status"] == "fail"


def test_cash_rich_company_with_no_cash_tag_is_insufficient_data(cfg):
    record, _ = cash_record(cfg, ShortTermInvestments=2500)
    assert record["status"] == "insufficient_data" and "cash_and_securities" in record["reason"]


def test_annual_interest_income_fallback_end_to_end(cfg):
    annual = [fact("2025-12-31", 60, "2026-02-10", start="2025-01-01", form="10-K", accn="k")]
    result, used = run(cfg, document(InvestmentIncomeInterest=None, InvestmentIncomeInterestAndDividend=annual))
    record = result_to_record(result, cfg)
    assert record["status"] == "pass" and record["impure_income"] == 60
    assert record["interest_income_basis"] == "annual_fallback"
    assert (record["interest_income_filing_date"], record["interest_income_accession"]) == ("2026-02-10", "k")
    assert "annual_fallback" in {u.role for u in used}
    assert "annual interest income" in format_record(record, used)


def test_understated_debt_end_to_end(cfg):
    # Debt tagged at 100 against trailing interest expense of 80: the Marriott pattern.
    result, _ = run(cfg, document(LongTermDebt=q2(100)))
    record = result_to_record(result, cfg)
    assert record["status"] == "insufficient_data" and record["debt_implausible"] == 1
    assert "debt not reliable" in record["reason"]


def test_cash_only_company_without_interest_income_is_bounded_on_total_assets(cfg):
    # No investment tags and no interest income: the bound is 5000 total assets x 5% = 250; 250 / 4500 = 5.6%.
    doc = document(InvestmentIncomeInterest=None, ShortTermInvestments=None)
    record = result_to_record(run(cfg, doc)[0], cfg)
    assert record["investments_assumed_zero"] == 1 and record["status"] == "insufficient_data"
    assert record["interest_income_bound_base"] == "total_assets" and "total assets" in record["reason"]
    # With total assets of 4000 the bound is 200 / 4500 = 4.4%, which passes.
    doc = document(
        InvestmentIncomeInterest=None,
        ShortTermInvestments=None,
        Assets=[fact(Q2_END, 4000, "2026-07-30", accn="q2-26")],
    )
    record = result_to_record(run(cfg, doc)[0], cfg)
    assert record["status"] == "pass" and record["impure_income"] == pytest.approx(200)
    assert record["total_assets"] == 4000


def test_financing_receivables_send_a_passing_business_to_review(cfg):
    lending = [fact(Q2_END, 300, "2026-07-30", accn="q2-26")]  # 300 / 5000 = 6% of total assets
    record = result_to_record(run(cfg, document(NotesReceivableNet=lending))[0], cfg)
    assert record["status"] == "needs_review" and record["business_category"] == "captive_finance"
    assert record["financing_receivables_share"] == pytest.approx(0.06)
    small = [fact(Q2_END, 200, "2026-07-30", accn="q2-26")]
    record = result_to_record(run(cfg, document(NotesReceivableNet=small))[0], cfg)
    assert record["status"] == "pass" and record["financing_receivables_share"] == pytest.approx(0.04)
    record = result_to_record(run(cfg)[0], cfg)
    assert record["financing_receivables_share"] is None and "financing_receivables" not in record["input_notes"]

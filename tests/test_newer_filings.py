"""A newer 10-Q or 10-K that EDGAR lists is never silently skipped, and staleness follows the company's
own filing cadence. Fake sources only; the shapes follow PPG (read from its XBRL instance) and LEN
(newer report with no usable balance sheet)."""

from datetime import date, timedelta

from conftest import companyfacts, fact
from halal_heatmap.config import parse_config
from halal_heatmap.filings import FilingEntry
from halal_heatmap.pipeline import _newer_and_due, screen_constituent
from halal_heatmap.sources import SourceError
from halal_heatmap.sources.prices import PriceHistory
from halal_heatmap.sources.wikipedia import Constituent

AS_OF = date(2026, 10, 1)
COMPANY = Constituent("TEST", "Test Corp", "Information Technology", "Semiconductors", 1234)
Q1 = ("q1-26", "10-Q", "2026-04-29", "2026-03-31")
Q2 = ("q2-26", "10-Q", "2026-07-29", "2026-06-30")


def submissions_row(accession, form, filed, period):
    return {"accession": accession, "form": form, "filed": filed, "period": period}


class Source:
    """EDGAR as the screen sees it: companyfacts may lack a filing, and each filing has its own instance."""

    def __init__(self, cf, rows, instances):
        self.cf, self.rows, self.instances = cf, rows, instances

    def company_facts(self, cik):
        return self.cf

    def submissions(self, cik):
        recent = {
            "accessionNumber": [r["accession"] for r in self.rows],
            "form": [r["form"] for r in self.rows],
            "filingDate": [r["filed"] for r in self.rows],
            "reportDate": [r["period"] for r in self.rows],
            "primaryDocument": [f"{r['accession']}.htm" for r in self.rows],
            "items": ["" for _ in self.rows],
        }
        return {"sic": "3674", "filings": {"recent": recent}}

    def filing_instance(self, cik, accession, primary_document):
        if accession not in self.instances:
            raise SourceError(f"no instance for {accession}")
        return self.instances[accession]

    def filing_text(self, cik, accession, primary_document):
        raise SourceError("no text")


class Prices:
    def history(self, ticker, start, end):
        return PriceHistory([], [])


def instance_fact(tag, end, value):
    return {"tag": tag, "unit": "USD", "val": value, "start": None, "end": end, "dims": []}


def q1_companyfacts():
    """companyfacts holds the March balance sheet only: the June 10-Q is not served yet."""
    return companyfacts(
        {
            "Assets": [fact("2026-03-31", 22150, "2026-04-29", accn="q1-26")],
            "LongTermDebt": [fact("2026-03-31", 7096, "2026-04-29", accn="q1-26")],
            "DebtCurrent": [fact("2026-03-31", 736, "2026-04-29", accn="q1-26")],
        }
    )


def june_instance():
    return [
        instance_fact("us-gaap:Assets", "2026-06-30", 22534),
        instance_fact("us-gaap:LongTermDebt", "2026-06-30", 6876),
        instance_fact("us-gaap:DebtCurrent", "2026-06-30", 691),
        instance_fact("us-gaap:ShortTermBorrowings", "2026-06-30", 4),
        instance_fact("us-gaap:FinanceLeaseLiability", "2026-06-30", 6),
    ]


def screen(cfg, source, as_of=AS_OF):
    result, _ = screen_constituent(COMPANY, as_of, cfg, source, Prices(), trigger="test")
    return result


def test_a_listed_newer_10q_that_companyfacts_lacks_is_read_from_its_instance(cfg):
    """The PPG case: the June 10-Q was listed on 29 July and its balance sheet is in its XBRL instance."""
    source = Source(q1_companyfacts(), [submissions_row(*Q2), submissions_row(*Q1)], {Q2[0]: june_instance()})
    result = screen(cfg, source)
    assert result.inputs.filing.accession == "q2-26"
    assert result.inputs.filing.period_end == date(2026, 6, 30)
    # LongTermDebt 6,876 (includes the 691 DebtCurrent, left out) + short-term borrowings 4 + finance leases 6
    assert result.inputs.debt == 6886
    assert "newer_filing" not in result.inputs.notes


def test_a_newer_report_with_no_usable_balance_sheet_is_insufficient_data(cfg):
    """The LEN case: the newer report has no undimensioned total assets, so the March sheet must not
    stand in for it."""
    instance = [i for i in june_instance() if i["tag"] != "us-gaap:Assets"]
    source = Source(q1_companyfacts(), [submissions_row(*Q2), submissions_row(*Q1)], {Q2[0]: instance})
    result = screen(cfg, source)
    assert result.status == "insufficient_data"
    assert "q2-26" in result.reason and "has no usable balance sheet" in result.reason
    assert result.inputs.notes["newer_filing"].startswith("listed by EDGAR but no usable balance sheet: q2-26")


def test_an_older_balance_sheet_stands_in_only_within_the_configured_maximum_age(cfg, raw):
    """March is 184 days old on 1 October. Allowed only when the configured maximum is at least that."""
    instance = [i for i in june_instance() if i["tag"] != "us-gaap:Assets"]
    source = Source(q1_companyfacts(), [submissions_row(*Q2), submissions_row(*Q1)], {Q2[0]: instance})
    raw["filings"]["max_fallback_age_days"] = 200
    result = screen(parse_config(raw), source)
    assert "has no usable balance sheet" not in result.reason
    assert result.inputs.notes["newer_filing"].startswith("listed by EDGAR but no usable balance sheet")


def test_no_newer_report_and_a_report_overdue_for_this_company_is_insufficient_data(cfg):
    """A company that files quarterly has its next report due one quarter after March plus the grace.
    Nothing newer is listed, so the March balance sheet is stale on 1 October."""
    source = Source(q1_companyfacts(), [submissions_row(*Q1)], {})
    result = screen(cfg, source)
    assert result.status == "insufficient_data"
    assert "no periodic filing listed after period 2026-03-31" in result.reason


def filing(accession, period, lag=40):
    """A periodic report for `period`, filed `lag` days after it."""
    return FilingEntry(1234, accession, "10-Q", period + timedelta(days=lag), f"{accession}.htm", (), period)


def test_the_next_report_is_due_on_the_companys_own_calendar_plus_its_longest_lag(cfg):
    """Last year the company reported 31 March, then 30 June, with a 75-day annual filing lag. Its 30 June
    report this year is therefore due 75 days after 30 June, plus the grace."""
    periods = [
        filing("y-mar", date(2025, 3, 31), lag=40),
        filing("y-jun", date(2025, 6, 30), lag=40),
        filing("y-dec", date(2025, 12, 31), lag=75),
        filing("q1", date(2026, 3, 31), lag=29),
    ]
    newer, due = _newer_and_due(periods, date(2026, 3, 31), AS_OF, cfg)
    assert newer is None
    assert due == date(2026, 6, 30) + timedelta(days=75 + 45)


def test_a_fiscal_calendar_is_followed_not_a_quarter_guessed(cfg):
    """A 52-week calendar whose year-end falls in late August: last year's report after 9 May was for
    29 August, so this year's is not due until well after the first of October."""
    periods = [
        filing("y-may", date(2025, 5, 10), lag=40),
        filing("y-aug", date(2025, 8, 30), lag=80),
        filing("q3", date(2026, 5, 9), lag=40),
    ]
    newer, due = _newer_and_due(periods, date(2026, 5, 9), AS_OF, cfg)
    assert newer is None
    assert due == date(2025, 8, 30) + timedelta(days=365 + 80 + 45)  # last year's 30 Aug, a year on, plus lag and grace
    assert due > AS_OF


def test_a_company_with_no_history_of_the_same_point_uses_the_default_cadence(cfg):
    newer, due = _newer_and_due([filing("q1", date(2026, 3, 31), lag=40)], date(2026, 3, 31), AS_OF, cfg)
    assert newer is None
    assert due == date(2026, 3, 31) + timedelta(days=91 + 40 + 45)


def test_a_later_report_is_the_newer_one(cfg):
    q2 = filing("q2", date(2026, 6, 30))
    newer, due = _newer_and_due([q2, filing("q1", date(2026, 3, 31))], date(2026, 3, 31), AS_OF, cfg)
    assert newer == ("q2", date(2026, 6, 30), q2.filed) and due is None

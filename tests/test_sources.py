import pytest

from halal_heatmap.config import ConfigError
from halal_heatmap.sources import SourceError
from halal_heatmap.sources.edgar import EdgarClient, RateLimiter, instance_name
from halal_heatmap.sources.wikipedia import parse_constituents


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
        return self.responses.pop(0)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def edgar_cfg(cfg, tmp_path):
    import dataclasses

    return dataclasses.replace(cfg.edgar, cache_dir=str(tmp_path / "cache"))


def client(edgar_cfg, session):
    return EdgarClient(edgar_cfg, session=session, sleep=lambda s: None)


def test_user_agent_comes_from_the_environment(edgar_cfg, monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Test Person test@example.com")
    session = FakeSession([FakeResponse(payload={"facts": {}})])
    assert client(edgar_cfg, session).company_facts(320193) == {"facts": {}}
    url, headers = session.calls[0]
    assert url == "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
    assert headers["User-Agent"] == "Test Person test@example.com"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_user_agent_is_a_hard_error(edgar_cfg, monkeypatch, value):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    if value is not None:
        monkeypatch.setenv("SEC_USER_AGENT", value)
    with pytest.raises(ConfigError, match="SEC_USER_AGENT"):
        EdgarClient(edgar_cfg, session=FakeSession([]))


def test_responses_are_cached(edgar_cfg, monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Test Person test@example.com")
    session = FakeSession([FakeResponse(payload={"sic": "3571"})])
    edgar = client(edgar_cfg, session)
    assert edgar.submissions(1) == edgar.submissions(1) == {"sic": "3571"}
    assert len(session.calls) == 1


def test_retries_then_succeeds_and_gives_up_on_client_errors(edgar_cfg, monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Test Person test@example.com")
    session = FakeSession([FakeResponse(429), FakeResponse(503), FakeResponse(payload={"ok": 1})])
    assert client(edgar_cfg, session).company_facts(1) == {"ok": 1}
    assert len(session.calls) == 3

    session = FakeSession([FakeResponse(404), FakeResponse(payload={})])
    with pytest.raises(SourceError, match="HTTP 404"):
        client(edgar_cfg, session).company_facts(2)
    assert len(session.calls) == 1


INSTANCE = (
    b'<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:us-gaap="http://fasb.org/us-gaap/2025">'
    b'<context id="c"><entity><identifier scheme="x">1</identifier></entity>'
    b"<period><instant>2026-06-30</instant></period></context>"
    b'<unit id="u"><measure>iso4217:USD</measure></unit>'
    b'<us-gaap:Assets contextRef="c" unitRef="u">5000</us-gaap:Assets></xbrl>'
)
ASSETS = [{"tag": "us-gaap:Assets", "unit": "USD", "val": 5000.0, "start": None, "end": "2026-06-30", "dims": []}]


def test_filing_instance_is_fetched_from_the_archive_and_cached_for_good(edgar_cfg, monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Test Person test@example.com")
    session = FakeSession([FakeResponse(content=INSTANCE)])
    edgar = client(edgar_cfg, session)
    assert edgar.filing_instance(320193, "0000320193-26-000020", "aapl-20260627.htm") == ASSETS
    assert edgar.filing_instance(320193, "0000320193-26-000020", "aapl-20260627.htm") == ASSETS
    url, headers = session.calls[0]
    assert url == "https://www.sec.gov/Archives/edgar/data/320193/000032019326000020/aapl-20260627_htm.xml"
    assert headers["User-Agent"] == "Test Person test@example.com"
    assert len(session.calls) == 1


def test_filing_instance_falls_back_to_the_folder_listing(edgar_cfg, monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Test Person test@example.com")
    listing = {"directory": {"item": [{"name": "x-20200630_lab.xml"}, {"name": "x-20200630.xml"}, {"name": "x.htm"}]}}
    session = FakeSession([FakeResponse(404), FakeResponse(payload=listing), FakeResponse(content=INSTANCE)])
    assert client(edgar_cfg, session).filing_instance(1, "0000000001-20-000001", "x.htm") == ASSETS
    assert [url.rpartition("/")[2] for url, _ in session.calls] == ["x_htm.xml", "index.json", "x-20200630.xml"]


def test_filing_without_an_instance_is_a_source_error(edgar_cfg, monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Test Person test@example.com")
    listing = {"directory": {"item": [{"name": "x.htm"}, {"name": "FilingSummary.xml"}]}}
    session = FakeSession([FakeResponse(404), FakeResponse(payload=listing)])
    with pytest.raises(SourceError, match="no XBRL instance"):
        client(edgar_cfg, session).filing_instance(1, "0000000001-20-000001", "x.htm")
    session = FakeSession([FakeResponse(content=b"<html>error page</html>")])
    with pytest.raises(SourceError, match="XBRL instance"):
        client(edgar_cfg, session).filing_instance(1, "0000000001-20-000002", "x.htm")


def test_instance_name_prefers_the_inline_extract():
    items = [{"name": n} for n in ("a_cal.xml", "a_pre.xml", "FilingSummary.xml", "a.xml", "a_htm.xml")]
    assert instance_name({"directory": {"item": items}}) == "a_htm.xml"
    assert instance_name({"directory": {"item": items[:4]}}) == "a.xml"
    assert instance_name({}) is None


def test_rate_limiter_spaces_requests():
    clock = FakeClock()
    limiter = RateLimiter(8, clock=clock, sleep=clock.sleep)
    for _ in range(17):
        limiter.wait()
    assert clock.now == pytest.approx(16 / 8)  # 17 requests take at least 2 seconds
    assert all(s == pytest.approx(1 / 8) for s in clock.sleeps)


def test_rate_limiter_does_not_sleep_when_idle():
    clock = FakeClock()
    limiter = RateLimiter(8, clock=clock, sleep=clock.sleep)
    limiter.wait()
    clock.now += 5
    limiter.wait()
    assert clock.sleeps == []


@pytest.mark.parametrize("rate", [0, -1, 10.5, 50])
def test_rate_above_the_sec_limit_is_rejected(rate):
    with pytest.raises(ConfigError):
        RateLimiter(rate)


def wiki_html(rows, table_id="constituents"):
    body = "".join(
        f"<tr><td><a href='#'>{t}</a></td><td>{n}</td><td>{s}</td><td>{sub}</td><td>HQ</td><td>1990-01-01</td>"
        f"<td>{cik}</td><td>1900</td></tr>"
        for t, n, s, sub, cik in rows
    )
    return (
        "<table id='other'><tr><th>Symbol</th></tr><tr><td>ZZZ</td></tr></table>"
        f"<table id='{table_id}'><tr><th>Symbol</th><th>Security</th><th>GICS Sector</th>"
        "<th>GICS Sub-Industry</th><th>Headquarters Location</th><th>Date added</th><th>CIK</th>"
        f"<th>Founded</th></tr>{body}</table>"
    )


def rows(n):
    return [(f"T{i}", f"Company {i}", "Information Technology", "Semiconductors", f"{i:010d}") for i in range(1, n + 1)]


def test_parse_constituents(cfg):
    data = rows(500)
    data[0] = ("BRK.B", "Berkshire Hathaway", "Financials", "Multi-Sector Holdings", "0001067983")
    parsed = parse_constituents(wiki_html(data), cfg.constituents)
    assert len(parsed) == 500
    first = parsed[0]
    assert (first.ticker, first.name, first.gics_sub_industry, first.cik) == (
        "BRK.B",
        "Berkshire Hathaway",
        "Multi-Sector Holdings",
        1067983,
    )


def test_constituent_sanity_checks(cfg):
    with pytest.raises(SourceError, match="expected"):
        parse_constituents(wiki_html(rows(40)), cfg.constituents)
    with pytest.raises(SourceError, match="not found"):
        parse_constituents(wiki_html(rows(500), table_id="renamed"), cfg.constituents)
    bad = rows(500)
    bad[3] = ("T4", "Company 4", "Information Technology", "Semiconductors", "n/a")
    with pytest.raises(SourceError, match="CIK"):
        parse_constituents(wiki_html(bad), cfg.constituents)
    dup = rows(500)
    dup[3] = dup[2]
    with pytest.raises(SourceError, match="duplicate"):
        parse_constituents(wiki_html(dup), cfg.constituents)


# --- prices: retry with backoff behind the PriceSource interface -----------------------------

import sys  # noqa: E402
import types  # noqa: E402
from datetime import date, datetime  # noqa: E402

from halal_heatmap.sources.prices import YFinancePrices  # noqa: E402


class _Frame:
    def __init__(self, closes):
        self.empty = not closes
        self._closes = closes

    def __getitem__(self, column):
        return self

    def items(self):
        return iter(self._closes)


def _fake_yfinance(monkeypatch, outcomes, calls):
    """A yfinance stand-in: each history() call takes the next outcome, an exception or a frame."""

    class Ticker:
        def __init__(self, symbol):
            self.splits = {}

        def history(self, **kwargs):
            calls.append(kwargs)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=Ticker))


CLOSES = _Frame([(datetime(2026, 8, 3), 101.0), (datetime(2026, 8, 4), 102.5)])


def test_a_throttled_fetch_is_retried_with_doubling_backoff(monkeypatch):
    calls, slept = [], []
    _fake_yfinance(monkeypatch, [RuntimeError("429"), RuntimeError("429"), CLOSES], calls)
    prices = YFinancePrices(4, 5, sleep=slept.append)
    history = prices.history("AAPL", date(2026, 8, 1), date(2026, 8, 4))
    assert history.closes == [(date(2026, 8, 3), 101.0), (date(2026, 8, 4), 102.5)]
    assert slept == [5, 10]
    assert len(calls) == 3


def test_an_empty_frame_is_a_failed_attempt_and_is_retried(monkeypatch):
    calls, slept = [], []
    _fake_yfinance(monkeypatch, [_Frame([]), CLOSES], calls)
    YFinancePrices(4, 5, sleep=slept.append).history("AAPL", date(2026, 8, 1), date(2026, 8, 4))
    assert slept == [5]


def test_a_successful_first_fetch_does_not_sleep(monkeypatch):
    calls, slept = [], []
    _fake_yfinance(monkeypatch, [CLOSES], calls)
    YFinancePrices(4, 5, sleep=slept.append).history("AAPL", date(2026, 8, 1), date(2026, 8, 4))
    assert slept == []


def test_giving_up_after_the_last_attempt_is_a_source_error(monkeypatch):
    calls, slept = [], []
    _fake_yfinance(monkeypatch, [RuntimeError("a"), RuntimeError("b"), RuntimeError("c")], calls)
    with pytest.raises(SourceError, match="price fetch failed for AAPL: c"):
        YFinancePrices(3, 2, sleep=slept.append).history("AAPL", date(2026, 8, 1), date(2026, 8, 4))
    assert len(calls) == 3
    assert slept == [2, 4]  # no sleep after the final attempt


def test_a_dotted_ticker_is_fetched_under_yfinance_symbol(monkeypatch):
    seen = []

    class Ticker:
        def __init__(self, symbol):
            seen.append(symbol)
            self.splits = {}

        def history(self, **kwargs):
            return CLOSES

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=Ticker))
    YFinancePrices(1, 0).history("BRK.B", date(2026, 8, 1), date(2026, 8, 4))
    assert seen == ["BRK-B"]

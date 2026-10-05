"""Facts read from filings' own XBRL: dimensional interest income, net investment income,
predecessor registrants and per-class share counts."""

import dataclasses
import json
from datetime import date

import pytest

from conftest import companyfacts, fact, make_inputs, trading_days
from halal_heatmap.cli import format_record
from halal_heatmap.config import ConfigError, parse_config
from halal_heatmap.facts import CompanyFacts, merge_company_facts
from halal_heatmap.filings import (
    FilingEntry,
    class_share_counts,
    extension_tags,
    income_filings,
    instance_documents,
    list_filings,
)
from halal_heatmap.interest import FilingViews, resolve_interest_income
from halal_heatmap.marketcap import daily_class_market_caps
from halal_heatmap.pipeline import screen_constituent
from halal_heatmap.screen.engine import result_to_record, screen
from halal_heatmap.sources import SourceError
from halal_heatmap.sources.prices import PriceHistory
from halal_heatmap.sources.wikipedia import Constituent
from halal_heatmap.store import RESULT_COLUMNS, Store
from halal_heatmap.xbrl import InstanceError, parse_instance

AS_OF = date(2026, 10, 1)
Q2_END = "2026-06-30"
SEGMENTS = "us-gaap:StatementBusinessSegmentsAxis"
LEGAL_ENTITY = "dei:LegalEntityAxis"
CLASS_AXIS = "us-gaap:StatementClassOfStockAxis"
CLASS_A = "us-gaap:CommonClassAMember"
CLASS_B = "us-gaap:CommonClassBMember"
SHARES_TAG = "dei:EntityCommonStockSharesOutstanding"
COMPANY = Constituent("TEST", "Test Corp", "Information Technology", "Semiconductors", 1234)

Q2 = FilingEntry(1234, "q2-26", "10-Q", date(2026, 7, 30), "test-20260630.htm")
K = FilingEntry(1234, "k", "10-K", date(2026, 2, 10), "test-20251231.htm")

INSTANCE = b"""<?xml version="1.0"?>
<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
      xmlns:us-gaap="http://fasb.org/us-gaap/2025" xmlns:dei="http://xbrl.sec.gov/dei/2025"
      xmlns:tst="http://test.example/20260630" xmlns:iso4217="http://www.xbrl.org/2003/iso4217">
  <context id="ytd"><entity><identifier scheme="x">1</identifier></entity>
    <period><startDate>2026-01-01</startDate><endDate>2026-06-30</endDate></period></context>
  <context id="ytd_seg"><entity><identifier scheme="x">1</identifier>
    <segment><xbrldi:explicitMember dimension="us-gaap:StatementBusinessSegmentsAxis">tst:AlphaMember</xbrldi:explicitMember></segment></entity>
    <period><startDate>2026-01-01</startDate><endDate>2026-06-30</endDate></period></context>
  <context id="cover_a"><entity><identifier scheme="x">1</identifier>
    <segment><xbrldi:explicitMember dimension="us-gaap:StatementClassOfStockAxis">us-gaap:CommonClassAMember</xbrldi:explicitMember></segment></entity>
    <period><instant>2026-07-20</instant></period></context>
  <unit id="usd"><measure>iso4217:USD</measure></unit>
  <unit id="shares"><measure>shares</measure></unit>
  <unit id="eps"><divide><unitNumerator><measure>iso4217:USD</measure></unitNumerator>
    <unitDenominator><measure>shares</measure></unitDenominator></divide></unit>
  <us-gaap:InvestmentIncomeInterest contextRef="ytd" unitRef="usd" decimals="-6">23000000</us-gaap:InvestmentIncomeInterest>
  <tst:InterestIncomeNonoperating contextRef="ytd_seg" unitRef="usd" decimals="-6">7000000</tst:InterestIncomeNonoperating>
  <us-gaap:EarningsPerShareBasic contextRef="ytd" unitRef="eps" decimals="2">1.23</us-gaap:EarningsPerShareBasic>
  <us-gaap:Revenues contextRef="ytd" unitRef="usd" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/>
  <dei:EntityCommonStockSharesOutstanding contextRef="cover_a" unitRef="shares" decimals="0">100</dei:EntityCommonStockSharesOutstanding>
  <dei:TradingSymbol contextRef="cover_a">TEST</dei:TradingSymbol>
  <us-gaap:AccountingPolicyTextBlock contextRef="ytd">long text</us-gaap:AccountingPolicyTextBlock>
</xbrl>"""  # noqa: E501


def test_instance_parser_keeps_dimensions_and_company_tags():
    facts = {f["tag"]: f for f in parse_instance(INSTANCE)}
    plain = facts["us-gaap:InvestmentIncomeInterest"]
    assert (plain["val"], plain["unit"], plain["start"], plain["end"], plain["dims"]) == (
        23000000.0,
        "USD",
        "2026-01-01",
        Q2_END,
        [],
    )
    assert facts["tst:InterestIncomeNonoperating"]["dims"] == [[SEGMENTS, "tst:AlphaMember"]]
    shares = facts[SHARES_TAG]
    assert (shares["val"], shares["unit"], shares["start"], shares["end"]) == (100.0, "shares", None, "2026-07-20")
    assert facts["dei:TradingSymbol"]["val"] == "TEST" and facts["dei:TradingSymbol"]["unit"] is None
    # Ratio units, nil facts and text outside the cover page are dropped.
    assert not {"us-gaap:EarningsPerShareBasic", "us-gaap:Revenues", "us-gaap:AccountingPolicyTextBlock"} & set(facts)


@pytest.mark.parametrize("content", [b"<html><body>Not found</body></html>", b"not xml at all"])
def test_instance_parser_rejects_other_documents(content):
    with pytest.raises(InstanceError):
        parse_instance(content)


def submissions(*rows, sic="3674"):
    recent = {
        "accessionNumber": [r[0] for r in rows],
        "form": [r[1] for r in rows],
        "filingDate": [r[2] for r in rows],
        "primaryDocument": [f"{r[0]}.htm" for r in rows],
        "items": [r[3] if len(r) > 3 else "" for r in rows],
    }
    return {"sic": sic, "filings": {"recent": recent}}


def test_filing_list_is_point_in_time_and_form_filtered():
    doc = submissions(
        ("q3", "10-Q", "2026-10-28"),
        ("q2", "10-Q", "2026-07-30"),
        ("e", "8-K", "2026-07-01"),
        ("k", "10-K", "2026-02-10"),
    )
    listed = list_filings(doc, 1234, ("10-K", "10-Q"), AS_OF)
    assert [f.accession for f in listed] == ["q2", "k"]
    assert listed[0] == FilingEntry(1234, "q2", "10-Q", date(2026, 7, 30), "q2.htm")
    assert list_filings({}, 1234, ("10-K",), AS_OF) == []


def test_income_filings_are_the_anchor_and_the_latest_annual_report():
    older_k = dataclasses.replace(K, accession="k-24", filed=date(2025, 2, 10))
    q1 = dataclasses.replace(Q2, accession="q1-26", filed=date(2026, 4, 30))
    filings = [Q2, q1, K, older_k]
    assert income_filings(filings, "q2-26", ("10-K",)) == [Q2, K]
    assert income_filings(filings, "k", ("10-K",)) == [K]
    assert income_filings(filings, "unknown", ("10-K",)) == []


def xfact(tag, val, start=None, end=Q2_END, dims=(), unit="USD"):
    return {"tag": tag, "unit": unit, "val": val, "start": start, "end": end, "dims": [list(d) for d in dims]}


def test_dimension_members_are_summed_only_on_listed_axes():
    facts = [
        xfact("us-gaap:InvestmentIncomeInterest", 10, "2026-01-01", dims=[(SEGMENTS, "t:AMember")]),
        xfact("us-gaap:InvestmentIncomeInterest", 30, "2026-01-01", dims=[(SEGMENTS, "t:BMember")]),
        xfact("us-gaap:InvestmentIncomeInterest", 900, "2026-01-01", dims=[(LEGAL_ENTITY, "t:SubsidiaryMember")]),
        xfact(
            "us-gaap:InvestmentIncomeInterest",
            5,
            "2026-01-01",
            dims=[(LEGAL_ENTITY, "t:SubsidiaryMember"), (SEGMENTS, "t:AMember")],
        ),
        xfact("us-gaap:Revenues", 4000, "2026-01-01"),
    ]
    plain, summed = instance_documents([(Q2, facts)], frozenset({SEGMENTS}), 2)
    assert set(plain["facts"]["us-gaap"]) == {"Revenues"}
    (item,) = summed["facts"]["us-gaap"]["InvestmentIncomeInterest"]["units"]["USD"]
    assert item["val"] == 40 and item["accn"] == "q2-26" and item["filed"] == "2026-07-30"
    assert item["dims"] == f"sum over {SEGMENTS}: t:AMember = 10; t:BMember = 30"


def test_a_single_member_is_not_a_total():
    facts = [xfact("us-gaap:InvestmentIncomeInterest", 10, "2026-01-01", dims=[(SEGMENTS, "t:CorporateMember")])]
    _, summed = instance_documents([(Q2, facts)], frozenset({SEGMENTS}), 2)
    assert summed["facts"] == {}
    _, summed = instance_documents([(Q2, facts)], frozenset({SEGMENTS}), 1)
    assert summed["facts"]["us-gaap"]["InvestmentIncomeInterest"]["units"]["USD"][0]["val"] == 10


def test_overlapping_breakdowns_take_the_larger_total():
    other = "us-gaap:InvestmentTypeAxis"
    facts = [
        xfact("us-gaap:InvestmentIncomeInterest", 10, "2026-01-01", dims=[(SEGMENTS, "t:AMember")]),
        xfact("us-gaap:InvestmentIncomeInterest", 15, "2026-01-01", dims=[(SEGMENTS, "t:BMember")]),
        xfact("us-gaap:InvestmentIncomeInterest", 20, "2026-01-01", dims=[(other, "t:BondsMember")]),
        xfact("us-gaap:InvestmentIncomeInterest", 12, "2026-01-01", dims=[(other, "t:CashMember")]),
    ]
    _, summed = instance_documents([(Q2, facts)], frozenset({SEGMENTS, other}), 2)
    assert summed["facts"]["us-gaap"]["InvestmentIncomeInterest"]["units"]["USD"][0]["val"] == 32


def test_extension_tags_match_by_name_outside_the_standard_taxonomies():
    facts = [
        xfact("tst:InterestIncomeNonoperating", 5, "2026-01-01"),
        xfact("tst:InterestIncomeFromAffiliatesNet", 5, "2026-01-01"),
        xfact("us-gaap:InterestIncomeNonoperating", 5, "2026-01-01"),
    ]
    plain, _ = instance_documents([(Q2, facts)], frozenset(), 2)
    assert [str(t) for t in extension_tags(plain, ("InterestIncomeNonoperating",))] == [
        "tst:InterestIncomeNonoperating"
    ]


def views(cfg, plain_facts=(), summed_facts=(), as_of=AS_OF):
    """Filing views from (filing, facts) pairs, with dimensional facts on the segment axis."""
    loaded = list(plain_facts) + list(summed_facts)
    plain, summed = instance_documents(loaded, frozenset({SEGMENTS}), 2)
    names = cfg.interest_income_sources.extension_tags
    return FilingViews(
        CompanyFacts(plain, forms=cfg.filings.forms, as_of=as_of),
        CompanyFacts(summed, forms=cfg.filings.forms, as_of=as_of),
        tuple(dict.fromkeys(extension_tags(plain, names) + extension_tags(summed, names))),
    )


def duration(tag, annual, prior_ytd, ytd, dims=()):
    """Filing facts for one tag: the 10-K's annual figure, and the 10-Q's two year-to-date figures."""
    return [
        (K, [xfact(tag, annual, "2025-01-01", "2025-12-31", dims)]),
        (Q2, [xfact(tag, prior_ytd, "2025-01-01", "2025-06-30", dims), xfact(tag, ytd, "2026-01-01", Q2_END, dims)]),
    ]


def cf_duration(annual, prior_ytd, ytd):
    return [
        fact("2025-12-31", annual, "2026-02-10", start="2025-01-01", form="10-K", accn="k"),
        fact("2025-06-30", prior_ytd, "2025-07-30", start="2025-01-01", accn="q2-25"),
        fact(Q2_END, ytd, "2026-07-30", start="2026-01-01", accn="q2-26"),
    ]


def resolve(cfg, usd, filing_views=None):
    cf = CompanyFacts(companyfacts(usd), forms=cfg.filings.forms, as_of=AS_OF)
    return resolve_interest_income(
        cf,
        cfg.inputs["interest_income"],
        date.fromisoformat(Q2_END),
        cfg.periods,
        cfg.interest_income_sources,
        lambda: filing_views,
    )


def test_companyfacts_gross_figure_wins_and_filings_are_not_read(cfg):
    def never():
        raise AssertionError("filings must not be read when companyfacts has the figure")

    cf = CompanyFacts(
        companyfacts(
            {"InvestmentIncomeInterest": cf_duration(40, 18, 23), "InvestmentIncomeNet": cf_duration(9, 4, 5)}
        ),
        forms=cfg.filings.forms,
        as_of=AS_OF,
    )
    found = resolve_interest_income(
        cf, cfg.inputs["interest_income"], date.fromisoformat(Q2_END), cfg.periods, cfg.interest_income_sources, never
    )
    assert (found.value, found.kind, found.source, found.max_share_of_limit) == (45, "gross", "companyfacts", None)


def test_company_specific_tag_is_read_from_the_filing(cfg):
    found = resolve(cfg, {}, views(cfg, duration("tst:InterestIncomeNonoperating", 40, 18, 23)))
    assert (found.value, found.kind, found.source, found.dimensional) == (45, "gross", "filing_xbrl", False)
    assert found.max_share_of_limit is None and found.annual_fallback is None
    assert {str(u.fact.tag) for u in found.used} == {"tst:InterestIncomeNonoperating"}


def test_unlisted_company_tag_names_are_ignored(cfg):
    found = resolve(cfg, {}, views(cfg, duration("tst:InterestAndOtherIncomeNet", 40, 18, 23)))
    assert found.value is None and found.missing == ("interest_income",)


def test_dimensional_sum_is_capped_and_recorded(cfg):
    a = duration("us-gaap:InvestmentIncomeInterest", 30, 14, 16, dims=[(SEGMENTS, "t:AMember")])
    b = duration("us-gaap:InvestmentIncomeInterest", 10, 4, 7, dims=[(SEGMENTS, "t:BMember")])
    merged = [(filing, fa + fb) for (filing, fa), (_, fb) in zip(a, b, strict=True)]
    found = resolve(cfg, {}, views(cfg, summed_facts=merged))
    assert (found.value, found.source, found.dimensional) == (45, "filing_xbrl", True)
    assert found.max_share_of_limit == cfg.interest_income_sources.dimensional_max_share_of_limit
    assert all(u.fact.dims.startswith(f"sum over {SEGMENTS}") for u in found.used)


def test_plain_annual_figure_is_preferred_over_a_dimensional_sum(cfg):
    a = duration("us-gaap:InvestmentIncomeInterest", 30, 14, 16, dims=[(SEGMENTS, "t:AMember")])
    b = duration("us-gaap:InvestmentIncomeInterest", 10, 4, 7, dims=[(SEGMENTS, "t:BMember")])
    merged = [(filing, fa + fb) for (filing, fa), (_, fb) in zip(a, b, strict=True)]
    annual_only = {"InvestmentIncomeInterest": cf_duration(40, 18, 23)[:1]}
    found = resolve(cfg, annual_only, views(cfg, summed_facts=merged))
    assert (found.value, found.source, found.dimensional) == (40, "companyfacts", False)
    assert found.annual_fallback is not None


def test_net_investment_income_is_the_last_resort(cfg):
    net = {"InvestmentIncomeNet": cf_duration(40, 18, 23)}
    found = resolve(cfg, net)
    assert (found.value, found.kind, found.source) == (45, "net_investment_income", "companyfacts")
    assert found.max_share_of_limit == cfg.interest_income_sources.net_max_share_of_limit
    gross = resolve(cfg, net, views(cfg, duration("tst:InterestIncome", 4, 1, 3)))
    assert (gross.value, gross.kind) == (6, "gross")


def test_net_other_income_tags_are_never_a_source(cfg):
    for tag in ("NonoperatingIncomeExpense", "OtherNonoperatingIncomeExpense", "InterestAndOtherIncome"):
        assert resolve(cfg, {tag: cf_duration(40, 18, 23)}).value is None
    names = {t.name for t in cfg.interest_income_sources.net_tags}
    assert names == {"InvestmentIncomeNet"}


def net_inputs(interest_income, **changes):
    return make_inputs(
        interest_income=interest_income,
        interest_income_kind="net_investment_income",
        interest_income_max_share_of_limit=0.5,
        **changes,
    )


def test_net_investment_income_supports_a_pass_below_half_the_limit(cfg):
    result = screen(net_inputs(24.9), cfg)  # 2.49% of revenue, cap is 2.5%
    assert result.status == "pass" and "net investment income" in result.reason
    record = result_to_record(result, cfg)
    assert record["interest_income_basis"] == "net_investment_income"
    assert record["interest_income_max_ratio"] == pytest.approx(0.025)
    assert record["interest_income_source"] == "companyfacts"


@pytest.mark.parametrize("value", [25.0, 30.0, 49.0, 50.0, 80.0])
def test_net_investment_income_at_or_above_the_cap_is_insufficient_data(cfg, value):
    # Never a fail on this basis, even above the 5% limit: the instruction is insufficient_data.
    result = screen(net_inputs(value), cfg)
    assert result.status == "insufficient_data"
    assert "net investment income" in result.reason and "2.50%" in result.reason
    assert result_to_record(result, cfg)["impure_ratio"] == pytest.approx(value / 1000)


def test_negative_net_investment_income_is_insufficient_data(cfg):
    result = screen(net_inputs(-5.0), cfg)
    assert result.status == "insufficient_data" and "negative" in result.reason


def test_net_basis_cap_is_configurable(raw):
    raw["interest_income_sources"]["net_investment_income"]["max_share_of_limit"] = 0.8
    inputs = dataclasses.replace(net_inputs(30.0), interest_income_max_share_of_limit=0.8)
    assert screen(inputs, parse_config(raw)).status == "pass"
    raw["interest_income_sources"]["net_investment_income"]["max_share_of_limit"] = 1.5
    with pytest.raises(ConfigError, match="max_share_of_limit"):
        parse_config(raw)


def test_net_basis_does_not_rescue_other_failures(cfg):
    assert screen(net_inputs(10.0, debt=400.0), cfg).status == "fail"
    assert screen(net_inputs(10.0, sic="6021", gics_sub_industry="Diversified Banks"), cfg).status == "fail"
    assert screen(net_inputs(10.0, sic="5812", gics_sub_industry="Restaurants"), cfg).status == "needs_review"
    # Above the cap the missing input comes first, as for any other missing input.
    assert screen(net_inputs(30.0, debt=400.0), cfg).status == "insufficient_data"


def test_dimensional_basis_is_capped_the_same_way(cfg):
    below = make_inputs(
        interest_income=20.0,
        interest_income_dimensional=True,
        interest_income_max_share_of_limit=0.5,
        interest_income_source="filing_xbrl",
    )
    result = screen(below, cfg)
    assert result.status == "pass" and "dimension members" in result.reason
    record = result_to_record(result, cfg)
    assert (record["interest_income_basis"], record["interest_income_source"]) == ("ttm", "filing_xbrl")
    assert record["interest_income_dimensional"] == 1
    above = dataclasses.replace(below, interest_income=26.0)
    assert screen(above, cfg).status == "insufficient_data"


def test_gross_basis_has_no_cap(cfg):
    assert screen(make_inputs(interest_income=49.0), cfg).status == "pass"
    record = result_to_record(screen(make_inputs(interest_income=49.0), cfg), cfg)
    assert record["interest_income_max_ratio"] is None and record["interest_income_dimensional"] == 0


def cover(shares_by_member, symbols=None, total=None, end="2026-07-20"):
    facts = [xfact(SHARES_TAG, v, end=end, dims=[(CLASS_AXIS, m)], unit="shares") for m, v in shares_by_member.items()]
    facts += [
        {"tag": "dei:TradingSymbol", "unit": None, "val": s, "start": None, "end": end, "dims": [[CLASS_AXIS, m]]}
        for m, s in (symbols or {}).items()
    ]
    if total is not None:
        facts.append(xfact(SHARES_TAG, total, end=end, unit="shares"))
    return facts


def test_each_listed_class_is_priced_at_its_own_symbol(cfg):
    facts = cover({CLASS_A: 10, CLASS_B: 15000}, {CLASS_A: "BRK.A", CLASS_B: "brk.b", "t:NotesMember": "BRK27"})
    counts, skipped = class_share_counts([(Q2, facts)], cfg.shares.classes, "BRK.B")
    assert [(c.symbol, c.shares, c.listed) for c in counts] == [("BRK.A", 10, True), ("BRK.B", 15000, True)]
    assert skipped == []
    assert counts[0].effective == date(2026, 7, 30) and counts[0].basis == date(2026, 7, 20)
    assert counts[0].fact.dims == f"{CLASS_AXIS}={CLASS_A}" and counts[0].fact.accession == "q2-26"


def test_unlisted_class_is_priced_at_the_largest_listed_class(cfg):
    capital_c = "goog:CapitalClassCMember"
    facts = cover({CLASS_A: 5868, CLASS_B: 835, capital_c: 5527}, {CLASS_A: "GOOGL", capital_c: "GOOG"})
    for ticker in ("GOOGL", "GOOG"):  # both tickers must get the same company-level counts
        counts, _ = class_share_counts([(Q2, facts)], cfg.shares.classes, ticker)
        assert {(c.fact.dims.rpartition("=")[2], c.symbol, c.listed) for c in counts} == {
            (CLASS_A, "GOOGL", True),
            (CLASS_B, "GOOGL", False),
            (capital_c, "GOOG", True),
        }


def test_cover_page_without_class_symbols_treats_the_largest_class_as_listed(cfg):
    counts, _ = class_share_counts([(Q2, cover({CLASS_A: 280, CLASS_B: 70}))], cfg.shares.classes, "TSN")
    assert [(c.symbol, c.shares, c.listed) for c in counts] == [("TSN", 280, True), ("TSN", 70, False)]


def test_preferred_and_unrecognised_members_are_not_counted(cfg):
    preferred = "t:SeriesBMandatoryConvertiblePreferredStockMember"
    units = "t:OperatingGroupUnitsMember"
    counts, skipped = class_share_counts(
        [(Q2, cover({CLASS_A: 224, preferred: 30, units: 100}))], cfg.shares.classes, "ARES"
    )
    assert [(c.shares, c.symbol) for c in counts] == [(224, "ARES")]
    assert skipped == sorted([preferred, units])


def test_unlisted_classes_are_excluded_for_listed_exception_tickers(cfg):
    assert "ARES" in cfg.shares.classes.exclude_unlisted_tickers
    members = {CLASS_A: 224, "us-gaap:CommonClassCMember": 103}
    counts, skipped = class_share_counts([(Q2, cover(members))], cfg.shares.classes, "ARES")
    assert [(c.shares, c.listed) for c in counts] == [(224, True)] and skipped == ["us-gaap:CommonClassCMember"]
    counts, _ = class_share_counts([(Q2, cover(members))], cfg.shares.classes, "TSN")
    assert [(c.shares, c.listed) for c in counts] == [(224, True), (103, False)]


def test_successor_that_kept_its_cik_is_not_flagged(cfg):
    subs = {1234: submissions(*FILED, ("reorg", "8-K12B", "2024-05-17"))}
    record, _ = run(cfg, Filings(companyfacts(usd_document(), PLAIN_SHARES), subs))
    assert "predecessor" not in json.loads(record["input_notes"])


def test_unlisted_classes_can_be_excluded(raw):
    raw["shares"]["classes"]["unlisted"] = "exclude"
    rules = parse_config(raw).shares.classes
    counts, skipped = class_share_counts([(Q2, cover({CLASS_A: 280, CLASS_B: 70}))], rules, "TSN")
    assert [(c.shares, c.listed) for c in counts] == [(280, True)] and skipped == [CLASS_B]
    raw["shares"]["classes"]["unlisted"] = "guess"
    with pytest.raises(ConfigError, match="unlisted"):
        parse_config(raw)


def test_undimensioned_cover_count_is_used_when_no_class_is_reported(cfg):
    counts, _ = class_share_counts([(Q2, cover({}, total=1677))], cfg.shares.classes, "C")
    assert [(c.symbol, c.shares, c.fact.dims) for c in counts] == [("C", 1677, "")]
    assert class_share_counts([(Q2, [xfact("us-gaap:Assets", 5)])], cfg.shares.classes, "C") == ([], [])


def test_company_market_cap_sums_classes_at_their_own_prices(cfg):
    old = dataclasses.replace(Q2, accession="q1-26", filed=date(2026, 4, 30))
    loaded = [
        (old, cover({CLASS_A: 10, CLASS_B: 1000}, {CLASS_A: "X.A", CLASS_B: "X.B"}, end="2026-04-20")),
        (Q2, cover({CLASS_A: 8, CLASS_B: 4000}, {CLASS_A: "X.A", CLASS_B: "X.B"})),
    ]
    counts, _ = class_share_counts(loaded, cfg.shares.classes, "X.B")
    days = [date(2026, 4, 29), date(2026, 4, 30), date(2026, 7, 29), date(2026, 7, 30), date(2026, 7, 31)]
    histories = {
        "X.A": ([(d, 1500.0) for d in days if d != date(2026, 7, 31)], []),  # no class A close on the last day
        "X.B": ([(d, 1.0) for d in days], []),
    }
    daily = daily_class_market_caps(histories, counts)
    assert daily == [
        (date(2026, 4, 30), 10 * 1500 + 1000),
        (date(2026, 7, 29), 10 * 1500 + 1000),
        (date(2026, 7, 30), 8 * 1500 + 4000),
    ]


def test_class_market_cap_is_unchanged_across_a_split(cfg):
    counts, _ = class_share_counts([(Q2, cover({CLASS_A: 100}, {CLASS_A: "X"}))], cfg.shares.classes, "X")
    # Closes are split-adjusted, so the 2-for-1 split shows as a flat 25 against 100 pre-split shares.
    histories = {"X": ([(date(2026, 8, 3), 25.0), (date(2026, 9, 1), 25.0)], [(date(2026, 8, 20), 2.0)])}
    assert daily_class_market_caps(histories, counts) == [(date(2026, 8, 3), 5000.0), (date(2026, 9, 1), 5000.0)]


# End to end over fake sources.


def usd_document(**overrides):
    usd = {
        "Assets": [fact(Q2_END, 5000, "2026-07-30", accn="q2-26")],
        "Liabilities": [fact(Q2_END, 2000, "2026-07-30", accn="q2-26")],
        "LongTermDebt": [fact(Q2_END, 2000, "2026-07-30", accn="q2-26")],
        "CashAndCashEquivalentsAtCarryingValue": [fact(Q2_END, 1500, "2026-07-30", accn="q2-26")],
        "ShortTermInvestments": [fact(Q2_END, 500, "2026-07-30", accn="q2-26")],
        "Revenues": cf_duration(4000, 1800, 2300),  # trailing 4500
        "InvestmentIncomeInterest": cf_duration(40, 18, 23),  # trailing 45
        "InterestExpense": cf_duration(80, 40, 40),
    }
    usd.update(overrides)
    return {tag: items for tag, items in usd.items() if items is not None}


PLAIN_SHARES = {
    SHARES_TAG: [
        fact("2023-07-20", 100, "2023-07-30", accn="q2-23"),
        fact("2026-07-20", 100, "2026-07-30", accn="q2-26"),
    ]
}
FILED = (("q2-26", "10-Q", "2026-07-30"), ("k", "10-K", "2026-02-10"), ("q2-23", "10-Q", "2023-07-30"))


class Filings:
    def __init__(self, docs, subs=None, instances=None, texts=None):
        self.texts = texts or {}
        self.text_calls = []
        self._init(docs, subs, instances)

    def filing_text(self, cik, accession, primary_document):
        self.text_calls.append(accession)
        if accession not in self.texts:
            raise SourceError(f"EDGAR request failed for {accession}")
        return self.texts[accession]

    def _init(self, docs, subs=None, instances=None):
        self.docs = docs if isinstance(docs, dict) and "facts" not in docs else {1234: docs}
        self.subs = subs or {1234: submissions(*FILED)}
        self.instances = instances or {}
        self.instance_calls = []

    def company_facts(self, cik):
        return self.docs[cik]

    def submissions(self, cik):
        return self.subs[cik]

    def filing_instance(self, cik, accession, primary_document):
        self.instance_calls.append((cik, accession, primary_document))
        if accession not in self.instances:
            raise SourceError(f"no XBRL instance document in filing {accession}")
        return self.instances[accession]


class Prices:
    def __init__(self, closes=None):
        self.closes = closes or {}
        self.asked = []

    def history(self, ticker, start, end):
        self.asked.append(ticker)
        close = self.closes.get(ticker, 100.0)
        return PriceHistory([(day, close) for day in trading_days(date(2023, 9, 1), end)], [])


def run(cfg, filings, prices=None, company=COMPANY, **kwargs):
    result, used = screen_constituent(company, AS_OF, cfg, filings, prices or Prices(), trigger="test", **kwargs)
    return result_to_record(result, cfg), used


def test_filing_xbrl_is_not_fetched_when_companyfacts_is_enough(cfg):
    filings = Filings(companyfacts(usd_document(), PLAIN_SHARES))
    record, _ = run(cfg, filings)
    assert record["status"] == "pass" and filings.instance_calls == []
    assert (record["interest_income_basis"], record["interest_income_source"], record["share_source"]) == (
        "ttm",
        "companyfacts",
        "companyfacts",
    )


def test_company_specific_interest_income_end_to_end(cfg, tmp_path):
    instances = dict(
        (filing.accession, facts) for filing, facts in duration("tst:InterestIncomeNonoperating", 40, 18, 23)
    )
    filings = Filings(companyfacts(usd_document(InvestmentIncomeInterest=None), PLAIN_SHARES), instances=instances)
    record, used = run(cfg, filings)
    assert record["status"] == "pass"
    assert (record["impure_income"], record["interest_income_source"]) == (45, "filing_xbrl")
    assert filings.instance_calls == [(1234, "q2-26", "q2-26.htm"), (1234, "k", "k.htm")]
    assert sorted(u.role for u in used if u.input == "interest_income") == ["fiscal_year", "prior_ytd", "ytd"]


def test_dimensional_interest_income_is_stored_with_its_members(cfg, tmp_path):
    a = duration("us-gaap:InvestmentIncomeInterest", 30, 14, 16, dims=[(SEGMENTS, "t:AMember")])
    b = duration("us-gaap:InvestmentIncomeInterest", 10, 4, 7, dims=[(SEGMENTS, "t:BMember")])
    instances = {filing.accession: fa + fb for (filing, fa), (_, fb) in zip(a, b, strict=True)}
    filings = Filings(companyfacts(usd_document(InvestmentIncomeInterest=None), PLAIN_SHARES), instances=instances)
    record, used = run(cfg, filings)
    assert record["status"] == "pass" and "dimension members" in record["reason"]
    assert (record["impure_income"], record["interest_income_dimensional"]) == (45, 1)
    store = Store(tmp_path / "screens.db")
    result_id = store.save_result(store.start_run(AS_OF.isoformat(), cfg.hash), record, used)
    rows = [r for r in store.facts_for(result_id) if r["input"] == "interest_income"]
    assert len(rows) == 3 and all("t:AMember" in r["dimensions"] and "t:BMember" in r["dimensions"] for r in rows)
    assert [r["dimensions"] for r in store.facts_for(result_id) if r["input"] == "revenue"] == [None] * 3


def test_unreadable_filing_xbrl_is_noted_and_gives_no_interest_figure(cfg):
    filings = Filings(companyfacts(usd_document(InvestmentIncomeInterest=None), PLAIN_SHARES))
    record, _ = run(cfg, filings)
    assert record["interest_income"] is None and record["interest_income_basis"] == "upper_bound_no_disclosure"
    assert "no XBRL instance document" in json.loads(record["input_notes"])["filing_xbrl"]


def test_net_investment_income_end_to_end(cfg):
    doc = usd_document(InvestmentIncomeInterest=None, InvestmentIncomeNet=cf_duration(40, 18, 23))
    record, _ = run(cfg, Filings(companyfacts(doc, PLAIN_SHARES)))  # 45 / 4500 = 1.0%
    assert record["status"] == "pass" and record["interest_income_basis"] == "net_investment_income"
    doc = usd_document(InvestmentIncomeInterest=None, InvestmentIncomeNet=cf_duration(120, 50, 65))
    record, _ = run(cfg, Filings(companyfacts(doc, PLAIN_SHARES)))  # 135 / 4500 = 3.0%
    assert record["status"] == "insufficient_data" and record["impure_ratio"] == pytest.approx(0.03)


def test_predecessor_filings_complete_a_successors_history(raw):
    """Exxon in 2026: the holding company's CIK has one 10-Q; everything earlier is under the old CIK."""
    new, old = 999, 34
    successor = companyfacts(
        {tag: [i for i in items if i["accn"] == "q2-26"] for tag, items in usd_document().items()},
        {SHARES_TAG: [fact("2026-07-20", 100, "2026-07-30", accn="q2-26")]},
    )
    predecessor = companyfacts(
        {tag: [i for i in items if i["accn"] != "q2-26"] for tag, items in usd_document().items()},
        {SHARES_TAG: [fact("2023-07-20", 100, "2023-07-30", accn="q2-23")]},
    )
    subs = {
        new: submissions(("q2-26", "10-Q", "2026-07-30"), ("reorg", "8-K12B", "2026-07-01")),
        old: submissions(("k", "10-K", "2026-02-10"), ("q2-23", "10-Q", "2023-07-30")),
    }
    company = Constituent("XOM", "Exxon", "Energy", "Integrated Oil & Gas", new)
    cfg = parse_config(raw)
    record, _ = run(cfg, Filings({new: successor, old: predecessor}, subs), company=company)
    assert record["status"] == "insufficient_data"
    assert "revenue" in record["reason"] and "market cap avg_12m" in record["reason"]
    assert "8-K12B on 2026-07-01" in json.loads(record["input_notes"])["predecessor"]

    raw["predecessors"] = {new: [old]}
    record, _ = run(parse_config(raw), Filings({new: successor, old: predecessor}, subs), company=company)
    assert record["status"] == "pass"
    assert record["revenue"] == 4500 and record["mcap_avg_36m"] == pytest.approx(10000)
    assert "predecessor" not in json.loads(record["input_notes"])


def test_merged_company_facts_keep_every_registrants_items():
    a = companyfacts({"Assets": [fact(Q2_END, 5000, "2026-07-30")]})
    b = companyfacts(
        {"Assets": [fact("2025-12-31", 4000, "2026-02-10")], "Liabilities": [fact(Q2_END, 1, "2026-07-30")]}
    )
    merged = merge_company_facts([a, b, {}])
    assert [i["val"] for i in merged["facts"]["us-gaap"]["Assets"]["units"]["USD"]] == [5000, 4000]
    assert "Liabilities" in merged["facts"]["us-gaap"]
    assert len(a["facts"]["us-gaap"]["Assets"]["units"]["USD"]) == 1  # inputs are not modified


def class_filings(shares_by_member, symbols=None, doc_shares=None):
    instances = {
        "q2-26": cover(shares_by_member, symbols),
        "k": cover(shares_by_member, symbols, end="2026-02-01"),
        "q2-23": cover(shares_by_member, symbols, end="2023-07-20"),
    }
    return Filings(companyfacts(usd_document(), doc_shares or {}), instances=instances)


def test_multi_class_company_gets_a_company_level_market_cap(cfg):
    """Berkshire: no share count in companyfacts, two listed classes at very different prices."""
    filings = class_filings({CLASS_A: 4, CLASS_B: 4000}, {CLASS_A: "BRK.A", CLASS_B: "BRK.B"})
    prices = Prices({"BRK.A": 1500.0, "BRK.B": 1.0})
    company = Constituent("BRK.B", "Berkshire", "Information Technology", "Semiconductors", 1234)
    record, used = run(cfg, filings, prices, company=company)
    assert record["mcap_spot"] == record["mcap_avg_36m"] == pytest.approx(4 * 1500 + 4000)
    assert (record["share_source"], record["unlisted_class_share"]) == ("filing_xbrl", 0)
    assert sorted(prices.asked) == ["BRK.A", "BRK.B"]
    classes = json.loads(record["share_classes"])
    assert [(c["class"], c["priced_as"], c["shares"]) for c in classes] == [
        (CLASS_A, "BRK.A", 4),
        (CLASS_B, "BRK.B", 4000),
    ]
    assert sorted(u.role for u in used if u.input == "shares_outstanding") == ["latest:BRK.A", "latest:BRK.B"]
    assert record["status"] == "pass"


def test_unlisted_class_share_of_market_cap_is_recorded(cfg):
    filings = class_filings({CLASS_A: 75, CLASS_B: 25})
    record, _ = run(cfg, filings)
    assert record["mcap_spot"] == pytest.approx(10000) and record["status"] == "pass"
    assert record["unlisted_class_share"] == pytest.approx(0.25)
    assert "unlisted share classes" in format_record(record, [])


def test_two_tickers_of_one_company_share_one_market_cap(cfg):
    """Alphabet: companyfacts has a usable total, but each ticker must get the same company figure."""
    capital_c = "goog:CapitalClassCMember"
    members = {CLASS_A: 50, CLASS_B: 10, capital_c: 40}
    symbols = {CLASS_A: "GOOGL", capital_c: "GOOG"}
    prices = {"GOOGL": 100.0, "GOOG": 110.0}
    records = {}
    for ticker in ("GOOGL", "GOOG"):
        company = Constituent(ticker, "Alphabet", "Information Technology", "Semiconductors", 1234)
        filings = class_filings(members, symbols, PLAIN_SHARES)
        records[ticker], _ = run(cfg, filings, Prices(prices), company=company, prefer_class_shares=True)
    expected = 50 * 100 + 10 * 100 + 40 * 110
    assert records["GOOGL"]["mcap_avg_12m"] == records["GOOG"]["mcap_avg_12m"] == pytest.approx(expected)
    assert records["GOOGL"]["status"] == records["GOOG"]["status"]
    # Without the preference each ticker would price the companyfacts total at its own close.
    plain, _ = run(
        cfg,
        class_filings(members, symbols, PLAIN_SHARES),
        Prices(prices),
        company=Constituent("GOOG", "Alphabet", "Information Technology", "Semiconductors", 1234),
    )
    assert plain["mcap_avg_12m"] == pytest.approx(100 * 110) and plain["share_source"] == "companyfacts"


def test_class_preference_falls_back_to_companyfacts(cfg):
    filings = Filings(companyfacts(usd_document(), PLAIN_SHARES))  # no instance can be read
    record, _ = run(cfg, filings, prefer_class_shares=True)
    assert record["status"] == "pass" and record["share_source"] == "companyfacts"


def test_stale_cover_page_counts_are_not_used(cfg):
    instances = {"q2-23": cover({CLASS_A: 75, CLASS_B: 25}, end="2023-07-20")}
    record, _ = run(cfg, Filings(companyfacts(usd_document(), {}), instances=instances))
    assert record["status"] == "insufficient_data" and "no recent share count found" in record["reason"]


def test_missing_class_price_never_passes(cfg):
    class NoClassA(Prices):
        def history(self, ticker, start, end):
            if ticker == "BRK.A":
                raise SourceError("no prices returned for BRK-A")
            return super().history(ticker, start, end)

    filings = class_filings({CLASS_A: 4, CLASS_B: 4000}, {CLASS_A: "BRK.A", CLASS_B: "BRK.B"})
    company = Constituent("BRK.B", "Berkshire", "Information Technology", "Semiconductors", 1234)
    record, _ = run(cfg, filings, NoClassA(), company=company)
    assert record["status"] == "insufficient_data" and record["mcap_spot"] is None


def test_old_database_gains_the_new_columns(cfg, tmp_path):
    import sqlite3

    added = (
        "share_source",
        "share_classes",
        "unlisted_class_share",
        "interest_income_source",
        "interest_income_dimensional",
        "interest_income_annual",
        "interest_income_max_ratio",
    )
    old_columns = ", ".join(
        f"{name} {kind.replace(' NOT NULL', '')}" for name, kind in RESULT_COLUMNS.items() if name not in added
    )
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE screen_results (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, created_at TEXT, "
        f"{old_columns});"
        "CREATE TABLE input_facts (result_id INTEGER, input TEXT, component TEXT, role TEXT, tag TEXT, unit TEXT,"
        " value REAL, period_start TEXT, period_end TEXT, accession TEXT, form TEXT, filed TEXT);"
        "INSERT INTO screen_results (run_id, created_at, ticker) VALUES (1, 'then', 'OLD');"
    )
    conn.commit()
    conn.close()
    store = Store(path)
    record, used = run(cfg, Filings(companyfacts(usd_document(), PLAIN_SHARES)))
    store.save_result(store.start_run(AS_OF.isoformat(), cfg.hash), record, used)
    assert store.latest_result("OLD")["interest_income_basis"] is None  # old rows are kept, new columns empty
    assert store.latest_result("TEST")["share_source"] == "companyfacts"


SPIN = "On October 1, 2026, the Company completed the separation of its Seed Business into Vylor Inc."
SALE = "On September 1, 2026, the Company completed the previously announced sale of its packaging division."


def event_run(cfg, reports, texts, doc=None, as_of=AS_OF):
    subs = {1234: submissions(*FILED, *reports)}
    filings = Filings(doc or companyfacts(usd_document(), PLAIN_SHARES), subs, texts=texts)
    result, used = screen_constituent(COMPANY, as_of, cfg, filings, Prices(), trigger="test")
    return result_to_record(result, cfg), filings


def test_spin_off_reported_after_the_balance_sheet_withdraws_a_pass(cfg):
    """Corteva: separation reported under item 8.01 three months after the balance sheet date."""
    record, _ = event_run(cfg, [("spin", "8-K", "2026-09-30", "8.01,9.01")], {"spin": SPIN})
    assert record["status"] == "insufficient_data" and record["stale_balance_sheet"] == 1
    assert record["post_balance_sheet_event"] == "separation reported in 8-K filed 2026-09-30 (spin)"
    assert "balance sheet is out of date" in record["reason"] and "next periodic filing" in record["reason"]
    assert "balance sheet out of date" in format_record(record, [])


def test_major_disposition_needs_item_2_01(cfg):
    record, _ = event_run(cfg, [("sale", "8-K", "2026-09-02", "2.01,9.01")], {"sale": SALE})
    assert record["status"] == "insufficient_data" and "disposition reported" in record["post_balance_sheet_event"]
    record, _ = event_run(cfg, [("sale", "8-K", "2026-09-02", "8.01")], {"sale": SALE})  # a routine sale notice
    assert record["status"] == "pass" and record["post_balance_sheet_event"] == ""


def test_event_before_the_balance_sheet_date_is_already_in_it(cfg):
    """Honeywell: the disposition was completed the day before the quarter end."""
    record, filings = event_run(cfg, [("spin", "8-K", "2026-06-29", "2.01")], {"spin": SPIN})
    assert record["status"] == "pass" and record["post_balance_sheet_event"] == ""
    assert filings.text_calls == []


def test_only_reports_with_a_listed_item_and_matching_words_count(cfg):
    reports = [
        ("earnings", "8-K", "2026-09-10", "2.02,9.01"),  # item not listed: never fetched
        ("deal", "8-K", "2026-09-12", "2.01"),
        ("plan", "8-K", "2026-09-15", "8.01"),
    ]
    texts = {
        "deal": "The Company completed the acquisition of Widget Corp.",
        "plan": "The Company announced its intention to pursue a separation of its Seed Business in 2027.",
    }
    record, filings = event_run(cfg, reports, texts)
    assert record["status"] == "pass" and record["post_balance_sheet_event"] == ""
    assert filings.text_calls == ["deal", "plan"]


@pytest.mark.parametrize(
    "text",
    [
        "The SMR will no longer apply to the new notes upon completion of the Separation.",
        "The appointments are conditioned upon the completion of the Spin-Off, which is subject to approval.",
        "The completion of the distribution is subject to the satisfaction of customary conditions.",
    ],
)
def test_a_planned_separation_is_not_a_completed_one(cfg, text):
    record, _ = event_run(cfg, [("plan", "8-K", "2026-08-31", "8.01,9.01")], {"plan": text})
    assert record["status"] == "pass" and record["post_balance_sheet_event"] == ""


def test_old_share_counts_outside_the_windows_are_not_checked(cfg):
    shares = {
        SHARES_TAG: [
            fact("2012-07-20", 100000, "2012-07-30", accn="q2-12"),  # mis-scaled long ago: irrelevant now
            fact("2023-01-20", 100, "2023-01-30", accn="k-22"),
            fact("2026-07-20", 100, "2026-07-30", accn="q2-26"),
        ]
    }
    record, _ = run(cfg, Filings(companyfacts(usd_document(), shares)))
    assert record["status"] == "pass"
    assert (record["share_counts_rejected"], record["share_count_jump"], record["share_count_note"]) == (0, 0, "")


def test_event_is_not_visible_before_it_is_filed(cfg):
    report = [("spin", "8-K", "2026-10-15", "8.01")]
    record, _ = event_run(cfg, report, {"spin": SPIN})
    assert record["status"] == "pass"
    record, _ = event_run(cfg, report, {"spin": SPIN}, as_of=date(2026, 10, 20))
    assert record["status"] == "insufficient_data"


def test_event_rule_leaves_a_fail_alone_and_unreadable_reports_are_noted(cfg):
    big_debt = usd_document(LongTermDebt=[fact(Q2_END, 4000, "2026-07-30", accn="q2-26")])
    record, _ = event_run(
        cfg, [("spin", "8-K", "2026-09-30", "8.01")], {"spin": SPIN}, companyfacts(big_debt, PLAIN_SHARES)
    )
    assert record["status"] == "fail" and record["stale_balance_sheet"] == 0
    assert record["post_balance_sheet_event"].startswith("separation reported")
    record, _ = event_run(cfg, [("spin", "8-K", "2026-09-30", "8.01")], {})
    assert record["status"] == "pass" and "events" in json.loads(record["input_notes"])


def test_mis_scaled_share_count_end_to_end(cfg):
    shares = {
        SHARES_TAG: [
            fact("2023-07-20", 100, "2023-07-30", accn="q2-23"),
            fact("2026-02-01", 100000, "2026-02-10", accn="k"),
            fact("2026-04-20", 100, "2026-04-30", accn="q1-26"),
            fact("2026-07-20", 100, "2026-07-30", accn="q2-26"),
        ]
    }
    record, _ = run(cfg, Filings(companyfacts(usd_document(), shares)))
    assert record["status"] == "pass" and record["share_counts_rejected"] == 1
    assert record["mcap_avg_12m"] == pytest.approx(10000)  # not inflated by the bad filing
    assert "rejected share count 100,000 filed 2026-02-10 (k)" in record["share_count_note"]
    assert "share count rejected" in format_record(record, [])


def test_no_reliable_share_count_is_insufficient_data(cfg):
    shares = {
        SHARES_TAG: [
            fact("2026-04-20", 100, "2026-04-30", accn="q1-26"),
            fact("2026-07-20", 90000, "2026-07-30", accn="q2-26"),
        ]
    }
    record, _ = run(cfg, Filings(companyfacts(usd_document(), shares)))
    assert record["status"] == "insufficient_data" and "no reliable share count" in record["reason"]


def test_superseded_run_is_not_read_as_current(cfg, tmp_path):
    store = Store(tmp_path / "screens.db")
    record, used = run(cfg, Filings(companyfacts(usd_document(), PLAIN_SHARES)))
    good = store.start_run(AS_OF.isoformat(), cfg.hash)
    store.save_result(good, record, used)
    bad = store.start_run(AS_OF.isoformat(), cfg.hash)
    store.save_result(bad, {**record, "status": "insufficient_data"}, used)
    assert store.latest_result("TEST")["status"] == "insufficient_data"
    store.supersede_run(bad, "bound applied too broadly")
    assert store.latest_result("TEST")["status"] == "pass" and store.valid_run_ids() == [good]
    row = store.conn.execute("SELECT superseded, superseded_reason FROM runs WHERE id = ?", (bad,)).fetchone()
    assert tuple(row) == (1, "bound applied too broadly")
    assert store.conn.execute("SELECT COUNT(*) FROM screen_results WHERE run_id = ?", (bad,)).fetchone()[0] == 1
    with pytest.raises(ValueError):
        store.supersede_run(bad, " ")
    with pytest.raises(ValueError):
        store.supersede_run(999, "no such run")

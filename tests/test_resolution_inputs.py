"""Regression structures from SEC companyfacts cached during the October 2026 screen.

The figures and filing identities are copied into offline fixtures; no cache or network is needed.
"""

from dataclasses import replace
from datetime import date

import pytest

from conftest import companyfacts, fact, make_inputs
from halal_heatmap.config import ConfigError, parse_config
from halal_heatmap.facts import CompanyFacts, resolve_input
from halal_heatmap.screen.engine import screen

END = date(2026, 6, 30)
AS_OF = date(2026, 10, 9)


def resolve(cfg, name, tags, as_of=AS_OF):
    cf = CompanyFacts(companyfacts(tags), forms=cfg.filings.forms, as_of=as_of)
    return resolve_input(cf, cfg.inputs[name], END, cfg.periods)


@pytest.mark.parametrize(
    "value, filed, accession",
    [
        (985545000, "2026-08-06", "0001628280-26-054458"),  # Datadog
        (1242800000, "2026-07-30", "0001093557-26-000143"),  # Dexcom
    ],
)
def test_convertible_long_term_notes_are_read_and_audited(cfg, value, filed, accession):
    tags = {"ConvertibleLongTermNotesPayable": [fact(END, value, filed, accn=accession)]}
    result = resolve(cfg, "debt", tags)
    assert result.value == value
    assert not result.missing
    assert [(str(u.fact.tag), u.fact.value, u.fact.accession) for u in result.used] == [
        ("us-gaap:ConvertibleLongTermNotesPayable", value, accession)
    ]
    assert resolve(cfg, "debt", tags, date.fromisoformat(filed).replace(day=1)).value is None


def test_verisign_current_and_noncurrent_senior_notes_are_both_audited(cfg):
    accession = "0001014473-26-000028"
    tags = {
        "SeniorLongTermNotes": [fact(END, 1785100000, "2026-07-23", accn=accession)],
        "SeniorNotesCurrent": [fact(END, 549300000, "2026-07-23", accn=accession)],
    }
    result = resolve(cfg, "debt", tags)
    assert result.value == 2334400000
    assert [(str(u.fact.tag), u.fact.value) for u in result.used] == [
        ("us-gaap:SeniorLongTermNotes", 1785100000),
        ("us-gaap:SeniorNotesCurrent", 549300000),
    ]
    # An overlapping broad total is an alternative, never another component to add.
    tags["LongTermDebt"] = [fact(END, 2334400000, "2026-07-23", accn=accession)]
    assert resolve(cfg, "debt", tags).value == 2334400000
    tags["LongTermDebt"] = [fact(END, 2400000000, "2026-07-23", accn=accession)]
    assert resolve(cfg, "debt", tags).value == 2400000000


def test_convertible_notes_choose_the_larger_total_and_keep_separate_leases(cfg):
    tags = {
        "ConvertibleLongTermNotesPayable": [fact(END, 1242800000, "2026-07-30")],
        "LongTermDebt": [fact(END, 1200000000, "2026-07-30")],
        "FinanceLeaseLiability": [fact(END, 52300000, "2026-07-30")],
    }
    result = resolve(cfg, "debt", tags)
    assert result.value == 1295100000
    assert {str(u.fact.tag) for u in result.used} == {
        "us-gaap:ConvertibleLongTermNotesPayable", "us-gaap:FinanceLeaseLiability"
    }


def test_xcel_regulated_and_unregulated_revenue_reconstructs_ttm_with_audit(cfg):
    tags = {
        "RegulatedAndUnregulatedOperatingRevenue": [
            fact("2025-12-31", 14669000000, "2026-02-25", start="2025-01-01", form="10-K",
                 accn="0000072903-26-000009"),
            fact("2025-06-30", 7193000000, "2026-07-30", start="2025-01-01",
                 accn="0000072903-26-000154"),
            fact(END, 7140000000, "2026-07-30", start="2026-01-01", accn="0000072903-26-000154"),
        ]
    }
    result = resolve(cfg, "revenue", tags)
    assert result.value == 14616000000
    assert [(u.role, u.fact.value) for u in result.used] == [
        ("fiscal_year", 14669000000), ("ytd", 7140000000), ("prior_ytd", 7193000000)
    ]
    # A missing annual leg remains missing; no annualisation or default can supply it.
    tags["RegulatedAndUnregulatedOperatingRevenue"].pop(0)
    assert resolve(cfg, "revenue", tags).value is None


@pytest.mark.parametrize("tag", ["LiabilitiesCurrent", "LiabilitiesNoncurrent"])
def test_partial_liabilities_are_not_enough_to_infer_zero_debt(cfg, tag):
    result = resolve(cfg, "total_liabilities", {tag: [fact(END, 1063631000, "2026-07-23")]})
    assert result.value is None
    screened = screen(make_inputs(debt=None, total_liabilities=result.value,
                                  interest_expense=None, interest_expense_reported=False), cfg)
    assert screened.status == "insufficient_data"


@pytest.mark.parametrize("current, noncurrent, total, filed, accession", [
    (1063631000, 503308000, 1566939000, "2026-07-30", "0000910521-26-000022"),  # Deckers
    (1770166000, 1203179000, 2973345000, "2026-08-06", "0001048695-26-000067"),  # F5
])
def test_complete_liabilities_pair_is_audited_and_can_support_zero_debt(
    cfg, current, noncurrent, total, filed, accession
):
    tags = {
        "LiabilitiesCurrent": [fact(END, current, filed, accn=accession)],
        "LiabilitiesNoncurrent": [fact(END, noncurrent, filed, accn=accession)],
    }
    result = resolve(cfg, "total_liabilities", tags)
    assert result.value == total
    assert [(str(u.fact.tag), u.fact.value) for u in result.used] == [
        ("us-gaap:LiabilitiesCurrent", current), ("us-gaap:LiabilitiesNoncurrent", noncurrent)
    ]
    assert all(u.fact.accession == accession and u.fact.filed == date.fromisoformat(filed) for u in result.used)
    screened = screen(make_inputs(debt=None, total_liabilities=result.value,
                                  interest_expense=None, interest_expense_reported=False), cfg)
    assert screened.status == "pass"
    tags["Liabilities"] = [fact(END, total, "2026-07-23")]
    direct = resolve(cfg, "total_liabilities", tags)
    assert direct.value == total
    assert [str(u.fact.tag) for u in direct.used] == ["us-gaap:Liabilities"]


@pytest.mark.parametrize("bad", ["true", "false", 0, 1, None])
def test_complete_alternatives_must_be_a_boolean(raw, bad):
    raw["inputs"]["total_liabilities"]["components"][0]["complete_alternatives"] = bad
    with pytest.raises(ConfigError, match="complete_alternatives.*boolean"):
        parse_config(raw)


def test_complete_alternatives_also_guard_annual_fallback(cfg):
    spec = replace(cfg.inputs["total_liabilities"], period="ttm", annual_fallback_max_age_days=450)
    tags = {
        "LiabilitiesCurrent": [fact("2025-12-31", 100, "2026-02-01", start="2025-01-01", form="10-K")],
    }
    cf = CompanyFacts(companyfacts(tags), forms=cfg.filings.forms, as_of=AS_OF)
    assert resolve_input(cf, spec, END, cfg.periods).value is None
    tags["LiabilitiesNoncurrent"] = [
        fact("2025-12-31", 200, "2026-02-01", start="2025-01-01", form="10-K")
    ]
    cf = CompanyFacts(companyfacts(tags), forms=cfg.filings.forms, as_of=AS_OF)
    result = resolve_input(cf, spec, END, cfg.periods)
    assert result.value == 300
    assert [u.role for u in result.used] == ["annual_fallback", "annual_fallback"]


def test_existing_debt_alternatives_still_accept_partial_reporting(cfg):
    assert all(not c.complete_alternatives for c in cfg.inputs["debt"].components)
    assert resolve(cfg, "debt", {"LongTermDebtNoncurrent": [fact(END, 100, "2026-07-23")]}).value == 100


def test_complete_liabilities_do_not_clear_unknown_reported_interest_expense(cfg):
    liabilities = resolve(cfg, "total_liabilities", {
        "LiabilitiesCurrent": [fact(END, 1_770_166_000, "2026-08-06")],
        "LiabilitiesNoncurrent": [fact(END, 1_203_179_000, "2026-08-06")],
    })
    result = screen(make_inputs(
        debt=None, total_liabilities=liabilities.value,
        interest_expense=None, interest_expense_reported=True,
    ), cfg)
    assert liabilities.value == 2_973_345_000
    assert result.status == "insufficient_data"
    assert "cannot be sized" in result.reason

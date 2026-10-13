"""Evidence-backed issuer exclusions, their audit trail and their temporal boundaries."""
from datetime import date

import pytest

from conftest import make_inputs
from halal_heatmap.config import ConfigError, parse_config
from halal_heatmap.export import build_site
from halal_heatmap.runner import BUSINESS_EVIDENCE_CHANGE, detect_due, run_screen
from halal_heatmap.screen.business import screen_business
from halal_heatmap.screen.engine import screen
from halal_heatmap.store import Store
from test_history import Company, World


@pytest.fixture
def store():
    value = Store(":memory:")
    yield value
    value.close()


def exclusion(**changes):
    value = {
        "cik": 1, "category": "weapons", "reason": "Manufactures missiles, documented in issuer filing",
        "sources": ["https://www.sec.gov/Archives/edgar/data/1/evidence.htm"],
        "reviewer": "Private Researcher", "reviewed_on": "2026-08-01", "expires_on": "2026-09-01",
    }
    value.update(changes)
    return value


def config(raw, **changes):
    raw["business"]["issuer_exclusions"] = [exclusion(**changes)]
    return parse_config(raw)


@pytest.mark.parametrize(
    "cik", [936468, 1133421, 40533, 101829, 217346, 820027, 886982, 895421, 1858681, 1404912, 202058]
)
def test_verified_issuer_business_fail_precedes_missing_financial_inputs(cfg, cik):
    inputs = make_inputs(
        cik=cik, screen_date=date(2026, 10, 13), sic=None, gics_sub_industry=None,
        debt=None, cash_and_securities=None, revenue=None, interest_income=None, filing=None,
    )
    result = screen(inputs, cfg)
    assert result.status == "fail"
    assert result.business.outcome == "fail"
    assert f"issuer CIK {cik}" in result.reason
    assert "https://" in result.reason
    assert "reviewed by automated evidence review" in result.reason


def test_issuer_identity_is_cik_not_ticker(raw):
    cfg = config(raw)
    for ticker in ("OLD", "NEW"):
        assert screen(make_inputs(ticker=ticker, cik=1, screen_date=date(2026, 8, 3)), cfg).status == "fail"
    assert screen(make_inputs(ticker="OLD", cik=2, screen_date=date(2026, 8, 3)), cfg).status == "pass"


def test_sector_rule_stays_review_for_unverified_aerospace_issuer(cfg):
    result = screen_business("3728", "Aerospace & Defense", cfg.business, cik=9999999, as_of=date(2026, 10, 13))
    assert result.outcome == "needs_review"


@pytest.mark.parametrize("day,outcome", [
    (date(2026, 7, 31), "pass"), (date(2026, 8, 1), "fail"),
    (date(2026, 9, 1), "fail"), (date(2026, 9, 2), "needs_review"),
])
def test_evidence_activation_and_expiry_never_default_to_pass(raw, day, outcome):
    cfg = config(raw)
    result = screen_business("3571", "Technology Hardware, Storage & Peripherals", cfg.business, cik=1, as_of=day)
    assert result.outcome == outcome
    if outcome == "needs_review":
        assert "expired" in result.rule


def test_generic_prohibited_activity_still_fails_after_evidence_expires(raw):
    cfg = config(raw)
    assert screen_business("3481", None, cfg.business, cik=1, as_of=date(2026, 9, 2)).outcome == "fail"


@pytest.mark.parametrize("changes", [
    {"cik": True}, {"cik": 0}, {"category": "unknown"}, {"reason": ""}, {"reviewer": ""},
    {"sources": []}, {"sources": ["http://example.com"]}, {"sources": ["https://"]}, {"reviewed_on": "not a date"},
    {"expires_on": "2026-07-31"},
])
def test_invalid_evidence_configuration_is_refused(raw, changes):
    with pytest.raises(ConfigError):
        config(raw, **changes)


def test_evidence_changes_are_methodology_changes(raw):
    old = parse_config(raw).hash
    new = config(raw).hash
    assert old != new
    assert config(raw, reason="Additional verified activity").hash != new


@pytest.mark.parametrize("reviewed,expires", [("2026-08-01", "2026-09-01"), ("2026-09-02", "2026-10-01")])
def test_daily_update_detects_evidence_expiry_and_activation_without_new_filings(raw, reviewed, expires, store):
    cfg = config(raw, reviewed_on=reviewed, expires_on=expires)
    world = World([Company("TEST", 1)])
    run_screen(date(2026, 8, 3), cfg, world.constituents("TEST"), world, world, {}, store)
    due, errors = detect_due(date(2026, 9, 2), cfg, world.constituents("TEST"), world, {}, store)
    assert due == {"TEST": BUSINESS_EVIDENCE_CHANGE}
    assert errors == []


def test_export_scrubs_issuer_reviewer_but_keeps_sources_and_review_dates(raw, store):
    cfg = config(raw)
    world = World([Company("TEST", 1)])
    run_screen(date(2026, 8, 3), cfg, world.constituents("TEST"), world, world, {}, store)
    site = build_site(store, cfg, {})
    rule = site["screens.json"]["screens"][0]["business"]["rule"]
    assert "Private Researcher" not in rule
    assert "Reviewer 1" in rule
    assert "https://www.sec.gov/Archives/edgar/data/1/evidence.htm" in rule
    assert "2026-08-01" in rule and "2026-09-01" in rule

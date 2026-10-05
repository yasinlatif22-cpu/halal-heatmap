import ast
import re
from pathlib import Path

import pytest

from halal_heatmap.config import ConfigError, load_config, parse_config
from halal_heatmap.overrides import parse_overrides

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "halal_heatmap"


def test_repo_config_loads_with_decided_defaults():
    cfg = load_config(ROOT / "config.yaml")
    assert {name: t.operator for name, t in cfg.thresholds.items()} == {
        "debt": "<",
        "cash": "<",
        "impure_income": "<",
    }
    assert cfg.market_cap.driving == "avg_12m"
    assert cfg.override_expiry_days == 365
    assert len(cfg.hash) == 12


@pytest.mark.parametrize(
    "path",
    [
        ("screens", "debt", "threshold"),
        ("screens", "impure_income", "operator"),
        ("market_cap", "driving"),
        ("market_cap", "window_months", "avg_36m"),
        ("near_threshold", "margin"),
        ("zero_debt", "max_interest_expense_to_revenue"),
        ("overrides", "expiry_days"),
        ("debt_plausibility", "max_interest_expense_to_debt"),
        ("inputs", "interest_income", "annual_fallback", "max_age_days"),
        ("inputs", "debt"),
        ("interest_income_sources", "net_investment_income", "max_share_of_limit"),
        ("interest_income_sources", "filing_xbrl", "sum_axes"),
        ("interest_income_sources", "filing_xbrl", "min_members"),
        ("shares", "classes", "axis"),
        ("filings", "annual_forms"),
        ("market_cap", "spot_divergence_factor"),
        ("business", "financing_receivables", "max_share_of_assets"),
        ("interest_income_sources", "upper_bound", "yield_ceiling"),
        ("inputs", "total_assets"),
        ("market_cap", "spot_divergence_against"),
        ("shares", "sanity", "reject_multiple"),
        ("events", "kinds"),
        ("business", "rules"),
        ("edgar", "max_requests_per_second"),
    ],
)
def test_missing_key_is_an_error(raw, path):
    node = raw
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    with pytest.raises(ConfigError, match=path[-1]):
        parse_config(raw)


def test_bad_operator_and_driving_are_rejected(raw):
    raw["screens"]["debt"]["operator"] = ">"
    with pytest.raises(ConfigError, match="operator"):
        parse_config(raw)
    raw["screens"]["debt"]["operator"] = "<"
    raw["market_cap"]["driving"] = "avg_24m"
    with pytest.raises(ConfigError, match="driving"):
        parse_config(raw)


def test_hash_changes_with_a_threshold(raw, cfg):
    raw["screens"]["debt"]["threshold"] = 0.33
    assert parse_config(raw).hash != cfg.hash


def test_override_requires_reason_reviewer_and_date():
    good = {"ticker": "xyz", "decision": "pass", "reason": "reviewed", "reviewer": "YL", "date": "2026-01-15"}
    assert parse_overrides({"overrides": [good]})["XYZ"].id == "XYZ@2026-01-15"
    for field in ("reason", "reviewer", "date", "decision"):
        with pytest.raises(ConfigError, match=field):
            parse_overrides({"overrides": [{k: v for k, v in good.items() if k != field}]})
    with pytest.raises(ConfigError, match="reason"):
        parse_overrides({"overrides": [{**good, "reason": "  "}]})
    with pytest.raises(ConfigError, match="decision"):
        parse_overrides({"overrides": [{**good, "decision": "maybe"}]})


def test_no_threshold_literals_in_source(raw_config):
    """Screening code may not contain numbers; shared code may not contain a configured threshold."""
    allowed = {0, 1}
    for path in (SRC / "screen").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Constant) or isinstance(node.value, bool):
                continue
            if isinstance(node.value, (int, float)):
                assert node.value in allowed, f"numeric literal {node.value} in {path.name}:{node.lineno}"

    limits = {s["threshold"] for s in raw_config["screens"].values()}
    limits |= {
        raw_config["near_threshold"]["margin"],
        raw_config["zero_debt"]["max_interest_expense_to_revenue"],
        raw_config["debt_plausibility"]["max_interest_expense_to_debt"],
        raw_config["interest_income_sources"]["filing_xbrl"]["max_share_of_limit"],
        raw_config["business"]["financing_receivables"]["max_share_of_assets"],
        raw_config["interest_income_sources"]["net_investment_income"]["max_share_of_limit"],
    }
    for path in SRC.rglob("*.py"):
        for token in re.findall(r"(?<![\w.])\d+\.\d+(?![\w.])", path.read_text()):
            assert float(token) not in limits, f"threshold literal {token} in {path.relative_to(SRC)}"


def test_near_threshold_default_is_relative_ten_percent(cfg):
    assert (cfg.near_threshold.mode, cfg.near_threshold.margin) == ("relative", 0.10)


def test_annual_fallback_is_only_configured_for_interest_income(cfg, raw):
    assert cfg.inputs["interest_income"].annual_fallback_max_age_days == 450
    others = [spec for name, spec in cfg.inputs.items() if name != "interest_income"]
    assert all(spec.annual_fallback_max_age_days is None for spec in others)
    raw["inputs"]["debt"]["annual_fallback"] = {"max_age_days": 450}
    with pytest.raises(ConfigError, match="annual_fallback"):
        parse_config(raw)


def test_aerospace_and_defence_stays_needs_review(cfg):
    actions = {rule.action for rule in cfg.business.rules if "Aerospace & Defense" in rule.gics_sub_industries}
    assert actions == {"needs_review"}


def test_net_investment_income_cap_defaults_to_half_the_limit(cfg):
    sources = cfg.interest_income_sources
    assert [str(t) for t in sources.net_tags] == ["us-gaap:InvestmentIncomeNet"]
    assert sources.net_max_share_of_limit == 0.5 and sources.dimensional_max_share_of_limit == 0.5


def test_predecessors_map_ciks(cfg, raw):
    assert cfg.predecessors[2115436] == (34088,)
    assert 2041610 not in cfg.predecessors  # Paramount Skydance is a merger, not a reorganisation
    raw["predecessors"] = {"abc": [1]}
    with pytest.raises(ConfigError, match="predecessors"):
        parse_config(raw)
    del raw["predecessors"]
    assert parse_config(raw).predecessors == {}


def test_share_sanity_and_event_settings_are_validated(raw):
    raw["shares"]["sanity"]["flag_multiple"] = 9
    with pytest.raises(ConfigError, match="flag_multiple"):
        parse_config(raw)
    raw["shares"]["sanity"]["flag_multiple"] = 1.5
    raw["events"]["kinds"][0]["phrases"] = ["completed (the"]
    with pytest.raises(ConfigError, match="regular expression"):
        parse_config(raw)


def test_spot_divergence_is_measured_against_the_twelve_month_average(cfg):
    assert (cfg.market_cap.spot_divergence_against, cfg.market_cap.spot_divergence_factor) == ("avg_12m", 1.5)
    assert (cfg.shares.sanity.reject_multiple, cfg.shares.sanity.flag_multiple) == (5, 1.5)

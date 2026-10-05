"""Load and validate config.yaml. Missing or malformed keys raise ConfigError."""

from __future__ import annotations

import hashlib
import json
import operator
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

OPERATORS = {"<": operator.lt, "<=": operator.le}
DENOMINATORS = ("spot", "avg_12m", "avg_36m")
AVERAGED_DENOMINATORS = DENOMINATORS[1:]
RATIOS = ("debt", "cash", "impure_income")
INPUTS = (
    "debt",
    "cash_and_securities",
    "total_assets",
    "financing_receivables",
    "total_liabilities",
    "revenue",
    "interest_income",
    "interest_expense",
)
PERIOD_KINDS = ("instant", "ttm")
COMBINE_MODES = ("max", "first")
BUSINESS_ACTIONS = ("fail", "needs_review")
CLASSIFICATIONS = ("sic", "gics_sub_industry")
NEAR_MODES = ("relative", "absolute")
SPLIT_BASES = ("end", "filed")
UNLISTED_CLASS_MODES = ("price_at_listed", "exclude")
DEFAULT_TAXONOMY = "us-gaap"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class TagRef:
    taxonomy: str
    name: str

    @classmethod
    def parse(cls, text: str) -> TagRef:
        taxonomy, _, name = str(text).rpartition(":")
        return cls(taxonomy or DEFAULT_TAXONOMY, name)

    def __str__(self) -> str:
        return f"{self.taxonomy}:{self.name}"


@dataclass(frozen=True)
class Threshold:
    limit: float
    operator: str

    def passes(self, ratio: float) -> bool:
        return OPERATORS[self.operator](ratio, self.limit)


@dataclass(frozen=True)
class MarketCapConfig:
    driving: str
    window_months: dict[str, int]
    trading_days_per_month: int
    min_coverage: float
    max_spot_age_days: int
    spot_divergence_factor: float
    spot_divergence_against: str


@dataclass(frozen=True)
class NearThreshold:
    mode: str
    margin: float


@dataclass(frozen=True)
class ZeroDebt:
    max_interest_expense_to_revenue: float
    interest_expense_lookback_days: int


@dataclass(frozen=True)
class DebtPlausibility:
    max_interest_expense_to_debt: float


@dataclass(frozen=True)
class Filings:
    forms: tuple[str, ...]
    balance_sheet_anchor: tuple[TagRef, ...]
    max_period_age_days: int
    annual_forms: tuple[str, ...]
    successor_forms: tuple[str, ...]


@dataclass(frozen=True)
class Periods:
    annual_min_days: int
    annual_max_days: int
    tolerance_days: int


@dataclass(frozen=True)
class ShareTag:
    tag: TagRef
    split_basis: str


@dataclass(frozen=True)
class ShareClasses:
    tag: TagRef
    symbol_tag: TagRef
    axis: str
    common_member_patterns: tuple[str, ...]
    excluded_member_patterns: tuple[str, ...]
    unlisted: str
    exclude_unlisted_tickers: frozenset[str]

    def unlisted_mode(self, ticker: str) -> str:
        return "exclude" if ticker in self.exclude_unlisted_tickers else self.unlisted

    def is_common(self, member: str) -> bool:
        name = member.rpartition(":")[2]
        if any(pattern in name for pattern in self.excluded_member_patterns):
            return False
        return any(pattern in name for pattern in self.common_member_patterns)


@dataclass(frozen=True)
class ShareSanity:
    reject_multiple: float
    flag_multiple: float
    neighbours: int


@dataclass(frozen=True)
class Shares:
    recent_days: int
    tags: tuple[ShareTag, ...]
    classes: ShareClasses
    sanity: ShareSanity


@dataclass(frozen=True)
class EventKind:
    name: str
    items: frozenset[str]
    phrases: tuple[re.Pattern, ...]


@dataclass(frozen=True)
class Events:
    forms: tuple[str, ...]
    kinds: tuple[EventKind, ...]

    def items(self) -> frozenset[str]:
        return frozenset(item for kind in self.kinds for item in kind.items)


@dataclass(frozen=True)
class InterestIncomeSources:
    extension_tags: tuple[str, ...]
    sum_axes: frozenset[str]
    dimensional_min_members: int
    dimensional_max_share_of_limit: float
    net_tags: tuple[TagRef, ...]
    net_max_share_of_limit: float
    upper_bound_yield_ceiling: float


@dataclass(frozen=True)
class Component:
    name: str
    required: bool
    any_of: tuple[tuple[TagRef, ...], ...]


@dataclass(frozen=True)
class InputSpec:
    name: str
    period: str
    combine: str
    components: tuple[Component, ...]
    annual_fallback_max_age_days: int | None = None

    def optional_components(self) -> tuple[str, ...]:
        return tuple(comp.name for comp in self.components if not comp.required)

    def all_tags(self) -> tuple[TagRef, ...]:
        return tuple(tag for comp in self.components for alt in comp.any_of for tag in alt)


@dataclass(frozen=True)
class BusinessRule:
    category: str
    action: str
    gics_sub_industries: frozenset[str]
    sic_ranges: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class Business:
    required_classifications: tuple[str, ...]
    rules: tuple[BusinessRule, ...]
    financing_receivables_max_share: float


@dataclass(frozen=True)
class Constituents:
    url: str
    user_agent: str
    table_id: str
    min_count: int
    max_count: int


@dataclass(frozen=True)
class Edgar:
    max_requests_per_second: float
    timeout_seconds: float
    max_retries: int
    backoff_seconds: float
    cache_dir: str
    cache_ttl_hours: float


@dataclass(frozen=True)
class Config:
    thresholds: dict[str, Threshold]
    market_cap: MarketCapConfig
    near_threshold: NearThreshold
    zero_debt: ZeroDebt
    debt_plausibility: DebtPlausibility
    filings: Filings
    periods: Periods
    override_expiry_days: int
    shares: Shares
    events: Events
    interest_income_sources: InterestIncomeSources
    predecessors: dict[int, tuple[int, ...]]
    inputs: dict[str, InputSpec]
    business: Business
    constituents: Constituents
    edgar: Edgar
    hash: str


def _req(node: Any, path: str, key: str) -> Any:
    if not isinstance(node, dict) or key not in node or node[key] is None:
        raise ConfigError(f"config: missing required key '{path}.{key}'".replace("'.", "'"))
    return node[key]


def _number(node: Any, path: str, key: str, *, positive: bool = True) -> float:
    value = _req(node, path, key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"config: '{path}.{key}' must be a number, got {value!r}")
    if positive and value <= 0:
        raise ConfigError(f"config: '{path}.{key}' must be positive, got {value!r}")
    return value


def _choice(node: Any, path: str, key: str, allowed: tuple[str, ...]) -> str:
    value = _req(node, path, key)
    if value not in allowed:
        raise ConfigError(f"config: '{path}.{key}' must be one of {list(allowed)}, got {value!r}")
    return value


def _sic_range(text: Any, path: str) -> tuple[int, int]:
    low, _, high = str(text).partition("-")
    try:
        return int(low), int(high or low)
    except ValueError:
        raise ConfigError(f"config: bad SIC code or range {text!r} in '{path}'") from None


def _thresholds(raw: dict) -> dict[str, Threshold]:
    screens = _req(raw, "", "screens")
    out = {}
    for name in RATIOS:
        node = _req(screens, "screens", name)
        path = f"screens.{name}"
        out[name] = Threshold(_number(node, path, "threshold"), _choice(node, path, "operator", tuple(OPERATORS)))
    return out


def _market_cap(raw: dict) -> MarketCapConfig:
    node = _req(raw, "", "market_cap")
    windows = _req(node, "market_cap", "window_months")
    return MarketCapConfig(
        driving=_choice(node, "market_cap", "driving", DENOMINATORS),
        window_months={n: int(_number(windows, "market_cap.window_months", n)) for n in AVERAGED_DENOMINATORS},
        trading_days_per_month=int(_number(node, "market_cap", "trading_days_per_month")),
        min_coverage=_number(node, "market_cap", "min_coverage"),
        max_spot_age_days=int(_number(node, "market_cap", "max_spot_age_days")),
        spot_divergence_factor=_number(node, "market_cap", "spot_divergence_factor"),
        spot_divergence_against=_choice(node, "market_cap", "spot_divergence_against", AVERAGED_DENOMINATORS),
    )


def _inputs(raw: dict) -> dict[str, InputSpec]:
    node = _req(raw, "", "inputs")
    out = {}
    for name in INPUTS:
        spec = _req(node, "inputs", name)
        path = f"inputs.{name}"
        components = []
        for comp in _req(spec, path, "components"):
            cpath = f"{path}.components"
            alternatives = tuple(tuple(TagRef.parse(t) for t in alt) for alt in _req(comp, cpath, "any_of"))
            if not alternatives or any(not alt for alt in alternatives):
                raise ConfigError(f"config: '{cpath}' has an empty any_of entry")
            components.append(Component(_req(comp, cpath, "name"), bool(_req(comp, cpath, "required")), alternatives))
        if not components:
            raise ConfigError(f"config: '{path}.components' is empty")
        out[name] = InputSpec(
            name,
            _choice(spec, path, "period", PERIOD_KINDS),
            _choice(spec, path, "combine", COMBINE_MODES),
            tuple(components),
            _annual_fallback(spec, path),
        )
    return out


def _annual_fallback(spec: dict, path: str) -> int | None:
    node = spec.get("annual_fallback")
    if node is None:
        return None
    if spec.get("period") != "ttm":
        raise ConfigError(f"config: '{path}.annual_fallback' only applies to ttm inputs")
    return int(_number(node, f"{path}.annual_fallback", "max_age_days"))


def _share_classes(shares: dict) -> ShareClasses:
    path = "shares.classes"
    node = _req(shares, "shares", "classes")
    common = tuple(_req(node, path, "common_member_patterns"))
    if not common:
        raise ConfigError(f"config: '{path}.common_member_patterns' is empty")
    return ShareClasses(
        tag=TagRef.parse(_req(node, path, "tag")),
        symbol_tag=TagRef.parse(_req(node, path, "symbol_tag")),
        axis=_req(node, path, "axis"),
        common_member_patterns=common,
        excluded_member_patterns=tuple(node.get("excluded_member_patterns") or ()),
        unlisted=_choice(node, path, "unlisted", UNLISTED_CLASS_MODES),
        exclude_unlisted_tickers=frozenset(node.get("exclude_unlisted_tickers") or ()),
    )


def _share_sanity(shares: dict) -> ShareSanity:
    path = "shares.sanity"
    node = _req(shares, "shares", "sanity")
    reject, flag = _number(node, path, "reject_multiple"), _number(node, path, "flag_multiple")
    if not 1 < flag <= reject:
        raise ConfigError(f"config: '{path}' needs 1 < flag_multiple <= reject_multiple")
    return ShareSanity(reject, flag, int(_number(node, path, "neighbours")))


def _events(raw: dict) -> Events:
    node = _req(raw, "", "events")
    kinds = []
    for kind in _req(node, "events", "kinds"):
        path = "events.kinds"
        try:
            phrases = tuple(re.compile(p, re.IGNORECASE) for p in _req(kind, path, "phrases"))
        except re.error as exc:
            raise ConfigError(f"config: bad regular expression in '{path}.phrases': {exc}") from None
        kinds.append(EventKind(_req(kind, path, "name"), frozenset(map(str, _req(kind, path, "items"))), phrases))
    return Events(tuple(_req(node, "events", "forms")), tuple(kinds))


def _share_of_limit(node: dict, path: str) -> float:
    value = _number(node, path, "max_share_of_limit")
    if value > 1:
        raise ConfigError(f"config: '{path}.max_share_of_limit' must be at most 1, got {value!r}")
    return value


def _interest_income_sources(raw: dict) -> InterestIncomeSources:
    path = "interest_income_sources"
    node = _req(raw, "", path)
    filing = _req(node, path, "filing_xbrl")
    net = _req(node, path, "net_investment_income")
    return InterestIncomeSources(
        extension_tags=tuple(filing.get("extension_tags") or ()),
        sum_axes=frozenset(_req(filing, f"{path}.filing_xbrl", "sum_axes")),
        dimensional_min_members=int(_number(filing, f"{path}.filing_xbrl", "min_members")),
        dimensional_max_share_of_limit=_share_of_limit(filing, f"{path}.filing_xbrl"),
        net_tags=tuple(TagRef.parse(t) for t in net.get("tags") or ()),
        net_max_share_of_limit=_share_of_limit(net, f"{path}.net_investment_income"),
        upper_bound_yield_ceiling=_number(_req(node, path, "upper_bound"), f"{path}.upper_bound", "yield_ceiling"),
    )


def _predecessors(raw: dict) -> dict[int, tuple[int, ...]]:
    out = {}
    for successor, earlier in (raw.get("predecessors") or {}).items():
        try:
            out[int(successor)] = tuple(int(cik) for cik in earlier)
        except (TypeError, ValueError):
            raise ConfigError(f"config: 'predecessors.{successor}' must map a CIK to a list of CIKs") from None
    return out


def _business(raw: dict) -> Business:
    node = _req(raw, "", "business")
    required = tuple(_req(node, "business", "required_classifications"))
    for item in required:
        if item not in CLASSIFICATIONS:
            raise ConfigError(f"config: unknown classification {item!r} in 'business.required_classifications'")
    rules = []
    for rule in _req(node, "business", "rules"):
        path = "business.rules"
        rules.append(
            BusinessRule(
                category=_req(rule, path, "category"),
                action=_choice(rule, path, "action", BUSINESS_ACTIONS),
                gics_sub_industries=frozenset(rule.get("gics_sub_industries") or ()),
                sic_ranges=tuple(_sic_range(s, path) for s in rule.get("sic") or ()),
            )
        )
    financing = _req(node, "business", "financing_receivables")
    return Business(
        required, tuple(rules), _number(financing, "business.financing_receivables", "max_share_of_assets")
    )


def parse_config(raw: Any) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config: top level must be a mapping")
    near = _req(raw, "", "near_threshold")
    zero = _req(raw, "", "zero_debt")
    filings = _req(raw, "", "filings")
    periods = _req(raw, "", "periods")
    shares = _req(raw, "", "shares")
    cons = _req(raw, "", "constituents")
    edgar = _req(raw, "", "edgar")
    share_tags = tuple(
        ShareTag(TagRef.parse(_req(t, "shares.tags", "tag")), _choice(t, "shares.tags", "split_basis", SPLIT_BASES))
        for t in _req(shares, "shares", "tags")
    )
    if not share_tags:
        raise ConfigError("config: 'shares.tags' is empty")
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()
    return Config(
        thresholds=_thresholds(raw),
        market_cap=_market_cap(raw),
        near_threshold=NearThreshold(
            _choice(near, "near_threshold", "mode", NEAR_MODES), _number(near, "near_threshold", "margin")
        ),
        zero_debt=ZeroDebt(
            _number(zero, "zero_debt", "max_interest_expense_to_revenue"),
            int(_number(zero, "zero_debt", "interest_expense_lookback_days")),
        ),
        debt_plausibility=DebtPlausibility(
            _number(_req(raw, "", "debt_plausibility"), "debt_plausibility", "max_interest_expense_to_debt")
        ),
        filings=Filings(
            forms=tuple(_req(filings, "filings", "forms")),
            balance_sheet_anchor=tuple(TagRef.parse(t) for t in _req(filings, "filings", "balance_sheet_anchor")),
            max_period_age_days=int(_number(filings, "filings", "max_period_age_days")),
            annual_forms=tuple(_req(filings, "filings", "annual_forms")),
            successor_forms=tuple(filings.get("successor_forms") or ()),
        ),
        periods=Periods(
            int(_number(periods, "periods", "annual_min_days")),
            int(_number(periods, "periods", "annual_max_days")),
            int(_number(periods, "periods", "tolerance_days")),
        ),
        override_expiry_days=int(_number(_req(raw, "", "overrides"), "overrides", "expiry_days")),
        shares=Shares(
            int(_number(shares, "shares", "recent_days")), share_tags, _share_classes(shares), _share_sanity(shares)
        ),
        events=_events(raw),
        interest_income_sources=_interest_income_sources(raw),
        predecessors=_predecessors(raw),
        inputs=_inputs(raw),
        business=_business(raw),
        constituents=Constituents(
            url=_req(cons, "constituents", "url"),
            user_agent=_req(cons, "constituents", "user_agent"),
            table_id=_req(cons, "constituents", "table_id"),
            min_count=int(_number(cons, "constituents", "min_count")),
            max_count=int(_number(cons, "constituents", "max_count")),
        ),
        edgar=Edgar(
            max_requests_per_second=_number(edgar, "edgar", "max_requests_per_second"),
            timeout_seconds=_number(edgar, "edgar", "timeout_seconds"),
            max_retries=int(_number(edgar, "edgar", "max_retries")),
            backoff_seconds=_number(edgar, "edgar", "backoff_seconds"),
            cache_dir=_req(edgar, "edgar", "cache_dir"),
            cache_ttl_hours=_number(edgar, "edgar", "cache_ttl_hours"),
        ),
        hash=digest[:12],
    )


def load_config(path: str | Path) -> Config:
    with open(path, encoding="utf-8") as handle:
        return parse_config(yaml.safe_load(handle))

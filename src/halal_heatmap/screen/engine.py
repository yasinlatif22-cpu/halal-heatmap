"""Combine the business and financial screens into one auditable verdict. Pure, no I/O."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date

from halal_heatmap.config import DENOMINATORS, RATIOS, Config
from halal_heatmap.facts import Fact
from halal_heatmap.interest import COMPANYFACTS, GROSS, NET_INVESTMENT_INCOME, UPPER_BOUND
from halal_heatmap.marketcap import MarketCap
from halal_heatmap.overrides import Override
from halal_heatmap.screen import business as biz
from halal_heatmap.screen.business import BusinessResult, screen_business
from halal_heatmap.screen.financial import (
    DebtCheck,
    DebtResolution,
    RatioResult,
    check_debt_plausibility,
    evaluate_ratio,
    resolve_debt,
)

PASS = biz.PASS
FAIL = biz.FAIL
NEEDS_REVIEW = biz.NEEDS_REVIEW
INSUFFICIENT_DATA = biz.INSUFFICIENT_DATA
STATUSES = (PASS, FAIL, NEEDS_REVIEW, INSUFFICIENT_DATA)

OVERRIDE_NONE = "none"
CAPTIVE_FINANCE = "captive_finance"
BOUND_CASH = "cash_and_securities"
BOUND_TOTAL_ASSETS = "total_assets"
SPOT = DENOMINATORS[0]
ASSETS_BOUND_NOTE = "with no investment tags its upper bound from total assets"
OVERRIDE_ACTIVE = "active"
OVERRIDE_EXPIRED = "expired"


@dataclass(frozen=True)
class FilingRef:
    accession: str
    form: str
    filed: date
    period_end: date


@dataclass(frozen=True)
class ScreenInputs:
    ticker: str
    cik: int | None
    screen_date: date
    sic: str | None
    gics_sub_industry: str | None
    filing: FilingRef | None
    debt: float | None
    cash_and_securities: float | None
    interest_income: float | None
    revenue: float | None
    interest_expense: float | None
    total_liabilities: float | None
    interest_expense_reported: bool
    market_caps: Mapping[str, MarketCap]
    override: Override | None = None
    total_assets: float | None = None
    financing_receivables: float | None = None
    interest_income_fallback: Fact | None = None  # annual fact used in place of trailing 12 months
    interest_income_kind: str = GROSS  # gross interest, or net investment income
    interest_income_source: str = COMPANYFACTS  # companyfacts, or the filing's own XBRL
    interest_income_dimensional: bool = False  # summed over dimension members
    interest_income_tags: str = ""  # the XBRL tags the figure came from
    # Share of the impure income limit the ratio must stay below for this basis to support a pass.
    interest_income_max_share_of_limit: float | None = None
    investments_assumed_zero: bool = False
    cash_note: str = ""
    share_source: str = COMPANYFACTS
    share_classes: str = ""  # per-class share counts behind the market cap, as JSON
    unlisted_class_share: float | None = None  # share of market cap from classes priced at another class
    share_counts_rejected: int = 0  # reported counts dropped as out of line with their neighbours
    share_count_jump: bool = False  # a large change between consecutive counts, for review
    share_count_note: str = ""
    post_balance_sheet_event: str = ""  # spin-off or major disposition reported after the balance sheet date
    notes: Mapping[str, str] = field(default_factory=dict)  # why an input is missing


@dataclass(frozen=True)
class AssetsBound:
    required: bool  # no interest income disclosed and no investment tag reported
    value: float | None  # total assets x yield ceiling
    ratio: RatioResult | None


@dataclass(frozen=True)
class DenominatorOutcome:
    name: str
    market_cap: MarketCap
    debt: RatioResult | None
    cash: RatioResult | None
    status: str
    reason: str

    @property
    def available(self) -> bool:
        return self.market_cap.value is not None


@dataclass(frozen=True)
class ScreenResult:
    inputs: ScreenInputs
    trigger: str
    business: BusinessResult
    debt: DebtResolution
    debt_check: DebtCheck
    impure_income: float | None
    impure: RatioResult | None
    interest_upper_bound: float | None  # set when no interest income is disclosed and it is bounded instead
    interest_bound_base: str | None  # which balance sheet figure the bound was taken from
    financing_share: float | None  # financing receivables / total assets
    spot_divergence: float | None  # largest factor between spot and an averaged market cap
    spot_diverges: bool
    stale_balance_sheet: bool  # a pass was withdrawn because the balance sheet is out of date
    outcomes: Mapping[str, DenominatorOutcome]
    driving: str
    denominator_disagreement: bool
    near_threshold: bool
    override_state: str
    status: str
    reason: str
    config_hash: str

    @property
    def driving_outcome(self) -> DenominatorOutcome:
        return self.outcomes[self.driving]

    def ratios(self) -> dict[str, RatioResult | None]:
        outcome = self.driving_outcome
        return {"debt": outcome.debt, "cash": outcome.cash, "impure_income": self.impure}


def _override_state(inputs: ScreenInputs, business: BusinessResult, cfg: Config) -> str:
    override = inputs.override
    if override is None or business.outcome != NEEDS_REVIEW or override.date > inputs.screen_date:
        return OVERRIDE_NONE
    if inputs.screen_date > override.expires_on(cfg.override_expiry_days):
        return OVERRIDE_EXPIRED
    return OVERRIDE_ACTIVE


def _basis_label(inputs: ScreenInputs) -> str:
    if inputs.interest_income_kind == NET_INVESTMENT_INCOME:
        return "net investment income"
    return "interest income summed over dimension members" if inputs.interest_income_dimensional else ""


def _basis_shortfall(inputs: ScreenInputs, impure: RatioResult | None) -> str | None:
    """Why a weaker interest income basis cannot support a verdict, if it cannot."""
    label = _basis_label(inputs)
    if inputs.interest_income is None or not label:
        return None
    if inputs.interest_income_kind == NET_INVESTMENT_INCOME and inputs.interest_income < 0:
        return f"interest_income (only {label} is reported and it is negative, so gross interest is unknown)"
    share = inputs.interest_income_max_share_of_limit
    if share is None or impure is None:
        return None
    allowed = share * impure.limit
    if impure.ratio < allowed:
        return None
    return (
        f"interest_income (only {label} is reported; at {impure.ratio:.2%} of revenue it is not below "
        f"the {allowed:.2%} that basis may support)"
    )


def _share(part: float | None, whole: float | None) -> float | None:
    return None if part is None or whole is None or whole <= 0 else part / whole


def _spot_divergence(outcomes: Mapping[str, DenominatorOutcome], against: str) -> float | None:
    """Factor, in either direction, between the spot market cap and the configured average."""
    spot = outcomes[SPOT].market_cap.value
    average = outcomes[against].market_cap.value
    if not spot or not average or spot <= 0 or average <= 0:
        return None
    return max(spot / average, average / spot)


def _stale_balance_sheet(
    inputs: ScreenInputs, driving: DenominatorOutcome, spot: DenominatorOutcome, divergence: float | None, cfg: Config
) -> str | None:
    """Why a pass cannot stand on this balance sheet. Never applies to any other status."""
    if driving.status != PASS:
        return None
    if inputs.post_balance_sheet_event:
        return f"balance sheet is out of date: {inputs.post_balance_sheet_event}; awaiting the next periodic filing"
    if divergence is not None and divergence > cfg.market_cap.spot_divergence_factor and spot.status != PASS:
        return (
            f"balance sheet or share count looks out of date: spot market cap is {divergence:.2f}x away from the "
            f"{cfg.market_cap.spot_divergence_against} average and the spot verdict is {spot.status}"
        )
    return None


def _undisclosed_interest(
    inputs: ScreenInputs, bound: float | None, base: str | None, impure: RatioResult | None
) -> str | None:
    """Why missing interest income blocks a verdict, or None when its upper bound clears the screen."""
    note = inputs.notes.get("interest_income", "not reported")
    if bound is None or base is None or impure is None:
        return f"interest_income ({note})"
    if impure.passed:
        return None
    return (
        f"interest_income ({note}; its upper bound from {base.replace('_', ' ')} is {impure.ratio:.2%} of revenue, "
        f"which is not {impure.operator} {impure.limit:.2%})"
    )


def _missing_inputs(
    inputs: ScreenInputs,
    business: BusinessResult,
    debt: DebtResolution,
    impure: RatioResult | None,
    bound: float | None,
    bound_base: str | None,
    cfg: Config,
) -> list[str]:
    missing = []
    if business.outcome == INSUFFICIENT_DATA:
        missing.append(business.rule)
    if inputs.filing is None:
        missing.append("no balance sheet filing found")
    else:
        age = (inputs.screen_date - inputs.filing.period_end).days
        if age > cfg.filings.max_period_age_days:
            missing.append(f"latest filing is stale (period end {inputs.filing.period_end}, {age} days old)")
    if debt.value is None:
        missing.append(f"debt ({debt.note or inputs.notes.get('debt', 'not reported')})")
    for name, value in (("cash_and_securities", inputs.cash_and_securities), ("revenue", inputs.revenue)):
        if value is None:
            missing.append(f"{name} ({inputs.notes.get(name, 'not reported')})")
    if inputs.interest_income is None:
        undisclosed = _undisclosed_interest(inputs, bound, bound_base, impure)
        if undisclosed:
            missing.append(undisclosed)
    if inputs.revenue is not None and inputs.revenue <= 0:
        missing.append("revenue is not positive")
    shortfall = _basis_shortfall(inputs, impure)
    if shortfall:
        missing.append(shortfall)
    return missing


def _describe_failure(ratio: RatioResult, denominator: str) -> str:
    return f"{ratio.name} / {denominator} {ratio.ratio:.2%} is not {ratio.operator} {ratio.limit:.2%}"


def _decide(
    inputs: ScreenInputs,
    business: BusinessResult,
    missing: list[str],
    ratios: list[tuple[RatioResult | None, str]],
    override_state: str,
    debt_check: DebtCheck,
    assets_bound: AssetsBound,
    cfg: Config,
) -> tuple[str, str]:
    status, reason = _decide_before_checks(inputs, business, missing, ratios, override_state, cfg)
    # Like the plausibility check below, the total-assets bound may only take a pass away.
    if status == PASS and assets_bound.required and (assets_bound.ratio is None or not assets_bound.ratio.passed):
        note = inputs.notes.get("interest_income", "not reported")
        if assets_bound.ratio is None:
            return INSUFFICIENT_DATA, (
                f"missing: interest_income ({note}; no investment tags and no total assets to bound it)"
            )
        ratio = assets_bound.ratio
        return INSUFFICIENT_DATA, (
            f"missing: interest_income ({note}; {ASSETS_BOUND_NOTE} is "
            f"{ratio.ratio:.2%} of revenue, which is not {ratio.operator} {ratio.limit:.2%})"
        )
    # The plausibility check may only take a pass away, never change any other status.
    if status == PASS and debt_check.implausible:
        return INSUFFICIENT_DATA, f"debt not reliable: {debt_check.note}"
    if status == PASS and inputs.interest_income_fallback is not None:
        fact = inputs.interest_income_fallback
        reason += f" (interest income is the annual figure to {fact.end}, filed {fact.filed})"
    if status == PASS and _basis_label(inputs):
        reason += f" (impure income basis: {_basis_label(inputs)})"
    if status == PASS and inputs.interest_income is None:
        base = BOUND_TOTAL_ASSETS if inputs.investments_assumed_zero else BOUND_CASH
        reason += f" (impure income basis: upper bound from {base.replace('_', ' ')}, no interest income disclosed)"
    return status, reason


def _decide_before_checks(
    inputs: ScreenInputs,
    business: BusinessResult,
    missing: list[str],
    ratios: list[tuple[RatioResult | None, str]],
    override_state: str,
    cfg: Config,
) -> tuple[str, str]:
    if business.outcome == FAIL:
        return FAIL, f"business screen: {business.rule}"
    if missing:
        return INSUFFICIENT_DATA, "missing: " + "; ".join(missing)
    failures = [_describe_failure(r, label) for r, label in ratios if r is not None and not r.passed]
    if failures:
        return FAIL, "; ".join(failures)
    if business.outcome == NEEDS_REVIEW:
        override = inputs.override
        if override_state == OVERRIDE_ACTIVE:
            return override.decision, (
                f"manual override by {override.reviewer} on {override.date}: {override.reason} "
                f"(business screen: {business.rule})"
            )
        if override_state == OVERRIDE_EXPIRED:
            expired = override.expires_on(cfg.override_expiry_days)
            return NEEDS_REVIEW, f"override expired on {expired}; business screen: {business.rule}"
        return NEEDS_REVIEW, f"business screen: {business.rule}"
    return PASS, "all screens passed"


def screen(inputs: ScreenInputs, cfg: Config, trigger: str = "manual") -> ScreenResult:
    business = screen_business(inputs.sic, inputs.gics_sub_industry, cfg.business)
    financing_share = _share(inputs.financing_receivables, inputs.total_assets)
    limit = cfg.business.financing_receivables_max_share
    if business.outcome == PASS and financing_share is not None and financing_share > limit:
        business = BusinessResult(
            NEEDS_REVIEW,
            CAPTIVE_FINANCE,
            f"{CAPTIVE_FINANCE}: financing receivables are {financing_share:.1%} of total assets, above {limit:.1%}",
        )
    debt = resolve_debt(
        inputs.debt,
        inputs.total_liabilities,
        inputs.interest_expense,
        inputs.interest_expense_reported,
        inputs.revenue,
        cfg.zero_debt,
    )
    debt_check = check_debt_plausibility(
        debt, inputs.interest_expense, inputs.revenue, cfg.debt_plausibility, cfg.zero_debt
    )
    override_state = _override_state(inputs, business, cfg)
    impure_income = inputs.interest_income
    ceiling = cfg.interest_income_sources.upper_bound_yield_ceiling
    extra = inputs.override.additional_impure_income if override_state == OVERRIDE_ACTIVE else 0
    bound = bound_base = None
    if impure_income is None and inputs.cash_and_securities is not None:
        bound, bound_base = inputs.cash_and_securities * ceiling, BOUND_CASH
        impure_income = bound
    if impure_income is not None:
        impure_income += extra
    impure = evaluate_ratio(
        "impure_income", impure_income, inputs.revenue, cfg.thresholds["impure_income"], cfg.near_threshold
    )
    # With no investment tag reported, cash alone may leave interest-bearing assets out, so a pass
    # must also clear the bound taken from total assets.
    assets_bound = AssetsBound(False, None, None)
    if inputs.interest_income is None and inputs.investments_assumed_zero:
        value = None if inputs.total_assets is None else inputs.total_assets * ceiling
        ratio = evaluate_ratio(
            "impure_income",
            None if value is None else value + extra,
            inputs.revenue,
            cfg.thresholds["impure_income"],
            cfg.near_threshold,
        )
        assets_bound = AssetsBound(True, value, ratio)
    common_missing = _missing_inputs(inputs, business, debt, impure, bound, bound_base, cfg)

    outcomes: dict[str, DenominatorOutcome] = {}
    for name in DENOMINATORS:
        market_cap = inputs.market_caps.get(name) or MarketCap(None, note="not computed")
        debt_ratio = evaluate_ratio("debt", debt.value, market_cap.value, cfg.thresholds["debt"], cfg.near_threshold)
        cash_ratio = evaluate_ratio(
            "cash", inputs.cash_and_securities, market_cap.value, cfg.thresholds["cash"], cfg.near_threshold
        )
        missing = list(common_missing)
        if market_cap.value is None or market_cap.value <= 0:
            missing.append(f"market cap {name} ({market_cap.note or 'not available'})")
        label = f"market cap ({name})"
        status, reason = _decide(
            inputs,
            business,
            missing,
            [(debt_ratio, label), (cash_ratio, label), (impure, "revenue")],
            override_state,
            debt_check,
            assets_bound,
            cfg,
        )
        outcomes[name] = DenominatorOutcome(name, market_cap, debt_ratio, cash_ratio, status, reason)

    driving = outcomes[cfg.market_cap.driving]
    # Show the total-assets bound in the record whenever it decided the verdict, either way.
    if assets_bound.ratio is not None and (driving.status == PASS or ASSETS_BOUND_NOTE in driving.reason):
        bound, bound_base = assets_bound.value, BOUND_TOTAL_ASSETS
        impure, impure_income = assets_bound.ratio, assets_bound.ratio.numerator
    comparable = {o.status for o in outcomes.values() if o.available}
    driving_ratios = (driving.debt, driving.cash, impure)
    divergence = _spot_divergence(outcomes, cfg.market_cap.spot_divergence_against)
    stale = _stale_balance_sheet(inputs, driving, outcomes[SPOT], divergence, cfg)
    if stale:
        driving = dataclasses.replace(driving, status=INSUFFICIENT_DATA, reason=stale)
        outcomes[driving.name] = driving
    return ScreenResult(
        inputs=inputs,
        trigger=trigger,
        business=business,
        debt=debt,
        debt_check=debt_check,
        impure_income=impure_income,
        impure=impure,
        interest_upper_bound=bound,
        interest_bound_base=bound_base,
        financing_share=financing_share,
        spot_divergence=divergence,
        spot_diverges=divergence is not None and divergence > cfg.market_cap.spot_divergence_factor,
        stale_balance_sheet=stale is not None,
        outcomes=outcomes,
        driving=driving.name,
        denominator_disagreement=len(comparable) > 1,
        near_threshold=any(r.near_threshold for r in driving_ratios if r is not None),
        override_state=override_state,
        status=driving.status,
        reason=f"{driving.reason} [denominator: {driving.name}]",
        config_hash=cfg.hash,
    )


def _basis_name(inputs: ScreenInputs) -> str | None:
    if inputs.interest_income is None:
        return None
    if inputs.interest_income_kind == NET_INVESTMENT_INCOME:
        return NET_INVESTMENT_INCOME
    return "annual_fallback" if inputs.interest_income_fallback else "ttm"


def result_to_record(result: ScreenResult, cfg: Config) -> dict:
    """Flat audit record: one value per column in the screen_results table."""
    inputs = result.inputs
    filing = inputs.filing
    override = inputs.override if result.override_state != OVERRIDE_NONE else None
    fallback = inputs.interest_income_fallback
    record = {
        "ticker": inputs.ticker,
        "cik": inputs.cik,
        "screen_date": inputs.screen_date.isoformat(),
        "trigger": result.trigger,
        "filing_accession": filing.accession if filing else None,
        "filing_form": filing.form if filing else None,
        "filing_date": filing.filed.isoformat() if filing else None,
        "period_end": filing.period_end.isoformat() if filing else None,
        "sic": inputs.sic,
        "gics_sub_industry": inputs.gics_sub_industry,
        "debt": result.debt.value,
        "debt_assumed_zero": int(result.debt.assumed_zero),
        "debt_note": result.debt.note,
        "debt_implied_rate": result.debt_check.implied_rate,
        "debt_implausible": int(result.debt_check.implausible),
        "debt_check_note": result.debt_check.note,
        "cash_and_securities": inputs.cash_and_securities,
        "investments_assumed_zero": int(inputs.investments_assumed_zero),
        "cash_note": inputs.cash_note,
        "share_source": inputs.share_source,
        "share_classes": inputs.share_classes,
        "unlisted_class_share": inputs.unlisted_class_share,
        "interest_income": inputs.interest_income,
        "interest_income_basis": UPPER_BOUND if result.interest_upper_bound is not None else _basis_name(inputs),
        "interest_income_upper_bound": result.interest_upper_bound,
        "interest_income_bound_base": result.interest_bound_base,
        "total_assets": inputs.total_assets,
        "financing_receivables": inputs.financing_receivables,
        "financing_receivables_share": result.financing_share,
        "spot_divergence": result.spot_divergence,
        "spot_diverges": int(result.spot_diverges),
        "stale_balance_sheet": int(result.stale_balance_sheet),
        "post_balance_sheet_event": inputs.post_balance_sheet_event,
        "share_counts_rejected": inputs.share_counts_rejected,
        "share_count_jump": int(inputs.share_count_jump),
        "share_count_note": inputs.share_count_note,
        "interest_income_yield_ceiling": (
            None
            if result.interest_upper_bound is None
            else cfg.interest_income_sources.upper_bound_yield_ceiling
        ),
        "interest_income_tags": inputs.interest_income_tags,
        "interest_income_source": None if inputs.interest_income is None else inputs.interest_income_source,
        "interest_income_dimensional": int(inputs.interest_income_dimensional),
        "interest_income_annual": int(fallback is not None),
        "interest_income_max_ratio": (
            None
            if inputs.interest_income_max_share_of_limit is None
            else inputs.interest_income_max_share_of_limit * cfg.thresholds["impure_income"].limit
        ),
        "interest_income_period_end": fallback.end.isoformat() if fallback else None,
        "interest_income_filing_date": fallback.filed.isoformat() if fallback else None,
        "interest_income_accession": fallback.accession if fallback else None,
        "override_impure_income": (
            override.additional_impure_income if result.override_state == OVERRIDE_ACTIVE else None
        ),
        "impure_income": result.impure_income,
        "revenue": inputs.revenue,
        "interest_expense": inputs.interest_expense,
        "total_liabilities": inputs.total_liabilities,
        "impure_ratio": result.impure.ratio if result.impure else None,
        "driving_denominator": result.driving,
        "denominator_disagreement": int(result.denominator_disagreement),
        "near_threshold": int(result.near_threshold),
        "business_result": result.business.outcome,
        "business_category": result.business.category,
        "business_rule": result.business.rule,
        "override_id": override.id if override else None,
        "override_state": result.override_state,
        "status": result.status,
        "reason": result.reason,
        "input_notes": json.dumps(dict(inputs.notes), sort_keys=True),
        "config_hash": result.config_hash,
    }
    for name in DENOMINATORS:
        outcome = result.outcomes[name]
        cap = outcome.market_cap
        record[f"mcap_{name}"] = cap.value
        record[f"mcap_{name}_start"] = cap.start.isoformat() if cap.start else None
        record[f"mcap_{name}_end"] = cap.end.isoformat() if cap.end else None
        record[f"mcap_{name}_obs"] = cap.observations
        record[f"debt_ratio_{name}"] = outcome.debt.ratio if outcome.debt else None
        record[f"cash_ratio_{name}"] = outcome.cash.ratio if outcome.cash else None
        # A denominator that could not be computed has no would-be status, unless it drives the verdict.
        record[f"status_{name}"] = outcome.status if outcome.available or name == result.driving else None
    ratios = result.ratios()
    for name in RATIOS:
        ratio = ratios[name]
        record[f"{name}_threshold"] = cfg.thresholds[name].limit
        record[f"{name}_operator"] = cfg.thresholds[name].operator
        record[f"{name}_headroom"] = ratio.headroom if ratio else None
        record[f"{name}_headroom_rel"] = ratio.headroom_rel if ratio else None
        record[f"{name}_near"] = int(ratio.near_threshold) if ratio else None
    return record

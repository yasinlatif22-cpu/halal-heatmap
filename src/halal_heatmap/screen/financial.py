"""Financial ratio screens. Pure functions; every limit comes from config."""

from __future__ import annotations

import math
from dataclasses import dataclass

from halal_heatmap.config import DebtPlausibility, NearThreshold, Threshold, ZeroDebt


@dataclass(frozen=True)
class RatioResult:
    name: str
    numerator: float
    denominator: float
    ratio: float
    limit: float
    operator: str
    passed: bool
    headroom: float  # limit - ratio, in ratio points
    headroom_rel: float  # headroom as a share of the limit
    near_threshold: bool


@dataclass(frozen=True)
class DebtResolution:
    value: float | None
    assumed_zero: bool
    note: str


def evaluate_ratio(
    name: str, numerator: float | None, denominator: float | None, threshold: Threshold, near: NearThreshold
) -> RatioResult | None:
    """None when the ratio cannot be computed, which callers must treat as missing data."""
    if numerator is None or denominator is None or denominator <= 0:
        return None
    ratio = numerator / denominator
    passed = threshold.passes(ratio)
    headroom = threshold.limit - ratio
    if near.mode == "relative":
        floor = threshold.limit - near.margin * threshold.limit
    else:
        floor = threshold.limit - near.margin
    is_near = passed and (ratio > floor or math.isclose(ratio, floor))
    return RatioResult(
        name=name,
        numerator=numerator,
        denominator=denominator,
        ratio=ratio,
        limit=threshold.limit,
        operator=threshold.operator,
        passed=passed,
        headroom=headroom,
        headroom_rel=headroom / threshold.limit,
        near_threshold=is_near,
    )


def resolve_debt(
    debt: float | None,
    total_liabilities: float | None,
    interest_expense: float | None,
    interest_expense_reported: bool,
    revenue: float | None,
    cfg: ZeroDebt,
) -> DebtResolution:
    """Debt as reported, or zero when the absence of debt tags is corroborated."""
    if debt is not None:
        return DebtResolution(debt, False, "")
    if total_liabilities is None:
        return DebtResolution(None, False, "debt tags absent and total liabilities not reported")
    if not interest_expense_reported:
        return DebtResolution(0, True, "debt tags absent, treated as 0 (no interest expense reported)")
    if interest_expense is None or revenue is None or revenue <= 0:
        return DebtResolution(None, False, "debt tags absent but interest expense is reported and cannot be sized")
    share = interest_expense / revenue
    if share < cfg.max_interest_expense_to_revenue:
        return DebtResolution(
            0, True, f"debt tags absent, treated as 0 (interest expense {share:.4%} of revenue)"
        )
    return DebtResolution(None, False, f"debt tags absent but interest expense is {share:.4%} of revenue")


@dataclass(frozen=True)
class DebtCheck:
    implied_rate: float | None  # trailing interest expense / tagged debt
    implausible: bool
    note: str


def check_debt_plausibility(
    debt: DebtResolution,
    interest_expense: float | None,
    revenue: float | None,
    cfg: DebtPlausibility,
    zero_debt: ZeroDebt,
) -> DebtCheck:
    """Flag tagged debt that is too small to explain the interest expense actually reported."""
    if debt.value is None or debt.assumed_zero or interest_expense is None or interest_expense <= 0:
        return DebtCheck(None, False, "")
    if debt.value > 0:
        rate = interest_expense / debt.value
        if rate > cfg.max_interest_expense_to_debt:
            return DebtCheck(rate, True, f"interest expense is {rate:.1%} of tagged debt, so debt looks understated")
        return DebtCheck(rate, False, "")
    if revenue is None or revenue <= 0 or interest_expense / revenue >= zero_debt.max_interest_expense_to_revenue:
        return DebtCheck(None, True, "debt is tagged as 0 but interest expense is reported")
    return DebtCheck(None, False, "")

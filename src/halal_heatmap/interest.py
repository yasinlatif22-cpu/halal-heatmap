"""Interest income from the best source available, with the basis recorded.

Order: gross tags over trailing 12 months from companyfacts, then from the filing's own XBRL;
the annual fallback from each; sums over dimensions; and last net investment income. The last
two can understate gross interest, so each carries a cap on the ratio it may support.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date

from halal_heatmap.config import Component, InputSpec, InterestIncomeSources, Periods, TagRef
from halal_heatmap.facts import CompanyFacts, Fact, ResolvedInput, UsedFact, resolve_input

GROSS = "gross"
NET_INVESTMENT_INCOME = "net_investment_income"
UPPER_BOUND = "upper_bound_no_disclosure"
COMPANYFACTS = "companyfacts"
FILING_XBRL = "filing_xbrl"


@dataclass(frozen=True)
class FilingViews:
    plain: CompanyFacts  # facts reported without dimensions
    summed: CompanyFacts  # facts summed over their dimension members
    extension_tags: tuple[TagRef, ...]


@dataclass(frozen=True)
class InterestIncome:
    value: float | None
    missing: tuple[str, ...] = ()
    used: tuple[UsedFact, ...] = ()
    kind: str = GROSS
    source: str = COMPANYFACTS
    dimensional: bool = False
    annual_fallback: Fact | None = None
    max_share_of_limit: float | None = None  # cap on the ratio this basis may support, as a share of the limit


def _found(resolved: ResolvedInput, **basis) -> InterestIncome:
    return InterestIncome(resolved.value, used=resolved.used, annual_fallback=resolved.annual_fallback, **basis)


def resolve_interest_income(
    cf: CompanyFacts,
    spec: InputSpec,
    end: date,
    periods: Periods,
    sources: InterestIncomeSources,
    filing_views: Callable[[], FilingViews | None],
) -> InterestIncome:
    ttm_only = replace(spec, annual_fallback_max_age_days=None)
    first = resolve_input(cf, ttm_only, end, periods)
    if first.value is not None:
        return _found(first)

    views = filing_views()
    filing_spec = None
    if views is not None:
        extra = tuple((tag,) for tag in views.extension_tags)
        filing_spec = replace(spec, components=tuple(replace(c, any_of=c.any_of + extra) for c in spec.components))
        resolved = resolve_input(views.plain, replace(filing_spec, annual_fallback_max_age_days=None), end, periods)
        if resolved.value is not None:
            return _found(resolved, source=FILING_XBRL)

    resolved = resolve_input(cf, spec, end, periods)
    if resolved.value is not None:
        return _found(resolved)
    if views is not None:
        resolved = resolve_input(views.plain, filing_spec, end, periods)
        if resolved.value is not None:
            return _found(resolved, source=FILING_XBRL)
        resolved = resolve_input(views.summed, filing_spec, end, periods)
        if resolved.value is not None:
            return _found(
                resolved,
                source=FILING_XBRL,
                dimensional=True,
                max_share_of_limit=sources.dimensional_max_share_of_limit,
            )

    if sources.net_tags:
        component = Component(NET_INVESTMENT_INCOME, True, tuple((tag,) for tag in sources.net_tags))
        resolved = resolve_input(cf, replace(spec, components=(component,)), end, periods)
        if resolved.value is not None:
            return _found(resolved, kind=NET_INVESTMENT_INCOME, max_share_of_limit=sources.net_max_share_of_limit)
    return InterestIncome(None, missing=first.missing)

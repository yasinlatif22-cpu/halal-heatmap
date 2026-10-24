# ruff: noqa: E501  (the metric catalogue is a table; one metric per line reads best)
"""General financial metrics derived from a screen's stored inputs.

Every metric is a plain ratio of reported figures. A metric is None when an input is missing or a
denominator is not positive: nothing is estimated or filled in. Screening criteria (criteria.py)
and the web view read these values; neither computes anything of its own.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    group: str
    unit: str  # "ratio" (shown as a percentage), "multiple" (shown as 12.3x) or "usd"
    description: str
    higher_is: str  # "better", "worse" or "neutral": only a hint for how the web view orders values


def _div(numerator: float | None, denominator: float | None) -> float | None:
    """numerator / denominator, or None when either is missing or the denominator is not positive."""
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


METRICS: tuple[Metric, ...] = (
    Metric("market_cap", "Market cap", "Size", "usd", "Market capitalisation on the configured basis (spot or trailing average).", "neutral"),
    Metric("revenue", "Revenue (TTM)", "Size", "usd", "Trailing-twelve-month revenue.", "neutral"),
    Metric("gross_margin", "Gross margin", "Profitability", "ratio", "Gross profit / revenue, trailing twelve months.", "better"),
    Metric("operating_margin", "Operating margin", "Profitability", "ratio", "Operating income / revenue, trailing twelve months.", "better"),
    Metric("net_margin", "Net margin", "Profitability", "ratio", "Net income / revenue, trailing twelve months.", "better"),
    Metric("roe", "Return on equity", "Returns", "ratio", "Net income (TTM) / stockholders' equity at the balance sheet date. Undefined for non-positive equity.", "better"),
    Metric("roa", "Return on assets", "Returns", "ratio", "Net income (TTM) / total assets at the balance sheet date.", "better"),
    Metric("debt_to_market_cap", "Debt / market cap", "Leverage", "ratio", "Reported debt / market cap.", "worse"),
    Metric("debt_to_equity", "Debt / equity", "Leverage", "multiple", "Reported debt / stockholders' equity. Undefined for non-positive equity.", "worse"),
    Metric("liabilities_to_assets", "Liabilities / assets", "Leverage", "ratio", "Total liabilities / total assets.", "worse"),
    Metric("cash_to_market_cap", "Cash & securities / market cap", "Liquidity", "ratio", "Cash plus marketable securities / market cap.", "neutral"),
    Metric("interest_coverage", "Interest coverage", "Leverage", "multiple", "Operating income (TTM) / interest expense (TTM). Undefined when interest expense is not positive.", "better"),
    Metric("fcf_margin", "Free cash flow margin", "Cash flow", "ratio", "(Operating cash flow - capital expenditures) / revenue, trailing twelve months.", "better"),
    Metric("fcf_yield", "Free cash flow yield", "Valuation", "ratio", "(Operating cash flow - capital expenditures) / market cap.", "better"),
    Metric("earnings_yield", "Earnings yield", "Valuation", "ratio", "Net income (TTM) / market cap.", "better"),
    Metric("price_to_earnings", "Price / earnings", "Valuation", "multiple", "Market cap / net income (TTM). Undefined for non-positive earnings.", "worse"),
    Metric("price_to_sales", "Price / sales", "Valuation", "multiple", "Market cap / revenue (TTM).", "worse"),
    Metric("price_to_book", "Price / book", "Valuation", "multiple", "Market cap / stockholders' equity. Undefined for non-positive equity.", "worse"),
    Metric("interest_income_to_revenue", "Interest income / revenue", "Income mix", "ratio", "Interest income / revenue, trailing twelve months, as disclosed (see the interest income basis).", "neutral"),
)
METRIC_KEYS = tuple(m.key for m in METRICS)
_BY_KEY = {m.key: m for m in METRICS}


def metric(key: str) -> Metric:
    try:
        return _BY_KEY[key]
    except KeyError:
        raise KeyError(f"unknown metric {key!r}; known metrics: {', '.join(METRIC_KEYS)}") from None


def _fcf(x: Mapping[str, float | None]) -> float | None:
    ocf, capex = x.get("operating_cash_flow"), x.get("capital_expenditures")
    if ocf is None or capex is None:
        return None
    return ocf - abs(capex)  # capex is reported as a positive payment


_FORMULAS: dict[str, Callable[[Mapping[str, float | None]], float | None]] = {
    "market_cap": lambda x: x.get("market_cap"),
    "revenue": lambda x: x.get("revenue"),
    "gross_margin": lambda x: _div(x.get("gross_profit"), x.get("revenue")),
    "operating_margin": lambda x: _div(x.get("operating_income"), x.get("revenue")),
    "net_margin": lambda x: _div(x.get("net_income"), x.get("revenue")),
    "roe": lambda x: _div(x.get("net_income"), x.get("stockholders_equity")),
    "roa": lambda x: _div(x.get("net_income"), x.get("total_assets")),
    "debt_to_market_cap": lambda x: _div(x.get("debt"), x.get("market_cap")),
    "debt_to_equity": lambda x: _div(x.get("debt"), x.get("stockholders_equity")),
    "liabilities_to_assets": lambda x: _div(x.get("total_liabilities"), x.get("total_assets")),
    "cash_to_market_cap": lambda x: _div(x.get("cash_and_securities"), x.get("market_cap")),
    "interest_coverage": lambda x: _div(x.get("operating_income"), x.get("interest_expense")),
    "fcf_margin": lambda x: _div(_fcf(x), x.get("revenue")),
    "fcf_yield": lambda x: _div(_fcf(x), x.get("market_cap")),
    "earnings_yield": lambda x: _div(x.get("net_income"), x.get("market_cap")),
    "price_to_earnings": lambda x: _div(x.get("market_cap"), x.get("net_income")),
    "price_to_sales": lambda x: _div(x.get("market_cap"), x.get("revenue")),
    "price_to_book": lambda x: _div(x.get("market_cap"), x.get("stockholders_equity")),
    "interest_income_to_revenue": lambda x: _div(x.get("interest_income"), x.get("revenue")),
}
assert set(_FORMULAS) == set(METRIC_KEYS)


def compute_metrics(figures: Mapping[str, float | None]) -> dict[str, float | None]:
    """All metrics from a flat mapping of input name -> value. Missing inputs give None, never a guess."""
    return {key: _FORMULAS[key](figures) for key in METRIC_KEYS}


def figures_from_result(result) -> dict[str, float | None]:
    """The flat input mapping for a ScreenResult: the screen's own inputs plus the fundamentals."""
    inputs = result.inputs
    cap = result.driving_outcome.market_cap.value
    return {
        **dict(inputs.extra),
        "market_cap": cap,
        "revenue": inputs.revenue,
        "debt": result.debt.value,
        "cash_and_securities": inputs.cash_and_securities,
        "total_assets": inputs.total_assets,
        "total_liabilities": inputs.total_liabilities,
        "interest_expense": inputs.interest_expense,
        "interest_income": inputs.interest_income,
    }


def figures_from_row(row) -> dict[str, float | None]:
    """The same mapping from a stored screen_results row, so an export needs no live data."""
    import json

    keys = row.keys()
    extra = json.loads(row["extra_inputs"]) if "extra_inputs" in keys and row["extra_inputs"] else {}
    driving = row["driving_denominator"]
    return {
        **extra,
        "market_cap": row[f"mcap_{driving}"] if driving else None,
        "revenue": row["revenue"],
        "debt": row["debt"],
        "cash_and_securities": row["cash_and_securities"],
        "total_assets": row["total_assets"],
        "total_liabilities": row["total_liabilities"],
        "interest_expense": row["interest_expense"],
        "interest_income": row["interest_income"],
    }

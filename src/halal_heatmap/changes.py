"""Compare two audit records of one ticker: did the status change, which ratio crossed its
threshold, and why. Pure, and read from stored records only, so a change can be worked out
again later when the run it was measured against is superseded."""

from __future__ import annotations

import json
from collections.abc import Mapping

from halal_heatmap.config import RATIOS, Threshold

METHODOLOGY_CHANGE = "methodology_change"
OVERRIDE_ADDED = "override_added"
OVERRIDE_EXPIRED = "override_expired"
OVERRIDE_REMOVED = "override_removed"
EVENT_8K = "event_8k"
NEW_FILING = "new_filing"
PRICE_MOVE = "price_move"
OTHER = "other"
CAUSES = (
    METHODOLOGY_CHANGE,
    OVERRIDE_ADDED,
    OVERRIDE_EXPIRED,
    OVERRIDE_REMOVED,
    EVENT_8K,
    NEW_FILING,
    PRICE_MOVE,
    OTHER,
)

ACTIVE = "active"
EXPIRED = "expired"
FUNDAMENTALS = (
    "debt",
    "cash_and_securities",
    "interest_income",
    "revenue",
    "interest_expense",
    "total_liabilities",
    "total_assets",
    "financing_receivables",
)
NUMERATORS = {"debt": "debt", "cash": "cash_and_securities", "impure_income": "impure_income"}
REVENUE = "revenue"
MARKET_CAP_RATIOS = ("debt", "cash")


def _ratio_state(record: Mapping, name: str) -> dict | None:
    """One ratio of the driving denominator as the record judged it, or None if it had no value."""
    driving = record["driving_denominator"]
    on_revenue = name == "impure_income"
    ratio = record["impure_ratio"] if on_revenue else record[f"{name}_ratio_{driving}"]
    limit, operator = record[f"{name}_threshold"], record[f"{name}_operator"]
    if ratio is None or limit is None or operator is None:
        return None
    return {
        "ratio": ratio,
        "threshold": limit,
        "operator": operator,
        "passed": Threshold(limit, operator).passes(ratio),
        "numerator": record[NUMERATORS[name]],
        "denominator": REVENUE if on_revenue else f"market cap ({driving})",
        "denominator_value": record[REVENUE] if on_revenue else record[f"mcap_{driving}"],
    }


def crossings(old: Mapping, new: Mapping) -> list[dict]:
    """Ratios that were on one side of their threshold before and on the other side after."""
    out = []
    for name in RATIOS:
        before, after = _ratio_state(old, name), _ratio_state(new, name)
        if before is None or after is None or before["passed"] == after["passed"]:
            continue
        direction = "breached" if before["passed"] else "cleared"
        out.append({"ratio": name, "direction": direction, "before": before, "after": after})
    return out


def _override_factor(old: Mapping, new: Mapping) -> tuple[str, str] | None:
    before, after = old["override_state"], new["override_state"]
    if after == ACTIVE and (before != ACTIVE or old["override_id"] != new["override_id"]):
        return OVERRIDE_ADDED, f"override {new['override_id']} applies"
    if before == ACTIVE and after == EXPIRED:
        return OVERRIDE_EXPIRED, f"override {old['override_id']} expired"
    if before == ACTIVE and after != ACTIVE:
        return OVERRIDE_REMOVED, f"override {old['override_id']} no longer applies and has not expired"
    return None


def _moved_by(crossing: dict) -> str | None:
    """Whether the new figure or the new market cap alone carries a ratio across its limit."""
    before, after = crossing["before"], crossing["after"]
    if crossing["ratio"] not in MARKET_CAP_RATIOS:
        return NEW_FILING
    if None in (before["numerator"], after["numerator"]) or not before["denominator_value"]:
        return None
    limit = Threshold(after["threshold"], after["operator"])
    by_figure = limit.passes(after["numerator"] / before["denominator_value"]) == after["passed"]
    by_market_cap = limit.passes(before["numerator"] / after["denominator_value"]) == after["passed"]
    if by_market_cap and not by_figure:
        return PRICE_MOVE
    return NEW_FILING if by_figure else None


def factors(old: Mapping, new: Mapping) -> list[tuple[str, str]]:
    """Everything that differs between the two screens and could move a status, as (cause, detail),
    most likely cause first. The effect of each cannot be fully separated, so all are kept."""
    found: list[tuple[str, str]] = []
    if old["config_hash"] != new["config_hash"]:
        found.append((METHODOLOGY_CHANGE, f"config {old['config_hash']} -> {new['config_hash']}"))
    # A failed fetch empties inputs just as a new filing changes them, so it is told apart first.
    failed = bool(old["source_failed"]) != bool(new["source_failed"])
    if failed:
        found.append((OTHER, "a data source failed" if new["source_failed"] else "a data source recovered"))
    override = _override_factor(old, new)
    if override:
        found.append(override)
    event = new["post_balance_sheet_event"]
    if event and new["stale_balance_sheet"] and event != old["post_balance_sheet_event"]:
        found.append((EVENT_8K, event))

    crossed = crossings(old, new)
    changed = [name for name in FUNDAMENTALS if old[name] != new[name]]
    refiled = old["filing_accession"] != new["filing_accession"]
    filing = None
    if changed and not failed:
        source = f"filing {new['filing_accession']}" if refiled else "a restatement of the same balance sheet"
        filing = (NEW_FILING, f"figures changed with {source}: " + ", ".join(changed))

    # Market cap moves every day, so it is a factor only where it could have moved the status: a
    # market cap ratio crossed its limit, a market cap appeared or went missing, or spot pulled
    # away from the average.
    driving = new["driving_denominator"]
    before, after = old[f"mcap_{driving}"], new[f"mcap_{driving}"]
    price = None
    if failed:
        pass
    elif (before is None) != (after is None):
        price = (PRICE_MOVE, f"market cap ({driving}) {'became available' if before is None else 'is missing'}")
    elif before != after and any(c["ratio"] in MARKET_CAP_RATIOS for c in crossed):
        price = (PRICE_MOVE, f"market cap ({driving}) {before:,.0f} -> {after:,.0f}")
    elif bool(old["spot_diverges"]) != bool(new["spot_diverges"]):
        price = (PRICE_MOVE, f"spot market cap {old['mcap_spot']:,.0f} -> {new['mcap_spot']:,.0f}")

    # New figures come before a market cap move unless the market cap alone explains every crossing.
    price_first = bool(crossed) and all(_moved_by(c) == PRICE_MOVE for c in crossed)
    found.extend(f for f in ((price, filing) if price_first else (filing, price)) if f)
    if refiled and not filing and not failed:
        found.append((NEW_FILING, f"filing {new['filing_accession']} follows {old['filing_accession']}, figures equal"))
    for name in ("sic", "gics_sub_industry"):
        if old[name] != new[name]:
            found.append((OTHER, f"{name} {old[name]} -> {new[name]}"))
    if not found:
        found.append((OTHER, "no input changed; the screen date moved on"))
    return found


def diff_results(old: Mapping, new: Mapping) -> dict | None:
    """The status change from `old` to `new` as a status_changes row, or None when there is none."""
    if old["status"] == new["status"]:
        return None
    found = factors(old, new)
    cause, detail = found[0]
    return {
        "ticker": new["ticker"],
        "cik": new["cik"],
        "screen_date": new["screen_date"],
        "previous_screen_date": old["screen_date"],
        "old_status": old["status"],
        "new_status": new["status"],
        "old_reason": old["reason"],
        "new_reason": new["reason"],
        "cause": cause,
        "cause_detail": detail,
        "factors": json.dumps([{"cause": name, "detail": text} for name, text in found]),
        "crossings": json.dumps(crossings(old, new)),
    }

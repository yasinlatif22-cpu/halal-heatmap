"""Spot and averaged market capitalisation from daily closes and reported share counts."""

from __future__ import annotations

import calendar
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from halal_heatmap.config import AVERAGED_DENOMINATORS, MarketCapConfig, ShareSanity
from halal_heatmap.facts import ShareCount


@dataclass(frozen=True)
class MarketCap:
    value: float | None
    start: date | None = None
    end: date | None = None
    observations: int = 0
    note: str = ""


def add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def split_factor(splits: list[tuple[date, float]], after: date) -> float:
    return math.prod(ratio for day, ratio in splits if day > after and ratio > 0)


def daily_market_caps(
    closes: list[tuple[date, float]], splits: list[tuple[date, float]], shares: list[ShareCount]
) -> list[tuple[date, float]]:
    """Daily market cap on today's share basis.

    Closes are split-adjusted while filed share counts are as reported, so each count is
    scaled by the splits that happened after its basis date.
    """
    shares = sorted(shares, key=lambda c: c.effective)
    out = []
    index = -1
    for day, close in sorted(closes):
        while index + 1 < len(shares) and shares[index + 1].effective <= day:
            index += 1
        if index < 0:
            continue
        count = shares[index]
        out.append((day, close * count.shares * split_factor(splits, count.basis)))
    return out


@dataclass(frozen=True)
class ShareCheck:
    counts: list[ShareCount]  # the counts that survived
    rejected: int
    jump: bool  # a large change between consecutive valid counts, for review
    note: str


def _apart(a: float, b: float) -> float:
    return max(a / b, b / a)


def check_share_counts(
    counts: list[ShareCount], splits: Mapping[str, list[tuple[date, float]]], cfg: ShareSanity
) -> ShareCheck:
    """Drop reported counts that are wildly out of line with the filings around them.

    Counts are compared on today's share basis, so a real split never looks like an error.
    A count with no neighbour cannot be checked and is kept.
    A lasting change of level is not rejected, since either side could be right; it is flagged.
    """
    series: dict[tuple, list[ShareCount]] = {}
    for count in counts:
        series.setdefault((count.symbol, count.fact.dims), []).append(count)
    kept: list[ShareCount] = []
    notes: list[str] = []
    rejected = 0
    jump = False
    for (symbol, _), items in series.items():
        items.sort(key=lambda c: (c.effective, c.fact.accession))
        adjusted = [c.shares * split_factor(splits.get(symbol, []), c.basis) for c in items]
        valid: list[tuple[ShareCount, float]] = []
        for index, (count, value) in enumerate(zip(items, adjusted, strict=True)):
            around = adjusted[max(0, index - cfg.neighbours) : index] + adjusted[index + 1 : index + 1 + cfg.neighbours]
            # A bad count is far from every neighbour; a good count beside a bad one is still near another.
            if around and min(_apart(value, other) for other in around) > cfg.reject_multiple:
                rejected += 1
                notes.append(
                    f"rejected share count {count.shares:,.0f} filed {count.fact.filed} ({count.fact.accession}): "
                    f"more than {cfg.reject_multiple:g}x from neighbouring filings"
                )
            else:
                valid.append((count, value))
        for (before, a), (after, b) in zip(valid, valid[1:], strict=False):
            if _apart(a, b) > cfg.flag_multiple:
                jump = True
                notes.append(
                    f"share count changed {_apart(a, b):.2f}x on a split-adjusted basis between filings of "
                    f"{before.fact.filed} and {after.fact.filed} (reported {before.shares:,.0f} then "
                    f"{after.shares:,.0f}); review"
                )
        kept.extend(count for count, _ in valid)
    kept.sort(key=lambda c: (c.effective, c.fact.accession, c.fact.dims))
    return ShareCheck(kept, rejected, jump, "; ".join(notes))


def daily_class_market_caps(
    histories: Mapping[str, tuple[list[tuple[date, float]], list[tuple[date, float]]]], shares: list[ShareCount]
) -> list[tuple[date, float]]:
    """Daily company market cap summed over share classes, each valued at its own symbol.

    `histories` maps a symbol to (closes, splits). Counts filed together form one snapshot; a
    day uses the latest snapshot public by then and is skipped unless every symbol in it has a
    close that day.
    """
    snapshots: dict[tuple[date, str], list[ShareCount]] = {}
    for count in shares:
        snapshots.setdefault((count.effective, count.fact.accession), []).append(count)
    ordered = [snapshots[key] for key in sorted(snapshots)]
    closes = {symbol: dict(history[0]) for symbol, history in histories.items()}
    out = []
    index = -1
    for day in sorted({day for series in closes.values() for day in series}):
        while index + 1 < len(ordered) and ordered[index + 1][0].effective <= day:
            index += 1
        if index < 0:
            continue
        total = 0.0
        for count in ordered[index]:
            close = closes.get(count.symbol, {}).get(day)
            if close is None:
                break
            total += close * count.shares * split_factor(histories[count.symbol][1], count.basis)
        else:
            out.append((day, total))
    return out


def compute_market_caps(
    daily: list[tuple[date, float]], as_of: date, cfg: MarketCapConfig
) -> dict[str, MarketCap]:
    visible = [(day, value) for day, value in sorted(daily) if day <= as_of]
    out: dict[str, MarketCap] = {}

    if not visible:
        out["spot"] = MarketCap(None, note="no market cap observations")
    else:
        day, value = visible[-1]
        age = (as_of - day).days
        if age > cfg.max_spot_age_days:
            out["spot"] = MarketCap(None, note=f"latest close {day} is {age} days old")
        else:
            out["spot"] = MarketCap(value, day, day, 1)

    for name in AVERAGED_DENOMINATORS:
        months = cfg.window_months[name]
        window_start = add_months(as_of, -months)
        window = [(day, value) for day, value in visible if day > window_start]
        needed = math.ceil(cfg.min_coverage * months * cfg.trading_days_per_month)
        if len(window) < needed:
            out[name] = MarketCap(
                None, observations=len(window), note=f"{len(window)} observations, {needed} needed"
            )
        else:
            mean = sum(value for _, value in window) / len(window)
            out[name] = MarketCap(mean, window[0][0], window[-1][0], len(window))
    return out

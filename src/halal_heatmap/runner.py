"""Run the screen for a date, store what is new, and record what changed since the last valid
result. Decides which companies are due between the monthly runs. Sources are passed in."""

from __future__ import annotations

import calendar
import json
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date

from halal_heatmap.changes import diff_results
from halal_heatmap.config import Config
from halal_heatmap.facts import UsedFact
from halal_heatmap.filings import newer_filings, watched_filings
from halal_heatmap.marketcap import add_months
from halal_heatmap.overrides import Override
from halal_heatmap.pipeline import FilingSource, company_filings, screen_constituent
from halal_heatmap.screen.engine import (
    NEEDS_REVIEW,
    OVERRIDE_ACTIVE,
    OVERRIDE_EXPIRED,
    OVERRIDE_NONE,
    ScreenResult,
    result_to_record,
)
from halal_heatmap.sources.prices import PriceSource
from halal_heatmap.sources.wikipedia import Constituent
from halal_heatmap.store import ADDED, FULL, PARTIAL, REMOVED, RESULT_COLUMNS, Store

MANUAL = "manual"
MONTHLY = "monthly"
FIRST_SCREEN = "first_screen"
METHODOLOGY_CHANGE = "methodology_change"
RETRY = "retry"
NEW_FILING = "new_filing"
EVENT_FILING = "event_filing"
OVERRIDE_CHANGE = "override_change"
# Notes that mean a source could not be read, as opposed to a company not reporting something.
SOURCE_NOTES = ("filing", "prices", "sic", "filing_xbrl", "predecessor", "events", "filing_lag")
NOT_COMPARED = ("trigger",)  # the same screen is the same screen whatever asked for it


class ForwardOnlyError(ValueError):
    pass


@dataclass
class RunSummary:
    as_of: date
    run_id: int | None = None  # None when nothing new was stored
    screened: int = 0
    saved: int = 0
    unchanged: int = 0  # screens identical to the one already stored for the date
    counts: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)
    index_events: list[dict] = field(default_factory=list)
    due: dict[str, str] = field(default_factory=dict)  # ticker -> why it was screened (update only)


def record_index_events(store: Store, as_of: date, constituents: list[Constituent]) -> list[dict]:
    """Save the constituent list and what joined or left since the list before it. These are
    events of their own: a company that joins has no status change, only a first status."""
    day = as_of.isoformat()
    previous = store.snapshot_before(day)
    store.save_constituents(day, constituents)
    if previous is None:
        return []
    previous_date, before = previous
    now = {c.ticker: c for c in constituents}
    events = [(ADDED, c.ticker, c.cik, c.name) for ticker, c in sorted(now.items()) if ticker not in before]
    events += [
        (REMOVED, ticker, row["cik"], row["name"]) for ticker, row in sorted(before.items()) if ticker not in now
    ]
    store.save_index_events(day, previous_date, events)
    return [dict(row) for row in store.index_events(day)]


def _same_screen(previous: Mapping, record: dict) -> bool:
    return all(previous[name] == record[name] for name in RESULT_COLUMNS if name not in NOT_COMPARED)


def run_screen(
    as_of: date,
    cfg: Config,
    constituents: list[Constituent],
    filings: FilingSource,
    prices: PriceSource,
    overrides: Mapping[str, Override] | None = None,
    store: Store | None = None,
    *,
    tickers: list[str] | None = None,
    trigger: str | Mapping[str, str] = MANUAL,
    scope: str | None = None,
    on_result: Callable[[str, ScreenResult | None, dict | None, list[UsedFact], str], None] | None = None,
) -> RunSummary:
    """Screen the given tickers (all constituents if none) as the world stood on `as_of`.

    Only filings filed and prices dated on or before `as_of` are read. A screen identical to the
    one already stored for the same date adds no row, and a run that adds no row is not recorded.
    Stored history only moves forward: a date before the latest stored screen is refused.
    """
    by_ticker = {c.ticker: c for c in constituents}
    wanted = sorted(by_ticker) if tickers is None else list(tickers)
    unknown = [t for t in wanted if t not in by_ticker]
    if unknown:
        raise ValueError(f"not in the constituent list: {', '.join(unknown)}")
    overrides = overrides or {}
    summary = RunSummary(as_of)
    day = as_of.isoformat()
    joined: set[str] = set()
    if store is not None:
        latest = store.latest_screen_date()
        if latest is not None and day < latest:
            raise ForwardOnlyError(
                f"screens are stored up to {latest}; history only moves forward, so {day} cannot be stored "
                "(use --no-store to look at a past date)"
            )
        summary.index_events = record_index_events(store, as_of, constituents)
        joined = {event["ticker"] for event in summary.index_events if event["kind"] == ADDED}
    scope = scope or (FULL if tickers is None else PARTIAL)
    run_trigger = trigger if isinstance(trigger, str) else next(iter(set(trigger.values())), MANUAL)
    per_cik = Counter(c.cik for c in constituents)

    for ticker in wanted:
        why = trigger if isinstance(trigger, str) else trigger.get(ticker, MANUAL)
        try:
            result, used = screen_constituent(
                by_ticker[ticker],
                as_of,
                cfg,
                filings,
                prices,
                overrides.get(ticker),
                trigger=why,
                prefer_class_shares=per_cik[by_ticker[ticker].cik] > 1,
            )
        except Exception as exc:  # one bad ticker must not stop a full run; it gets no verdict
            summary.counts["error"] += 1
            message = f"unexpected {type(exc).__name__}: {exc}"
            summary.errors.append(f"{ticker}: {message}")
            if on_result:
                on_result(ticker, None, None, [], message)
            continue
        record = result_to_record(result, cfg)
        summary.screened += 1
        summary.counts[record["status"]] += 1
        summary.errors.extend(f"{ticker}: {k}: {v}" for k, v in result.inputs.notes.items() if k in SOURCE_NOTES)
        outcome = "not stored"
        if store is not None:
            previous = store.latest_result(ticker)
            if previous is not None and _same_screen(previous, record):
                summary.unchanged += 1
                outcome = "unchanged"
            else:
                if summary.run_id is None:
                    summary.run_id = store.start_run(day, cfg.hash, run_trigger, scope)
                result_id = store.save_result(summary.run_id, record, used)
                summary.saved += 1
                outcome = "stored"
                # A company that has just joined the index starts a new history.
                change = None if previous is None or ticker in joined else diff_results(previous, record)
                if change:
                    store.save_change(summary.run_id, result_id, previous["id"], change)
                    summary.changes.append(change)
                    outcome = f"{change['old_status']} -> {change['new_status']} ({change['cause']})"
        if on_result:
            on_result(ticker, result, record, used, outcome)
    if store is not None and summary.run_id is not None:
        store.finish_run(summary.run_id, dict(summary.counts), summary.errors)
    return summary


def scheduled_date(as_of: date, monthly_day: int) -> date:
    """The latest monthly screening date on or before `as_of`. A month too short for the
    configured day uses its last day."""

    def in_month(day: date) -> date:
        return day.replace(day=min(monthly_day, calendar.monthrange(day.year, day.month)[1]))

    this_month = in_month(as_of)
    return this_month if as_of >= this_month else in_month(add_months(as_of.replace(day=1), -1))


def monthly_due(as_of: date, cfg: Config, store: Store) -> bool:
    """True until a full valid run exists on or after the latest monthly date, so a missed day is
    made up by the next run."""
    return not store.has_full_run_since(scheduled_date(as_of, cfg.monthly_day).isoformat())


def _expected_override(override: Override | None, as_of: date, cfg: Config) -> tuple[str, str | None]:
    if override is None or override.date > as_of:
        return OVERRIDE_NONE, None
    state = OVERRIDE_EXPIRED if as_of > override.expires_on(cfg.override_expiry_days) else OVERRIDE_ACTIVE
    return state, override.id


def detect_due(
    as_of: date,
    cfg: Config,
    constituents: list[Constituent],
    filings: FilingSource,
    overrides: Mapping[str, Override],
    store: Store,
) -> tuple[dict[str, str], list[str]]:
    """Which constituents must be screened again and why, from the EDGAR submissions list and
    the last valid result of each. Also returns the companies whose list could not be read."""
    due: dict[str, str] = {}
    errors: list[str] = []
    for constituent in sorted(constituents, key=lambda c: c.ticker):
        ticker = constituent.ticker
        last = store.latest_result(ticker)
        if last is None:
            due[ticker] = FIRST_SCREEN
        elif last["config_hash"] != cfg.hash:
            due[ticker] = METHODOLOGY_CHANGE
        elif last["source_failed"] or last["latest_filing_accessions"] is None:
            due[ticker] = RETRY
        if ticker in due:
            continue
        periodic, reports, _, missing = company_filings(filings, constituent, as_of, cfg)
        errors.extend(f"{ticker}: {key}: {note}" for key, note in missing.items())
        mark = date.fromisoformat(last["latest_filing_date"]) if last["latest_filing_date"] else None
        seen = json.loads(last["latest_filing_accessions"])
        new = newer_filings(watched_filings(periodic, reports, cfg.events.items()), mark, seen)
        if new:
            due[ticker] = NEW_FILING if any(f.form in cfg.filings.forms for f in new) else EVENT_FILING
        elif last["business_result"] == NEEDS_REVIEW:
            expected = _expected_override(overrides.get(ticker), as_of, cfg)
            if expected != (last["override_state"], last["override_id"]):
                due[ticker] = OVERRIDE_CHANGE
    return due, errors


def update(
    as_of: date,
    cfg: Config,
    constituents: list[Constituent],
    filings: FilingSource,
    prices: PriceSource,
    overrides: Mapping[str, Override],
    store: Store,
    *,
    on_result: Callable | None = None,
) -> RunSummary:
    """The scheduled entry point: every constituent when the monthly screen is due, otherwise
    only the ones something has changed for. With nothing due, nothing is screened or stored."""
    if monthly_due(as_of, cfg, store):
        summary = run_screen(
            as_of, cfg, constituents, filings, prices, overrides, store, trigger=MONTHLY, on_result=on_result
        )
        summary.due = {c.ticker: MONTHLY for c in constituents}
        return summary
    due, errors = detect_due(as_of, cfg, constituents, filings, overrides, store)
    summary = run_screen(
        as_of,
        cfg,
        constituents,
        filings,
        prices,
        overrides,
        store,
        tickers=sorted(due),
        trigger=due,
        on_result=on_result,
    )
    summary.due = due
    summary.errors[:0] = errors
    return summary


def supersede_run(store: Store, run_id: int, reason: str) -> list[dict]:
    """Mark a run invalid and measure again the changes that were measured against it: the next
    valid result of each of its tickers is compared with the valid result now before it."""
    superseded = store.results_of_run(run_id)
    store.supersede_run(run_id, reason)
    known = {(row["result_id"], row["previous_result_id"]) for row in store.status_changes()}
    added = []
    for row in superseded:
        following = store.result_after(row["ticker"], row["id"])
        if following is None:
            continue
        before = store.result_before(row["ticker"], following["id"])
        if before is None or (following["id"], before["id"]) in known:
            continue
        change = diff_results(before, following)
        if change:
            store.save_change(following["run_id"], following["id"], before["id"], change)
            added.append(change)
    return added

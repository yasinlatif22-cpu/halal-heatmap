"""Gather inputs for one company from the data sources and run the screen."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta
from typing import Protocol

from halal_heatmap.config import Config
from halal_heatmap.facts import (
    CompanyFacts,
    ShareCount,
    UsedFact,
    find_anchor,
    has_recent_facts,
    merge_company_facts,
    resolve_input,
    share_counts,
)
from halal_heatmap.filings import (
    FilingEntry,
    class_share_counts,
    event_reported,
    extension_tags,
    income_filings,
    instance_documents,
    list_filings,
)
from halal_heatmap.interest import COMPANYFACTS, FILING_XBRL, FilingViews, InterestIncome, resolve_interest_income
from halal_heatmap.marketcap import (
    MarketCap,
    add_months,
    check_share_counts,
    compute_market_caps,
    daily_class_market_caps,
)
from halal_heatmap.overrides import Override
from halal_heatmap.screen.engine import FilingRef, ScreenInputs, ScreenResult, screen
from halal_heatmap.sources import SourceError
from halal_heatmap.sources.prices import PriceSource
from halal_heatmap.sources.wikipedia import Constituent

PRICE_LEAD_DAYS = 7


class FilingSource(Protocol):
    def company_facts(self, cik: int) -> dict: ...
    def submissions(self, cik: int) -> dict: ...
    def filing_instance(self, cik: int, accession: str, primary_document: str) -> list[dict]: ...
    def filing_text(self, cik: int, accession: str, primary_document: str) -> str: ...


def _load_instances(
    filings: FilingSource, entries: list[FilingEntry], notes: dict[str, str]
) -> list[tuple[FilingEntry, list[dict]]]:
    loaded = []
    for entry in entries:
        try:
            loaded.append((entry, filings.filing_instance(entry.cik, entry.accession, entry.primary_document)))
        except SourceError as exc:
            notes["filing_xbrl"] = str(exc)
    return loaded


def _successor_note(submissions: dict, cik: int, as_of: date, floor: date, cfg: Config) -> str | None:
    """A registrant that recently took over from another has little history under its own CIK."""
    if cik in cfg.predecessors:
        return None
    notices = [f for f in list_filings(submissions, cik, cfg.filings.successor_forms, as_of) if f.filed >= floor]
    if not notices:
        return None
    if any(f.filed < notices[0].filed for f in list_filings(submissions, cik, cfg.filings.forms, as_of)):
        return None  # it kept its CIK through the reorganisation, so its history is all here
    return (
        f"registrant filed {notices[0].form} on {notices[0].filed} and has no predecessor CIK configured; "
        "its earlier history is not visible"
    )


def _event_after(
    filings: FilingSource, reports: list[FilingEntry], after: date, cfg: Config, notes: dict[str, str]
) -> str:
    """A spin-off or major disposition reported after the balance sheet date, described; else ''."""
    wanted = cfg.events.items()
    for report in sorted(reports, key=lambda f: (f.filed, f.accession)):
        if report.filed <= after or not wanted & set(report.items) or not report.primary_document:
            continue
        try:
            text = filings.filing_text(report.cik, report.accession, report.primary_document)
        except SourceError as exc:
            notes["events"] = str(exc)
            continue
        kind = event_reported(report, text, cfg.events)
        if kind:
            return f"{kind} reported in {report.form} filed {report.filed} ({report.accession})"
    return ""


def _class_summary(counts: list[ShareCount], history: dict, as_of: date) -> tuple[str, float | None]:
    """The latest per-class counts as JSON, and the share of their value priced at another class."""
    current = [c for c in counts if c.effective <= as_of]
    if not current:
        return "", None
    latest = [c for c in current if c.fact.accession == current[-1].fact.accession]
    summary = [
        {"class": c.fact.dims.rpartition("=")[2], "shares": c.shares, "priced_as": c.symbol, "listed": c.listed}
        for c in latest
    ]
    values = [(c.listed, c.shares * history[c.symbol].closes[-1][1]) for c in latest if history[c.symbol].closes]
    total = sum(value for _, value in values)
    unlisted = sum(value for listed, value in values if not listed)
    return json.dumps(summary), (unlisted / total if total > 0 else None)


def gather_inputs(
    constituent: Constituent,
    as_of: date,
    cfg: Config,
    filings: FilingSource,
    prices: PriceSource,
    override: Override | None = None,
    prefer_class_shares: bool = False,
) -> tuple[ScreenInputs, list[UsedFact]]:
    """`prefer_class_shares` is for a company with more than one listed ticker: per-class counts
    give every ticker the same company-level market cap."""
    notes: dict[str, str] = {}
    used: list[UsedFact] = []
    values: dict[str, float | None] = {name: None for name in cfg.inputs}
    filing = None
    sic = None
    interest_expense_reported = True  # unknown is treated as reported, the conservative reading
    market_caps: dict[str, MarketCap] = {}
    counts: list[ShareCount] = []
    interest = InterestIncome(None)
    investments_assumed_zero = False
    cash_note = ""
    share_source = COMPANYFACTS
    share_classes = ""
    unlisted_class_share = None
    filing_list: list[FilingEntry] = []
    reports: list[FilingEntry] = []
    event = ""
    share_check = None
    ciks = [constituent.cik, *cfg.predecessors.get(constituent.cik, ())]
    window_start = add_months(as_of, -max(cfg.market_cap.window_months.values()))
    history_floor = window_start - timedelta(days=cfg.shares.recent_days)

    for cik in ciks:
        try:
            submissions = filings.submissions(cik)
        except SourceError as exc:
            notes["sic" if cik == constituent.cik else "predecessor"] = str(exc)
            continue
        filing_list.extend(list_filings(submissions, cik, cfg.filings.forms, as_of))
        if cik == constituent.cik:
            reports = list_filings(submissions, cik, cfg.events.forms, as_of)
            sic = str(submissions.get("sic") or "").strip() or None
            successor = _successor_note(submissions, cik, as_of, history_floor, cfg)
            if successor:
                notes["predecessor"] = successor
    filing_list.sort(key=lambda f: (f.filed, f.accession), reverse=True)

    try:
        raw = merge_company_facts([filings.company_facts(cik) for cik in ciks])
        cf = CompanyFacts(raw, forms=cfg.filings.forms, as_of=as_of)
    except SourceError as exc:
        cf = None
        notes["filing"] = str(exc)

    if cf is not None:
        anchor = find_anchor(cf, cfg.filings.balance_sheet_anchor)
        if anchor is None:
            notes["filing"] = "no balance sheet anchor fact found"
        else:
            filing = FilingRef(anchor.accession, anchor.form, anchor.filed, anchor.end)
            event = _event_after(filings, reports, anchor.end, cfg, notes)

            def filing_views() -> FilingViews | None:
                entries = income_filings(filing_list, anchor.accession, cfg.filings.annual_forms)
                loaded = _load_instances(filings, entries, notes)
                if not loaded:
                    return None
                sources = cfg.interest_income_sources
                plain, summed = instance_documents(loaded, sources.sum_axes, sources.dimensional_min_members)
                names = cfg.interest_income_sources.extension_tags
                return FilingViews(
                    CompanyFacts(plain, forms=cfg.filings.forms, as_of=as_of),
                    CompanyFacts(summed, forms=cfg.filings.forms, as_of=as_of),
                    tuple(dict.fromkeys(extension_tags(plain, names) + extension_tags(summed, names))),
                )

            for name, spec in cfg.inputs.items():
                if name == "interest_income":
                    interest = resolve_interest_income(
                        cf, spec, anchor.end, cfg.periods, cfg.interest_income_sources, filing_views
                    )
                    values[name] = interest.value
                    used.extend(interest.used)
                    if interest.value is None:
                        notes[name] = "no usable tag for: " + ", ".join(interest.missing)
                    continue
                resolved = resolve_input(cf, spec, anchor.end, cfg.periods)
                values[name] = resolved.value
                used.extend(resolved.used)
                if resolved.value is None:
                    if name != "financing_receivables":  # most companies have none; absence is not a gap
                        notes[name] = "no usable tag for: " + ", ".join(resolved.missing)
                elif name == "cash_and_securities" and resolved.absent_optional:
                    absent = resolved.absent_optional
                    investments_assumed_zero = set(absent) == set(spec.optional_components())
                    label = "investment" if investments_assumed_zero else ", ".join(absent)
                    cash_note = f"{label} tags absent, treated as 0"
            interest_expense_reported = has_recent_facts(
                cf, cfg.inputs["interest_expense"], anchor.end, cfg.zero_debt.interest_expense_lookback_days
            )

        share_tag = None
        if not prefer_class_shares:
            share_tag, counts = share_counts(cf, cfg.shares.tags, cfg.shares.recent_days)
        if not counts:
            wanted = [f for f in filing_list if f.filed >= history_floor]
            by_class, skipped = class_share_counts(
                _load_instances(filings, wanted, notes), cfg.shares.classes, constituent.ticker
            )
            if by_class and (as_of - by_class[-1].effective).days <= cfg.shares.recent_days:
                counts = by_class
                share_source = FILING_XBRL
                if skipped:
                    notes["share_classes"] = "class members not counted: " + ", ".join(skipped)
        if not counts and prefer_class_shares:
            share_tag, counts = share_counts(cf, cfg.shares.tags, cfg.shares.recent_days)
        if not counts:
            notes["shares"] = "no recent share count found"
        counts = [c if c.symbol else replace(c, symbol=constituent.ticker) for c in counts]

    if counts:
        start = window_start - timedelta(days=PRICE_LEAD_DAYS)
        try:
            history = {symbol: prices.history(symbol, start, as_of) for symbol in sorted({c.symbol for c in counts})}
            # Only counts that can reach a market cap window matter: the last one before the windows and later.
            earlier = [c.effective for c in counts if c.effective < history_floor]
            counts = [c for c in counts if not earlier or c.effective >= max(earlier)]
            share_check = check_share_counts(counts, {s: h.splits for s, h in history.items()}, cfg.shares.sanity)
            counts = share_check.counts
            if not counts:
                raise SourceError("no reliable share count: " + share_check.note)
            daily = daily_class_market_caps({s: (h.closes, h.splits) for s, h in history.items()}, counts)
            market_caps = compute_market_caps(daily, as_of, cfg.market_cap)
            current = [c for c in counts if c.effective <= as_of]
            if share_source == FILING_XBRL:
                share_classes, unlisted_class_share = _class_summary(counts, history, as_of)
                latest = [c for c in current if c.fact.accession == current[-1].fact.accession] if current else []
                used.extend(
                    UsedFact("shares_outstanding", str(cfg.shares.classes.tag), f"latest:{c.symbol}", c.fact)
                    for c in latest
                )
            elif current:
                used.append(UsedFact("shares_outstanding", str(share_tag.tag), "latest", current[-1].fact))
        except SourceError as exc:
            notes["prices"] = str(exc)
    if not market_caps:
        reason = notes.get("prices") or notes.get("shares") or notes.get("filing") or "not available"
        market_caps = {name: MarketCap(None, note=reason) for name in ("spot", *cfg.market_cap.window_months)}

    inputs = ScreenInputs(
        ticker=constituent.ticker,
        cik=constituent.cik,
        screen_date=as_of,
        sic=sic,
        gics_sub_industry=constituent.gics_sub_industry or None,
        filing=filing,
        debt=values["debt"],
        cash_and_securities=values["cash_and_securities"],
        interest_income=values["interest_income"],
        revenue=values["revenue"],
        interest_expense=values["interest_expense"],
        total_liabilities=values["total_liabilities"],
        total_assets=values["total_assets"],
        financing_receivables=values["financing_receivables"],
        interest_expense_reported=interest_expense_reported,
        market_caps=market_caps,
        override=override,
        interest_income_fallback=interest.annual_fallback,
        interest_income_kind=interest.kind,
        interest_income_source=interest.source,
        interest_income_dimensional=interest.dimensional,
        interest_income_tags=", ".join(sorted({str(u.fact.tag) for u in interest.used})),
        interest_income_max_share_of_limit=interest.max_share_of_limit,
        investments_assumed_zero=investments_assumed_zero,
        cash_note=cash_note,
        share_source=share_source,
        share_classes=share_classes,
        share_counts_rejected=share_check.rejected if share_check else 0,
        share_count_jump=bool(share_check and share_check.jump),
        share_count_note=share_check.note if share_check else "",
        post_balance_sheet_event=event,
        unlisted_class_share=unlisted_class_share,
        notes=notes,
    )
    return inputs, used


def screen_constituent(
    constituent: Constituent,
    as_of: date,
    cfg: Config,
    filings: FilingSource,
    prices: PriceSource,
    override: Override | None = None,
    trigger: str = "manual",
    prefer_class_shares: bool = False,
) -> tuple[ScreenResult, list[UsedFact]]:
    inputs, used = gather_inputs(constituent, as_of, cfg, filings, prices, override, prefer_class_shares)
    return screen(inputs, cfg, trigger), used

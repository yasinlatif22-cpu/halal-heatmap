"""What a company's own filings say beyond companyfacts: which filings to read, their facts in
companyfacts shape (plain and summed over dimensions), and per-class share counts. Pure."""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import date

from halal_heatmap.config import Events, ShareClasses, TagRef
from halal_heatmap.facts import SHARES_UNIT, Fact, ShareCount

STANDARD_TAXONOMIES = frozenset(
    {"us-gaap", "dei", "srt", "ifrs-full", "country", "currency", "exch", "naics", "sic", "stpr", "ecd", "cyd", "ffd"}
)


@dataclass(frozen=True)
class FilingEntry:
    cik: int
    accession: str
    form: str
    filed: date
    primary_document: str
    items: tuple[str, ...] = ()  # 8-K item numbers


def list_filings(submissions: dict, cik: int, forms: tuple[str, ...], as_of: date) -> list[FilingEntry]:
    """Filings of the given forms visible at the screen date, newest first."""
    recent = ((submissions or {}).get("filings") or {}).get("recent") or {}
    rows = zip(
        recent.get("accessionNumber") or [],
        recent.get("form") or [],
        recent.get("filingDate") or [],
        recent.get("primaryDocument") or [],
        recent.get("items") or itertools.repeat(""),
        strict=False,
    )
    out = []
    for accession, form, filed, document, items in rows:
        if form not in forms:
            continue
        try:
            day = date.fromisoformat(filed)
        except (TypeError, ValueError):
            continue
        if day <= as_of:
            listed = tuple(item.strip() for item in str(items or "").split(",") if item.strip())
            out.append(FilingEntry(cik, accession, form, day, document or "", listed))
    return sorted(out, key=lambda f: (f.filed, f.accession), reverse=True)


def income_filings(
    filings: list[FilingEntry], anchor_accession: str, annual_forms: tuple[str, ...]
) -> list[FilingEntry]:
    """The filing behind the latest balance sheet and the latest annual report up to it. Together
    they hold everything a trailing-12-month figure needs."""
    anchor = next((f for f in filings if f.accession == anchor_accession), None)
    if anchor is None:
        return []
    annual = next((f for f in filings if f.form in annual_forms and f.filed <= anchor.filed), None)
    return [anchor] if annual is None or annual == anchor else [anchor, annual]


def event_reported(filing: FilingEntry, text: str, events: Events) -> str | None:
    """The kind of event a current report announces, if its items and wording match one."""
    for kind in events.kinds:
        if kind.items & set(filing.items) and any(phrase.search(text) for phrase in kind.phrases):
            return kind.name
    return None


def _add(doc: dict, filing: FilingEntry, key: tuple, value: float, dims: str) -> None:
    tag, unit, start, end = key
    taxonomy, _, name = tag.partition(":")
    item = {"end": end, "val": value, "accn": filing.accession, "form": filing.form, "filed": filing.filed.isoformat()}
    if start:
        item["start"] = start
    if dims:
        item["dims"] = dims
    doc["facts"].setdefault(taxonomy, {}).setdefault(name, {"units": {}})["units"].setdefault(unit, []).append(item)


def instance_documents(
    loaded: list[tuple[FilingEntry, list[dict]]], sum_axes: frozenset[str], min_members: int
) -> tuple[dict, dict]:
    """Two companyfacts-shaped documents: facts reported without dimensions, and facts summed
    over their members where every axis is in `sum_axes`.

    Members can be reported along several axis combinations. The combination with the largest
    total is used, so an overlap overstates the figure rather than understating it. A
    combination with fewer than `min_members` members is one part of the company, not a total,
    and is ignored.
    """
    plain: dict = {"facts": {}}
    summed: dict = {"facts": {}}
    for filing, facts in loaded:
        groups: dict[tuple, dict[tuple, float]] = {}
        for fact in facts:
            if fact.get("unit") is None:
                continue
            dims = tuple((axis, member) for axis, member in fact.get("dims") or ())
            key = (fact["tag"], fact["unit"], fact.get("start"), fact["end"])
            if not dims:
                _add(plain, filing, key, fact["val"], "")
            elif all(axis in sum_axes for axis, _ in dims):
                groups.setdefault(key, {})[dims] = fact["val"]
        for key, members in groups.items():
            by_axes: dict[tuple, list[tuple[tuple, float]]] = {}
            for dims, value in members.items():
                by_axes.setdefault(tuple(axis for axis, _ in dims), []).append((dims, value))
            by_axes = {axes: rows for axes, rows in by_axes.items() if len(rows) >= min_members}
            if not by_axes:
                continue
            axes, rows = max(by_axes.items(), key=lambda item: (sum(value for _, value in item[1]), item[0]))
            parts = "; ".join(
                f"{' / '.join(member for _, member in dims)} = {value:,.0f}" for dims, value in sorted(rows)
            )
            _add(summed, filing, key, sum(value for _, value in rows), f"sum over {' x '.join(axes)}: {parts}")
    return plain, summed


def extension_tags(doc: dict, names: tuple[str, ...]) -> tuple[TagRef, ...]:
    """Company-specific tags in the document whose name is one of the accepted names."""
    return tuple(
        TagRef(taxonomy, name)
        for taxonomy, tags in sorted(doc["facts"].items())
        if taxonomy not in STANDARD_TAXONOMIES
        for name in names
        if name in tags
    )


def class_share_counts(
    loaded: list[tuple[FilingEntry, list[dict]]], rules: ShareClasses, ticker: str
) -> tuple[list[ShareCount], list[str]]:
    """Share counts per class from each filing's cover page, and the class members left out.

    A class with its own trading symbol is valued at that symbol. Other common classes are
    valued at the largest listed class, or dropped, as the rules say for this ticker. When the cover
    page ties no symbol to a class, the largest class is taken to be the listed one.
    """
    counts: list[ShareCount] = []
    skipped: set[str] = set()
    for filing, facts in loaded:
        reported = [
            f
            for f in facts
            if f["tag"] == str(rules.tag) and f.get("unit") == SHARES_UNIT and f["val"] > 0 and not f.get("start")
        ]
        if not reported:
            continue
        latest = max(f["end"] for f in reported)
        symbols = {
            f["dims"][0][1]: str(f["val"]).strip().upper()
            for f in facts
            if f["tag"] == str(rules.symbol_tag) and len(f["dims"]) == 1 and f["dims"][0][0] == rules.axis
        }
        by_class: dict[str, float] = {}
        total = None
        for f in reported:
            if f["end"] != latest:
                continue
            if not f["dims"]:
                total = f["val"]
            elif len(f["dims"]) == 1 and f["dims"][0][0] == rules.axis:
                member = f["dims"][0][1]
                if rules.is_common(member):
                    by_class[member] = f["val"]
                else:
                    skipped.add(member)

        def count(shares: float, member: str, symbol: str, listed: bool, *, filing=filing, latest=latest) -> ShareCount:
            end = date.fromisoformat(latest)
            dims = f"{rules.axis}={member}" if member else ""
            fact = Fact(rules.tag, SHARES_UNIT, shares, None, end, filing.accession, filing.form, filing.filed, dims)
            return ShareCount(filing.filed, end, shares, fact, symbol, listed)

        if by_class:
            listed = {m for m in by_class if m in symbols} or {max(by_class, key=lambda m: (by_class[m], m))}
            primary = symbols.get(max(listed, key=lambda m: (by_class[m], m)), ticker)
            for member, shares in sorted(by_class.items()):
                if member in listed:
                    counts.append(count(shares, member, symbols.get(member, ticker), True))
                elif rules.unlisted_mode(ticker) == "price_at_listed":
                    counts.append(count(shares, member, primary, False))
                else:
                    skipped.add(member)
        elif total is not None:
            counts.append(count(total, "", ticker, True))
    counts.sort(key=lambda c: (c.effective, c.fact.accession, c.fact.dims))
    return counts, sorted(skipped)

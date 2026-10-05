"""Turn an EDGAR companyfacts document into normalised screen inputs.

Only facts filed on or before the screen date are visible, so a screen never uses a value
that was not public at the time.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date, timedelta

from halal_heatmap.config import InputSpec, Periods, ShareTag, TagRef

MONEY_UNIT = "USD"
SHARES_UNIT = "shares"


@dataclass(frozen=True)
class Fact:
    tag: TagRef
    unit: str
    value: float
    start: date | None
    end: date
    accession: str
    form: str
    filed: date
    dims: str = ""  # how a fact read from a filing's own XBRL was dimensioned; empty for plain facts

    @property
    def days(self) -> int:
        return (self.end - self.start).days if self.start else 0


@dataclass(frozen=True)
class UsedFact:
    input: str
    component: str
    role: str
    fact: Fact


@dataclass(frozen=True)
class ResolvedInput:
    name: str
    value: float | None
    missing: tuple[str, ...]
    used: tuple[UsedFact, ...]
    absent_optional: tuple[str, ...] = ()  # optional components with no tag, counted as 0
    annual_fallback: Fact | None = None  # set when an annual figure stood in for trailing 12 months
    left_out: tuple[str, ...] = ()  # tags reported but not summed, because a total already holds them
    kept: tuple[str, ...] = ()  # contained tags kept, because their container is smaller than they are


@dataclass(frozen=True)
class ShareCount:
    effective: date  # first day the count was public
    basis: date  # date the count is "as of" for split adjustment
    shares: float
    fact: Fact
    symbol: str | None = None  # ticker whose price values this count; None means the screened ticker
    listed: bool = True  # False for a class priced at another class's price


class CompanyFacts:
    def __init__(self, raw: dict, *, forms: tuple[str, ...], as_of: date):
        self._facts = (raw or {}).get("facts") or {}
        self._forms = set(forms)
        self.as_of = as_of
        self._cache: dict[tuple[TagRef, str], list[Fact]] = {}
        self._accessions: frozenset[str] | None = None

    def accessions(self) -> frozenset[str]:
        """Every filing the document holds a fact from, whatever its form or date."""
        if self._accessions is None:
            self._accessions = frozenset(
                item.get("accn")
                for tags in self._facts.values()
                for body in tags.values()
                for items in (body.get("units") or {}).values()
                for item in items
            )
        return self._accessions

    def facts(self, tag: TagRef, unit: str = MONEY_UNIT) -> list[Fact]:
        key = (tag, unit)
        if key not in self._cache:
            self._cache[key] = self._parse(tag, unit)
        return self._cache[key]

    def _parse(self, tag: TagRef, unit: str) -> list[Fact]:
        items = self._facts.get(tag.taxonomy, {}).get(tag.name, {}).get("units", {}).get(unit, [])
        out = []
        for item in items:
            try:
                filed = date.fromisoformat(item["filed"])
                form = item["form"]
                if form not in self._forms or filed > self.as_of:
                    continue
                out.append(
                    Fact(
                        tag=tag,
                        unit=unit,
                        value=float(item["val"]),
                        start=date.fromisoformat(item["start"]) if item.get("start") else None,
                        end=date.fromisoformat(item["end"]),
                        accession=item["accn"],
                        form=form,
                        filed=filed,
                        dims=item.get("dims", ""),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return out


def merge_company_facts(docs: list[dict]) -> dict:
    """One companyfacts document from several registrants of the same company."""
    merged: dict = {"facts": {}}
    for doc in docs:
        for taxonomy, tags in ((doc or {}).get("facts") or {}).items():
            for name, body in tags.items():
                target = merged["facts"].setdefault(taxonomy, {}).setdefault(name, {"units": {}})
                for unit, items in (body.get("units") or {}).items():
                    target["units"].setdefault(unit, []).extend(items)
    return merged


def with_instance_facts(raw: dict, loaded: list) -> dict:
    """companyfacts plus the undimensioned facts of filings it does not serve yet. `loaded` holds
    (filing, instance facts) pairs, where a filing has `form`, `accession` and `filed`. Facts are added
    in companyfacts shape, so the anchor search and every other reader treat them the same way."""
    merged = copy.deepcopy(raw)
    for filing, facts in loaded:
        for fact in facts:
            if fact.get("dims") or fact.get("unit") is None:
                continue
            taxonomy, _, name = str(fact["tag"]).rpartition(":")
            item = {
                "end": fact["end"],
                "val": fact["val"],
                "filed": filing.filed.isoformat(),
                "form": filing.form,
                "accn": filing.accession,
            }
            if fact.get("start"):
                item["start"] = fact["start"]
            tags = merged.setdefault("facts", {}).setdefault(taxonomy or "us-gaap", {})
            target = tags.setdefault(name, {"units": {}})
            target["units"].setdefault(fact["unit"], []).append(item)
    return merged


def _latest_filed(facts: list[Fact]) -> Fact:
    return max(facts, key=lambda f: (f.filed, f.accession))


def find_anchor(cf: CompanyFacts, anchor_tags: tuple[TagRef, ...]) -> Fact | None:
    """The latest balance sheet visible, as the fact from the filing that first reported it."""
    instants = [f for tag in anchor_tags for f in cf.facts(tag) if f.start is None]
    if not instants:
        return None
    end = max(f.end for f in instants)
    return min((f for f in instants if f.end == end), key=lambda f: (f.filed, f.accession))


def instant_fact(cf: CompanyFacts, tag: TagRef, end: date) -> Fact | None:
    candidates = [f for f in cf.facts(tag) if f.start is None and f.end == end]
    return _latest_filed(candidates) if candidates else None


def ttm_facts(cf: CompanyFacts, tag: TagRef, end: date, periods: Periods) -> list[tuple[str, Fact]] | None:
    """Facts that make up the trailing twelve months ending at `end`.

    Either one annual fact, or prior fiscal year + year to date - prior year to date.
    """
    durations = [f for f in cf.facts(tag) if f.start is not None]
    at_end = [f for f in durations if f.end == end and f.days <= periods.annual_max_days]
    if not at_end:
        return None
    start = min(f.start for f in at_end)
    ytd = _latest_filed([f for f in at_end if f.start == start])
    if ytd.days >= periods.annual_min_days:
        return [("annual", ytd)]

    tolerance = periods.tolerance_days
    annuals = [f for f in durations if periods.annual_min_days <= f.days <= periods.annual_max_days]
    before = [f for f in annuals if abs((ytd.start - f.end).days - 1) <= tolerance]
    if not before:
        return None
    closest = min(before, key=lambda f: abs((ytd.start - f.end).days - 1))
    fiscal_year = _latest_filed([f for f in before if (f.start, f.end) == (closest.start, closest.end)])

    priors = [
        f
        for f in durations
        if abs((f.start - fiscal_year.start).days) <= tolerance and abs(f.days - ytd.days) <= tolerance
    ]
    if not priors:
        return None
    nearest = min(priors, key=lambda f: abs(f.days - ytd.days))
    prior_ytd = _latest_filed([f for f in priors if (f.start, f.end) == (nearest.start, nearest.end)])
    return [("fiscal_year", fiscal_year), ("ytd", ytd), ("prior_ytd", prior_ytd)]


def latest_annual_fact(cf: CompanyFacts, tag: TagRef, end: date, periods: Periods, max_age_days: int) -> Fact | None:
    """The most recent annual fact ending on or before `end`, if recent enough at the screen date."""
    annuals = [
        f
        for f in cf.facts(tag)
        if f.start is not None and f.end <= end and periods.annual_min_days <= f.days <= periods.annual_max_days
    ]
    if not annuals:
        return None
    latest_end = max(f.end for f in annuals)
    if (cf.as_of - latest_end).days > max_age_days:
        return None
    return _latest_filed([f for f in annuals if f.end == latest_end])


def ttm_value(parts: list[tuple[str, Fact]]) -> float:
    return sum(-f.value if role == "prior_ytd" else f.value for role, f in parts)


def _value_at(cf: CompanyFacts, tag: TagRef, end: date, spec: InputSpec, periods: Periods) -> float | None:
    if spec.period == "instant":
        fact = instant_fact(cf, tag, end)
        return fact.value if fact else None
    parts = ttm_facts(cf, tag, end, periods)
    return ttm_value(parts) if parts else None


def _reported_at(cf: CompanyFacts, tag: TagRef, end: date, spec: InputSpec, periods: Periods) -> bool:
    return _value_at(cf, tag, end, spec, periods) is not None


def _overlaps(cf: CompanyFacts, spec: InputSpec, end: date, periods: Periods) -> tuple[set, set]:
    """Tags left out of the sum, and contained tags kept because their container is smaller.

    A contained tag is left out only when its container is at least as large at the same period.
    When the container is smaller, the contained tag cannot be part of it, so both are kept and the
    kept tag is noted. A preferred line removes the broader total it is reported with."""
    excluded: set[TagRef] = set()
    kept: set[TagRef] = set()
    for rule in spec.within:
        tops = [v for v in (_value_at(cf, tag, end, spec, periods) for tag in rule.inside) if v is not None]
        if not tops:
            continue
        top = max(tops)
        for tag in rule.contains:
            value = _value_at(cf, tag, end, spec, periods)
            if value is None:
                continue
            if value <= top:
                excluded.add(tag)
            else:
                kept.add(tag)
    kept -= excluded
    for rule in spec.prefer:
        line = [v for v in (_value_at(cf, tag, end, spec, periods) for tag in rule.use) if v is not None]
        total = [v for v in (_value_at(cf, tag, end, spec, periods) for tag in rule.over) if v is not None]
        cover = [v for v in (_value_at(cf, tag, end, spec, periods) for tag in rule.covered_by) if v is not None]
        if line and total and cover and max(total) - max(line) <= max(cover):
            excluded.update(rule.over)
    return excluded, kept


def resolve_input(cf: CompanyFacts, spec: InputSpec, end: date, periods: Periods) -> ResolvedInput:
    total = 0.0
    found = False
    missing: list[str] = []
    absent_optional: list[str] = []
    fallback: Fact | None = None
    used: list[UsedFact] = []
    # Tags a reported total already holds are left out, so a figure is never counted twice.
    contained, kept_tags = _overlaps(cf, spec, end, periods)
    left_out = tuple(sorted(str(t) for t in contained if _reported_at(cf, t, end, spec, periods)))
    kept = tuple(sorted(str(t) for t in kept_tags if _reported_at(cf, t, end, spec, periods)))
    for comp in spec.components:
        candidates: list[list[tuple[float, list[tuple[str, Fact]]]]] = []
        for alternative in comp.any_of:
            reported = []
            for tag in alternative:
                if tag in contained:
                    continue
                if spec.period == "instant":
                    fact = instant_fact(cf, tag, end)
                    if fact:
                        reported.append((fact.value, [("instant", fact)]))
                else:
                    parts = ttm_facts(cf, tag, end, periods)
                    if parts:
                        reported.append((ttm_value(parts), parts))
            if reported:
                candidates.append(reported)
                if spec.combine == "first":
                    break
        if not candidates and spec.annual_fallback_max_age_days is not None:
            for alternative in comp.any_of:
                annual = [
                    None
                    if tag in contained
                    else latest_annual_fact(cf, tag, end, periods, spec.annual_fallback_max_age_days)
                    for tag in alternative
                ]
                reported = [(f.value, [("annual_fallback", f)]) for f in annual if f is not None]
                if reported:
                    candidates.append(reported)
                    if spec.combine == "first":
                        break
        if not candidates:
            if comp.required:
                missing.append(comp.name)
            else:
                absent_optional.append(comp.name)
            continue
        found = True
        chosen = max(candidates, key=lambda reported: sum(value for value, _ in reported))
        for value, parts in chosen:
            total += value
            used.extend(UsedFact(spec.name, comp.name, role, fact) for role, fact in parts)
            for role, fact in parts:
                if role == "annual_fallback":
                    fallback = fallback or fact
    if not found and not missing:
        missing = [comp.name for comp in spec.components]
    if missing:
        return ResolvedInput(spec.name, None, tuple(missing), (), left_out=left_out, kept=kept)
    return ResolvedInput(spec.name, total, (), tuple(used), tuple(absent_optional), fallback, left_out, kept)


def has_recent_facts(cf: CompanyFacts, spec: InputSpec, end: date, lookback_days: int) -> bool:
    floor = end - timedelta(days=lookback_days)
    return any(floor < f.end <= end for tag in spec.all_tags() for f in cf.facts(tag))


def share_counts(
    cf: CompanyFacts, share_tags: tuple[ShareTag, ...], recent_days: int
) -> tuple[ShareTag | None, list[ShareCount]]:
    """History of reported share counts from the first tag that is still in use."""
    for share_tag in share_tags:
        facts = [f for f in cf.facts(share_tag.tag, SHARES_UNIT) if f.value > 0]
        if not facts or (cf.as_of - max(f.filed for f in facts)).days > recent_days:
            continue
        per_filing: dict[str, Fact] = {}
        for fact in facts:
            current = per_filing.get(fact.accession)
            # Latest period in the filing; for durations, the shortest one ending then.
            rank = (fact.end, fact.start or date.min)
            if current is None or rank > (current.end, current.start or date.min):
                per_filing[fact.accession] = fact
        counts = [
            ShareCount(
                effective=f.filed,
                basis=f.end if share_tag.split_basis == "end" else f.filed,
                shares=f.value,
                fact=f,
            )
            for f in per_filing.values()
        ]
        counts.sort(key=lambda c: (c.effective, c.fact.end))
        return share_tag, counts
    return None, []

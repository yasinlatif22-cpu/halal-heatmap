"""Business activity screen from SIC code and GICS sub-industry. Pure, no I/O."""

from __future__ import annotations

from dataclasses import dataclass

from halal_heatmap.config import Business

PASS = "pass"
FAIL = "fail"
NEEDS_REVIEW = "needs_review"
INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True)
class BusinessResult:
    outcome: str
    category: str | None
    rule: str


def _sic_number(sic: str | None) -> int | None:
    try:
        return int(str(sic).strip())
    except (TypeError, ValueError):
        return None


def screen_business(sic: str | None, gics_sub_industry: str | None, cfg: Business) -> BusinessResult:
    sic_number = _sic_number(sic)
    gics = (gics_sub_industry or "").strip() or None
    matches: dict[str, list[tuple[str, str]]] = {FAIL: [], NEEDS_REVIEW: []}
    for rule in cfg.rules:
        if gics and gics in rule.gics_sub_industries:
            matches[rule.action].append((rule.category, f"GICS sub-industry '{gics}'"))
        if sic_number is not None and any(low <= sic_number <= high for low, high in rule.sic_ranges):
            matches[rule.action].append((rule.category, f"SIC {sic_number}"))

    for outcome in (FAIL, NEEDS_REVIEW):
        if matches[outcome]:
            description = "; ".join(f"{category}: {source}" for category, source in matches[outcome])
            return BusinessResult(outcome, matches[outcome][0][0], description)

    present = {"sic": sic_number is not None, "gics_sub_industry": gics is not None}
    absent = [name for name in cfg.required_classifications if not present[name]]
    if absent:
        return BusinessResult(INSUFFICIENT_DATA, None, "classification missing: " + ", ".join(absent))
    return BusinessResult(PASS, None, f"no rule matched SIC {sic_number} / GICS sub-industry '{gics}'")

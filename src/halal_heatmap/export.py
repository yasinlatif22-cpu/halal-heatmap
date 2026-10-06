"""Static site data: the latest valid screen of every stock and the stored status changes, as the
JSON that web/ reads. Nothing is screened here. Verdicts come from the stored records; the only
things computed at export time are the near-threshold flags (under the current margin) and the
daily percent change, which is the one value fetched from the price source.

Reviewer names never leave this module: each reviewer is a neutral label, and any name that
reaches a text field is replaced.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from halal_heatmap.config import DENOMINATORS, Config, Threshold
from halal_heatmap.interest import NET_INVESTMENT_INCOME, UPPER_BOUND
from halal_heatmap.overrides import Override
from halal_heatmap.screen.financial import near_threshold_flag
from halal_heatmap.sources import SourceError
from halal_heatmap.sources.prices import PriceSource
from halal_heatmap.store import Store

DISCLAIMER = (
    "Automated screen, not a fatwa and not investment advice. Each verdict depends on the methodology "
    "in the config hash below, on SEC filings and on market data that may be late, incomplete or wrong. "
    "Confirm any decision with a qualified Shari'ah adviser and your own research."
)
RATIO_LABELS = {
    "debt": "Interest-bearing debt / market cap",
    "cash": "Cash and interest-bearing securities / market cap",
    "impure_income": "Impure income (interest) / revenue",
}
UNKNOWN_REVIEWER = "Reviewer (no longer in overrides.yaml)"
NEAR_STATUSES = ("pass", "needs_review")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
NEUTRAL_REVIEWER = "maintainer"
OVERRIDE_BY = re.compile(r"manual override by .*? on (\d{4}-\d{2}-\d{2})")
SEC_ARCHIVE = "https://www.sec.gov/Archives/edgar/data"


def reviewer_labels(overrides: Mapping[str, Override]) -> dict[str, str]:
    """Every reviewer is shown as the same neutral label, so no name reaches the export."""
    return {override.reviewer: NEUTRAL_REVIEWER for override in overrides.values()}


def scrub_text(text: str, labels: Mapping[str, str]) -> str:
    for name, label in labels.items():  # whole words only, so short names do not touch other text
        text = re.sub(rf"(?<![\w]){re.escape(name)}(?![\w])", label, text)
    text = OVERRIDE_BY.sub(rf"manual override by {NEUTRAL_REVIEWER} on \1", text)
    return EMAIL.sub("[email removed]", text)


def scrub(value: Any, labels: Mapping[str, str]) -> Any:
    """Every string in an exported payload, scrubbed. Keys are written by this module, not read from data."""
    if isinstance(value, str):
        return scrub_text(value, labels)
    if isinstance(value, dict):
        return {key: scrub(item, labels) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item, labels) for item in value]
    return value


def _json(text: str | None, default: Any) -> Any:
    return default if not text else json.loads(text)


def filing_url(cik: int | None, accession: str | None) -> str | None:
    if not cik or not accession:
        return None
    return f"{SEC_ARCHIVE}/{int(cik)}/{accession.replace('-', '')}/"


def _confidence(key: str) -> str:
    if key == "none":
        return "none"
    return "standard" if key == "disclosed" else "lower"


def _interest_basis(row) -> dict:
    code = row["interest_income_basis"]
    if code is None:
        key = "none"
    elif code == UPPER_BOUND:
        on_cash = row["interest_income_bound_base"] == "cash_and_securities"
        key = "upper_bound_cash" if on_cash else "upper_bound_total_assets"
    elif code == NET_INVESTMENT_INCOME:
        key = "net_investment_income"
    elif code == "annual_fallback":
        key = "annual_fallback"
    else:  # "ttm": gross interest over the trailing 12 months
        key = "disclosed_partial" if row["interest_income_dimensional"] else "disclosed"
    annual = None
    if row["interest_income_annual"]:
        annual = {
            "period_end": row["interest_income_period_end"],
            "filed": row["interest_income_filing_date"],
            "accession": row["interest_income_accession"],
        }
    bound = None
    if code == UPPER_BOUND:
        bound = {
            "value": row["interest_income_upper_bound"],
            "yield_ceiling": row["interest_income_yield_ceiling"],
            "base": row["interest_income_bound_base"],
        }
    return {
        "key": key,
        # Only disclosed gross interest is standard. Every other basis is lower confidence: a ceiling
        # from the balance sheet, a net or partial figure, or one from an older annual filing.
        "confidence": _confidence(key),
        "value": row["interest_income"],
        "source": row["interest_income_source"],
        "tags": [tag.strip() for tag in (row["interest_income_tags"] or "").split(",") if tag.strip()],
        "dimensional": bool(row["interest_income_dimensional"]),
        "annual": annual,
        "upper_bound": bound,
        "max_ratio_for_pass": row["interest_income_max_ratio"],
    }


def _ratio(row, name: str, cfg: Config) -> dict:
    """One financial ratio as stored, with its near-threshold flag recomputed under the current margin."""
    if name == "impure_income":
        ratio_value, numerator, denominator = row["impure_ratio"], row["impure_income"], row["revenue"]
        denominator_basis = "revenue"
    else:
        numerator = row["debt"] if name == "debt" else row["cash_and_securities"]
        ratio_value = row[f"{name}_ratio_{row['driving_denominator']}"]
        denominator = row[f"mcap_{row['driving_denominator']}"]
        denominator_basis = f"market cap ({row['driving_denominator']})"
    threshold = Threshold(row[f"{name}_threshold"], row[f"{name}_operator"])
    if ratio_value is None:
        near = False
        passed = None
    else:
        passed = threshold.passes(ratio_value)
        near = near_threshold_flag(ratio_value, passed, threshold, cfg.near_threshold)
    return {
        "name": name,
        "label": RATIO_LABELS[name],
        "ratio": ratio_value,
        "numerator": numerator,
        "denominator": denominator,
        "denominator_basis": denominator_basis,
        "limit": threshold.limit,
        "operator": threshold.operator,
        "passed": passed,
        "headroom": row[f"{name}_headroom"],
        "headroom_rel": row[f"{name}_headroom_rel"],
        "near_threshold": near,
    }


def _override(row, cfg: Config, by_id: Mapping[str, Override], labels: Mapping[str, str]) -> dict | None:
    if row["override_state"] == "none":
        return None
    ident = row["override_id"]
    entry = by_id.get(ident)
    if entry is not None:
        expires = entry.expires_on(cfg.override_expiry_days).isoformat()
        reviewer = labels.get(entry.reviewer, UNKNOWN_REVIEWER)
        decision, reason, when = entry.decision, entry.reason, entry.date.isoformat()
    else:  # an override since removed from overrides.yaml: only what the record kept
        when = ident.split("@", 1)[1]
        expires = (date.fromisoformat(when) + timedelta(days=cfg.override_expiry_days)).isoformat()
        reviewer, decision, reason = UNKNOWN_REVIEWER, None, None
    return {
        "id": ident,
        "state": row["override_state"],
        "decision": decision,
        "reason": reason,
        "reviewer": reviewer,
        "date": when,
        "expires_on": expires,
        "additional_impure_income": row["override_impure_income"],
    }


def _screen(row, facts: list, cfg: Config, member: Mapping[str, Any], by_id, labels) -> dict:
    """`member` holds the constituent's name, sector and sub-industry, and whether it is in the index."""
    driving = row["driving_denominator"]
    ratios = {name: _ratio(row, name, cfg) for name in ("debt", "cash", "impure_income")}
    override = _override(row, cfg, by_id, labels)
    # Near a limit matters only where the verdict could still be a pass: a fail or insufficient_data
    # has nothing to protect. The ratio flags above keep the raw figure for each limit.
    near = row["status"] in NEAR_STATUSES and any(r["near_threshold"] for r in ratios.values())
    return {
        "ticker": row["ticker"],
        "name": member.get("name") or row["ticker"],
        "sector": member.get("sector"),
        "sub_industry": member.get("sub_industry") or row["gics_sub_industry"],
        "in_index": member.get("in_index", False),
        "cik": row["cik"],
        "status": row["status"],
        "reason": row["reason"],
        "screen_date": row["screen_date"],
        "run_id": row["run_id"],
        "config_hash": row["config_hash"],
        "driving_denominator": driving,
        "near_threshold": near,
        "spot_market_cap": row["mcap_spot"],
        "daily_change": None,  # filled in by the export when prices are read
        "ratios": ratios,
        "market_cap": {
            "driving": driving,
            "denominators": {
                name: {
                    "value": row[f"mcap_{name}"],
                    "start": row[f"mcap_{name}_start"],
                    "end": row[f"mcap_{name}_end"],
                    "observations": row[f"mcap_{name}_obs"],
                    "status": row[f"status_{name}"],
                    "debt_ratio": row[f"debt_ratio_{name}"],
                    "cash_ratio": row[f"cash_ratio_{name}"],
                }
                for name in DENOMINATORS
            },
        },
        "interest_income": _interest_basis(row),
        "filing": {
            "form": row["filing_form"],
            "accession": row["filing_accession"],
            "filed": row["filing_date"],
            "period_end": row["period_end"],
            "url": filing_url(row["cik"], row["filing_accession"]),
        },
        "inputs": {
            "debt": {
                "value": row["debt"],
                "assumed_zero": bool(row["debt_assumed_zero"]),
                "note": row["debt_note"],
                "check_note": row["debt_check_note"],
                "interest_expense_to_debt": row["debt_implied_rate"],
            },
            "cash_and_securities": {
                "value": row["cash_and_securities"],
                "investments_assumed_zero": bool(row["investments_assumed_zero"]),
                "note": row["cash_note"],
            },
            "revenue": row["revenue"],
            "impure_income": row["impure_income"],
            "interest_expense": row["interest_expense"],
            "total_liabilities": row["total_liabilities"],
            "total_assets": row["total_assets"],
            "financing_receivables": {
                "value": row["financing_receivables"],
                "share_of_assets": row["financing_receivables_share"],
            },
            "notes": _json(row["input_notes"], {}),
        },
        "shares": {
            "source": row["share_source"],
            "classes": _json(row["share_classes"], None),
            "unlisted_share": row["unlisted_class_share"],
            "rejected": row["share_counts_rejected"],
            "jump": bool(row["share_count_jump"]),
            "note": row["share_count_note"],
        },
        "spot_divergence": {"factor": row["spot_divergence"], "diverges": bool(row["spot_diverges"])},
        "balance_sheet": {
            "stale": bool(row["stale_balance_sheet"]),
            "post_balance_sheet_event": row["post_balance_sheet_event"],
            "latest_filing_date": row["latest_filing_date"],
        },
        "business": {
            "result": row["business_result"],
            "category": row["business_category"],
            "rule": row["business_rule"],
            "sic": row["sic"],
        },
        "override": override,
        "flags": {
            "denominator_disagreement": bool(row["denominator_disagreement"]),
            "spot_divergence": bool(row["spot_diverges"]),
            "share_count": bool(row["share_count_jump"]) or bool(row["share_counts_rejected"]),
            "stale_balance_sheet": bool(row["stale_balance_sheet"]),
            "override": override is not None and override["state"] == "active",
            "debt_implausible": bool(row["debt_implausible"]),
            "investments_assumed_zero": bool(row["investments_assumed_zero"]),
            "debt_assumed_zero": bool(row["debt_assumed_zero"]),
            "annual_interest_income": bool(row["interest_income_annual"]),
            "unlisted_share_classes": bool(row["unlisted_class_share"]),
        },
        "facts": [
            {
                "input": f["input"],
                "component": f["component"],
                "role": f["role"],
                "tag": f["tag"],
                "unit": f["unit"],
                "value": f["value"],
                "period_start": f["period_start"],
                "period_end": f["period_end"],
                "form": f["form"],
                "accession": f["accession"],
                "filed": f["filed"],
                "dimensions": f["dimensions"],
            }
            for f in facts
        ],
    }


def daily_changes(
    prices: PriceSource, wanted: Iterable[tuple[str, date]]
) -> tuple[dict[str, dict | None], list[str]]:
    """Percent change between the last two closes before each screen date. The screen date's own
    close is never read, as the screen does not read it. Only the derived change is returned."""
    changes: dict[str, dict | None] = {}
    failed: list[str] = []
    for ticker, screen_date in wanted:
        try:
            history = prices.history(ticker, screen_date - timedelta(days=14), screen_date - timedelta(days=1))
        except SourceError:
            failed.append(ticker)
            changes[ticker] = None
            continue
        closes = sorted((day, close) for day, close in history.closes if day < screen_date)
        if len(closes) < 2 or closes[-2][1] <= 0:
            changes[ticker] = None
            continue
        (_, before), (day, after) = closes[-2], closes[-1]
        changes[ticker] = {"date": day.isoformat(), "pct": after / before - 1}
    return changes, failed


def build_site(
    store: Store, cfg: Config, overrides: Mapping[str, Override], prices: PriceSource | None = None
) -> dict[str, Any]:
    """The three payloads, keyed by file name. Pure except for the price source, when one is given."""
    labels = reviewer_labels(overrides)
    by_id = {override.id: override for override in overrides.values()}
    snapshot = store.latest_snapshot()
    snapshot_date, members = snapshot if snapshot else (None, {})
    member = {
        ticker: {"name": row["name"], "sector": row["gics_sector"], "sub_industry": row["gics_sub_industry"]}
        for ticker, row in members.items()
    }

    screens = []
    for row in store.latest_results():
        facts = [dict(f) for f in store.facts_for(row["id"])]
        info = {**member.get(row["ticker"], {}), "in_index": row["ticker"] in members}
        screens.append(_screen(row, facts, cfg, info, by_id, labels))

    daily, failed = {}, []
    if prices is not None:
        daily, failed = daily_changes(prices, [(s["ticker"], date.fromisoformat(s["screen_date"])) for s in screens])
        for screen in screens:
            screen["daily_change"] = daily.get(screen["ticker"])

    changes = []
    for row in store.status_changes():
        changes.append(
            {
                "ticker": row["ticker"],
                "name": member.get(row["ticker"], {}).get("name") or row["ticker"],
                "screen_date": row["screen_date"],
                "previous_screen_date": row["previous_screen_date"],
                "old_status": row["old_status"],
                "new_status": row["new_status"],
                "old_reason": row["old_reason"],
                "new_reason": row["new_reason"],
                "cause": row["cause"],
                "cause_detail": row["cause_detail"],
                "factors": _json(row["factors"], []),
                "crossings": _json(row["crossings"], []),
            }
        )
    changes.sort(key=lambda c: (c["screen_date"], c["ticker"]), reverse=True)
    index_events = [
        {"event_date": e["event_date"], "kind": e["kind"], "ticker": e["ticker"], "name": e["name"]}
        for e in store.index_events()
    ]

    by_status = {}
    for screen in screens:
        by_status[screen["status"]] = by_status.get(screen["status"], 0) + 1
    hashes = sorted({s["config_hash"] for s in screens})
    run_ids = sorted({s["run_id"] for s in screens})
    latest_screen = max((s["screen_date"] for s in screens), default=None)
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "screen_date": latest_screen,
        "run_ids": run_ids,
        "config_hash": hashes[0] if len(hashes) == 1 else hashes,
        "current_config_hash": cfg.hash,
        "config_matches": hashes == [cfg.hash],
        "constituents_as_of": snapshot_date,
        "disclaimer": DISCLAIMER,
        "thresholds": {
            name: {"label": RATIO_LABELS[name], "limit": t.limit, "operator": t.operator}
            for name, t in cfg.thresholds.items()
        },
        "near_threshold": {"mode": cfg.near_threshold.mode, "margin": cfg.near_threshold.margin},
        "market_cap": {
            "driving": cfg.market_cap.driving,
            "window_months": cfg.market_cap.window_months,
            "min_coverage": cfg.market_cap.min_coverage,
            "trading_days_per_month": cfg.market_cap.trading_days_per_month,
            "max_spot_age_days": cfg.market_cap.max_spot_age_days,
            "spot_divergence_factor": cfg.market_cap.spot_divergence_factor,
            "spot_divergence_against": cfg.market_cap.spot_divergence_against,
        },
        "override_expiry_days": cfg.override_expiry_days,
        "daily_change": {
            "included": prices is not None,
            "reference": "the last two closes before each screen date",
            "failed": sorted(failed),
        },
        "counts": {
            "stocks": len(screens),
            "by_status": dict(sorted(by_status.items())),
            "sized": sum(1 for s in screens if s["spot_market_cap"] is not None),
            "near_threshold": sum(1 for s in screens if s["near_threshold"]),
            "lower_confidence_basis": sum(1 for s in screens if s["interest_income"]["confidence"] == "lower"),
            "screened_before_latest_date": sum(1 for s in screens if s["screen_date"] != latest_screen),
        },
    }
    meta["unsized"] = [
        {"ticker": s["ticker"], "name": s["name"], "status": s["status"], "reason": s["reason"]}
        for s in screens
        if s["in_index"] and s["spot_market_cap"] is None
    ]
    meta["counts"]["unsized"] = len(meta["unsized"])
    payloads = {
        "screens.json": {"screens": screens},
        "changes.json": {"changes": changes, "index_events": index_events},
        "meta.json": meta,
    }
    return scrub(payloads, labels)


def write_site(payloads: Mapping[str, Any], out_dir: str | Path) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, payload in payloads.items():
        path = out / name
        path.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
        written.append(path)
    return written

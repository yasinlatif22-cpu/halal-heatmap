from __future__ import annotations

import copy
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from halal_heatmap.config import parse_config
from halal_heatmap.marketcap import MarketCap
from halal_heatmap.screen.engine import FilingRef, ScreenInputs

ROOT = Path(__file__).resolve().parents[1]
SCREEN_DATE = date(2026, 10, 1)
PERIOD_END = date(2026, 6, 30)


@pytest.fixture(scope="session")
def raw_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@pytest.fixture
def raw(raw_config) -> dict:
    return copy.deepcopy(raw_config)


@pytest.fixture
def cfg(raw_config):
    return parse_config(raw_config)


def caps(spot=1000.0, avg_12m=1000.0, avg_36m=1000.0) -> dict[str, MarketCap]:
    def cap(value):
        if value is None:
            return MarketCap(None, note="short history")
        return MarketCap(value, SCREEN_DATE, SCREEN_DATE, 1)

    return {"spot": cap(spot), "avg_12m": cap(avg_12m), "avg_36m": cap(avg_36m)}


def make_inputs(**changes) -> ScreenInputs:
    """A company that passes every screen under the repo config, with market cap 1000."""
    base = dict(
        ticker="TEST",
        cik=1,
        screen_date=SCREEN_DATE,
        sic="3571",
        gics_sub_industry="Technology Hardware, Storage & Peripherals",
        filing=FilingRef("0000000001-26-000001", "10-Q", date(2026, 7, 30), PERIOD_END),
        debt=100.0,
        cash_and_securities=100.0,
        interest_income=1.0,
        revenue=1000.0,
        interest_expense=5.0,
        total_liabilities=400.0,
        interest_expense_reported=True,
        market_caps=caps(),
    )
    base.update(changes)
    return ScreenInputs(**base)


def fact(end, val, filed, *, start=None, form="10-Q", accn=None) -> dict:
    item = {"end": str(end), "val": val, "filed": str(filed), "form": form, "accn": accn or f"acc-{filed}"}
    if start:
        item["start"] = str(start)
    return item


def companyfacts(usd: dict[str, list[dict]] | None = None, shares: dict[str, list[dict]] | None = None) -> dict:
    """Build a companyfacts document. Keys are 'Tag' (us-gaap) or 'taxonomy:Tag'."""
    doc: dict = {"facts": {}}
    for unit, tags in (("USD", usd or {}), ("shares", shares or {})):
        for key, items in tags.items():
            taxonomy, _, name = key.rpartition(":")
            doc["facts"].setdefault(taxonomy or "us-gaap", {}).setdefault(name, {"units": {}})["units"][unit] = items
    return doc


def trading_days(start: date, end: date) -> list[date]:
    days = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days

"""Write a SYNTHETIC export so the web tool can be tried and screenshotted without network access.

Every company here is fictional (tickers SYN001...). The figures come from a seeded random generator, are not
real filings, and say nothing about any real company. The export goes through the real pipeline (fake sources,
the real screening code, store and exporter), so the file formats are exactly those of a live export.

    python scripts/make_demo_data.py --out web/data
"""

from __future__ import annotations

import argparse
import random
from datetime import date, timedelta
from pathlib import Path

from equity_screener.config import load_config
from equity_screener.criteria import load_profiles
from equity_screener.export import build_site, write_site
from equity_screener.runner import run_screen
from equity_screener.sources.prices import PriceHistory
from equity_screener.sources.wikipedia import Constituent
from equity_screener.store import Store

ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 10, 1)
Q2 = "2026-06-30"
SECTORS = {
    "Information Technology": "Software",
    "Health Care": "Biotechnology",
    "Consumer Discretionary": "Specialty Retail",
    "Industrials": "Machinery",
    "Consumer Staples": "Packaged Foods",
    "Energy": "Oil & Gas Exploration",
    "Utilities": "Electric Utilities",
    "Materials": "Chemicals",
}


def fact(end, val, filed, start=None, form="10-Q", accn="q2-26"):
    item = {"end": end, "val": val, "filed": filed, "form": form, "accn": accn}
    if start:
        item["start"] = start
    return item


def ttm(total):
    """Facts whose trailing-twelve-month total is `total` (annual 0.9x, prior YTD 0.4x, current YTD 0.5x -> 1.0x)."""
    return [
        fact("2025-12-31", total * 0.9, "2026-02-10", "2025-01-01", "10-K", "k"),
        fact("2025-06-30", total * 0.4, "2025-07-30", "2025-01-01", accn="q2-25"),
        fact(Q2, total * 0.5, "2026-07-30", "2026-01-01"),
    ]


def document(rng):
    revenue = rng.uniform(2e9, 80e9)
    op_margin = rng.uniform(-0.03, 0.38)
    net_margin = op_margin * rng.uniform(0.5, 0.85)
    assets = revenue * rng.uniform(0.6, 2.2)
    equity = assets * rng.uniform(0.2, 0.6)
    debt = assets * rng.uniform(0.0, 0.3)
    cash = assets * rng.uniform(0.02, 0.25)
    inst = lambda v: [fact(Q2, v, "2026-07-30")]  # noqa: E731
    usd = {
        "Assets": inst(assets),
        "Liabilities": inst(assets - equity),
        "StockholdersEquity": inst(equity),
        "LongTermDebt": inst(debt),
        "CashAndCashEquivalentsAtCarryingValue": inst(cash),
        "Revenues": ttm(revenue),
        "GrossProfit": ttm(revenue * rng.uniform(0.2, 0.75)),
        "OperatingIncomeLoss": ttm(revenue * op_margin),
        "NetIncomeLoss": ttm(revenue * net_margin),
        "InvestmentIncomeInterest": ttm(cash * rng.uniform(0.0, 0.04)),
        "InterestExpense": ttm(max(debt, 1e6) * rng.uniform(0.02, 0.06)),
        "NetCashProvidedByUsedInOperatingActivities": ttm(revenue * (net_margin + rng.uniform(0.0, 0.1))),
        "PaymentsToAcquirePropertyPlantAndEquipment": ttm(revenue * rng.uniform(0.01, 0.1)),
    }
    shares = rng.uniform(2e8, 3e9)
    doc: dict = {"facts": {"us-gaap": {}, "dei": {}}}
    for tag, items in usd.items():
        doc["facts"]["us-gaap"][tag] = {"units": {"USD": items}}
    doc["facts"]["dei"]["EntityCommonStockSharesOutstanding"] = {
        "units": {
            "shares": [fact("2023-07-20", shares, "2023-07-30", accn="q2-23"), fact("2026-07-20", shares, "2026-07-30")]
        }
    }
    return doc, shares, revenue * net_margin


class Filings:
    def __init__(self, docs):
        self.docs = docs

    def company_facts(self, cik):
        return self.docs[cik]

    def submissions(self, cik):
        return {"sic": "7372"}


class Prices:
    def __init__(self, closes):
        self.closes = closes

    def history(self, ticker, start, end):
        days, day = [], start
        while day <= end:
            if day.weekday() < 5:
                days.append((day, self.closes[ticker]))
            day += timedelta(days=1)
        return PriceHistory(days, [])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "web" / "data"))
    parser.add_argument("--count", type=int, default=80)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    cfg = load_config(ROOT / "config.yaml")
    docs, closes, constituents = {}, {}, []
    sectors = list(SECTORS)
    for i in range(args.count):
        cik, ticker = 9_000_000 + i, f"SYN{i + 1:03d}"
        doc, shares, earnings = document(rng)
        pe = rng.uniform(8, 45)  # price/earnings target; loss-makers get an arbitrary price
        cap = earnings * pe if earnings > 0 else rng.uniform(5e9, 40e9)
        docs[cik], closes[ticker] = doc, cap / shares
        sector = sectors[i % len(sectors)]
        constituents.append(Constituent(ticker, f"Synthetic Company {i + 1:03d}", sector, SECTORS[sector], cik))
    store = Store(":memory:")
    store.save_constituents(AS_OF.isoformat(), constituents)
    run_screen(AS_OF, cfg, constituents, Filings(docs), Prices(closes), {}, store)
    payloads = build_site(store, cfg, {}, None, load_profiles(ROOT / "profiles"))
    payloads["meta.json"]["disclaimer"] = (
        "SYNTHETIC DEMO DATA: fictional companies and random figures from scripts/make_demo_data.py. "
        "Nothing here describes a real company."
    )
    for path in write_site(payloads, args.out):
        print(path)
    store.close()


if __name__ == "__main__":
    main()

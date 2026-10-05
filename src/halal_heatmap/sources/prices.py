"""Daily closes and split history. yfinance sits behind PriceSource so it can be swapped."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

from halal_heatmap.sources import SourceError


@dataclass(frozen=True)
class PriceHistory:
    closes: list[tuple[date, float]]  # split-adjusted, not dividend-adjusted
    splits: list[tuple[date, float]]  # full history: (ex-date, new shares per old share)


class PriceSource(Protocol):
    def history(self, ticker: str, start: date, end: date) -> PriceHistory: ...


class YFinancePrices:
    def history(self, ticker: str, start: date, end: date) -> PriceHistory:
        import yfinance as yf

        symbol = ticker.replace(".", "-")
        try:
            handle = yf.Ticker(symbol)
            frame = handle.history(
                start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(), auto_adjust=False, actions=False
            )
            split_series = handle.splits
        except Exception as exc:  # yfinance raises a wide range of errors
            raise SourceError(f"price fetch failed for {symbol}: {exc}") from None
        if frame is None or frame.empty:
            raise SourceError(f"no prices returned for {symbol}")
        closes = [(stamp.date(), float(close)) for stamp, close in frame["Close"].items() if close == close]
        splits = [(stamp.date(), float(ratio)) for stamp, ratio in split_series.items() if ratio and ratio == ratio]
        return PriceHistory(closes, splits)

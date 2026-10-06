"""Daily closes and split history. yfinance sits behind PriceSource so it can be swapped."""

from __future__ import annotations

import time
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
    """yfinance is unofficial and throttles, so a failed fetch is retried with doubling backoff
    (`prices` in config.yaml). After the last attempt the SourceError is raised, and the caller
    treats the company's prices as missing rather than guessing."""

    def __init__(self, max_attempts: int, backoff_seconds: float, *, sleep=time.sleep):
        self._max_attempts = max_attempts
        self._backoff = backoff_seconds
        self._sleep = sleep

    def history(self, ticker: str, start: date, end: date) -> PriceHistory:
        error: SourceError | None = None
        for attempt in range(self._max_attempts):
            if attempt:
                self._sleep(self._backoff * 2 ** (attempt - 1))
            try:
                return self._fetch(ticker, start, end)
            except SourceError as exc:
                error = exc
        assert error is not None  # max_attempts is at least 1 (config)
        raise error

    def _fetch(self, ticker: str, start: date, end: date) -> PriceHistory:
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

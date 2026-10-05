"""Manual review decisions from overrides.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from halal_heatmap.config import ConfigError

DECISIONS = ("pass", "fail")
REQUIRED_FIELDS = ("ticker", "decision", "reason", "reviewer", "date")


@dataclass(frozen=True)
class Override:
    ticker: str
    decision: str
    reason: str
    reviewer: str
    date: date
    additional_impure_income: float = 0

    @property
    def id(self) -> str:
        return f"{self.ticker}@{self.date.isoformat()}"

    def expires_on(self, expiry_days: int) -> date:
        return self.date + timedelta(days=expiry_days)


def parse_overrides(raw: Any) -> dict[str, Override]:
    entries = (raw or {}).get("overrides") or []
    out: dict[str, Override] = {}
    for index, entry in enumerate(entries):
        where = f"overrides[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where}: must be a mapping")
        for field in REQUIRED_FIELDS:
            value = entry.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise ConfigError(f"{where}: missing required field '{field}'")
        if entry["decision"] not in DECISIONS:
            raise ConfigError(f"{where}: decision must be one of {list(DECISIONS)}")
        when = entry["date"]
        if not isinstance(when, date):
            try:
                when = date.fromisoformat(str(when))
            except ValueError:
                raise ConfigError(f"{where}: date must be YYYY-MM-DD") from None
        extra = entry.get("additional_impure_income") or 0
        if isinstance(extra, bool) or not isinstance(extra, (int, float)) or extra < 0:
            raise ConfigError(f"{where}: additional_impure_income must be a non-negative number")
        override = Override(
            ticker=str(entry["ticker"]).upper(),
            decision=entry["decision"],
            reason=str(entry["reason"]).strip(),
            reviewer=str(entry["reviewer"]).strip(),
            date=when,
            additional_impure_income=extra,
        )
        current = out.get(override.ticker)
        if current is None or override.date > current.date:
            out[override.ticker] = override
    return out


def load_overrides(path: str | Path) -> dict[str, Override]:
    path = Path(path)
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as handle:
        return parse_overrides(yaml.safe_load(handle))

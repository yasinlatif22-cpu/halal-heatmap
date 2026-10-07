"""S&P 500 constituents from the Wikipedia list."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

import requests

from halal_heatmap.config import Constituents
from halal_heatmap.sources import SourceError

COLUMNS = {
    "ticker": "symbol",
    "name": "security",
    "gics_sector": "gics sector",
    "gics_sub_industry": "gics sub-industry",
    "cik": "cik",
}


# Stray text the Wikipedia table carries around a name: a pipe left after a link (ResMed), footnote markers such as
# [a], and zero-width characters. Nothing here is part of a company name.
_STRAY = re.compile(r"[|\u200b\u200c\u200d\ufeff]|\[[^\]]*\]")


def clean_name(text: str) -> str:
    """A company name as it should be shown: the stray text removed, the spaces collapsed."""
    return " ".join(_STRAY.sub("", text).split())


@dataclass(frozen=True)
class Constituent:
    ticker: str
    name: str
    gics_sector: str
    gics_sub_industry: str
    cik: int


class _TableParser(HTMLParser):
    def __init__(self, table_id: str):
        super().__init__()
        self._table_id = table_id
        self._depth = 0
        self._cell: list[str] | None = None
        self._row: list[str] | None = None
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            if self._depth or dict(attrs).get("id") == self._table_id:
                self._depth += 1
        elif self._depth == 1:
            if tag == "tr":
                self._row = []
            elif tag in ("td", "th") and self._row is not None:
                self._cell = []

    def handle_endtag(self, tag):
        if tag == "table" and self._depth:
            self._depth -= 1
        elif self._depth == 1:
            if tag in ("td", "th") and self._cell is not None and self._row is not None:
                self._row.append(" ".join("".join(self._cell).split()))
                self._cell = None
            elif tag == "tr" and self._row is not None:
                if self._row:
                    self.rows.append(self._row)
                self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def parse_constituents(html: str, cfg: Constituents) -> list[Constituent]:
    parser = _TableParser(cfg.table_id)
    parser.feed(html)
    if not parser.rows:
        raise SourceError(f"constituents table '{cfg.table_id}' not found")
    header = [cell.lower() for cell in parser.rows[0]]
    try:
        index = {field: header.index(label) for field, label in COLUMNS.items()}
    except ValueError as exc:
        raise SourceError(f"constituents table is missing a column: {exc}") from None
    out = []
    for row in parser.rows[1:]:
        if len(row) <= max(index.values()):
            raise SourceError(f"constituents row has too few cells: {row}")
        cik = row[index["cik"]]
        if not cik.isdigit():
            raise SourceError(f"constituents row has a non-numeric CIK: {row}")
        out.append(
            Constituent(
                ticker=row[index["ticker"]].upper(),
                name=clean_name(row[index["name"]]),
                gics_sector=row[index["gics_sector"]],
                gics_sub_industry=row[index["gics_sub_industry"]],
                cik=int(cik),
            )
        )
    if not cfg.min_count <= len(out) <= cfg.max_count:
        raise SourceError(f"expected {cfg.min_count} to {cfg.max_count} constituents, parsed {len(out)}")
    if len({c.ticker for c in out}) != len(out):
        raise SourceError("constituents table has duplicate tickers")
    return out


def fetch_constituents(cfg: Constituents, session=None) -> list[Constituent]:
    try:
        response = (session or requests).get(cfg.url, headers={"User-Agent": cfg.user_agent}, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise SourceError(f"could not fetch constituents: {exc}") from None
    return parse_constituents(response.text, cfg)

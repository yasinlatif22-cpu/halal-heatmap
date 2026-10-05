"""SEC EDGAR client: company facts and submissions, rate limited and cached on disk."""

from __future__ import annotations

import gzip
import html
import json
import os
import re
import threading
import time
from pathlib import Path

import requests

from halal_heatmap.config import ConfigError, Edgar
from halal_heatmap.sources import SourceError
from halal_heatmap.xbrl import InstanceError, parse_instance

USER_AGENT_ENV = "SEC_USER_AGENT"
SEC_MAX_REQUESTS_PER_SECOND = 10
BASE_URL = "https://data.sec.gov"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data"
LINKBASE_SUFFIXES = ("_cal.xml", "_def.xml", "_lab.xml", "_pre.xml")
NOT_INSTANCES = ("filingsummary.xml", "metalinks.xml")
RETRY_STATUSES = {429, 500, 502, 503, 504}


def user_agent_from_env() -> str:
    value = os.environ.get(USER_AGENT_ENV, "").strip()
    if not value:
        raise ConfigError(f"{USER_AGENT_ENV} is not set; the SEC requires a User-Agent identifying you")
    return value


class RateLimiter:
    def __init__(self, max_per_second: float, clock=time.monotonic, sleep=time.sleep):
        if not 0 < max_per_second <= SEC_MAX_REQUESTS_PER_SECOND:
            raise ConfigError(
                f"edgar.max_requests_per_second must be in (0, {SEC_MAX_REQUESTS_PER_SECOND}], got {max_per_second}"
            )
        self._interval = 1 / max_per_second
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            if now < self._next:
                self._sleep(self._next - now)
                now = self._next
            self._next = now + self._interval


def instance_name(index: dict) -> str | None:
    """Pick the XBRL instance out of a filing folder listing."""
    names = [str(item.get("name", "")) for item in (index.get("directory") or {}).get("item") or []]
    inline = [n for n in names if n.lower().endswith("_htm.xml")]
    if inline:
        return inline[0]
    plain = [
        n
        for n in names
        if n.lower().endswith(".xml") and not n.lower().endswith(LINKBASE_SUFFIXES) and n.lower() not in NOT_INSTANCES
    ]
    return plain[0] if plain else None


class EdgarClient:
    def __init__(self, cfg: Edgar, *, session=None, limiter: RateLimiter | None = None, sleep=time.sleep):
        self._cfg = cfg
        self._user_agent = user_agent_from_env()
        self._session = session or requests.Session()
        self._limiter = limiter or RateLimiter(cfg.max_requests_per_second)
        self._sleep = sleep
        self._cache_dir = Path(cfg.cache_dir)

    def company_facts(self, cik: int) -> dict:
        return self._get_json(f"/api/xbrl/companyfacts/CIK{cik:010d}.json", f"facts-{cik:010d}")

    def submissions(self, cik: int) -> dict:
        return self._get_json(f"/submissions/CIK{cik:010d}.json", f"submissions-{cik:010d}")

    def _get_json(self, path: str, cache_key: str) -> dict:
        cached = self._cache_dir / f"{cache_key}.json"
        if cached.exists() and time.time() - cached.stat().st_mtime < self._cfg.cache_ttl_hours * 3600:
            with open(cached, encoding="utf-8") as handle:
                return json.load(handle)
        data = self._fetch(BASE_URL + path).json()
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        with open(cached, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        return data

    def filing_instance(self, cik: int, accession: str, primary_document: str) -> list[dict]:
        """Every fact in one filing's XBRL instance, dimensions included. Filings never change, so
        the parsed result is cached without expiry."""
        cached = self._cache_dir / "instances" / f"{accession}.json.gz"
        if cached.exists():
            with gzip.open(cached, "rt", encoding="utf-8") as handle:
                return json.load(handle)
        folder = f"{ARCHIVE_URL}/{cik}/{accession.replace('-', '')}"
        content = None
        stem, _, extension = primary_document.rpartition(".")
        if stem and extension.lower() in ("htm", "html"):
            try:  # where EDGAR puts the instance extracted from an inline XBRL filing
                content = self._fetch(f"{folder}/{stem}_htm.xml").content
            except SourceError:
                pass
        if content is None:
            name = instance_name(self._fetch(f"{folder}/index.json").json())
            if name is None:
                raise SourceError(f"no XBRL instance document in filing {accession}")
            content = self._fetch(f"{folder}/{name}").content
        try:
            facts = parse_instance(content)
        except InstanceError as exc:
            raise SourceError(f"filing {accession}: {exc}") from None
        cached.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(cached, "wt", encoding="utf-8") as handle:
            json.dump(facts, handle)
        return facts

    def filing_text(self, cik: int, accession: str, primary_document: str) -> str:
        """The plain text of a filing's main document. Cached without expiry."""
        cached = self._cache_dir / "text" / f"{accession}.txt.gz"
        if cached.exists():
            with gzip.open(cached, "rt", encoding="utf-8") as handle:
                return handle.read()
        url = f"{ARCHIVE_URL}/{cik}/{accession.replace('-', '')}/{primary_document}"
        raw = self._fetch(url).content.decode("utf-8", "replace")
        text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw))).replace("\u2019", "'").strip()
        cached.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(cached, "wt", encoding="utf-8") as handle:
            handle.write(text)
        return text

    def _fetch(self, url: str):
        headers = {"User-Agent": self._user_agent, "Accept-Encoding": "gzip, deflate"}
        last = ""
        for attempt in range(self._cfg.max_retries + 1):
            if attempt:
                self._sleep(self._cfg.backoff_seconds * 2 ** (attempt - 1))
            self._limiter.wait()
            try:
                response = self._session.get(url, headers=headers, timeout=self._cfg.timeout_seconds)
            except requests.RequestException as exc:
                last = str(exc)
                continue
            if response.status_code == 200:
                return response
            last = f"HTTP {response.status_code}"
            if response.status_code not in RETRY_STATUSES:
                break
        raise SourceError(f"EDGAR request failed for {url}: {last}")

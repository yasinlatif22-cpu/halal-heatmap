"""SQLite storage. Rows are only ever inserted, so every past verdict stays auditable."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from halal_heatmap.config import DENOMINATORS, RATIOS
from halal_heatmap.facts import UsedFact

RESULT_COLUMNS: dict[str, str] = {
    "ticker": "TEXT NOT NULL",
    "cik": "INTEGER",
    "screen_date": "TEXT NOT NULL",
    "trigger": "TEXT NOT NULL",
    "filing_accession": "TEXT",
    "filing_form": "TEXT",
    "filing_date": "TEXT",
    "period_end": "TEXT",
    "sic": "TEXT",
    "gics_sub_industry": "TEXT",
    "debt": "REAL",
    "debt_assumed_zero": "INTEGER NOT NULL",
    "debt_note": "TEXT",
    "debt_implied_rate": "REAL",
    "debt_implausible": "INTEGER NOT NULL",
    "debt_check_note": "TEXT",
    "cash_and_securities": "REAL",
    "investments_assumed_zero": "INTEGER NOT NULL",
    "cash_note": "TEXT",
    "share_source": "TEXT",
    "share_classes": "TEXT",
    "unlisted_class_share": "REAL",
    "interest_income": "REAL",
    "interest_income_basis": "TEXT",
    "interest_income_source": "TEXT",
    "interest_income_upper_bound": "REAL",
    "interest_income_bound_base": "TEXT",
    "total_assets": "REAL",
    "financing_receivables": "REAL",
    "financing_receivables_share": "REAL",
    "spot_divergence": "REAL",
    "spot_diverges": "INTEGER",
    "stale_balance_sheet": "INTEGER",
    "post_balance_sheet_event": "TEXT",
    "share_counts_rejected": "INTEGER",
    "share_count_jump": "INTEGER",
    "share_count_note": "TEXT",
    "latest_filing_date": "TEXT",
    "latest_filing_accessions": "TEXT",
    "source_failed": "INTEGER",
    "interest_income_yield_ceiling": "REAL",
    "interest_income_tags": "TEXT",
    "interest_income_dimensional": "INTEGER",
    "interest_income_annual": "INTEGER",
    "interest_income_max_ratio": "REAL",
    "interest_income_period_end": "TEXT",
    "interest_income_filing_date": "TEXT",
    "interest_income_accession": "TEXT",
    "override_impure_income": "REAL",
    "impure_income": "REAL",
    "revenue": "REAL",
    "interest_expense": "REAL",
    "total_liabilities": "REAL",
    "impure_ratio": "REAL",
    "driving_denominator": "TEXT NOT NULL",
    "denominator_disagreement": "INTEGER NOT NULL",
    "near_threshold": "INTEGER NOT NULL",
    "business_result": "TEXT NOT NULL",
    "business_category": "TEXT",
    "business_rule": "TEXT",
    "override_id": "TEXT",
    "override_state": "TEXT NOT NULL",
    "status": "TEXT NOT NULL",
    "reason": "TEXT NOT NULL",
    "input_notes": "TEXT",
    "config_hash": "TEXT NOT NULL",
}
for _name in DENOMINATORS:
    RESULT_COLUMNS.update(
        {
            f"mcap_{_name}": "REAL",
            f"mcap_{_name}_start": "TEXT",
            f"mcap_{_name}_end": "TEXT",
            f"mcap_{_name}_obs": "INTEGER",
            f"debt_ratio_{_name}": "REAL",
            f"cash_ratio_{_name}": "REAL",
            f"status_{_name}": "TEXT",
        }
    )
for _name in RATIOS:
    RESULT_COLUMNS.update(
        {
            f"{_name}_threshold": "REAL NOT NULL",
            f"{_name}_operator": "TEXT NOT NULL",
            f"{_name}_headroom": "REAL",
            f"{_name}_headroom_rel": "REAL",
            f"{_name}_near": "INTEGER",
        }
    )

# A change of status between a result and the valid result before it. crossings and factors are JSON.
CHANGE_COLUMNS: dict[str, str] = {
    "ticker": "TEXT NOT NULL",
    "cik": "INTEGER",
    "screen_date": "TEXT NOT NULL",
    "previous_screen_date": "TEXT NOT NULL",
    "old_status": "TEXT NOT NULL",
    "new_status": "TEXT NOT NULL",
    "old_reason": "TEXT NOT NULL",
    "new_reason": "TEXT NOT NULL",
    "cause": "TEXT NOT NULL",
    "cause_detail": "TEXT NOT NULL",
    "factors": "TEXT NOT NULL",
    "crossings": "TEXT NOT NULL",
}
FULL = "full"
PARTIAL = "partial"
ADDED = "added"
REMOVED = "removed"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    screen_date TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status_counts TEXT,
    errors TEXT,
    superseded INTEGER NOT NULL DEFAULT 0,
    superseded_reason TEXT,
    trigger TEXT,
    scope TEXT
);
CREATE TABLE IF NOT EXISTS constituents_snapshot (
    snapshot_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    cik INTEGER,
    name TEXT,
    gics_sector TEXT,
    gics_sub_industry TEXT,
    PRIMARY KEY (snapshot_date, ticker)
);
CREATE TABLE IF NOT EXISTS screen_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    created_at TEXT NOT NULL,
    {", ".join(f"{name} {kind}" for name, kind in RESULT_COLUMNS.items())}
);
CREATE INDEX IF NOT EXISTS idx_results_ticker_date ON screen_results (ticker, screen_date);
CREATE TABLE IF NOT EXISTS input_facts (
    result_id INTEGER NOT NULL REFERENCES screen_results(id),
    input TEXT NOT NULL,
    component TEXT NOT NULL,
    role TEXT NOT NULL,
    tag TEXT NOT NULL,
    unit TEXT NOT NULL,
    value REAL NOT NULL,
    period_start TEXT,
    period_end TEXT NOT NULL,
    accession TEXT NOT NULL,
    form TEXT NOT NULL,
    filed TEXT NOT NULL,
    dimensions TEXT
);
CREATE INDEX IF NOT EXISTS idx_facts_result ON input_facts (result_id);
CREATE TABLE IF NOT EXISTS status_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    result_id INTEGER NOT NULL REFERENCES screen_results(id),
    previous_result_id INTEGER NOT NULL REFERENCES screen_results(id),
    created_at TEXT NOT NULL,
    {", ".join(f"{name} {kind}" for name, kind in CHANGE_COLUMNS.items())}
);
CREATE INDEX IF NOT EXISTS idx_changes_ticker ON status_changes (ticker, screen_date);
CREATE TABLE IF NOT EXISTS index_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date TEXT NOT NULL,
    previous_snapshot_date TEXT NOT NULL,
    kind TEXT NOT NULL,
    ticker TEXT NOT NULL,
    cik INTEGER,
    name TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (event_date, ticker, kind)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._add_missing_columns("screen_results", RESULT_COLUMNS)
        self._add_missing_columns("input_facts", {"dimensions": "TEXT"})
        run_columns = {"superseded": "INTEGER NOT NULL DEFAULT 0", "superseded_reason": "TEXT"}
        self._add_missing_columns("runs", {**run_columns, "trigger": "TEXT", "scope": "TEXT"})

    def _add_missing_columns(self, table: str, columns: dict[str, str]) -> None:
        """Bring a database made by an earlier version up to date. Old rows keep NULL there."""
        existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns.items():
            if name not in existing:
                kind = kind if "DEFAULT" in kind else kind.replace(" NOT NULL", "")
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def start_run(self, screen_date: str, config_hash: str, trigger: str = "manual", scope: str = PARTIAL) -> int:
        cursor = self.conn.execute(
            "INSERT INTO runs (screen_date, config_hash, started_at, trigger, scope) VALUES (?, ?, ?, ?, ?)",
            (screen_date, config_hash, _now(), trigger, scope),
        )
        self.conn.commit()
        return cursor.lastrowid

    def finish_run(self, run_id: int, status_counts: dict[str, int], errors: list[str]) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, status_counts = ?, errors = ? WHERE id = ?",
            (_now(), json.dumps(status_counts, sort_keys=True), json.dumps(errors), run_id),
        )
        self.conn.commit()

    def supersede_run(self, run_id: int, reason: str) -> None:
        """Mark a run as invalid. Its rows stay for the record but nothing reads them as current."""
        if not reason.strip():
            raise ValueError("a superseded run needs a reason")
        cursor = self.conn.execute(
            "UPDATE runs SET superseded = 1, superseded_reason = ? WHERE id = ?", (reason.strip(), run_id)
        )
        if cursor.rowcount != 1:
            raise ValueError(f"no run with id {run_id}")
        self.conn.commit()

    def valid_run_ids(self) -> list[int]:
        return [row["id"] for row in self.conn.execute("SELECT id FROM runs WHERE superseded = 0 ORDER BY id")]

    def save_constituents(self, snapshot_date: str, constituents: list) -> None:
        self.conn.executemany(
            "INSERT OR IGNORE INTO constituents_snapshot VALUES (?, ?, ?, ?, ?, ?)",
            [(snapshot_date, c.ticker, c.cik, c.name, c.gics_sector, c.gics_sub_industry) for c in constituents],
        )
        self.conn.commit()

    def save_result(self, run_id: int, record: dict, used: list[UsedFact]) -> int:
        if set(record) != set(RESULT_COLUMNS):
            raise ValueError(f"audit record does not match schema: {sorted(set(record) ^ set(RESULT_COLUMNS))}")
        names = list(RESULT_COLUMNS)
        cursor = self.conn.execute(
            f"INSERT INTO screen_results (run_id, created_at, {', '.join(names)}) "
            f"VALUES (?, ?, {', '.join('?' for _ in names)})",
            [run_id, _now(), *(record[name] for name in names)],
        )
        result_id = cursor.lastrowid
        self.conn.executemany(
            "INSERT INTO input_facts (result_id, input, component, role, tag, unit, value, period_start, "
            "period_end, accession, form, filed, dimensions) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    result_id,
                    u.input,
                    u.component,
                    u.role,
                    str(u.fact.tag),
                    u.fact.unit,
                    u.fact.value,
                    u.fact.start.isoformat() if u.fact.start else None,
                    u.fact.end.isoformat(),
                    u.fact.accession,
                    u.fact.form,
                    u.fact.filed.isoformat(),
                    u.fact.dims or None,
                )
                for u in used
            ],
        )
        self.conn.commit()
        return result_id

    def latest_result(self, ticker: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT r.* FROM screen_results r JOIN runs ON runs.id = r.run_id "
            "WHERE r.ticker = ? AND runs.superseded = 0 ORDER BY r.screen_date DESC, r.id DESC LIMIT 1",
            (ticker,),
        ).fetchone()

    def result_before(self, ticker: str, result_id: int) -> sqlite3.Row | None:
        """The valid result a later one is compared with. History only moves forward, so a lower
        id is an earlier screen."""
        return self.conn.execute(
            "SELECT r.* FROM screen_results r JOIN runs ON runs.id = r.run_id "
            "WHERE r.ticker = ? AND runs.superseded = 0 AND r.id < ? ORDER BY r.id DESC LIMIT 1",
            (ticker, result_id),
        ).fetchone()

    def result_after(self, ticker: str, result_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT r.* FROM screen_results r JOIN runs ON runs.id = r.run_id "
            "WHERE r.ticker = ? AND runs.superseded = 0 AND r.id > ? ORDER BY r.id LIMIT 1",
            (ticker, result_id),
        ).fetchone()

    def results_of_run(self, run_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM screen_results WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()

    def latest_screen_date(self) -> str | None:
        """The latest screen date of any valid result."""
        return self.conn.execute(
            "SELECT MAX(r.screen_date) FROM screen_results r JOIN runs ON runs.id = r.run_id WHERE runs.superseded = 0"
        ).fetchone()[0]

    def has_full_run_since(self, screen_date: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM runs WHERE superseded = 0 AND scope = ? AND finished_at IS NOT NULL AND screen_date >= ? "
            "LIMIT 1",
            (FULL, screen_date),
        ).fetchone()
        return row is not None

    def save_change(self, run_id: int, result_id: int, previous_result_id: int, change: dict) -> int:
        if set(change) != set(CHANGE_COLUMNS):
            raise ValueError(f"status change does not match schema: {sorted(set(change) ^ set(CHANGE_COLUMNS))}")
        names = list(CHANGE_COLUMNS)
        cursor = self.conn.execute(
            f"INSERT INTO status_changes (run_id, result_id, previous_result_id, created_at, {', '.join(names)}) "
            f"VALUES (?, ?, ?, ?, {', '.join('?' for _ in names)})",
            [run_id, result_id, previous_result_id, _now(), *(change[name] for name in names)],
        )
        self.conn.commit()
        return cursor.lastrowid

    def status_changes(self, ticker: str | None = None, since: str | None = None) -> list[sqlite3.Row]:
        """Changes between two valid results. A change measured from or to a superseded run stays
        in the table for the record and is never returned."""
        return self.conn.execute(
            "SELECT c.* FROM status_changes c "
            "JOIN screen_results new ON new.id = c.result_id JOIN runs new_run ON new_run.id = new.run_id "
            "JOIN screen_results old ON old.id = c.previous_result_id JOIN runs old_run ON old_run.id = old.run_id "
            "WHERE new_run.superseded = 0 AND old_run.superseded = 0 "
            "AND (? IS NULL OR c.ticker = ?) AND (? IS NULL OR c.screen_date >= ?) ORDER BY c.result_id, c.id",
            (ticker, ticker, since, since),
        ).fetchall()

    def snapshot_before(self, snapshot_date: str) -> tuple[str, dict[str, sqlite3.Row]] | None:
        """The latest constituent list saved before the given date."""
        previous = self.conn.execute(
            "SELECT MAX(snapshot_date) FROM constituents_snapshot WHERE snapshot_date < ?", (snapshot_date,)
        ).fetchone()[0]
        if previous is None:
            return None
        rows = self.conn.execute("SELECT * FROM constituents_snapshot WHERE snapshot_date = ?", (previous,))
        return previous, {row["ticker"]: row for row in rows}

    def save_index_events(self, event_date: str, previous_date: str, events: list[tuple]) -> None:
        """`events` holds (kind, ticker, cik, name). An event already stored for the date is kept."""
        self.conn.executemany(
            "INSERT OR IGNORE INTO index_events (event_date, previous_snapshot_date, kind, ticker, cik, name, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(event_date, previous_date, kind, ticker, cik, name, _now()) for kind, ticker, cik, name in events],
        )
        self.conn.commit()

    def index_events(self, event_date: str | None = None) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM index_events WHERE (? IS NULL OR event_date = ?) ORDER BY event_date, kind, ticker",
            (event_date, event_date),
        ).fetchall()

    def facts_for(self, result_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM input_facts WHERE result_id = ?", (result_id,)).fetchall()

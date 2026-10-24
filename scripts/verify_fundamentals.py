"""Check, against live EDGAR data, which general metrics resolve for a spread of company types.

Run on a machine with network access (EDGAR, Wikipedia, Yahoo) and a contact user_agent set in config.yaml:

    python scripts/verify_fundamentals.py --out fundamentals_coverage.csv
    python scripts/verify_fundamentals.py AAPL JPM O NEE --out coverage.csv

It screens each ticker without storing anything, then prints, per metric, how many tickers resolved it and which did
not. A metric that is unavailable for a whole company type (for example interest coverage for a bank) is expected;
a metric unavailable for an ordinary industrial is a gap in the tag lists under `fundamentals:` in config.yaml.
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys

from equity_screener.metrics import METRIC_KEYS, compute_metrics, figures_from_row

# One or two of each kind of filer, so tag gaps show up by company type.
DEFAULT_TICKERS = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN",  # large technology and platforms
    "JNJ", "PFE", "UNH",  # health care
    "KO", "PG", "WMT",  # staples
    "XOM", "CVX",  # energy
    "CAT", "UPS", "HON",  # industrials
    "NEE", "DUK",  # utilities
    "O", "PLD",  # REITs
    "JPM", "BAC", "BRK.B",  # financials
    "TSLA", "HD", "MCD",  # discretionary
]


def coverage(records: list[dict]) -> dict:
    """metric -> {'resolved': [tickers], 'missing': [tickers]}"""
    out = {key: {"resolved": [], "missing": []} for key in METRIC_KEYS}
    for record in records:
        metrics = compute_metrics(figures_from_row(record))
        for key, value in metrics.items():
            out[key]["resolved" if value is not None else "missing"].append(record["ticker"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*", default=DEFAULT_TICKERS)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--out", default="fundamentals_coverage.csv")
    args = ap.parse_args()
    proc = subprocess.run(
        [sys.executable, "-m", "equity_screener", "screen", *args.tickers, "--no-store", "--quiet", "--json",
         "--config", args.config],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(proc.stderr or proc.stdout, file=sys.stderr)
        return proc.returncode
    try:
        records = json.loads(proc.stdout)  # `screen --json` prints one JSON array; progress goes to stderr
    except json.JSONDecodeError:
        records = []
    if not records:
        print("no audit records came back; run `equity-screener screen AAPL --json --no-store` to see why",
              file=sys.stderr)
        return 1
    result = coverage(records)
    with open(args.out, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "resolved", "of", "missing_tickers"])
        for key, cell in result.items():
            writer.writerow([key, len(cell["resolved"]), len(records), " ".join(cell["missing"])])
    for key, cell in result.items():
        print(f"{key:28s} {len(cell['resolved']):3d}/{len(records)}  missing: {' '.join(cell['missing']) or '-'}")
    print(f"\nwritten to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

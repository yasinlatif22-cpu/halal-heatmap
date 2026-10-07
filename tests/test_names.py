"""The names the site shows are plain company names. They are checked in the exported data, so a stray character
that gets past the parser fails here. Skipped when the export has not been generated (CI has no export)."""

import json
import re
from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parents[1] / "web" / "data"
# Letters, digits, spaces and the punctuation that real names use: . , & ' ( ) - – / ! +
NAME_OK = re.compile(r"^[\w .,&'()\-–/!+]+$")


def _load(name):
    path = DATA / name
    if not path.exists():
        pytest.skip(f"no exported data: {name} not generated")
    return json.loads(path.read_text(encoding="utf-8"))


def test_every_name_in_the_screens_is_plain_text():
    for row in _load("screens.json")["screens"]:
        assert NAME_OK.match(row["name"]), (row["ticker"], row["name"])
        assert row["name"] == row["name"].strip() and "  " not in row["name"], (row["ticker"], row["name"])


def test_every_name_in_the_ticker_index_is_plain_text():
    for row in _load("ticker_index.json")["stocks"]:
        assert NAME_OK.match(row["name"]), (row["ticker"], row["name"])


def test_the_ticker_index_matches_the_screens_one_for_one():
    screens = [row["ticker"] for row in _load("screens.json")["screens"]]
    index = [row["ticker"] for row in _load("ticker_index.json")["stocks"]]
    assert index == sorted(screens)

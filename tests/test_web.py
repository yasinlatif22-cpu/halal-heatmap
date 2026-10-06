"""The price-change colouring is named for what it measures, and its legend states the close date."""

from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"


def test_colour_option_is_named_last_close_price_change():
    html = (WEB / "index.html").read_text()
    assert "Last-close price change" in html
    assert "Daily price change</label>" not in html


def test_legend_states_the_close_date_it_refers_to():
    js = (WEB / "app.js").read_text()
    assert "Price change on the last close before the screen date" in js
    assert "changeDate()" in js  # the date comes from the exported daily_change.date, not a constant

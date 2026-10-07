"""The landing page states what the tool is not, links into the tool, and keeps to the house style of no dashes."""

from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
LANDING = (WEB / "index.html").read_text()


def test_disclaimer_says_what_the_tool_is_not():
    phrases = (
        "automated screening and research aid",
        "not a religious ruling",
        "not a certification",
        "not investment advice",
        "Consult a qualified scholar",
    )
    for phrase in phrases:
        assert phrase in LANDING, phrase


def test_landing_links_into_the_tool_and_the_tool_exists():
    assert 'href="tool/"' in LANDING
    assert (WEB / "tool" / "index.html").exists()


def test_figures_come_from_the_exported_meta_not_from_the_page():
    assert "data/meta.json" in LANDING
    for key in ("screen_date", "constituents_as_of", "generated_at"):
        assert key in LANDING


def test_no_em_dashes_or_en_dashes_in_the_copy():
    assert "—" not in LANDING
    assert "–" not in LANDING
    assert "&mdash;" not in LANDING and "&ndash;" not in LANDING


def test_worked_example_ticker_is_in_the_export():
    import json

    screens = json.loads((WEB / "data" / "screens.json").read_text())["screens"]
    assert any(row["ticker"] == "AAPL" for row in screens)
    assert 'data/screens.json' in LANDING


def test_hero_shows_the_live_preview_of_the_tool():
    assert 'src="tool/?display=1&amp;embed=1"' in LANDING

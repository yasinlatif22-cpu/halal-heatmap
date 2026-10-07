"""The tool's copy and wiring: the ownership note, the price change named for what it measures, the kiosk and embed
views, the stale warning, and no third-party requests. The tool is dark only, so there is no light theme to test."""

from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
TOOL = WEB / "tool"


def test_ownership_note_is_on_the_page_and_no_logos_are_loaded():
    html = (TOOL / "index.html").read_text()
    assert "Company names and marks belong to their owners. No affiliation is implied." in html
    js = (TOOL / "app.js").read_text()
    assert "<img" not in js  # no logos are loaded, so no third-party request is made
    assert "monogram" not in js  # the letter monogram is gone from the detail panel


def test_the_price_change_is_named_for_what_it_measures():
    html = (TOOL / "index.html").read_text()
    assert "Last-close price change" in html
    assert "Daily price change" not in html
    assert "Daily change" not in (TOOL / "app.js").read_text()


def test_price_change_detail_states_the_close_date_it_refers_to():
    js = (TOOL / "app.js").read_text()
    assert "Last-close price change" in js
    assert "daily_change.date" in js  # the date comes from the export, not a constant


def test_the_price_colour_mode_is_gone_from_the_interface():
    html = (TOOL / "index.html").read_text()
    js = (TOOL / "app.js").read_text()
    for gone in ("colour-change", "legend", "hover-strip", "open-list"):
        assert f'id="{gone}"' not in html and f"'{gone}'" not in js, gone
    for gone in ("changeColour", "changeScale", "priceRGB", "colourMode"):
        assert gone not in js, gone


def test_the_tools_start_hidden_and_the_kiosk_is_the_map_only():
    html = (TOOL / "index.html").read_text()
    js = (TOOL / "app.js").read_text()
    assert '<aside id="drawer"' in html and " inert>" in html
    assert "get('display') === '1'" in js
    assert "get('embed') === '1'" in js
    assert ".kiosk .drawer" in (TOOL / "style.css").read_text()


def test_tool_has_the_stale_warning_and_the_reload_on_a_new_export():
    js = (TOOL / "app.js").read_text()
    assert "const STALE_DAYS = 4;" in js
    assert "location.reload()" in js  # kiosk reloads when meta.json reports a new export


def test_csv_export_and_watchlist_only_use_fields_the_list_shows():
    js = (TOOL / "app.js").read_text()
    assert "halal-heatmap-watchlist" in js
    assert "price_change_fraction" in js
    assert "Pinned tickers stay at the top" in (TOOL / "index.html").read_text()


def test_old_ticker_links_are_redirected_to_the_tool():
    landing = (WEB / "index.html").read_text()
    assert "location.replace('tool/#'" in landing


def test_no_third_party_requests_from_the_tool():
    html = (TOOL / "index.html").read_text()
    css = (TOOL / "style.css").read_text() + (WEB / "fonts.css").read_text()
    for text in (html, css):
        assert "googleapis" not in text and "gstatic" not in text
        assert "cdn.plot" not in text and "cdnjs" not in text
    assert 'src="../vendor/plotly.min.js"' in html


def test_social_tags_name_the_product_and_keep_the_research_aid_wording():
    for name in ("index.html", "tool/index.html"):
        html = (WEB / name).read_text()
        assert 'property="og:title" content="Mirsad' in html, name
        assert 'property="og:type" content="website"' in html, name
        assert 'name="twitter:card" content="summary"' in html, name
        # no og:url or og:image: a crawler would fetch them, and the page makes no third-party requests
        assert 'og:url' not in html and 'og:image' not in html, name
    for name in ("index.html", "tool/index.html"):
        assert "not a fatwa" in (WEB / name).read_text().lower(), name


def test_the_product_name_is_mirsad_everywhere_user_facing():
    for name in ("index.html", "tool/index.html", "tool/app.js", "tool/style.css", "fonts.css"):
        assert "Mizan" not in (WEB / name).read_text(), name
    assert "Mizan" not in (WEB.parent / "README.md").read_text()

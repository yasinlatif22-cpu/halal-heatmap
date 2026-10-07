"""The landing page states what the tool is not, links into the tool, reads only the small exported files, and keeps
to the house style of no dashes. It is plain HTML and must not load anything from another host."""

from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
LANDING = (WEB / "index.html").read_text()


def test_the_notice_says_what_the_tool_is_not_near_the_top():
    head = LANDING[: LANDING.index('<main>')]
    assert "An automated screen, not a fatwa or a certification." in head
    assert "not yet been verified against the AAOIFI primary text" in head
    assert "AAOIFI-compliant" not in LANDING and "halal-certified" not in LANDING.lower()


def test_the_footer_disclaimer_says_the_rest():
    footer = LANDING[LANDING.index('<footer'):]
    for phrase in ("automated research aid", "not a fatwa", "not a religious ruling", "not a certification",
                   "not investment advice", "Consult a qualified scholar"):
        assert phrase in footer, phrase


def test_landing_links_into_the_tool_and_the_tool_exists():
    assert 'href="tool/"' in LANDING
    assert (WEB / "tool" / "index.html").exists()


def test_counts_and_dates_come_from_the_small_meta_file_not_the_screens():
    assert "data/meta.json" in LANDING
    assert "fetch('data/screens.json'" not in LANDING  # 3.7 MB: the landing page never fetches it
    for key in ("screen_date", "constituents_as_of", "generated_at", "cycle_start", "config_hash"):
        assert key in LANDING, key


def test_no_em_dashes_or_en_dashes_in_the_copy():
    assert "—" not in LANDING
    assert "–" not in LANDING
    assert "&mdash;" not in LANDING and "&ndash;" not in LANDING


def test_the_landing_uses_the_mirsad_fonts_and_loads_no_other_host():
    assert '<link rel="stylesheet" href="fonts.css">' in LANDING
    css = (WEB / "landing.css").read_text()
    assert "Mirsad Mono" in css and "Mirsad Condensed" in css
    assert "IBM Plex" not in css and "IBM Plex" not in LANDING
    for text in (LANDING, css):
        assert "googleapis" not in text and "gstatic" not in text and "cdn" not in text.lower()


def test_social_tags_and_the_name():
    for tag in ('property="og:title"', 'property="og:description"', 'property="og:type"', 'name="twitter:card"'):
        assert tag in LANDING, tag
    assert "Mizan" not in LANDING


def test_the_old_hash_redirect_only_takes_ticker_shaped_hashes():
    # Section links such as #method must stay on the landing page; only TICKER-looking hashes go to the tool.
    assert "/^[A-Z][A-Z0-9.-]{0,9}$/" in LANDING
    assert "location.replace('tool/#'" in LANDING

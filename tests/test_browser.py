"""Browser checks for the tool: the tools are hidden until asked for and never move the map, a tile click opens its
stock and keeps the whole map, a sector click leaves the map whole, the command search and the status chips work from
the keyboard, the kiosk has no tools, and the #TICKER and ?sector= links open the tool.

These run against Chrome, installed on this machine, through Playwright. They are skipped when Playwright or Chrome
is missing, or when the export has not been generated, so CI (which has none of these) runs the offline tests only."""

import functools
import http.server
import socketserver
import threading
from pathlib import Path
from urllib.parse import urlparse

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

WEB = Path(__file__).resolve().parents[1] / "web"
WHOLE_MAP_LABELS = 1030  # every constituent on the map, plus the sector and root labels


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output clean
        pass


@pytest.fixture(scope="module")
def site():
    if not (WEB / "data" / "screens.json").exists():
        pytest.skip("no exported data: run halal-heatmap export first")
    handler = functools.partial(_Quiet, directory=str(WEB))
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def page():
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="chrome")
        except Exception as error:  # Chrome not installed
            pytest.skip(f"Chrome is not available: {error}")
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        yield page
        browser.close()


def _open(page, url, whole=True):
    page.goto(url)
    page.wait_for_selector("#treemap .slicetext")
    if whole:
        page.wait_for_function(f"document.querySelectorAll('#treemap .slicetext').length === {WHOLE_MAP_LABELS}")
    else:
        page.wait_for_function("document.getElementById('count').textContent.length > 0")


def _labels(page):
    return page.eval_on_selector_all("#treemap .slicetext", "els => els.length")


def _drawer_open(page):
    return page.evaluate("document.getElementById('drawer').classList.contains('open')")


def _point(page, label, sector=False):
    """A point inside a stock's tile (its surface), or inside a sector's header label. Labels sit on top of the
    surface, so a click on the label itself would hit the surface, not the tile."""
    box = page.evaluate("""([label, sector]) => {
        const slice = [...document.querySelectorAll('#treemap g.slice')].find((g) => {
            const t = g.querySelector('.slicetext');
            return t && (sector ? t.textContent === label : t.textContent.startsWith(label + ' '));
        });
        if (!slice) return null;
        const target = sector ? slice.querySelector('.slicetext') : slice.querySelector('path.surface');
        const rect = target.getBoundingClientRect();
        return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
    }""", [label, sector])
    assert box, f"no tile found for {label}"
    return box


def test_tools_are_hidden_by_default_and_the_map_does_not_move(site, page):
    _open(page, f"{site}/tool/")
    assert not _drawer_open(page)
    before = page.locator("#treemap").bounding_box()
    page.click("#tools-toggle")
    page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
    assert page.locator("#treemap").bounding_box() == before
    page.click("#drawer-close")
    page.wait_for_function("!document.getElementById('drawer').classList.contains('open')")
    assert page.locator("#treemap").bounding_box() == before


def test_leaf_click_opens_the_stock_and_keeps_the_whole_map(site, page):
    _open(page, f"{site}/tool/")
    point = _point(page, "NVDA")
    page.mouse.click(point["x"], point["y"])
    page.wait_for_function("document.querySelector('#detail h2')?.textContent.includes('NVDA')")
    page.wait_for_function(f"document.querySelectorAll('#treemap .slicetext').length === {WHOLE_MAP_LABELS}")
    assert _labels(page) == WHOLE_MAP_LABELS
    assert _drawer_open(page)


def test_sector_click_keeps_the_sector_view_fixed_to_the_whole_map(site, page):
    _open(page, f"{site}/tool/")
    point = _point(page, "Information Technology", sector=True)
    page.mouse.click(point["x"], point["y"])
    page.wait_for_timeout(300)
    assert _labels(page) == WHOLE_MAP_LABELS


def test_hover_names_the_company_on_the_status_line(site, page):
    _open(page, f"{site}/tool/")
    point = _point(page, "AAPL")
    page.mouse.move(point["x"], point["y"])
    page.wait_for_function("document.getElementById('status-hover').textContent.includes('AAPL')")


def test_list_selection_opens_the_detail(site, page):
    _open(page, f"{site}/tool/")
    page.click("#fn-list")
    page.click('#list-body .stock[data-ticker="MSFT"]')
    page.wait_for_function("document.querySelector('#detail h2').textContent.includes('MSFT')")


def test_command_search_opens_a_stock_from_the_keyboard(site, page):
    _open(page, f"{site}/tool/")
    page.fill("#q", "AAPL")
    page.wait_for_selector("#q-results li[aria-selected='true']")
    page.press("#q", "Enter")
    page.wait_for_function("document.querySelector('#detail h2').textContent.includes('AAPL')")
    assert _drawer_open(page)


def test_command_search_arrow_keys_move_the_choice(site, page):
    _open(page, f"{site}/tool/")
    page.fill("#q", "JP")
    page.wait_for_selector("#q-results li")
    page.press("#q", "ArrowDown")
    selected = page.eval_on_selector("#q-results li[aria-selected='true']", "el => el.dataset.ticker")
    page.press("#q", "Enter")
    page.wait_for_function(f"document.querySelector('#detail h2').textContent.includes('{selected}')")


def test_ticker_hash_opens_that_company(site, page):
    _open(page, f"{site}/tool/#AMZN")
    page.wait_for_function("document.querySelector('#detail h2').textContent.includes('AMZN')")


def test_old_root_ticker_link_is_sent_to_the_tool(site, page):
    page.goto(f"{site}/#AMZN")
    page.wait_for_url("**/tool/#AMZN")
    assert page.url.endswith("/tool/#AMZN")


def test_all_sectors_is_the_default_overview(site, page):
    _open(page, f"{site}/tool/")
    page.click("#tools-toggle")
    assert page.input_value("#sector") == "all"
    assert "sector=" not in page.url


def test_sector_link_reveals_the_tools_and_scopes_the_map(site, page):
    _open(page, f"{site}/tool/?sector=Financials", whole=False)
    assert _drawer_open(page)
    assert page.input_value("#sector") == "Financials"
    page.wait_for_function("document.getElementById('count').textContent.includes('in Financials')")
    assert "Information Technology" not in page.eval_on_selector_all(
        "#treemap .slicetext", "els => els.map((e) => e.textContent)")
    assert _labels(page) < WHOLE_MAP_LABELS


def test_choosing_a_sector_writes_the_url_and_All_clears_it(site, page):
    _open(page, f"{site}/tool/")
    page.click("#tools-toggle")
    page.select_option("#sector", "Financials")
    page.wait_for_function("location.search.includes('sector=Financials')")
    page.select_option("#sector", "all")
    page.wait_for_function("!location.search.includes('sector=')")


def test_list_search_narrows_the_list_within_a_sector(site, page):
    _open(page, f"{site}/tool/?sector=Financials", whole=False)
    page.fill("#list-q", "JPM")
    page.wait_for_function("document.getElementById('count').textContent.includes('Showing')")
    assert page.text_content("#list-count") == "1"
    assert "in Financials" in page.text_content("#count")


def test_reset_is_hidden_until_a_filter_is_active(site, page):
    _open(page, f"{site}/tool/")
    assert not page.is_visible("#reset")
    page.click('#counts [data-status="fail"]')
    page.wait_for_function("!document.getElementById('reset').hidden")
    page.click("#reset")
    page.wait_for_function("document.getElementById('reset').hidden")


def test_status_chip_is_a_filter_and_the_legend(site, page):
    _open(page, f"{site}/tool/")
    page.click('#counts [data-status="fail"]')
    page.wait_for_function(
        "document.querySelector('#counts [data-status=\"fail\"]').getAttribute('aria-pressed') === 'false'")
    assert page.is_checked('input[name="status"][value="fail"]') is False
    page.click("#reset")


def test_ticker_hash_works_alongside_a_sector(site, page):
    _open(page, f"{site}/tool/?sector=Financials#AAPL", whole=False)
    page.wait_for_function("document.querySelector('#detail h2')?.textContent.includes('AAPL')")
    assert page.input_value("#sector") == "Financials"


def test_kiosk_has_no_tools_and_tiles_do_not_open_detail(site, page):
    _open(page, f"{site}/tool/?display=1")
    assert page.is_hidden("#drawer")
    assert page.is_hidden("#q")
    point = _point(page, "NVDA")
    page.mouse.click(point["x"], point["y"])
    page.wait_for_timeout(300)
    assert not _drawer_open(page)
    assert page.evaluate("document.querySelector('#detail').textContent.includes('Select a stock')")


def test_the_tool_loads_its_own_fonts_and_nothing_from_a_third_party(site, page):
    hosts = set()
    def record(request):
        hosts.add(urlparse(request.url).hostname)

    page.on("request", record)
    try:
        _open(page, f"{site}/tool/")
        loaded = page.evaluate("""async () => { await document.fonts.ready;
            return [...document.fonts].filter((f) => f.status === 'loaded').map((f) => f.family); }""")
    finally:
        page.remove_listener("request", record)
    assert "Mirsad Mono" in loaded and "Mirsad Condensed" in loaded
    assert hosts == {"127.0.0.1"}, hosts


@pytest.mark.parametrize("typed", ["BRKB", "BRK-B", "BRK.B", "brk.b"])
def test_landing_search_finds_a_share_class_however_it_is_typed(site, page, typed):
    page.goto(f"{site}/")
    page.fill("#lookup-q", typed)
    page.press("#lookup-q", "Enter")
    page.wait_for_url("**/tool/#BRK.B")
    page.wait_for_function("document.querySelector('#detail h2')?.textContent.includes('BRK.B')")


def test_tool_hash_with_a_hyphen_opens_the_dotted_ticker(site, page):
    _open(page, f"{site}/tool/#BRK-B")
    page.wait_for_function("document.querySelector('#detail h2')?.textContent.includes('BRK.B')")


def test_tool_search_finds_a_share_class_without_the_dot(site, page):
    _open(page, f"{site}/tool/")
    page.fill("#q", "BRKB")
    page.wait_for_selector("#q-results li[aria-selected='true']")
    page.press("#q", "Enter")
    page.wait_for_function("document.querySelector('#detail h2').textContent.includes('BRK.B')")


def test_landing_search_finds_a_company_by_name(site, page):
    page.goto(f"{site}/")
    page.fill("#lookup-q", "resmed")
    page.press("#lookup-q", "Enter")
    page.wait_for_url("**/tool/#RMD")


def test_landing_search_says_when_nothing_matches(site, page):
    page.goto(f"{site}/")
    page.fill("#lookup-q", "ZZZZ9")
    page.press("#lookup-q", "Enter")
    page.wait_for_function("document.getElementById('lookup-msg').textContent.includes('No S&P 500 stock')")
    assert "tool/" not in page.url


def test_the_stale_notice_is_hidden_after_a_healthy_run(site, page):
    _open(page, f"{site}/tool/")
    assert "last screened before" not in page.text_content("#banner")


def test_the_stale_notice_shows_the_count_when_stocks_missed_the_cycle(site, page):
    import json as _json

    def stale_meta(route):
        meta = _json.loads((WEB / "data" / "meta.json").read_text())
        meta["counts"]["stale_before_cycle"] = 3
        meta["cycle_start"] = "2026-10-01"
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(meta))

    page.route("**/data/meta.json", stale_meta)
    try:
        _open(page, f"{site}/tool/")
        page.wait_for_function("document.getElementById('banner').textContent.includes('3 stock(s)')")
        assert "since 2026-10-01" not in page.text_content("#banner")
        assert "screen cycle" in page.text_content("#banner")
    finally:
        page.unroute("**/data/meta.json")

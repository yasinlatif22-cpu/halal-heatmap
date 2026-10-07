"""The tool keeps WCAG contrast: its text on its background, each status label on its tile fill, and the outlines that
mark a selected stock or a near-limit one. The values are read from the source files, so a change in one place fails
here. The landing page has its own check, added with its rebuild."""

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web" / "tool"
APP = (WEB / "app.js").read_text()
CSS = (WEB / "style.css").read_text()


def _rgb(hex_value):
    h = hex_value.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _lum(rgb):
    def channel(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ratio(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _css_var(name):
    match = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", CSS)
    assert match, name
    return match.group(1)


def _status_palette():
    """(label, fill, text) for each status, from the STATUS table in app.js."""
    block = APP[APP.index("const STATUS = {"):APP.index("};", APP.index("const STATUS = {"))]
    row = (r"(\w+): \{ label: '[^']*', symbol: '[^']*', "
           r"fill: '(#[0-9a-fA-F]{6})', text: '(#[0-9a-fA-F]{6})' \}")
    rows = re.findall(row, block)
    return {key: (fill, text) for key, fill, text in rows}


def _const(name):
    match = re.search(rf"const {name} = '(#[0-9a-fA-F]{{6}})'", APP)
    assert match, name
    return match.group(1)


PAGE = _css_var("bg")
PANEL = _css_var("panel")
INK = _css_var("ink")
INK_2 = _css_var("ink-2")
INK_3 = _css_var("ink-3")
LINK = _css_var("link")


def test_status_palette_is_read_from_the_tool():
    palette = _status_palette()
    assert set(palette) == {"pass", "needs_review", "fail", "insufficient_data"}


def test_each_status_label_passes_on_its_tile_fill():
    palette = _status_palette()
    for key, (fill, text) in palette.items():
        assert _ratio(_rgb(text), _rgb(fill)) >= 4.5, key


def test_near_review_tile_label_passes_on_its_hatch():
    assert _ratio(_rgb(_const("REVIEW_NEAR_TEXT")), _rgb(_const("REVIEW_NEAR"))) >= 4.5


def test_insufficient_data_label_passes_on_its_dark_hatch():
    assert _ratio(_rgb(_status_palette()["insufficient_data"][1]), _rgb(_const("UNKNOWN_BG"))) >= 4.5


def test_status_words_pass_on_the_page_and_the_panel():
    for var in ("pass-ink", "review-ink", "fail-ink"):
        colour = _rgb(_css_var(var))
        assert _ratio(colour, _rgb(PAGE)) >= 4.5, var
        assert _ratio(colour, _rgb(PANEL)) >= 4.5, var


def test_page_text_passes_on_the_page_and_the_panel():
    for colour in (INK, INK_2, INK_3, LINK):
        assert _ratio(_rgb(colour), _rgb(PAGE)) >= 4.5, colour
        assert _ratio(_rgb(colour), _rgb(PANEL)) >= 4.5, colour


def test_outlines_stand_out_against_the_tile_seams():
    # A selected tile and a near-limit pass tile are both outlined. Non-text contrast needs 3:1 against the seam.
    assert _ratio(_rgb(_const("SELECT_LINE")), _rgb(PAGE)) >= 3.0
    assert _ratio(_rgb("#f5f6f8"), _rgb(PAGE)) >= 3.0

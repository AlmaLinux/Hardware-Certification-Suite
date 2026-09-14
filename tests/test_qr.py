"""The terminal QR for device authorization.

Two things have to hold: the block we print is a faithful rendering of the encoder's module grid
(checked by parsing it back and comparing, so no QR decoder is needed here), and it is shown only
where it will actually scan - a QR half off the top of a small terminal is worse than none.

The vendored encoder (``alma_certify._qrcodegen``) is Project Nayuki's, tested upstream; it is not
re-tested here beyond confirming we drive it and lay out its grid correctly.
"""

import re

from alma_certify.submit import qr

URL = "https://lumina.almalinux.dev/activate/?code=ABCD-1234"

# One rendered cell: ``\033[<fg>;<bg>m▀``. fg 30/37 = top module dark/light, bg 40/47 = bottom.
_CELL = re.compile(r"\033\[(\d+);(\d+)m▀")


def _parse(block: str):
    """The padded module grid the rendered ``block`` encodes (True = dark)."""
    rows = []
    for line in block.split("\n"):
        top, bottom = [], []
        for fg, bg in _CELL.findall(line):
            top.append(fg == "30")
            bottom.append(bg == "40")
        rows.append(top)
        rows.append(bottom)
    return rows


def test_matrix_is_square_and_odd():
    grid = qr.matrix(URL)
    assert grid, "the URL should encode"
    assert all(len(row) == len(grid) for row in grid), "square"
    assert len(grid) % 2 == 1, "every QR version has an odd module count"


def test_render_reproduces_the_encoder_grid():
    """The heart of it: the block we print, parsed back, is exactly the encoder's grid inside a
    quiet zone. If this holds and the encoder is correct, the on-screen code is correct."""
    grid = qr.matrix(URL)
    size = len(grid)
    parsed = _parse(qr.render(URL))

    # Padded to size + 2*QUIET on each axis (the last line's spare bottom half may be trimmed).
    full = size + 2 * qr.QUIET
    assert len(parsed) >= full
    assert all(len(row) == full for row in parsed[:full])
    for y in range(size):
        for x in range(size):
            assert parsed[y + qr.QUIET][x + qr.QUIET] == grid[y][x], (x, y)


def test_the_quiet_zone_is_light():
    """Scanners need the light border. Every module in it must be light (a cell drawn white)."""
    parsed = _parse(qr.render(URL))
    full = len(parsed[0])
    grid_rows = full  # the padded grid is square; parsed may carry one spare half-row
    for y in range(qr.QUIET):  # top quiet rows
        assert not any(parsed[y]), "top quiet zone is light"
    for y in range(grid_rows):  # left and right quiet columns, every row
        row = parsed[y]
        assert not any(row[:qr.QUIET]), "left quiet zone is light"
        assert not any(row[full - qr.QUIET:]), "right quiet zone is light"


def test_dimensions_match_the_rendered_block():
    grid = qr.matrix(URL)
    cols, lines = qr.dimensions(grid)
    block = qr.render(URL)
    rendered = block.split("\n")
    assert len(rendered) == lines
    assert len(_CELL.findall(rendered[0])) == cols


# --- when it is shown ------------------------------------------------------------


def test_no_qr_without_a_terminal():
    assert qr.render_block(URL, isatty=False, columns=200, lines=200, env={}) is None


def test_no_qr_when_colour_is_disabled():
    for env in ({"NO_COLOR": "1"}, {"TERM": "dumb"}, {"ALMA_CERTIFY_NO_QR": "1"}):
        assert qr.render_block(URL, isatty=True, columns=200, lines=200, env=env) is None, env


def test_no_qr_when_the_terminal_is_too_small():
    # Too narrow, then too short: either way the whole code cannot be shown, so it is skipped.
    assert qr.render_block(URL, isatty=True, columns=10, lines=200, env={}) is None
    assert qr.render_block(URL, isatty=True, columns=200, lines=3, env={}) is None


def test_a_qr_is_rendered_when_it_fits_a_real_terminal():
    block = qr.render_block(URL, isatty=True, columns=200, lines=200, env={})
    assert block is not None
    assert "▀" in block
    # And it is the same faithful grid the direct renderer produces.
    assert _parse(block) == _parse(qr.render(URL))

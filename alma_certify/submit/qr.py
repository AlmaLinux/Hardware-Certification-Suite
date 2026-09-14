"""Render a QR code to the terminal, for the device-authorization flow.

An operator authorizing a headless box over SSH has to read a URL and a code off the screen and
type them into a phone. A QR of the approval URL - with the code already in it - turns that into a
scan-and-tap. This draws one from the module grid the vendored encoder (``.._qrcodegen``) produces.

**Additive, never the only path.** The plain URL and code are always printed; the QR is an extra
shown only where it will actually scan. ``render_block`` returns None - and the caller shows nothing
extra - when there is no terminal, color is turned off, the terminal is too small to hold the whole
code, or ``ALMA_CERTIFY_NO_QR`` is set. A QR half off the top of the screen is worse than no QR.

**Theme-independent.** Modules are drawn black-on-white with explicit ANSI color rather than with
the terminal's own foreground/background, because a light-on-dark theme would otherwise invert the
code and many scanners refuse an inverted one. Each character is an upper-half block whose top half
(foreground) is one module and bottom half (background) is the module below it, so two module rows
share a line and the code stays roughly square - a terminal cell is about twice as tall as it is
wide, so one module per cell would come out stretched and a stretched code does not scan.
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import List, Optional

from .. import _qrcodegen as qrcodegen

# The spec's quiet zone. Scanners need this light border to find the code; trimming it to save lines
# is the first thing that stops a terminal QR from scanning.
QUIET = 4

_UPPER_HALF = "▀"  # '▀'
_RESET = "\033[0m"


def matrix(data: str, ecc=None) -> List[List[bool]]:
    """The QR module grid for ``data`` (``True`` = dark), without the quiet zone.

    Error-correction level M (~15%) by default: a middle ground that tolerates a phone camera at an
    angle or a slightly imperfect terminal render without growing the code more than it has to.
    """
    if ecc is None:
        ecc = qrcodegen.QrCode.Ecc.MEDIUM
    code = qrcodegen.QrCode.encode_text(data, ecc)
    size = code.get_size()
    return [[code.get_module(x, y) for x in range(size)] for y in range(size)]


def dimensions(grid: List[List[bool]], quiet: int = QUIET) -> tuple[int, int]:
    """``(columns, lines)`` a rendered ``grid`` needs: one column per module (plus quiet zone), two
    module rows per line."""
    full = len(grid) + 2 * quiet
    return full, (full + 1) // 2


def render(data: str, ecc=None, quiet: int = QUIET) -> str:
    """A scannable QR of ``data`` as ANSI text (no trailing newline)."""
    return _render_grid(matrix(data, ecc), quiet)


def _render_grid(grid: List[List[bool]], quiet: int = QUIET) -> str:
    size = len(grid)
    full = size + 2 * quiet

    def dark(x: int, y: int) -> bool:
        gx, gy = x - quiet, y - quiet
        if 0 <= gx < size and 0 <= gy < size:
            return grid[gy][gx]
        return False  # quiet zone, and the odd bottom half of the last line, are light

    lines = []
    for row in range(0, full, 2):
        cells = []
        for x in range(full):
            top = dark(x, row)
            bottom = dark(x, row + 1)
            # Upper-half block: foreground fills the top module, background the bottom one. Dark ->
            # black (30/40), light -> white (37/47).
            fg = 30 if top else 37
            bg = 40 if bottom else 47
            cells.append("\033[%d;%dm%s" % (fg, bg, _UPPER_HALF))
        lines.append("".join(cells) + _RESET)
    return "\n".join(lines)


def _disabled_by_env(env) -> bool:
    # NO_COLOR: the ANSI color the code depends on would be stripped, leaving an unscannable smear.
    # TERM=dumb: no cursor/color handling at all. ALMA_CERTIFY_NO_QR: an operator opt-out.
    return bool(env.get("ALMA_CERTIFY_NO_QR") or env.get("NO_COLOR") or env.get("TERM") == "dumb")


def render_block(
    data: str,
    *,
    columns: Optional[int] = None,
    lines: Optional[int] = None,
    isatty: Optional[bool] = None,
    env=None,
) -> Optional[str]:
    """The QR to print, or None when it should be skipped and only the text shown.

    Never raises: a failure to draw a QR must not be why authorization does not start, so any
    problem (an over-long URL the encoder rejects, a surprise from the terminal) falls back to None.
    """
    env = os.environ if env is None else env
    if isatty is None:
        isatty = sys.stdout.isatty()
    if not isatty or _disabled_by_env(env):
        return None
    try:
        grid = matrix(data)
        need_cols, need_lines = dimensions(grid)
    except Exception:  # noqa: BLE001 - drawing a QR is best-effort; the text below always works
        return None
    if columns is None or lines is None:
        size = shutil.get_terminal_size((80, 24))
        columns = size.columns if columns is None else columns
        lines = size.lines if lines is None else lines
    # The whole code must fit: printed last, a QR no taller than the screen stays fully visible even
    # after the text above it scrolls off, but one taller than the screen loses its top and cannot
    # be scanned.
    if need_cols > columns or need_lines > lines:
        return None
    try:
        return _render_grid(grid)
    except Exception:  # noqa: BLE001
        return None

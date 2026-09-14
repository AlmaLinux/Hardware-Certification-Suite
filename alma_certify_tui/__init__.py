"""Textual front-end for alma-certify, shipped as the ``alma-certify-tui`` subpackage.

Deliberately a separate top-level package rather than a module under ``alma_certify``: the base
package's build-time import walk (and its stdlib-only contract) must never touch Textual, and this
keeps the whole vendored third-party stack out of that walk. Nothing in the base suite imports this
unless the guided interface is actually launched and the subpackage is installed.

Importing this package puts the vendored stack (``alma_certify_tui/_vendor``, produced by
packaging/vendor/update_vendor.py) on ``sys.path``, so ``import textual`` resolves to the vendored
copy. The insert happens here rather than in the base CLI so it is scoped to launching the TUI and
can never shadow anything on an ordinary, non-TUI invocation.

The public surface is two functions, ``unavailable_reason`` and ``start``, which is all
``cli.cmd_tui`` needs to know about it.
"""
from __future__ import annotations

import os
import sys

_VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_vendor")
if os.path.isdir(_VENDOR) and _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

from .app import start, unavailable_reason  # noqa: E402 - must follow the sys.path insert above

__all__ = ["start", "unavailable_reason"]

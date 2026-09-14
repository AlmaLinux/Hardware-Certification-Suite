"""NOT Pygments. A minimal in-tree shim that stands in for it.

Rich and Textual both declare Pygments as an unconditional dependency and import it (at module top,
so the import is not optional) from a few modules that only run when code or a traceback is being
syntax-highlighted: ``rich.syntax``, ``rich.traceback``, and ``textual.highlight``. The suite's TUI
does not highlight anything, so rather than vendor the real Pygments (a multi-megabyte lexer library
under its own license, which would then sit in our bundled() Provides and CVE surface) we ship this
shim, which satisfies exactly the imports those three modules make and renders everything as plain,
uncolored text.

Nothing here is derived from Pygments; it is an independent, deliberately trivial reimplementation
of the small, stable API surface Rich and Textual touch, under this suite's own MIT license. The
vendoring script imports Rich and Textual against it and constructs a traceback to prove the shim
still covers that surface, so an upstream change that reaches for more of Pygments fails the vendor
run rather than a user's machine.

If real syntax highlighting is ever wanted, delete this directory and add ``pygments`` to
packaging/vendor/manifest.json; the script will vendor it and adjust the Provides and License lines.
"""
from __future__ import annotations

# Sentinel the vendoring script asserts, so a real Pygments accidentally landing on the path (which
# would drag in the license and CVE surface this shim exists to avoid) is caught rather than used.
__vendored_stub__ = True
__version__ = "0.0.0+alma-certify-shim"


def highlight(code, lexer=None, formatter=None, outfile=None):
    """The module-level entry point some callers use. No highlighting: return the code unchanged."""
    if outfile is not None:
        outfile.write(code)
        return None
    return code


def lex(code, lexer):
    return lexer.get_tokens(code)


def format(tokens, formatter=None, outfile=None):  # noqa: A001 (matches Pygments' name)
    text = "".join(value for _, value in tokens)
    if outfile is not None:
        outfile.write(text)
        return None
    return text

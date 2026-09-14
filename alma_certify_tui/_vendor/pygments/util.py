"""The one utility Rich and Textual import from Pygments: the not-found exception.

They wrap ``get_lexer_by_name`` / ``guess_lexer_for_filename`` in ``except ClassNotFound`` to fall
back to plain text. This shim's factories never actually raise it (they always return the trivial
lexer), so those handlers simply never fire; the class exists only so the ``except`` clauses have
something to name.
"""
from __future__ import annotations


class ClassNotFound(ValueError):
    pass

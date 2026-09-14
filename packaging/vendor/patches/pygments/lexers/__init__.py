"""Lexer lookup, reduced to always handing back the null lexer.

Rich and Textual call these to pick a lexer by language name or filename. Here they all return the
same do-nothing :class:`~pygments.lexer.Lexer`, and never raise ``ClassNotFound``, so whatever the
requested language, the text comes through unhighlighted rather than erroring.
"""
from __future__ import annotations

from ..lexer import Lexer


def get_lexer_by_name(_alias, **options):
    return Lexer(**options)


def guess_lexer(_text, **options):
    return Lexer(**options)


def guess_lexer_for_filename(_filename, _text, **options):
    return Lexer(**options)

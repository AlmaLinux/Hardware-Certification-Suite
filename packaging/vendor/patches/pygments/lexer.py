"""The lexer base class, reduced to a null lexer.

A real lexer turns source into a stream of ``(token_type, text)`` pairs. This one emits the input as
plain ``Token.Text``, one item per line so callers that measure by line still line up, which is what
makes highlighted code and traceback frames render as uncolored text. Constructor options
(``stripnl``, ``ensurenl``, ``tabsize``, ...) are accepted and ignored, since there is nothing to
tune about doing nothing.
"""
from __future__ import annotations

from .token import Token


class Lexer:
    name = "Text only"
    aliases = ["text", "plain"]
    filenames = ["*.txt"]

    def __init__(self, **options):
        self.options = options

    def add_filter(self, *args, **kwargs):
        return None

    def get_tokens(self, text, unfiltered=False):
        for line in text.splitlines(keepends=True) or [""]:
            yield Token.Text, line

    def get_tokens_unprocessed(self, text):
        index = 0
        for line in text.splitlines(keepends=True) or [""]:
            yield index, Token.Text, line
            index += len(line)

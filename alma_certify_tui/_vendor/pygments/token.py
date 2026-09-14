"""Token types, the one piece of the Pygments API that has real behavior worth keeping.

Rich and Textual use token types as dict keys and walk them by ``.parent`` to find a style, so the
type has to support attribute chaining (``Token.Name.Function``), be hashable, and know its parent.
This is an independent reimplementation of that tiny, decades-stable structure (a tuple of name
parts), not a copy of Pygments' file. Every real token maps to no style in this shim, so highlighted
output comes out as plain text.
"""
from __future__ import annotations


class _TokenType(tuple):
    parent = None

    def split(self):
        """The chain from the root down to this type, e.g. ``Token.Name.Function`` -> the three."""
        buf = []
        node = self
        while node is not None:
            buf.append(node)
            node = node.parent
        buf.reverse()
        return buf

    def __getattr__(self, name):
        # Any plain attribute access is a request for a subtype; make it, cache it so identity is
        # stable, and record its parent. Underscore names are never subtypes, so ordinary attribute
        # errors still happen for copy/pickle/hasattr machinery.
        if name.startswith("_"):
            raise AttributeError(name)
        child = _TokenType(self + (name,))
        child.parent = self
        setattr(self, name, child)
        return child

    def __contains__(self, other):
        return self is other or (
            isinstance(other, _TokenType) and other[: len(self)] == self
        )

    def __repr__(self):
        return "Token" + ("." if self else "") + ".".join(self)

    # A token type is a constant; copies are itself so it stays a stable dict key.
    def __copy__(self):
        return self

    def __deepcopy__(self, _memo):
        return self


Token = _TokenType()

# The standard roots and aliases Pygments exposes, so ``from pygments.token import Name`` and the
# like resolve. Subtypes below these are created on demand by the attribute access above.
Text = Token.Text
Whitespace = Text.Whitespace
Escape = Token.Escape
Error = Token.Error
Other = Token.Other
Keyword = Token.Keyword
Name = Token.Name
Literal = Token.Literal
String = Literal.String
Number = Literal.Number
Punctuation = Token.Punctuation
Operator = Token.Operator
Comment = Token.Comment
Generic = Token.Generic

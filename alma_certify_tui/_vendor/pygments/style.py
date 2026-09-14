"""A null style: every token resolves to no color.

Rich asks a style class for its ``background_color`` and calls ``style_for_token`` per token, and
reads color, bgcolor, bold, italic, and underline off the result. This one returns an empty style
for every token, which is what makes the output plain. It is a class (not an instance) because
``get_style_by_name`` hands the class back and Rich uses it that way.
"""
from __future__ import annotations

_EMPTY_TOKEN_STYLE = {
    "color": None,
    "bgcolor": None,
    "bold": False,
    "italic": False,
    "underline": False,
    "border": None,
    "roman": None,
    "sans": None,
    "mono": None,
    "ansicolor": None,
    "bgansicolor": None,
}


class Style:
    background_color = None
    highlight_color = None
    line_number_color = None
    line_number_background_color = None
    styles = {}

    @classmethod
    def style_for_token(cls, _token_type):
        return dict(_EMPTY_TOKEN_STYLE)

    @classmethod
    def list_styles(cls):
        return []

    @classmethod
    def styles_token(cls, _ttype):
        return False

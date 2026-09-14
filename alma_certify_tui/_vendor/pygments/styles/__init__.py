"""Style lookup, reduced to always handing back the null style.

``get_style_by_name`` never raises here, whatever theme name is asked for, so Rich's traceback and
syntax paths get a usable (empty) style instead of a ``ClassNotFound``.
"""
from __future__ import annotations

from ..style import Style


def get_style_by_name(_name):
    return Style


def get_all_styles():
    return iter(())

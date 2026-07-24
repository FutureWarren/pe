"""Spreadsheet formula-injection neutralisation for untrusted cell text.

Values extracted from seller-supplied documents (quotes, file names, notes) flow
into the generated workbook. openpyxl turns any string beginning with ``=`` into
a live formula, and Excel/Calc treat leading ``= + - @`` as executable when a CSV
is opened — so a source cell like ``=HYPERLINK("http://evil",A1)`` or
``=cmd|'/c calc'!A1`` would re-arm when the analyst opens the databook.

Only *untrusted text* is passed through here. Intentional formulas the engine
writes go through a separate ``formula`` field and are never sanitised.
"""

from __future__ import annotations

from typing import Any

_HARD_TRIGGERS = ("=", "@")
_SIGN_TRIGGERS = ("+", "-")


def _is_number_like(text: str) -> bool:
    try:
        float(text.replace(",", "").replace("$", "").strip())
        return True
    except ValueError:
        return False


def sanitize_cell_text(value: Any) -> Any:
    """Return ``value`` with any formula-trigger lead neutralised.

    Non-string values (numbers, None) pass through untouched. A genuine negative
    number written as text (``-5``, ``-1,234.50``) is preserved; only strings that
    would be interpreted as a formula/command get a leading apostrophe.
    """

    if not isinstance(value, str) or not value:
        return value
    head = value[0]
    if head in _HARD_TRIGGERS:
        return "'" + value
    if head in _SIGN_TRIGGERS and not _is_number_like(value):
        return "'" + value
    return value

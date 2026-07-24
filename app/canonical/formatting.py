"""Period labels, unit scaling, and display formatting used across outputs."""

from __future__ import annotations

import re
from datetime import date
from typing import Optional

from app.models.canonical import MetricUnit
from app.models.mapping import ResolvedMetricValue, ResolvedPnlPeriod

MONTH_ABBREVS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

_PERIOD_KEY_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_PERIOD_KEY_QUARTER = re.compile(r"^(\d{4})-Q([1-4])$", re.IGNORECASE)
_PERIOD_KEY_LTM = re.compile(r"^LTM(-(\d{4}-\d{2}))?$", re.IGNORECASE)


def period_display_label(period: ResolvedPnlPeriod) -> str:
    """Return a compact analyst-friendly label like ``Jan-25`` or ``Q4-24``."""

    if period.period_granularity == "month":
        month_match = _PERIOD_KEY_MONTH.match(period.period_key)
        if month_match:
            year = int(month_match.group(1))
            month = int(month_match.group(2))
            if 1 <= month <= 12:
                return f"{MONTH_ABBREVS[month - 1]}-{year % 100:02d}"
    if period.period_granularity == "quarter":
        quarter_match = _PERIOD_KEY_QUARTER.match(period.period_key)
        if quarter_match:
            year = int(quarter_match.group(1))
            return f"Q{quarter_match.group(2)}-{year % 100:02d}"
    if period.period_granularity == "year":
        if period.period_key.isdigit() and len(period.period_key) == 4:
            return f"FY{period.period_key[-2:]}"
    if period.period_granularity == "ltm":
        return period.period_label or "LTM"
    # Fall back to the raw label or key — never show the word "unknown".
    return period.period_label or period.period_key


def period_sort_key(period: ResolvedPnlPeriod) -> tuple[int, str]:
    """Sort periods deterministically: months ascending, then LTM, then others.

    Returns (bucket, secondary). Lower bucket sorts first.
    """

    if period.period_start:
        return (0, period.period_start.isoformat())
    if period.period_granularity == "ltm":
        return (1, period.period_key)
    return (2, period.period_key)


def to_canonical_value(metric: ResolvedMetricValue) -> Optional[float]:
    """Return the analyst-facing number for a resolved metric.

    IMPORTANT unit convention: extractors already normalise ``value`` to *ones*
    (``extract/pnl.py`` expands ``"5m"`` to ``5_000_000`` and the Gemini adapter
    stores fully-expanded ``normalized_value``). ``unit_scale`` is therefore only
    a provenance label of the source's reporting scale — it must NOT be applied
    again here, or every non-"ones" USD figure would be inflated by 1,000× /
    1,000,000× (a "$5M" revenue would render as $5 trillion).

    Percent values are returned as a ratio (``0.37`` for 37%) so Excel's ``0.0%``
    format renders correctly.
    """

    if metric.value is None:
        return None
    value = metric.value
    if metric.unit_scale == "percent":
        # Values that already look like 0.37 are kept; values like 37 are
        # converted on the assumption they represent a display-style percentage.
        return value if abs(value) <= 1.5 else value / 100.0
    return value


def canonical_unit(metric_key: str, metric: Optional[ResolvedMetricValue]) -> MetricUnit:
    """Return the analyst-facing unit for a metric."""

    if metric_key in {"gross_margin_pct", "ebitda_margin_pct"}:
        return "%"
    if metric_key == "headcount":
        return "count"
    if metric and metric.unit_scale == "percent":
        return "%"
    return "USD"


def format_date_iso(value: Optional[date]) -> str:
    """Return an ISO date string or empty string (never ``None``)."""

    return value.isoformat() if value else ""

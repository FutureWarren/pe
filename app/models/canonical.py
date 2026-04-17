"""Canonical analyst-facing output models.

These are the single source of truth for anything an analyst sees:
Model_Input (wide table), Exceptions (lean), Source_Map (traceable).

Raw extraction records and resolved period records are upstream inputs
and must not reach the UI directly.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

ConfidenceLevel = Literal["High", "Medium", "Low"]
ValidationResult = Literal["Matched", "Formula", "Single-source", "Mismatch"]
MetricStatus = Literal["Ready", "Review"]
ExceptionSeverity = Literal["Info", "Review", "Critical"]
DirectOrDerived = Literal["direct", "derived"]
MetricUnit = Literal["USD", "USD_thousands", "%", "count", "months", "ratio"]


# ---------------------------------------------------------------------------
# Metric catalog
# ---------------------------------------------------------------------------
# Fixed ordering for the Model_Input sheet. Display labels are the user-facing
# names; backend keys map to the internal ResolvedPnlPeriod fields.

METRIC_ORDER: list[str] = [
    "revenue",
    "cogs",
    "gross_profit",
    "gross_margin_pct",
    "operating_expenses",
    "ebitda",
    "ebitda_margin_pct",
    "headcount",
]

METRIC_DISPLAY: dict[str, str] = {
    "revenue": "Revenue",
    "cogs": "COGS",
    "gross_profit": "Gross Profit",
    "gross_margin_pct": "Gross Margin %",
    "operating_expenses": "Operating Expenses",
    "ebitda": "EBITDA",
    "ebitda_margin_pct": "EBITDA Margin %",
    "headcount": "Headcount",
}

# Mapping from canonical metric_key (analyst-facing) to the ResolvedPnlPeriod
# attribute name. The pipeline's internal metric names are kept deliberately
# separate from what the analyst sees.
METRIC_KEY_TO_RESOLVED_FIELD: dict[str, str] = {
    "revenue": "revenue",
    "cogs": "direct_costs",
    "gross_profit": "gross_profit",
    "operating_expenses": "operating_expenses",
    "ebitda": "ebitda",
    "headcount": "employee_count",
}

# Metrics that are always derived as Excel formulas in the workbook.
DERIVED_METRIC_FORMULAS: dict[str, tuple[str, str]] = {
    # metric_key -> (human formula, machine formula in canonical keys)
    "gross_profit": ("Revenue - COGS", "revenue-cogs"),
    "gross_margin_pct": ("Gross Profit / Revenue", "gross_profit/revenue"),
    "ebitda": ("Gross Profit - Operating Expenses", "gross_profit-operating_expenses"),
    "ebitda_margin_pct": ("EBITDA / Revenue", "ebitda/revenue"),
}

METRIC_UNIT_DEFAULT: dict[str, MetricUnit] = {
    "revenue": "USD",
    "cogs": "USD",
    "gross_profit": "USD",
    "gross_margin_pct": "%",
    "operating_expenses": "USD",
    "ebitda": "USD",
    "ebitda_margin_pct": "%",
    "headcount": "count",
}


# ---------------------------------------------------------------------------
# Canonical output data structures
# ---------------------------------------------------------------------------


class SourceCitation(BaseModel):
    """One concrete, checkable pointer back into a source file."""

    file: str
    tab: Optional[str] = None
    range: Optional[str] = None
    value: Optional[float] = None
    source_id: Optional[str] = None


class FinalMetricRecord(BaseModel):
    """The canonical, analyst-facing record for one metric in one period.

    Rules:
      - `final_value` is the only value the UI should render.
      - `selected_source` must never be None when direct_or_derived=='direct'.
      - Derived rows (gross_profit, margins, ebitda) carry a `derivation_formula`
        and do not require a selected_source.
      - Display strings like `Unknown file` / `Unknown source` are NEVER
        allowed. Missing sources surface as an ExceptionRow instead.
    """

    metric_key: str
    metric_name: str
    period: str
    period_key: str
    period_order: int = 0

    final_value: Optional[float] = None
    unit: Optional[MetricUnit] = None

    selected_source: Optional[SourceCitation] = None
    backup_sources: list[SourceCitation] = Field(default_factory=list)
    source_priority_reason: Optional[str] = None

    direct_or_derived: DirectOrDerived = "direct"
    derivation_formula: Optional[str] = None

    validation_result: ValidationResult = "Single-source"
    confidence_level: ConfidenceLevel = "Medium"
    confidence_reason: str = ""

    status: MetricStatus = "Ready"
    note: Optional[str] = None  # <= 1 sentence for Model_Input
    cross_check_log: list[str] = Field(default_factory=list)  # copilot only


class ExceptionRow(BaseModel):
    """One item that truly merits human attention."""

    metric: str
    period: str
    issue: str
    system_view: str
    suggested_action: str
    severity: ExceptionSeverity = "Review"
    related_metric_key: Optional[str] = None
    related_period_key: Optional[str] = None


class ModelInputBundle(BaseModel):
    """The full analyst-facing output of one run."""

    metrics: list[FinalMetricRecord]
    exceptions: list[ExceptionRow]
    period_order: list[str] = Field(default_factory=list)  # display labels for columns
    period_keys: list[str] = Field(default_factory=list)  # sortable keys, same order as period_order
    metric_order: list[str] = Field(default_factory=lambda: list(METRIC_ORDER))


# ---------------------------------------------------------------------------
# Display formatting rules (shared by workbook + UI)
# ---------------------------------------------------------------------------

EXCEL_NUMBER_FORMAT: dict[MetricUnit, str] = {
    "USD": "#,##0;(#,##0);\"\"",
    "USD_thousands": "#,##0;(#,##0);\"\"",
    "%": "0.0%;(0.0%);\"\"",
    "count": "#,##0;(#,##0);\"\"",
    "months": "0.0;(0.0);\"\"",
    "ratio": "0.00x;(0.00x);\"\"",
}

EXCEPTION_SEVERITY_ORDER = {"Critical": 0, "Review": 1, "Info": 2}

"""Workbook binding and resolved period models."""

from __future__ import annotations

from datetime import date
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field

from app.models.source import EvidenceRef


class ResolvedMetricValue(BaseModel):
    """Represents a deterministic value selected or derived for workbook output."""

    value: float
    unit_scale: Literal["ones", "thousands", "millions", "percent", "count"]
    currency: Optional[str] = "USD"
    source_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    status: Literal["provided", "derived"] = "provided"
    formula: Optional[str] = None
    notes: list[str] = Field(default_factory=list)
    conflicting_values: list[float] = Field(default_factory=list)


class ResolvedPnlPeriod(BaseModel):
    """Represents the resolved values for one normalized reporting period."""

    period_label: str
    period_key: str
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    period_granularity: Literal["month", "quarter", "year", "ltm", "unknown"] = "unknown"
    revenue: Optional[ResolvedMetricValue] = None
    direct_costs: Optional[ResolvedMetricValue] = None
    gross_profit: Optional[ResolvedMetricValue] = None
    operating_expenses: Optional[ResolvedMetricValue] = None
    ebitda: Optional[ResolvedMetricValue] = None
    adjusted_ebitda: Optional[ResolvedMetricValue] = None
    customer_concentration_pct: Optional[ResolvedMetricValue] = None
    employee_count: Optional[ResolvedMetricValue] = None
    notes: list[str] = Field(default_factory=list)


class WorkbookCellBinding(BaseModel):
    """Represents one deterministic workbook write operation."""

    sheet_name: str
    cell: str
    cell_role: Literal["label", "header", "input", "formula", "note"]
    line_item_code: Optional[str] = None
    period_key: Optional[str] = None
    value: Optional[Union[str, float, int]] = None
    formula: Optional[str] = None
    number_format: str
    comment: Optional[str] = None
    hyperlink: Optional[str] = None
    source_ids: list[str] = Field(default_factory=list)


class SourceMapEntry(BaseModel):
    """Represents one audit row in the workbook source map tab."""

    sheet_name: str
    cell: str
    line_item_code: str
    period_key: str
    value_display: str
    source_ids: list[str]
    locators: list[str]
    quotes: list[str]

"""Structured extraction models for the P&L-focused pilot schema."""

from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.models.source import EvidenceRef


class MetricValue(BaseModel):
    """Represents one extracted numeric value plus its supporting evidence."""

    value: float
    raw_value: str
    unit_scale: Literal["ones", "thousands", "millions", "percent", "count"]
    currency: Optional[str] = "USD"
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    confidence: float = 0.75


class PnlExtractionRecord(BaseModel):
    """Normalized JSON-first extraction record for one period and one source."""

    extraction_id: str
    schema_name: Literal["pnl_v1"] = "pnl_v1"
    source_id: str
    source_file_name: str
    period_label: Optional[str] = None
    period_key: Optional[str] = None
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    period_granularity: Literal["month", "quarter", "year", "ltm", "unknown"] = "unknown"
    revenue: Optional[MetricValue] = None
    direct_costs: Optional[MetricValue] = None
    gross_profit: Optional[MetricValue] = None
    operating_expenses: Optional[MetricValue] = None
    ebitda: Optional[MetricValue] = None
    adjusted_ebitda: Optional[MetricValue] = None
    customer_concentration_pct: Optional[MetricValue] = None
    employee_count: Optional[MetricValue] = None
    notes: list[str] = Field(default_factory=list)
    uncertainty: list[str] = Field(default_factory=list)
    conflicting_values: dict[str, list[str]] = Field(default_factory=dict)


class ExtractionBundle(BaseModel):
    """Represents the JSON-only output of one extraction pass."""

    schema_name: Literal["pnl_v1"] = "pnl_v1"
    record_count: int
    records: list[PnlExtractionRecord]
    assumptions: list[str] = Field(default_factory=list)

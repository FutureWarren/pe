"""Validation models for workbook auditability and exception handling."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ValidationIssue(BaseModel):
    """Represents a single validation error, warning, or informational note."""

    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    context: dict[str, Any] = Field(default_factory=dict)


class ValidationReport(BaseModel):
    """Represents the set of validation outcomes for a pipeline run."""

    status: Literal["pass", "warning", "fail"]
    issue_count: int
    assumptions: list[str] = Field(default_factory=list)
    issues: list[ValidationIssue]

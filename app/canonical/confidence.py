"""Confidence scoring + status assignment + exception generation.

The rules here are deliberately small and deterministic so they are easy to
review and reason about. Each call returns *why* it chose a level.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.canonical.selector import SourceFamily
from app.models.canonical import (
    ConfidenceLevel,
    ExceptionRow,
    ExceptionSeverity,
    FinalMetricRecord,
    MetricStatus,
    ValidationResult,
)


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------


@dataclass
class ConfidenceDecision:
    level: ConfidenceLevel
    reason: str


_HIGH_PRIORITY_FAMILIES = {"qoe", "audited_fs", "monthly_fs"}


def assess_confidence(
    validation_result: ValidationResult,
    selected_family: Optional[SourceFamily],
    source_count: int,
    has_conflicting_values: bool,
    direct_or_derived: str,
) -> ConfidenceDecision:
    """Return a confidence level with a short explanation."""

    if validation_result == "Mismatch":
        return ConfidenceDecision("Low", "Arithmetic or cross-source check did not close.")
    if has_conflicting_values:
        return ConfidenceDecision("Low", "Conflicting values were found across the uploaded files.")

    if direct_or_derived == "derived":
        return ConfidenceDecision(
            "High" if validation_result == "Formula" else "Medium",
            "Derived from validated inputs via a deterministic formula.",
        )

    family_key = selected_family.key if selected_family else "other"
    if family_key in _HIGH_PRIORITY_FAMILIES and validation_result == "Matched":
        return ConfidenceDecision(
            "High",
            "High-priority financial source and the value agrees with another independent file.",
        )
    if family_key in _HIGH_PRIORITY_FAMILIES:
        return ConfidenceDecision(
            "Medium",
            "High-priority financial source but only a single file supplies this number.",
        )
    if validation_result == "Matched":
        return ConfidenceDecision(
            "Medium",
            "Multiple files agree on this value, though the primary source is not a QoE-grade package.",
        )
    return ConfidenceDecision(
        "Medium" if source_count > 0 else "Low",
        "Single supporting source with no corroborating file.",
    )


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def assess_status(
    confidence: ConfidenceLevel,
    validation_result: ValidationResult,
    final_value: Optional[float],
) -> MetricStatus:
    if final_value is None:
        return "Review"
    if validation_result == "Mismatch":
        return "Review"
    if confidence == "Low":
        return "Review"
    return "Ready"


# ---------------------------------------------------------------------------
# Exception generation
# ---------------------------------------------------------------------------


def derive_exceptions(metrics: list[FinalMetricRecord]) -> list[ExceptionRow]:
    """Emit only the exceptions an analyst truly needs to look at.

    Rules:
      * Mismatch              -> Critical (formula did not tie)
      * Low + missing value   -> Review   (source-missing)
      * Low + present value   -> Review   (source disagreement)
      * Ready                 -> no exception
    """

    exceptions: list[ExceptionRow] = []
    for metric in metrics:
        if metric.status == "Ready":
            continue

        severity: ExceptionSeverity = "Review"
        if metric.validation_result == "Mismatch":
            severity = "Critical"

        if metric.final_value is None:
            issue = "No source provided this metric for this period."
            system_view = "The upload did not carry enough evidence to resolve this value deterministically."
            suggested = "Confirm the source file is in the upload and that the metric appears in it."
        elif metric.validation_result == "Mismatch":
            issue = "Arithmetic check did not close."
            system_view = metric.confidence_reason
            suggested = "Inspect the component metrics in the Source_Map and reconcile the underlying figures."
        else:
            issue = "Low confidence in the selected source."
            system_view = metric.confidence_reason
            suggested = "Open the Source_Map to compare the primary source against any backup candidates."

        exceptions.append(
            ExceptionRow(
                metric=metric.metric_name,
                period=metric.period,
                issue=issue,
                system_view=system_view,
                suggested_action=suggested,
                severity=severity,
                related_metric_key=metric.metric_key,
                related_period_key=metric.period_key,
            )
        )
    return exceptions

"""Grounded copilot explanations.

This module never invents facts — every answer it returns is assembled from
fields on the given FinalMetricRecord. The copilot UI calls into these
functions via the ``/runs/{id}/explain`` endpoint.
"""

from __future__ import annotations

from typing import Literal, Optional

from app.models.canonical import FinalMetricRecord, SourceCitation

QuestionType = Literal[
    "source",
    "why_this_source",
    "direct_or_derived",
    "cross_checks",
    "confidence",
    "where_to_verify",
    "compare_files",
    "summary",
]


def explain(record: FinalMetricRecord, question: QuestionType = "summary") -> str:
    """Return a short analyst-style explanation for one FinalMetricRecord."""

    dispatch = {
        "source": _explain_source,
        "why_this_source": _explain_priority,
        "direct_or_derived": _explain_direct_or_derived,
        "cross_checks": _explain_cross_checks,
        "confidence": _explain_confidence,
        "where_to_verify": _explain_where_to_verify,
        "compare_files": _explain_compare_files,
        "summary": _explain_summary,
    }
    return dispatch[question](record)


def _cite(citation: Optional[SourceCitation]) -> str:
    if citation is None:
        return "no source attached"
    pieces = [citation.file]
    if citation.tab:
        pieces.append(citation.tab)
    if citation.range:
        pieces.append(citation.range)
    return " → ".join(p for p in pieces if p)


def _fmt_value(record: FinalMetricRecord) -> str:
    value = record.final_value
    if value is None:
        return "no value"
    if record.unit == "%":
        return f"{value * 100:.1f}%"
    if record.unit == "count":
        return f"{value:,.0f}"
    return f"{value:,.0f}"


def _explain_source(record: FinalMetricRecord) -> str:
    if record.direct_or_derived == "derived":
        return (
            f"{record.metric_name} for {record.period} is a calculated value, not pulled from a single cell. "
            f"It is derived as {record.derivation_formula}. The component metrics trace back through the Source_Map."
        )
    return (
        f"{record.metric_name} for {record.period} was taken from {_cite(record.selected_source)} "
        f"and read as {_fmt_value(record)}."
    )


def _explain_priority(record: FinalMetricRecord) -> str:
    if record.source_priority_reason:
        return record.source_priority_reason
    return "No explicit priority reason recorded."


def _explain_direct_or_derived(record: FinalMetricRecord) -> str:
    if record.direct_or_derived == "derived":
        return (
            f"The system computed {record.metric_name} ({record.period}) deterministically as "
            f"{record.derivation_formula} rather than pulling it from a single cell. This keeps the ratio or "
            "sub-total consistent with the underlying inputs even when a source file's own reported value drifts."
        )
    return (
        f"{record.metric_name} for {record.period} was read directly from a source cell; no recomputation was applied."
    )


def _explain_cross_checks(record: FinalMetricRecord) -> str:
    if not record.cross_check_log:
        return "No cross-checks were executed for this metric."
    return " ".join(record.cross_check_log)


def _explain_confidence(record: FinalMetricRecord) -> str:
    reason = record.confidence_reason or "No explicit reason recorded."
    return f"Confidence is {record.confidence_level}. {reason}"


def _explain_where_to_verify(record: FinalMetricRecord) -> str:
    if record.selected_source is None:
        return (
            "There is no direct cell reference for this metric — open the Source_Map sheet and look up the "
            f"{record.metric_name} row for {record.period} to see the supporting evidence."
        )
    citation = record.selected_source
    pieces = [f"Open {citation.file}"]
    if citation.tab:
        pieces.append(f"on the '{citation.tab}' tab")
    if citation.range:
        pieces.append(f"at {citation.range}")
    return (
        ", ".join(pieces)
        + f" to verify {record.metric_name} for {record.period}."
    )


def _explain_compare_files(record: FinalMetricRecord) -> str:
    primary_file = record.selected_source.file if record.selected_source else None
    independent_backups = [
        backup for backup in record.backup_sources if backup.file and backup.file != primary_file
    ]

    if record.validation_result == "Matched":
        return (
            f"The value was corroborated across {len(independent_backups) + 1} source files. "
            f"Primary: {_cite(record.selected_source)}. Backups: "
            + "; ".join(_cite(backup) for backup in independent_backups)
            + "."
        )
    if independent_backups:
        return (
            "Additional files reference this metric but the system did not treat them as independent corroboration: "
            + "; ".join(_cite(backup) for backup in independent_backups)
            + "."
        )
    return "No second file independently corroborated this metric in the current upload."


def _explain_summary(record: FinalMetricRecord) -> str:
    parts = [
        _explain_source(record),
        _explain_priority(record),
        _explain_cross_checks(record),
        _explain_confidence(record),
    ]
    return " ".join(parts)

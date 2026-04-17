"""Validation report builders for the pilot pipeline."""

from __future__ import annotations

from app.models.extraction import ExtractionBundle
from app.models.mapping import ResolvedPnlPeriod
from app.models.source import SourceManifest
from app.models.validation import ValidationIssue, ValidationReport

REQUIRED_FIELDS = ("revenue", "direct_costs")
CORE_METRIC_FIELDS = (
    "revenue",
    "direct_costs",
    "gross_profit",
    "operating_expenses",
    "ebitda",
    "adjusted_ebitda",
)
ASSUMPTIONS = [
    "Phase 1 focuses on one deterministic P&L tab only.",
    "Spreadsheet-style sources are parsed more reliably than narrative PDFs.",
    "Plain numeric values without explicit scale markers are treated as ones.",
    "Undated customer concentration and employee count values may appear in an 'UNDATED' period column.",
    "Periods that only contain supporting KPI metrics do not trigger missing core P&L warnings.",
]


def build_validation_report(
    manifest: SourceManifest,
    extraction_bundle: ExtractionBundle,
    resolved_periods: list[ResolvedPnlPeriod],
) -> ValidationReport:
    """Build a first-class validation report for each pilot run."""

    issues: list[ValidationIssue] = []

    if manifest.document_count == 0:
        issues.append(
            ValidationIssue(
                severity="error",
                code="empty_data_room",
                message="No supported source documents were found in the data room.",
                context={"data_room_dir": str(manifest.data_room_dir)},
            )
        )
    if manifest.skipped_files:
        issues.append(
            ValidationIssue(
                severity="info",
                code="skipped_files",
                message="Some files were skipped because their extensions are not supported.",
                context={
                    "skipped_files": [skipped.rel_path for skipped in manifest.skipped_files],
                },
            )
        )
    if extraction_bundle.record_count == 0:
        issues.append(
            ValidationIssue(
                severity="warning",
                code="no_pnl_records",
                message="No P&L extraction records were produced from the parsed sources.",
                context={"document_count": manifest.document_count},
            )
        )

    granularities = {
        period.period_granularity
        for period in resolved_periods
        if period.period_granularity not in {"unknown"}
    }
    if len(granularities) > 1:
        issues.append(
            ValidationIssue(
                severity="warning",
                code="mixed_period_granularity",
                message="Multiple period granularities were detected across resolved periods.",
                context={"granularities": sorted(granularities)},
            )
        )

    for period in resolved_periods:
        requires_core_inputs = _period_requires_core_inputs(period)
        for field_name in REQUIRED_FIELDS:
            if not requires_core_inputs or getattr(period, field_name) is not None:
                continue
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="missing_required_field",
                    message=f"{field_name} is missing for period {period.period_key}.",
                    context={"period_key": period.period_key, "field": field_name},
                )
            )

        if period.period_key != "UNDATED" and (period.period_start is None or period.period_end is None):
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="period_mismatch",
                    message=f"Period metadata is incomplete for {period.period_key}.",
                    context={"period_key": period.period_key},
                )
            )

        for metric_name in (
            "revenue",
            "direct_costs",
            "gross_profit",
            "operating_expenses",
            "ebitda",
            "adjusted_ebitda",
            "customer_concentration_pct",
            "employee_count",
        ):
            metric = getattr(period, metric_name)
            if metric is None:
                continue
            if metric.conflicting_values:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        code="conflicting_values",
                        message=f"Conflicting {metric_name} values were found for period {period.period_key}.",
                        context={
                            "period_key": period.period_key,
                            "field": metric_name,
                            "selected_value": metric.value,
                            "other_values": metric.conflicting_values,
                        },
                    )
                )
            for note in metric.notes:
                if "unit mismatch" in note.lower():
                    issues.append(
                        ValidationIssue(
                            severity="warning",
                            code="obvious_unit_issue",
                            message=note,
                            context={"period_key": period.period_key, "field": metric_name},
                        )
                    )

        if period.revenue and period.direct_costs and period.gross_profit:
            expected_gp = period.revenue.value - period.direct_costs.value
            if abs(period.gross_profit.value - expected_gp) > 0.01:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        code="formula_mismatch",
                        message=f"Gross profit does not reconcile for {period.period_key}.",
                        context={
                            "period_key": period.period_key,
                            "expected": expected_gp,
                            "actual": period.gross_profit.value,
                        },
                    )
                )

        if period.gross_profit and period.operating_expenses and period.ebitda:
            expected_ebitda = period.gross_profit.value - period.operating_expenses.value
            if abs(period.ebitda.value - expected_ebitda) > 0.01:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        code="formula_mismatch",
                        message=f"EBITDA does not reconcile for {period.period_key}.",
                        context={
                            "period_key": period.period_key,
                            "expected": expected_ebitda,
                            "actual": period.ebitda.value,
                        },
                    )
                )

        for note in period.notes:
            if "reconcile" in note.lower():
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        code="formula_mismatch",
                        message=note,
                        context={"period_key": period.period_key},
                    )
                )

    status = "pass"
    if any(issue.severity == "error" for issue in issues):
        status = "fail"
    elif issues:
        status = "warning"

    merged_assumptions = []
    for assumption in [*ASSUMPTIONS, *extraction_bundle.assumptions]:
        if assumption not in merged_assumptions:
            merged_assumptions.append(assumption)

    return ValidationReport(
        status=status,
        issue_count=len(issues),
        assumptions=merged_assumptions,
        issues=issues,
    )


def _period_requires_core_inputs(period: ResolvedPnlPeriod) -> bool:
    """Return whether this period should be treated as a core P&L period.

    Month/quarter/year periods are always expected to carry the core Revenue and
    COGS inputs. LTM, UNDATED, or otherwise KPI-only periods should not produce
    missing core input warnings unless they already contain at least one core
    P&L metric.
    """

    if period.period_granularity in {"month", "quarter"}:
        return True

    return any(getattr(period, field_name) is not None for field_name in CORE_METRIC_FIELDS)


def render_validation_markdown(report: ValidationReport) -> str:
    """Render a human-readable markdown summary for local review."""

    lines = [
        "# Validation Report",
        "",
        f"Status: **{report.status.upper()}**",
        f"Issue count: **{report.issue_count}**",
        "",
    ]

    if report.assumptions:
        lines.append("## Assumptions")
        lines.append("")
        for assumption in report.assumptions:
            lines.append(f"- {assumption}")
        lines.append("")

    lines.append("## Issues")
    lines.append("")
    for issue in report.issues:
        lines.append(f"- [{issue.severity.upper()}] `{issue.code}`: {issue.message}")

    if not report.issues:
        lines.append("- No issues found.")

    return "\n".join(lines) + "\n"

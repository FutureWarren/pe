"""Deterministic workbook mapping rules for the pilot P&L tab."""

from __future__ import annotations

from datetime import date
from typing import Optional

from openpyxl.utils import get_column_letter

from app.models.mapping import (
    ResolvedMetricValue,
    ResolvedPnlPeriod,
    SourceMapEntry,
    WorkbookCellBinding,
)

P_AND_L_SHEET_NAME = "P&L"
SOURCE_MAP_SHEET_NAME = "Source Map"
SOURCE_REGISTRY_SHEET_NAME = "Sources"
VALIDATION_SHEET_NAME = "Validation"

ROW_LAYOUT = [
    ("period_start", "Period Start", "date"),
    ("period_end", "Period End", "date"),
    ("revenue", "Revenue", "currency"),
    ("direct_costs", "COGS / Direct Costs", "currency"),
    ("gross_profit", "Gross Profit", "currency"),
    ("gross_margin_pct", "Gross Margin %", "percent"),
    ("operating_expenses", "Operating Expenses", "currency"),
    ("ebitda", "EBITDA", "currency"),
    ("adjusted_ebitda", "Adjusted EBITDA", "currency"),
    ("ebitda_margin_pct", "EBITDA Margin %", "percent"),
    ("customer_concentration_pct", "Customer Concentration %", "percent"),
    ("employee_count", "Employee Count", "integer"),
    ("notes", "Notes / Uncertainty", "text"),
]
ROW_INDEX_BY_KEY = {row_key: index for index, (row_key, _, _) in enumerate(ROW_LAYOUT, start=5)}


def build_pnl_workbook_bindings(
    resolved_periods: list[ResolvedPnlPeriod],
) -> tuple[list[WorkbookCellBinding], list[SourceMapEntry]]:
    """Build deterministic cell bindings and source map rows for the P&L tab."""

    bindings: list[WorkbookCellBinding] = [
        WorkbookCellBinding(
            sheet_name=P_AND_L_SHEET_NAME,
            cell="A1",
            cell_role="label",
            value="P&L Analysis",
            number_format="General",
        ),
        WorkbookCellBinding(
            sheet_name=P_AND_L_SHEET_NAME,
            cell="A2",
            cell_role="note",
            value="Deterministic pilot output. Source-backed cells include comments and the Source Map tab.",
            number_format="General",
        ),
    ]
    source_map_entries: list[SourceMapEntry] = []

    bindings.append(
        WorkbookCellBinding(
            sheet_name=P_AND_L_SHEET_NAME,
            cell="A4",
            cell_role="header",
            value="Line Item",
            number_format="General",
        )
    )

    for row_key, label, _ in ROW_LAYOUT:
        bindings.append(
            WorkbookCellBinding(
                sheet_name=P_AND_L_SHEET_NAME,
                cell=f"A{ROW_INDEX_BY_KEY[row_key]}",
                cell_role="label",
                line_item_code=row_key,
                value=label,
                number_format="General",
            )
        )

    for period_index, period in enumerate(resolved_periods, start=2):
        column_letter = get_column_letter(period_index)
        bindings.append(
            WorkbookCellBinding(
                sheet_name=P_AND_L_SHEET_NAME,
                cell=f"{column_letter}4",
                cell_role="header",
                period_key=period.period_key,
                value=period.period_label,
                number_format="General",
            )
        )
        bindings.extend(_build_period_bindings(period, column_letter))
        source_map_entries.extend(_build_source_map_entries(period, column_letter))

    return bindings, source_map_entries


def _build_period_bindings(
    period: ResolvedPnlPeriod,
    column_letter: str,
) -> list[WorkbookCellBinding]:
    """Build cell bindings for one resolved period."""

    bindings: list[WorkbookCellBinding] = [
        _value_binding(
            column_letter,
            "period_start",
            period.period_key,
            _format_date(period.period_start),
            "yyyy-mm-dd",
        ),
        _value_binding(
            column_letter,
            "period_end",
            period.period_key,
            _format_date(period.period_end),
            "yyyy-mm-dd",
        ),
    ]

    for metric_key in (
        "revenue",
        "direct_costs",
        "gross_profit",
        "operating_expenses",
        "ebitda",
        "adjusted_ebitda",
        "customer_concentration_pct",
        "employee_count",
    ):
        metric = getattr(period, metric_key)
        if metric is None:
            continue
        bindings.append(
            _metric_binding(
                column_letter=column_letter,
                row_key=metric_key,
                period_key=period.period_key,
                metric=metric,
            )
        )

    if period.revenue and period.ebitda:
        bindings.append(
            WorkbookCellBinding(
                sheet_name=P_AND_L_SHEET_NAME,
                cell=f"{column_letter}{ROW_INDEX_BY_KEY['ebitda_margin_pct']}",
                cell_role="formula",
                line_item_code="ebitda_margin_pct",
                period_key=period.period_key,
                formula=f"={column_letter}{ROW_INDEX_BY_KEY['ebitda']}/{column_letter}{ROW_INDEX_BY_KEY['revenue']}",
                number_format="0.0%",
                comment="Derived as EBITDA divided by Revenue.",
                source_ids=_merge_source_ids(period.ebitda, period.revenue),
            )
        )

    if period.revenue and period.gross_profit:
        bindings.append(
            WorkbookCellBinding(
                sheet_name=P_AND_L_SHEET_NAME,
                cell=f"{column_letter}{ROW_INDEX_BY_KEY['gross_margin_pct']}",
                cell_role="formula",
                line_item_code="gross_margin_pct",
                period_key=period.period_key,
                formula=f"={column_letter}{ROW_INDEX_BY_KEY['gross_profit']}/{column_letter}{ROW_INDEX_BY_KEY['revenue']}",
                number_format="0.0%",
                comment="Derived as Gross Profit divided by Revenue.",
                source_ids=_merge_source_ids(period.gross_profit, period.revenue),
            )
        )

    if period.notes:
        bindings.append(
            WorkbookCellBinding(
                sheet_name=P_AND_L_SHEET_NAME,
                cell=f"{column_letter}{ROW_INDEX_BY_KEY['notes']}",
                cell_role="note",
                line_item_code="notes",
                period_key=period.period_key,
                value=" | ".join(period.notes),
                number_format="General",
            )
        )

    return bindings


def _metric_binding(
    column_letter: str,
    row_key: str,
    period_key: str,
    metric: ResolvedMetricValue,
) -> WorkbookCellBinding:
    """Build a single metric binding."""

    cell = f"{column_letter}{ROW_INDEX_BY_KEY[row_key]}"
    comment = _build_metric_comment(metric)

    if row_key == "gross_profit" and metric.status == "derived":
        formula = f"={column_letter}{ROW_INDEX_BY_KEY['revenue']}-{column_letter}{ROW_INDEX_BY_KEY['direct_costs']}"
        return WorkbookCellBinding(
            sheet_name=P_AND_L_SHEET_NAME,
            cell=cell,
            cell_role="formula",
            line_item_code=row_key,
            period_key=period_key,
            formula=formula,
            number_format=_number_format_for(row_key),
            comment=comment,
            source_ids=metric.source_ids,
        )

    if row_key == "ebitda" and metric.status == "derived":
        formula = f"={column_letter}{ROW_INDEX_BY_KEY['gross_profit']}-{column_letter}{ROW_INDEX_BY_KEY['operating_expenses']}"
        return WorkbookCellBinding(
            sheet_name=P_AND_L_SHEET_NAME,
            cell=cell,
            cell_role="formula",
            line_item_code=row_key,
            period_key=period_key,
            formula=formula,
            number_format=_number_format_for(row_key),
            comment=comment,
            source_ids=metric.source_ids,
        )

    return WorkbookCellBinding(
        sheet_name=P_AND_L_SHEET_NAME,
        cell=cell,
        cell_role="input",
        line_item_code=row_key,
        period_key=period_key,
        value=metric.value,
        number_format=_number_format_for(row_key),
        comment=comment,
        source_ids=metric.source_ids,
    )


def _build_source_map_entries(
    period: ResolvedPnlPeriod,
    column_letter: str,
) -> list[SourceMapEntry]:
    """Build source map rows for one resolved period."""

    entries: list[SourceMapEntry] = []
    for metric_key in (
        "revenue",
        "direct_costs",
        "gross_profit",
        "operating_expenses",
        "ebitda",
        "adjusted_ebitda",
        "customer_concentration_pct",
        "employee_count",
    ):
        metric = getattr(period, metric_key)
        if metric is None:
            continue
        entries.append(
            SourceMapEntry(
                sheet_name=P_AND_L_SHEET_NAME,
                cell=f"{column_letter}{ROW_INDEX_BY_KEY[metric_key]}",
                line_item_code=metric_key,
                period_key=period.period_key,
                value_display=str(metric.value),
                source_ids=metric.source_ids,
                locators=[evidence.locator_label for evidence in metric.evidence_refs],
                quotes=[evidence.quote for evidence in metric.evidence_refs[:3]],
            )
        )

    if period.revenue and period.gross_profit:
        entries.append(
            SourceMapEntry(
                sheet_name=P_AND_L_SHEET_NAME,
                cell=f"{column_letter}{ROW_INDEX_BY_KEY['gross_margin_pct']}",
                line_item_code="gross_margin_pct",
                period_key=period.period_key,
                value_display="Formula: Gross Profit / Revenue",
                source_ids=_merge_source_ids(period.gross_profit, period.revenue),
                locators=[
                    *[evidence.locator_label for evidence in period.gross_profit.evidence_refs],
                    *[evidence.locator_label for evidence in period.revenue.evidence_refs],
                ][:6],
                quotes=[
                    *[evidence.quote for evidence in period.gross_profit.evidence_refs],
                    *[evidence.quote for evidence in period.revenue.evidence_refs],
                ][:3],
            )
        )

    if period.revenue and period.ebitda:
        entries.append(
            SourceMapEntry(
                sheet_name=P_AND_L_SHEET_NAME,
                cell=f"{column_letter}{ROW_INDEX_BY_KEY['ebitda_margin_pct']}",
                line_item_code="ebitda_margin_pct",
                period_key=period.period_key,
                value_display="Formula: EBITDA / Revenue",
                source_ids=_merge_source_ids(period.ebitda, period.revenue),
                locators=[
                    *[evidence.locator_label for evidence in period.ebitda.evidence_refs],
                    *[evidence.locator_label for evidence in period.revenue.evidence_refs],
                ][:6],
                quotes=[
                    *[evidence.quote for evidence in period.ebitda.evidence_refs],
                    *[evidence.quote for evidence in period.revenue.evidence_refs],
                ][:3],
            )
        )
    return entries


def _build_metric_comment(metric: ResolvedMetricValue) -> str:
    """Build an Excel comment for a source-backed or derived metric."""

    lines = []
    if metric.status == "derived" and metric.formula:
        lines.append(f"Derived formula: {metric.formula}")
    if metric.notes:
        lines.extend(metric.notes[:3])
    for evidence in metric.evidence_refs[:3]:
        lines.append(f"{evidence.source_id} | {evidence.locator_label}")
        lines.append(evidence.quote[:180])
    return "\n".join(lines[:8])


def _value_binding(
    column_letter: str,
    row_key: str,
    period_key: str,
    value: str,
    number_format: str,
) -> WorkbookCellBinding:
    """Build a simple static value binding."""

    return WorkbookCellBinding(
        sheet_name=P_AND_L_SHEET_NAME,
        cell=f"{column_letter}{ROW_INDEX_BY_KEY[row_key]}",
        cell_role="input",
        line_item_code=row_key,
        period_key=period_key,
        value=value,
        number_format=number_format,
    )


def _number_format_for(row_key: str) -> str:
    """Return a number format for a logical row key."""

    if row_key in {"customer_concentration_pct", "ebitda_margin_pct"}:
        return "0.0%"
    if row_key == "employee_count":
        return "0"
    return '#,##0.00_);(#,##0.00)'


def _format_date(value: Optional[date]) -> str:
    """Return a stable string for a date value or blank."""

    return value.isoformat() if value else ""


def _merge_source_ids(*metrics: ResolvedMetricValue) -> list[str]:
    """Merge source ids from multiple resolved metrics."""

    merged: list[str] = []
    for metric in metrics:
        for source_id in metric.source_ids:
            if source_id not in merged:
                merged.append(source_id)
    return merged

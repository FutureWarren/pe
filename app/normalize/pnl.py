"""Deterministic normalization and reconciliation helpers for P&L records."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Optional

from app.models.extraction import ExtractionBundle, MetricValue, PnlExtractionRecord
from app.models.mapping import ResolvedMetricValue, ResolvedPnlPeriod
from app.models.source import SourceManifest

METRIC_FIELDS = [
    "revenue",
    "direct_costs",
    "gross_profit",
    "operating_expenses",
    "ebitda",
    "adjusted_ebitda",
    "customer_concentration_pct",
    "employee_count",
]


def resolve_statement_facts(
    extraction_bundle: ExtractionBundle,
    manifest: SourceManifest,
) -> list[ResolvedPnlPeriod]:
    """Resolve extracted records into canonical values for workbook binding."""

    rank_by_source = {document.source_id: document.priority_rank for document in manifest.documents}
    records_by_period: dict[str, list[PnlExtractionRecord]] = defaultdict(list)

    for record in extraction_bundle.records:
        period_key = record.period_key or "UNDATED"
        records_by_period[period_key].append(record)

    resolved_periods: list[ResolvedPnlPeriod] = []
    for period_key, period_records in sorted(records_by_period.items(), key=_sort_period_key):
        template_record = period_records[0]
        resolved = ResolvedPnlPeriod(
            period_label=template_record.period_label or period_key,
            period_key=period_key,
            period_start=template_record.period_start,
            period_end=template_record.period_end,
            period_granularity=template_record.period_granularity,
            notes=[],
        )

        for metric_name in METRIC_FIELDS:
            candidates = [
                (record, getattr(record, metric_name))
                for record in period_records
                if getattr(record, metric_name) is not None
            ]
            if candidates:
                setattr(
                    resolved,
                    metric_name,
                    _resolve_metric(metric_name, candidates, rank_by_source),
                )

        _normalize_cost_family_sign(resolved)
        _derive_missing_metrics(resolved)
        _attach_formula_notes(resolved)
        resolved_periods.append(resolved)

    return resolved_periods


# Cost-family metrics are subtracted from revenue / gross profit downstream, so
# they must be stored as positive magnitudes. Statements frequently present costs
# in parentheses (e.g. ``(400)``), which the extractor negates to ``-400``; left
# as-is that flips ``revenue - direct_costs`` into ``revenue + direct_costs`` and
# silently inflates Gross Profit and EBITDA.
COST_FAMILY_FIELDS = ("direct_costs", "operating_expenses")


def _normalize_cost_family_sign(period: ResolvedPnlPeriod) -> None:
    """Store cost-family metrics as positive magnitudes for correct subtraction."""

    for metric_name in COST_FAMILY_FIELDS:
        metric = getattr(period, metric_name)
        if metric is None or metric.value is None or metric.value >= 0:
            continue
        metric.value = abs(metric.value)
        metric.notes.append(
            f"{metric_name.replace('_', ' ').title()} was presented as a negative "
            "(e.g. in parentheses); stored as a positive cost magnitude for subtraction."
        )


def _resolve_metric(
    metric_name: str,
    candidates: list[tuple[PnlExtractionRecord, MetricValue]],
    rank_by_source: dict[str, int],
) -> ResolvedMetricValue:
    """Choose the preferred candidate for a metric and preserve conflicts."""

    sorted_candidates = sorted(
        candidates,
        key=lambda item: (
            rank_by_source.get(item[0].source_id, 999),
            item[0].source_file_name.lower(),
        ),
    )

    chosen_record, chosen_metric = sorted_candidates[0]
    unique_values = {
        round(metric.value, 6): metric.value for _, metric in sorted_candidates
    }
    conflicting_values = list(unique_values.values())
    notes: list[str] = []

    if len(unique_values) > 1:
        notes.append(
            f"Conflicting {metric_name} values found across sources; selected "
            f"{chosen_record.source_file_name} using source priority rules."
        )
        if _looks_like_unit_issue(list(unique_values.values())):
            notes.append(f"Possible unit mismatch detected for {metric_name}.")

    same_value_group = [
        metric
        for _, metric in sorted_candidates
        if round(metric.value, 6) == round(chosen_metric.value, 6)
    ]
    evidence_refs = []
    source_ids = []
    for metric in same_value_group:
        evidence_refs.extend(metric.evidence_refs)
        for evidence in metric.evidence_refs:
            if evidence.source_id not in source_ids:
                source_ids.append(evidence.source_id)

    return ResolvedMetricValue(
        value=chosen_metric.value,
        unit_scale=chosen_metric.unit_scale,
        currency=chosen_metric.currency,
        source_ids=source_ids or [chosen_record.source_id],
        evidence_refs=evidence_refs,
        status="provided",
        notes=notes,
        conflicting_values=conflicting_values[1:] if len(conflicting_values) > 1 else [],
    )


def _derive_missing_metrics(period: ResolvedPnlPeriod) -> None:
    """Derive formula-friendly metrics when source-backed inputs exist."""

    if period.revenue and period.direct_costs:
        derived_gp = period.revenue.value - period.direct_costs.value
        if period.gross_profit is None:
            period.gross_profit = ResolvedMetricValue(
                value=derived_gp,
                unit_scale=period.revenue.unit_scale,
                currency=period.revenue.currency,
                source_ids=_merge_source_ids(period.revenue, period.direct_costs),
                evidence_refs=_merge_evidence(period.revenue, period.direct_costs),
                status="derived",
                formula="revenue-direct_costs",
                notes=["Derived as Revenue minus COGS / Direct Costs."],
            )
        elif abs(period.gross_profit.value - derived_gp) > 0.01:
            period.notes.append(
                "Gross profit does not reconcile to revenue minus direct costs."
            )

    if period.gross_profit and period.operating_expenses:
        derived_ebitda = period.gross_profit.value - period.operating_expenses.value
        if period.ebitda is None:
            period.ebitda = ResolvedMetricValue(
                value=derived_ebitda,
                unit_scale=period.gross_profit.unit_scale,
                currency=period.gross_profit.currency,
                source_ids=_merge_source_ids(period.gross_profit, period.operating_expenses),
                evidence_refs=_merge_evidence(period.gross_profit, period.operating_expenses),
                status="derived",
                formula="gross_profit-operating_expenses",
                notes=["Derived as Gross Profit minus Operating Expenses."],
            )
        elif abs(period.ebitda.value - derived_ebitda) > 0.01:
            period.notes.append("EBITDA does not reconcile to gross profit minus operating expenses.")


def _attach_formula_notes(period: ResolvedPnlPeriod) -> None:
    """Copy metric-level notes to the period-level note list for reporting."""

    for metric_name in METRIC_FIELDS:
        metric = getattr(period, metric_name)
        if metric is None:
            continue
        period.notes.extend(metric.notes)


def _merge_source_ids(*metrics: Optional[ResolvedMetricValue]) -> list[str]:
    """Merge unique source ids from one or more resolved metrics."""

    merged: list[str] = []
    for metric in metrics:
        if metric is None:
            continue
        for source_id in metric.source_ids:
            if source_id not in merged:
                merged.append(source_id)
    return merged


def _merge_evidence(*metrics: Optional[ResolvedMetricValue]) -> list:
    """Merge evidence references from one or more resolved metrics."""

    merged = []
    seen: set[str] = set()
    for metric in metrics:
        if metric is None:
            continue
        for evidence in metric.evidence_refs:
            if evidence.evidence_id in seen:
                continue
            seen.add(evidence.evidence_id)
            merged.append(evidence)
    return merged


def _looks_like_unit_issue(values: list[float]) -> bool:
    """Return True when the candidate values look off by common scale factors."""

    if len(values) < 2:
        return False
    smallest = min(abs(value) for value in values if value != 0)
    largest = max(abs(value) for value in values)
    if smallest == 0:
        return False
    ratio = largest / smallest
    return any(abs(ratio - factor) < 0.05 * factor for factor in (1000, 1_000_000))


def _sort_period_key(item: tuple[str, list[PnlExtractionRecord]]) -> tuple[date, str]:
    """Sort periods by start date when available and put undated content last."""

    period_key, records = item
    record = records[0]
    if record.period_start:
        return (record.period_start, period_key)
    return (date.max, period_key)

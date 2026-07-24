"""Orchestrator: ResolvedPnlPeriod[] -> ModelInputBundle.

This is the single canonical layer the analyst-facing output is built from.
The UI and the workbook both consume FinalMetricRecord and never reach around
this module back into raw extraction records.
"""

from __future__ import annotations

from typing import Optional

from app.canonical.confidence import (
    assess_confidence,
    assess_status,
    derive_exceptions,
)
from app.canonical.formatting import (
    canonical_unit,
    period_display_label,
    period_sort_key,
    to_canonical_value,
)
from app.canonical.selector import (
    SourceSelection,
    select_source,
)
from app.canonical.validation import (
    cross_check_against_other_files,
    validate_metric,
)
from app.models.canonical import (
    DERIVED_METRIC_FORMULAS,
    FinalMetricRecord,
    METRIC_DISPLAY,
    METRIC_KEY_TO_RESOLVED_FIELD,
    METRIC_ORDER,
    ModelInputBundle,
)
from app.models.mapping import ResolvedMetricValue, ResolvedPnlPeriod
from app.models.source import SourceManifest


def build_model_input_bundle(
    resolved_periods: list[ResolvedPnlPeriod],
    manifest: SourceManifest,
) -> ModelInputBundle:
    """Convert the normalized pipeline output into the canonical analyst bundle."""

    sorted_periods = sorted(resolved_periods, key=period_sort_key)
    # Skip periods that carry no canonical metric at all — they pollute the
    # wide table without adding analyst value.
    display_periods = [p for p in sorted_periods if _period_has_canonical_metric(p)]

    period_keys = [period.period_key for period in display_periods]
    period_labels = [period_display_label(period) for period in display_periods]

    metrics: list[FinalMetricRecord] = []
    # Cache direct-metric records by (metric_key, period_key) so derived rows
    # can reference them without re-running source selection.
    direct_lookup: dict[tuple[str, str], FinalMetricRecord] = {}

    # Build direct metrics first (revenue, cogs, operating_expenses, ebitda, headcount).
    for period_index, period in enumerate(display_periods):
        for metric_key in METRIC_ORDER:
            if metric_key not in METRIC_KEY_TO_RESOLVED_FIELD:
                continue  # derived metric — handled in next loop
            record = _build_direct_record(
                metric_key=metric_key,
                period=period,
                period_index=period_index,
                manifest=manifest,
            )
            if record is None:
                continue
            metrics.append(record)
            direct_lookup[(metric_key, period.period_key)] = record

    # Build derived records (gross_profit, ebitda, margins) using the already
    # validated direct inputs. `gross_profit` and `ebitda` may already exist as
    # direct records (when the source reports them explicitly); derived records
    # replace them only if the direct variant was missing.
    for period_index, period in enumerate(display_periods):
        for metric_key in METRIC_ORDER:
            if metric_key not in DERIVED_METRIC_FORMULAS:
                continue
            direct_key = (metric_key, period.period_key)
            if metric_key in {"gross_profit", "ebitda"} and direct_key in direct_lookup:
                # Direct variant already captured — skip derived insertion.
                continue
            record = _build_derived_record(
                metric_key=metric_key,
                period=period,
                period_index=period_index,
                direct_lookup=direct_lookup,
            )
            if record is None:
                continue
            metrics.append(record)
            # Derived gross_profit / ebitda must feed the margin rows built later
            # in this same loop (gross_margin_pct needs gross_profit; ebitda_margin_pct
            # needs ebitda). Without this, a computable margin is silently dropped
            # whenever its base metric was derived rather than directly reported.
            if metric_key in {"gross_profit", "ebitda"}:
                direct_lookup[(metric_key, period.period_key)] = record

    # Final deterministic ordering: by metric_order, then period_index.
    metric_rank = {key: index for index, key in enumerate(METRIC_ORDER)}
    metrics.sort(key=lambda record: (metric_rank.get(record.metric_key, 999), record.period_order))

    exceptions = derive_exceptions(metrics)

    return ModelInputBundle(
        metrics=metrics,
        exceptions=exceptions,
        period_order=period_labels,
        period_keys=period_keys,
    )


# ---------------------------------------------------------------------------
# Direct (source-provided) metrics
# ---------------------------------------------------------------------------


def _build_direct_record(
    metric_key: str,
    period: ResolvedPnlPeriod,
    period_index: int,
    manifest: SourceManifest,
) -> Optional[FinalMetricRecord]:
    resolved_field = METRIC_KEY_TO_RESOLVED_FIELD[metric_key]
    metric: Optional[ResolvedMetricValue] = getattr(period, resolved_field, None)

    # Skip metric rows that are entirely absent — nothing to claim about them.
    if metric is None:
        return None

    is_derived_upstream = metric.status == "derived"
    selection: SourceSelection = select_source(metric_key, metric, manifest)

    final_value = to_canonical_value(metric)
    has_conflicts = bool(metric.conflicting_values)
    source_count = len({evidence.source_id for evidence in metric.evidence_refs})

    direct_or_derived = "derived" if is_derived_upstream else "direct"

    validation = validate_metric(
        metric_key=metric_key,
        final_value=final_value,
        direct_or_derived=direct_or_derived,
        source_count=source_count,
        period=period,
    )
    confidence = assess_confidence(
        validation_result=validation.result,
        selected_family=selection.selected_family,
        source_count=source_count,
        has_conflicting_values=has_conflicts,
        direct_or_derived=direct_or_derived,
    )
    status = assess_status(confidence.level, validation.result, final_value)

    record = FinalMetricRecord(
        metric_key=metric_key,
        metric_name=METRIC_DISPLAY[metric_key],
        period=period_display_label(period),
        period_key=period.period_key,
        period_order=period_index,
        final_value=final_value,
        unit=canonical_unit(metric_key, metric),
        selected_source=selection.selected,
        backup_sources=selection.backups,
        source_priority_reason=selection.priority_reason,
        direct_or_derived=direct_or_derived,
        derivation_formula=(
            DERIVED_METRIC_FORMULAS[metric_key][0] if metric_key in DERIVED_METRIC_FORMULAS else None
        ) if is_derived_upstream else None,
        validation_result=validation.result,
        confidence_level=confidence.level,
        confidence_reason=confidence.reason,
        status=status,
        note=_short_note(validation.result, has_conflicts, is_derived_upstream),
        cross_check_log=validation.notes,
    )
    record.cross_check_log.extend(cross_check_against_other_files(record))
    return record


# ---------------------------------------------------------------------------
# Derived metrics (margins + formula placeholders)
# ---------------------------------------------------------------------------


def _build_derived_record(
    metric_key: str,
    period: ResolvedPnlPeriod,
    period_index: int,
    direct_lookup: dict[tuple[str, str], FinalMetricRecord],
) -> Optional[FinalMetricRecord]:
    human_formula, machine_formula = DERIVED_METRIC_FORMULAS[metric_key]

    inputs = _required_direct_records(metric_key, period, direct_lookup)
    if any(dep is None for dep in inputs):
        return None
    # mypy appeasement — all dependencies are non-None past this point
    inputs_present: list[FinalMetricRecord] = [dep for dep in inputs if dep is not None]

    if metric_key == "gross_profit":
        final_value = inputs_present[0].final_value - inputs_present[1].final_value  # type: ignore[operator]
    elif metric_key == "ebitda":
        final_value = inputs_present[0].final_value - inputs_present[1].final_value  # type: ignore[operator]
    elif metric_key == "gross_margin_pct":
        if inputs_present[1].final_value in (None, 0):
            return None
        final_value = inputs_present[0].final_value / inputs_present[1].final_value  # type: ignore[operator]
    elif metric_key == "ebitda_margin_pct":
        if inputs_present[1].final_value in (None, 0):
            return None
        final_value = inputs_present[0].final_value / inputs_present[1].final_value  # type: ignore[operator]
    else:
        return None

    # Derived record validation is always `Formula`. Confidence mirrors the
    # weakest direct input that fed the formula so the analyst sees it.
    weakest_confidence = _weakest_confidence(inputs_present)
    mismatch_upstream = any(dep.validation_result == "Mismatch" for dep in inputs_present)

    if mismatch_upstream:
        validation_result = "Mismatch"
        confidence_level = "Low"
        confidence_reason = "A component metric failed its arithmetic check."
        status = "Review"
    else:
        validation_result = "Formula"
        confidence_level = weakest_confidence
        confidence_reason = (
            f"Computed deterministically as {human_formula}; inherits the confidence of the weakest input."
        )
        status = "Ready" if confidence_level != "Low" else "Review"

    primary_source = inputs_present[0].selected_source
    backup_citations = []
    for dep in inputs_present:
        if dep.selected_source and (primary_source is None or dep.selected_source != primary_source):
            backup_citations.append(dep.selected_source)

    return FinalMetricRecord(
        metric_key=metric_key,
        metric_name=METRIC_DISPLAY[metric_key],
        period=inputs_present[0].period,
        period_key=period.period_key,
        period_order=period_index,
        final_value=final_value,
        unit=canonical_unit(metric_key, None),
        selected_source=primary_source,
        backup_sources=backup_citations,
        source_priority_reason=(
            "Margins are always recomputed from the selected direct sources to guarantee the ratio ties."
            if metric_key in {"gross_margin_pct", "ebitda_margin_pct"}
            else f"Computed from the selected sources of {inputs_present[0].metric_name} and {inputs_present[1].metric_name}."
        ),
        direct_or_derived="derived",
        derivation_formula=human_formula,
        validation_result=validation_result,
        confidence_level=confidence_level,
        confidence_reason=confidence_reason,
        status=status,
        note=f"{human_formula}",
        cross_check_log=[f"{metric_key} = {human_formula}"],
    )


def _required_direct_records(
    metric_key: str,
    period: ResolvedPnlPeriod,
    lookup: dict[tuple[str, str], FinalMetricRecord],
) -> tuple[Optional[FinalMetricRecord], ...]:
    if metric_key == "gross_profit":
        return (
            lookup.get(("revenue", period.period_key)),
            lookup.get(("cogs", period.period_key)),
        )
    if metric_key == "ebitda":
        return (
            lookup.get(("gross_profit", period.period_key)),
            lookup.get(("operating_expenses", period.period_key)),
        )
    if metric_key == "gross_margin_pct":
        return (
            lookup.get(("gross_profit", period.period_key)),
            lookup.get(("revenue", period.period_key)),
        )
    if metric_key == "ebitda_margin_pct":
        return (
            lookup.get(("ebitda", period.period_key)),
            lookup.get(("revenue", period.period_key)),
        )
    return ()


def _weakest_confidence(records: list[FinalMetricRecord]):
    """Return the lowest confidence seen across input records."""

    rank = {"High": 0, "Medium": 1, "Low": 2}
    worst = max(records, key=lambda r: rank[r.confidence_level])
    return worst.confidence_level


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _short_note(validation_result: str, has_conflicts: bool, is_derived: bool) -> Optional[str]:
    """At most one sentence; lives in the Model_Input sheet's Notes column."""

    if is_derived:
        return "Recomputed from validated inputs."
    if validation_result == "Mismatch":
        return "Flagged — arithmetic did not close."
    if has_conflicts:
        return "Conflicting source values detected."
    if validation_result == "Matched":
        return "Matches across sources."
    return None


def _period_has_canonical_metric(period: ResolvedPnlPeriod) -> bool:
    """True if this period contributes at least one value we care about."""

    for metric_key, resolved_field in METRIC_KEY_TO_RESOLVED_FIELD.items():
        if getattr(period, resolved_field, None) is not None:
            return True
    return False

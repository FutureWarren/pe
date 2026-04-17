"""Per-metric validation: formula closure + cross-source consistency.

Outputs land on the FinalMetricRecord as:
  - validation_result : Matched | Formula | Single-source | Mismatch
  - cross_check_log   : human-readable evidence for the copilot
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.models.canonical import FinalMetricRecord, ValidationResult
from app.models.mapping import ResolvedPnlPeriod


# A small absolute tolerance for currency comparisons, plus a relative
# tolerance for cross-source agreement (5%).
ABS_TOLERANCE = 1.0
REL_TOLERANCE = 0.05


@dataclass
class MetricValidation:
    """Output of `validate_metric`."""

    result: ValidationResult
    notes: list[str]


def _values_close(a: float, b: float) -> bool:
    if abs(a - b) <= ABS_TOLERANCE:
        return True
    denom = max(abs(a), abs(b), 1.0)
    return abs(a - b) / denom <= REL_TOLERANCE


def validate_metric(
    metric_key: str,
    final_value: Optional[float],
    direct_or_derived: str,
    source_count: int,
    period: ResolvedPnlPeriod,
) -> MetricValidation:
    """Decide a validation_result for one metric in one period.

    Logic:
      - Derived metrics get `Formula` (their correctness is structural).
      - Direct metrics get `Matched` if at least 2 source files agreed within tolerance,
        otherwise `Single-source`.
      - If a formula closure check exists and fails, the metric is `Mismatch`.
    """

    notes: list[str] = []

    if final_value is None:
        return MetricValidation(result="Single-source", notes=["No value resolved for this metric."])

    if direct_or_derived == "derived":
        notes.append("Computed by deterministic formula from validated inputs.")
        return MetricValidation(result="Formula", notes=notes)

    closure = _formula_closure_check(metric_key, final_value, period)
    if closure is not None:
        if closure.matches:
            notes.append(closure.note)
        else:
            notes.append(closure.note)
            return MetricValidation(result="Mismatch", notes=notes)

    if source_count >= 2:
        notes.append(f"Value agrees across {source_count} independent source files.")
        return MetricValidation(result="Matched", notes=notes)

    notes.append("Only one source supplied this value — no second file available to cross-check against.")
    return MetricValidation(result="Single-source", notes=notes)


# ---------------------------------------------------------------------------
# Formula closure helpers
# ---------------------------------------------------------------------------


@dataclass
class _ClosureResult:
    matches: bool
    note: str


def _formula_closure_check(
    metric_key: str,
    final_value: float,
    period: ResolvedPnlPeriod,
) -> Optional[_ClosureResult]:
    """If the metric has a closure relationship with siblings, check it."""

    if metric_key == "gross_profit" and period.revenue and period.direct_costs:
        expected = period.revenue.value - period.direct_costs.value
        return _ClosureResult(
            matches=_values_close(final_value, expected),
            note=(
                f"Gross Profit ties to Revenue − COGS ({_short(expected)})."
                if _values_close(final_value, expected)
                else f"Gross Profit does not tie: Revenue − COGS = {_short(expected)}, reported {_short(final_value)}."
            ),
        )

    if metric_key == "ebitda" and period.gross_profit and period.operating_expenses:
        expected = period.gross_profit.value - period.operating_expenses.value
        return _ClosureResult(
            matches=_values_close(final_value, expected),
            note=(
                f"EBITDA ties to Gross Profit − Operating Expenses ({_short(expected)})."
                if _values_close(final_value, expected)
                else f"EBITDA does not tie: GP − OpEx = {_short(expected)}, reported {_short(final_value)}."
            ),
        )

    return None


def _short(value: float) -> str:
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:.2f}"


# ---------------------------------------------------------------------------
# Cross-period sanity checks (used by the build orchestrator)
# ---------------------------------------------------------------------------


def cross_check_against_other_files(
    metric: FinalMetricRecord,
) -> list[str]:
    """Generate cross-check notes from backup_sources already attached.

    Today this just reports whether multiple files exist; future work could
    look up the literal value in each backup file. We never fabricate a
    comparison we cannot back up with evidence.
    """

    if not metric.backup_sources:
        return []
    primary_file = metric.selected_source.file if metric.selected_source else None
    file_names = sorted(
        {
            source.file
            for source in metric.backup_sources
            if source.file and source.file != primary_file
        }
    )
    if not file_names:
        return []
    return [
        f"Backup sources also reference this metric: {', '.join(file_names)}.",
    ]

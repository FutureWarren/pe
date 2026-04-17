from pathlib import Path

from app.models.extraction import ExtractionBundle
from app.models.mapping import ResolvedMetricValue, ResolvedPnlPeriod
from app.models.source import SourceManifest
from app.validate.reporting import build_validation_report


def _build_manifest() -> SourceManifest:
    return SourceManifest(
        data_room_dir=Path("/tmp/data_room"),
        indexed_at="2026-04-14T00:00:00Z",
        document_count=1,
        skipped_count=0,
        documents=[],
    )


def test_validation_skips_missing_core_warnings_for_kpi_only_ltm_period() -> None:
    report = build_validation_report(
        _build_manifest(),
        ExtractionBundle(schema_name="pnl_v1", record_count=1, records=[]),
        [
            ResolvedPnlPeriod(
                period_label="LTM 2025",
                period_key="LTM 2025",
                period_granularity="ltm",
                employee_count=ResolvedMetricValue(
                    value=101,
                    unit_scale="count",
                    currency=None,
                    status="provided",
                ),
            )
        ],
    )

    issue_codes = [issue.code for issue in report.issues]
    assert "missing_required_field" not in issue_codes


def test_validation_still_requires_core_inputs_for_monthly_periods() -> None:
    report = build_validation_report(
        _build_manifest(),
        ExtractionBundle(schema_name="pnl_v1", record_count=1, records=[]),
        [
            ResolvedPnlPeriod(
                period_label="Jan-2025",
                period_key="2025-01",
                period_granularity="month",
                period_start=None,
                period_end=None,
            )
        ],
    )

    missing_fields = [issue.context.get("field") for issue in report.issues if issue.code == "missing_required_field"]
    assert sorted(missing_fields) == ["direct_costs", "revenue"]

"""Regression tests for the deterministic unit / sign conventions.

These lock in three correctness fixes that are invisible to the happy-path
suite (which only exercises ``unit_scale="ones"``):

* ``to_canonical_value`` must treat ``unit_scale`` as a provenance label, not a
  factor to re-apply — otherwise every non-"ones" USD figure is inflated 1,000×
  or 1,000,000× (a "$5M" revenue rendering as $5 trillion).
* Cost-family metrics presented in parentheses (negative) must be stored as
  positive magnitudes so ``revenue - cost`` does not silently become
  ``revenue + cost``.
* The formula-closure check must not raise a false ``Mismatch`` on perfectly
  reconciling data reported in thousands/millions.
"""

from __future__ import annotations

from datetime import date

from app.canonical.formatting import to_canonical_value
from app.canonical.validation import validate_metric
from app.models.extraction import ExtractionBundle, MetricValue, PnlExtractionRecord
from app.models.mapping import ResolvedMetricValue, ResolvedPnlPeriod
from app.models.source import EvidenceRef, SourceDocument, SourceManifest
from app.normalize.pnl import resolve_statement_facts


def _rmv(value: float, unit_scale: str) -> ResolvedMetricValue:
    return ResolvedMetricValue(value=value, unit_scale=unit_scale, currency="USD")


def test_unit_scale_is_a_label_not_a_second_multiplier() -> None:
    # Values arrive already normalised to ones; the scale is provenance only.
    assert to_canonical_value(_rmv(5_000_000, "millions")) == 5_000_000
    assert to_canonical_value(_rmv(250_000, "thousands")) == 250_000
    assert to_canonical_value(_rmv(1_234, "ones")) == 1_234
    # Percent stays a ratio for Excel's 0.0% format.
    assert to_canonical_value(_rmv(0.37, "percent")) == 0.37


def test_gross_profit_closure_passes_for_millions_scaled_data() -> None:
    # Revenue 5.0M, COGS 2.0M, GP 3.0M — all reported "in millions" but stored
    # in ones. The closure check must tie, not flag a false Mismatch.
    period = ResolvedPnlPeriod(
        period_label="FY2024",
        period_key="FY2024",
        period_granularity="year",
        revenue=_rmv(5_000_000, "millions"),
        direct_costs=_rmv(2_000_000, "millions"),
    )
    gp_canonical = to_canonical_value(_rmv(3_000_000, "millions"))
    result = validate_metric(
        metric_key="gross_profit",
        final_value=gp_canonical,
        direct_or_derived="direct",
        source_count=1,
        period=period,
    )
    assert result.result != "Mismatch"


def test_parenthesized_cost_is_stored_as_positive_magnitude() -> None:
    manifest = SourceManifest(
        data_room_dir="/tmp/dr",  # type: ignore[arg-type]
        indexed_at="2026-03-30T00:00:00+00:00",
        document_count=1,
        skipped_count=0,
        documents=[
            SourceDocument(
                source_id="src-1",
                rel_path="pnl.csv",
                absolute_path="pnl.csv",  # type: ignore[arg-type]
                file_name="pnl.csv",
                extension=".csv",
                file_type="csv",
                modified_timestamp="2026-03-30T00:00:00+00:00",
                content_fingerprint="abc",
                document_role="qoe",
                parser_used="csv_reader",
                priority_rank=1,
            )
        ],
    )
    evidence = EvidenceRef(
        evidence_id="e-1",
        source_id="src-1",
        segment_id="seg-1",
        locator_label="pnl.csv CSV!A2:B2",
        quote="COGS | (400)",
        file_name="pnl.csv",
        extraction_method="heuristic",
        confidence=0.9,
    )
    bundle = ExtractionBundle(
        record_count=1,
        records=[
            PnlExtractionRecord(
                extraction_id="ext-1",
                source_id="src-1",
                source_file_name="pnl.csv",
                period_label="FY2024",
                period_key="FY2024",
                period_start=date(2024, 1, 1),
                period_end=date(2024, 12, 31),
                period_granularity="year",
                revenue=MetricValue(value=1000, raw_value="1000", unit_scale="ones", evidence_refs=[evidence]),
                # Presented as "(400)" → extractor negates to -400.
                direct_costs=MetricValue(value=-400, raw_value="(400)", unit_scale="ones", evidence_refs=[evidence]),
            )
        ],
    )

    resolved = resolve_statement_facts(bundle, manifest)

    assert len(resolved) == 1
    assert resolved[0].direct_costs is not None
    assert resolved[0].direct_costs.value == 400  # normalised to positive magnitude
    assert resolved[0].gross_profit is not None
    assert resolved[0].gross_profit.value == 600  # 1000 - 400, not 1000 + 400

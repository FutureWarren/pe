from datetime import date
from pathlib import Path

from app.models.extraction import ExtractionBundle, MetricValue, PnlExtractionRecord
from app.models.source import EvidenceRef, SourceDocument, SourceManifest
from app.normalize.pnl import resolve_statement_facts


def test_resolve_statement_facts_prefers_higher_priority_source_and_derives_gp() -> None:
    manifest = SourceManifest(
        data_room_dir=Path("/tmp/data_room"),
        indexed_at="2026-03-30T00:00:00+00:00",
        document_count=2,
        skipped_count=0,
        documents=[
            SourceDocument(
                source_id="src-high",
                rel_path="qoe.csv",
                absolute_path="qoe.csv",  # type: ignore[arg-type]
                file_name="qoe.csv",
                extension=".csv",
                file_type="csv",
                modified_timestamp="2026-03-30T00:00:00+00:00",
                content_fingerprint="abc",
                document_role="qoe",
                parser_used="csv_reader",
                priority_rank=1,
            ),
            SourceDocument(
                source_id="src-low",
                rel_path="deck.csv",
                absolute_path="deck.csv",  # type: ignore[arg-type]
                file_name="deck.csv",
                extension=".csv",
                file_type="csv",
                modified_timestamp="2026-03-30T00:00:00+00:00",
                content_fingerprint="def",
                document_role="board_deck",
                parser_used="csv_reader",
                priority_rank=4,
            ),
        ],
    )
    evidence = EvidenceRef(
        evidence_id="e-1",
        source_id="src-high",
        segment_id="seg-1",
        locator_label="qoe.csv CSV!A2:B2",
        quote="Revenue | 120",
        file_name="qoe.csv",
        extraction_method="heuristic",
        confidence=0.9,
    )
    bundle = ExtractionBundle(
        record_count=2,
        records=[
            PnlExtractionRecord(
                extraction_id="ext-1",
                source_id="src-high",
                source_file_name="qoe.csv",
                period_label="FY2024",
                period_key="FY2024",
                period_start=date(2024, 1, 1),
                period_end=date(2024, 12, 31),
                period_granularity="year",
                revenue=MetricValue(value=120, raw_value="120", unit_scale="ones", evidence_refs=[evidence]),
                direct_costs=MetricValue(value=45, raw_value="45", unit_scale="ones", evidence_refs=[evidence]),
            ),
            PnlExtractionRecord(
                extraction_id="ext-2",
                source_id="src-low",
                source_file_name="deck.csv",
                period_label="FY2024",
                period_key="FY2024",
                period_start=date(2024, 1, 1),
                period_end=date(2024, 12, 31),
                period_granularity="year",
                revenue=MetricValue(value=100, raw_value="100", unit_scale="ones", evidence_refs=[evidence]),
            ),
        ],
    )

    resolved = resolve_statement_facts(bundle, manifest)

    assert len(resolved) == 1
    assert resolved[0].revenue is not None
    assert resolved[0].revenue.value == 120
    assert resolved[0].revenue.conflicting_values == [100]
    assert resolved[0].gross_profit is not None
    assert resolved[0].gross_profit.status == "derived"

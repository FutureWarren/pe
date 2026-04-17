from pathlib import Path

from app.extract.pipeline import extract_statement_facts
from app.models.extraction import ExtractionBundle, MetricValue, PnlExtractionRecord
from app.models.source import EvidenceRef, SourceDocument, SourceManifest, SourceSegment


def test_extract_statement_facts_gemini_backend_uses_gemini_first_with_structured_fallback(
    monkeypatch,
) -> None:
    manifest = SourceManifest(
        data_room_dir=Path("/tmp/data_room"),
        indexed_at="2026-04-07T12:00:00Z",
        document_count=2,
        skipped_count=0,
        documents=[
            SourceDocument(
                source_id="src-csv",
                rel_path="demo.csv",
                absolute_path=Path("/tmp/demo.csv"),
                file_name="demo.csv",
                extension=".csv",
                file_type="csv",
                modified_timestamp="2026-04-07T12:00:00Z",
                content_fingerprint="csv123",
                document_role="monthly_fs",
                parser_used="csv",
                priority_rank=1,
            ),
            SourceDocument(
                source_id="src-pdf",
                rel_path="notes.pdf",
                absolute_path=Path("/tmp/notes.pdf"),
                file_name="notes.pdf",
                extension=".pdf",
                file_type="pdf",
                modified_timestamp="2026-04-07T12:00:00Z",
                content_fingerprint="pdf123",
                document_role="board_deck",
                parser_used="pypdf",
                priority_rank=2,
            ),
        ],
    )
    segments = [
        SourceSegment(
            segment_id="src-csv-CSV-2",
            source_id="src-csv",
            segment_type="csv_row",
            page_number=None,
            sheet_name="CSV",
            cell_range="A2:B2",
            section_name="CSV",
            row_number=2,
            locator_label="demo.csv CSV!A2:B2",
            content="Revenue | 120",
            parsed_artifact_path=None,
            metadata={
                "file_name": "demo.csv",
                "row_values": ["Revenue", "120"],
                "header_values": ["Metric", "FY2024"],
            },
        ),
        SourceSegment(
            segment_id="src-pdf-pdf-1",
            source_id="src-pdf",
            segment_type="page_text",
            page_number=1,
            sheet_name=None,
            cell_range=None,
            section_name=None,
            row_number=None,
            locator_label="notes.pdf p.1",
            content="Employee count FY2024: 110",
            parsed_artifact_path=None,
            metadata={"file_name": "notes.pdf"},
        ),
    ]

    def fake_gemini_extraction(
        _manifest: SourceManifest,
        _segments: list[SourceSegment],
    ) -> ExtractionBundle:
        return ExtractionBundle(
            schema_name="pnl_v1",
            record_count=1,
            records=[
                PnlExtractionRecord(
                    extraction_id="gemini-src-pdf-001",
                    source_id="src-pdf",
                    source_file_name="notes.pdf",
                    period_label="FY2024",
                    period_key="FY2024",
                    period_granularity="year",
                    employee_count=MetricValue(
                        value=110,
                        raw_value="110",
                        unit_scale="count",
                        currency=None,
                        confidence=0.92,
                        evidence_refs=[
                            EvidenceRef(
                                evidence_id="evidence-src-pdf-employee_count-1",
                                source_id="src-pdf",
                                segment_id="src-pdf-pdf-1",
                                locator_label="notes.pdf p.1",
                                quote="Employee count FY2024: 110",
                                file_name="notes.pdf",
                                page_number=1,
                                sheet_name=None,
                                cell_range=None,
                                section_name=None,
                                extraction_method="llm",
                                confidence=0.92,
                            )
                        ],
                    ),
                )
            ],
            assumptions=["Gemini extracted the PDF note but returned nothing for the CSV source."],
        )

    monkeypatch.setattr(
        "app.extract.pipeline.extract_statement_facts_with_gemini",
        fake_gemini_extraction,
    )

    bundle = extract_statement_facts(
        segments,
        manifest=manifest,
        backend="gemini",
    )

    assert bundle.record_count == 2
    by_source = {record.source_id: record for record in bundle.records}
    assert by_source["src-csv"].revenue is not None
    assert by_source["src-csv"].revenue.value == 120
    assert by_source["src-pdf"].employee_count is not None
    assert by_source["src-pdf"].employee_count.value == 110
    assert any(
        "Structured spreadsheet fallback was used only for sources where Gemini returned no P&L records."
        in item
        for item in bundle.assumptions
    )
    assert any("Gemini extracted the PDF note" in item for item in bundle.assumptions)
    assert any(
        "Gemini is the primary extraction interpreter" in item
        for item in bundle.assumptions
    )

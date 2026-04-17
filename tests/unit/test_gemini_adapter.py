from pathlib import Path

from app.extract.gemini import (
    GeminiEvidenceCandidate,
    GeminiMetricCandidate,
    GeminiPnlRecordCandidate,
    _build_document_prompt,
    _convert_response_records,
)
from app.models.source import SourceDocument, SourceSegment


def test_convert_response_records_preserves_traceability() -> None:
    document = SourceDocument(
        source_id="src-pdf",
        rel_path="demo.pdf",
        absolute_path=Path("/tmp/demo.pdf"),
        file_name="demo.pdf",
        extension=".pdf",
        file_type="pdf",
        modified_timestamp="2026-04-07T12:00:00Z",
        content_fingerprint="abc123",
        document_role="board_deck",
        parser_used="pypdf",
        priority_rank=2,
    )
    segments = [
        SourceSegment(
            segment_id="src-pdf-pdf-1",
            source_id="src-pdf",
            segment_type="page_text",
            page_number=1,
            sheet_name=None,
            cell_range=None,
            section_name=None,
            row_number=None,
            locator_label="demo.pdf p.1",
            content="Revenue FY2024 1.25m",
            parsed_artifact_path=None,
            metadata={"file_name": "demo.pdf"},
        )
    ]
    records = [
        GeminiPnlRecordCandidate(
            period_label="FY2024",
            period_key="FY2024",
            period_start="2024-01-01",
            period_end="2024-12-31",
            period_granularity="year",
            revenue=GeminiMetricCandidate(
                raw_value="$1.25m",
                normalized_value=1_250_000.0,
                unit_scale="millions",
                currency="USD",
                confidence=0.94,
                evidence=[
                    GeminiEvidenceCandidate(
                        locator_label="demo.pdf p.1",
                        quote="Revenue FY2024 1.25m",
                        page_number=1,
                        confidence=0.91,
                    )
                ],
            ),
            notes=["Explicitly reported in the board deck."],
        )
    ]

    converted = _convert_response_records(document, segments, records)

    assert len(converted) == 1
    record = converted[0]
    assert record.period_key == "FY2024"
    assert record.revenue is not None
    assert record.revenue.value == 1_250_000.0
    assert record.revenue.evidence_refs[0].locator_label == "demo.pdf p.1"
    assert record.revenue.evidence_refs[0].extraction_method == "llm"
    assert record.notes == ["Explicitly reported in the board deck."]


def test_build_document_prompt_guides_spreadsheet_aggregation() -> None:
    document = SourceDocument(
        source_id="src-xlsx",
        rel_path="demo.xlsx",
        absolute_path=Path("/tmp/demo.xlsx"),
        file_name="demo.xlsx",
        extension=".xlsx",
        file_type="xlsx",
        modified_timestamp="2026-04-07T12:00:00Z",
        content_fingerprint="xlsx123",
        document_role="qoe",
        parser_used="openpyxl",
        priority_rank=1,
    )
    segments = [
        SourceSegment(
            segment_id="src-xlsx-QoE Monthly P&L-6",
            source_id="src-xlsx",
            segment_type="sheet_row",
            page_number=None,
            sheet_name="QoE Monthly P&L",
            cell_range="B6:O6",
            section_name="QoE Monthly P&L",
            row_number=6,
            locator_label="demo.xlsx QoE Monthly P&L!B6:O6",
            content="Subscription revenue | 940000 | 955000",
            parsed_artifact_path=None,
            metadata={
                "file_name": "demo.xlsx",
                "row_values": ["", "Subscription revenue", "940000", "955000"],
                "header_values": ["", "", "Jan-2025", "Feb-2025"],
            },
        )
    ]

    prompt = _build_document_prompt(document, segments)

    assert "row_values" in prompt
    assert "header_values" in prompt
    assert "subtotal rows are blank" in prompt
    assert "aggregate those explicit component values into revenue, direct_costs, or operating_expenses" in prompt

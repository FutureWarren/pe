from app.extract.pipeline import extract_statement_facts
from app.models.source import SourceSegment


def test_extract_statement_facts_from_table_rows() -> None:
    segments = [
        SourceSegment(
            segment_id="seg-1",
            source_id="src-1",
            segment_type="csv_row",
            page_number=None,
            sheet_name="CSV",
            cell_range="A2:C2",
            section_name="CSV",
            row_number=2,
            locator_label="demo.csv CSV!A2:C2",
            content="Revenue | 100 | 120",
            parsed_artifact_path=None,
            metadata={
                "file_name": "demo.csv",
                "row_values": ["Revenue", "100", "120"],
                "header_values": ["Metric", "FY2023", "FY2024"],
            },
        ),
        SourceSegment(
            segment_id="seg-2",
            source_id="src-1",
            segment_type="csv_row",
            page_number=None,
            sheet_name="CSV",
            cell_range="A3:C3",
            section_name="CSV",
            row_number=3,
            locator_label="demo.csv CSV!A3:C3",
            content="COGS | 40 | 45",
            parsed_artifact_path=None,
            metadata={
                "file_name": "demo.csv",
                "row_values": ["COGS", "40", "45"],
                "header_values": ["Metric", "FY2023", "FY2024"],
            },
        ),
    ]

    bundle = extract_statement_facts(segments)

    assert bundle.record_count == 2
    by_period = {record.period_key: record for record in bundle.records}
    assert by_period["FY2023"].revenue is not None
    assert by_period["FY2023"].revenue.value == 100
    assert by_period["FY2024"].direct_costs is not None
    assert by_period["FY2024"].direct_costs.value == 45


def test_extract_statement_facts_from_text_prefers_metric_value_over_year() -> None:
    segments = [
        SourceSegment(
            segment_id="seg-text-1",
            source_id="src-2",
            segment_type="text_section",
            page_number=None,
            sheet_name=None,
            cell_range=None,
            section_name="section_1",
            row_number=None,
            locator_label="notes.txt section 1",
            content="Top customer concentration FY2024: 35%",
            parsed_artifact_path=None,
            metadata={"file_name": "notes.txt"},
        )
    ]

    bundle = extract_statement_facts(segments)

    assert bundle.record_count == 1
    record = bundle.records[0]
    assert record.period_key == "FY2024"
    assert record.customer_concentration_pct is not None
    assert record.customer_concentration_pct.value == 0.35

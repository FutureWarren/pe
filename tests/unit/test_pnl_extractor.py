from app.extract.pnl import extract_pnl_records
from app.models.source import SourceSegment


def test_extract_pnl_records_aggregates_component_rows_with_shifted_label_column() -> None:
    segments = [
        SourceSegment(
            segment_id="seg-1",
            source_id="src-qoe",
            segment_type="sheet_row",
            page_number=None,
            sheet_name="QoE Monthly P&L",
            cell_range="A5:D5",
            section_name="QoE Monthly P&L",
            row_number=5,
            locator_label="QoE Monthly P&L!A5:D5",
            content="Revenue ($)",
            parsed_artifact_path=None,
            metadata={
                "file_name": "HarborOps_QoE_Package.xlsx",
                "row_values": ["", "Revenue ($)", "", ""],
                "header_values": ["", "", "Jan-2025", "Feb-2025"],
            },
        ),
        SourceSegment(
            segment_id="seg-2",
            source_id="src-qoe",
            segment_type="sheet_row",
            page_number=None,
            sheet_name="QoE Monthly P&L",
            cell_range="A6:D6",
            section_name="QoE Monthly P&L",
            row_number=6,
            locator_label="QoE Monthly P&L!A6:D6",
            content="Subscription revenue",
            parsed_artifact_path=None,
            metadata={
                "file_name": "HarborOps_QoE_Package.xlsx",
                "row_values": ["", "Subscription revenue", "940000", "955000"],
                "header_values": ["", "", "Jan-2025", "Feb-2025"],
            },
        ),
        SourceSegment(
            segment_id="seg-3",
            source_id="src-qoe",
            segment_type="sheet_row",
            page_number=None,
            sheet_name="QoE Monthly P&L",
            cell_range="A7:D7",
            section_name="QoE Monthly P&L",
            row_number=7,
            locator_label="QoE Monthly P&L!A7:D7",
            content="Implementation services revenue",
            parsed_artifact_path=None,
            metadata={
                "file_name": "HarborOps_QoE_Package.xlsx",
                "row_values": ["", "Implementation services revenue", "145000", "150000"],
                "header_values": ["", "", "Jan-2025", "Feb-2025"],
            },
        ),
        SourceSegment(
            segment_id="seg-4",
            source_id="src-qoe",
            segment_type="sheet_row",
            page_number=None,
            sheet_name="QoE Monthly P&L",
            cell_range="A11:D11",
            section_name="QoE Monthly P&L",
            row_number=11,
            locator_label="QoE Monthly P&L!A11:D11",
            content="COGS ($)",
            parsed_artifact_path=None,
            metadata={
                "file_name": "HarborOps_QoE_Package.xlsx",
                "row_values": ["", "COGS ($)", "", ""],
                "header_values": ["", "", "Jan-2025", "Feb-2025"],
            },
        ),
        SourceSegment(
            segment_id="seg-5",
            source_id="src-qoe",
            segment_type="sheet_row",
            page_number=None,
            sheet_name="QoE Monthly P&L",
            cell_range="A12:D12",
            section_name="QoE Monthly P&L",
            row_number=12,
            locator_label="QoE Monthly P&L!A12:D12",
            content="Hosting & cloud infrastructure",
            parsed_artifact_path=None,
            metadata={
                "file_name": "HarborOps_QoE_Package.xlsx",
                "row_values": ["", "Hosting & cloud infrastructure", "118000", "121000"],
                "header_values": ["", "", "Jan-2025", "Feb-2025"],
            },
        ),
        SourceSegment(
            segment_id="seg-6",
            source_id="src-qoe",
            segment_type="sheet_row",
            page_number=None,
            sheet_name="QoE Monthly P&L",
            cell_range="A13:D13",
            section_name="QoE Monthly P&L",
            row_number=13,
            locator_label="QoE Monthly P&L!A13:D13",
            content="Implementation payroll",
            parsed_artifact_path=None,
            metadata={
                "file_name": "HarborOps_QoE_Package.xlsx",
                "row_values": ["", "Implementation payroll", "162000", "168000"],
                "header_values": ["", "", "Jan-2025", "Feb-2025"],
            },
        ),
        SourceSegment(
            segment_id="seg-7",
            source_id="src-qoe",
            segment_type="sheet_row",
            page_number=None,
            sheet_name="QoE Monthly P&L",
            cell_range="A19:D19",
            section_name="QoE Monthly P&L",
            row_number=19,
            locator_label="QoE Monthly P&L!A19:D19",
            content="Operating Expenses ($)",
            parsed_artifact_path=None,
            metadata={
                "file_name": "HarborOps_QoE_Package.xlsx",
                "row_values": ["", "Operating Expenses ($)", "", ""],
                "header_values": ["", "", "Jan-2025", "Feb-2025"],
            },
        ),
        SourceSegment(
            segment_id="seg-8",
            source_id="src-qoe",
            segment_type="sheet_row",
            page_number=None,
            sheet_name="QoE Monthly P&L",
            cell_range="A20:D20",
            section_name="QoE Monthly P&L",
            row_number=20,
            locator_label="QoE Monthly P&L!A20:D20",
            content="Sales & marketing",
            parsed_artifact_path=None,
            metadata={
                "file_name": "HarborOps_QoE_Package.xlsx",
                "row_values": ["", "Sales & marketing", "240000", "245000"],
                "header_values": ["", "", "Jan-2025", "Feb-2025"],
            },
        ),
        SourceSegment(
            segment_id="seg-9",
            source_id="src-qoe",
            segment_type="sheet_row",
            page_number=None,
            sheet_name="QoE Monthly P&L",
            cell_range="A21:D21",
            section_name="QoE Monthly P&L",
            row_number=21,
            locator_label="QoE Monthly P&L!A21:D21",
            content="Research & development",
            parsed_artifact_path=None,
            metadata={
                "file_name": "HarborOps_QoE_Package.xlsx",
                "row_values": ["", "Research & development", "142000", "145000"],
                "header_values": ["", "", "Jan-2025", "Feb-2025"],
            },
        ),
    ]

    bundle = extract_pnl_records(segments)

    by_period = {record.period_key: record for record in bundle.records}
    assert by_period["2025-01"].revenue is not None
    assert by_period["2025-01"].revenue.value == 1_085_000
    assert by_period["2025-01"].direct_costs is not None
    assert by_period["2025-01"].direct_costs.value == 280_000
    assert by_period["2025-01"].operating_expenses is not None
    assert by_period["2025-01"].operating_expenses.value == 382_000

    assert by_period["2025-02"].revenue is not None
    assert by_period["2025-02"].revenue.value == 1_105_000
    assert by_period["2025-02"].direct_costs is not None
    assert by_period["2025-02"].direct_costs.value == 289_000


def test_extract_pnl_records_ignores_unsupported_kpi_labels() -> None:
    bundle = extract_pnl_records(
        [
            SourceSegment(
                segment_id="seg-kpi",
                source_id="src-kpi",
                segment_type="csv_row",
                page_number=None,
                sheet_name="Operating KPIs",
                cell_range="A2:B2",
                section_name="Operating KPIs",
                row_number=2,
                locator_label="Operating KPIs!A2:B2",
                content="Net revenue retention | 1.08",
                parsed_artifact_path=None,
                metadata={
                    "file_name": "HarborOps_Operating_KPIs.csv",
                    "row_values": ["Net revenue retention", "1.08"],
                    "header_values": ["Metric", "2025"],
                },
            )
        ]
    )

    assert bundle.record_count == 0

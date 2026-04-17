"""Contracts for future structured extraction adapters.

The core rule for this project is that any model-backed extractor may emit
JSON-like records only. It may not write workbook cells or decide final
workbook mappings.
"""

from __future__ import annotations

from app.models.extraction import MetricValue, PnlExtractionRecord
from app.models.source import EvidenceRef


def extraction_contract_example() -> dict[str, object]:
    """Return a minimal example payload for future prompt engineering."""

    example = PnlExtractionRecord.model_construct(
        extraction_id="ext-0001",
        schema_name="pnl_v1",
        source_id="src-demo",
        source_file_name="demo_financials.csv",
        period_label="FY2024",
        period_key="FY2024",
        period_start="2024-01-01",  # type: ignore[arg-type]
        period_end="2024-12-31",  # type: ignore[arg-type]
        period_granularity="year",
        revenue=MetricValue.model_construct(
            value=1250000.0,
            raw_value="$1.25m",
            unit_scale="millions",
            currency="USD",
            confidence=0.95,
            evidence_refs=[
                EvidenceRef.model_construct(
                    evidence_id="evidence-0001",
                    source_id="src-demo",
                    segment_id="src-demo-CSV-2",
                    locator_label="demo_financials.csv CSV!A2:B2",
                    quote="Revenue | FY2024 | 1.25m",
                    file_name="demo_financials.csv",
                    extraction_method="heuristic",
                    confidence=0.95,
                )
            ],
        ),
        notes=["Example only."],
    )
    return example.model_dump(mode="json")

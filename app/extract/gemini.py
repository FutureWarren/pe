"""Gemini-backed extraction adapter for source documents.

This module keeps the LLM confined to structured extraction. Downstream
normalization, mapping, formula execution, and workbook writing remain fully
deterministic.
"""

from __future__ import annotations

from datetime import date
from hashlib import sha256
import json
from typing import Iterable, Literal, Optional

from pydantic import BaseModel, Field

from app.config import get_settings
from app.models.extraction import ExtractionBundle, MetricValue, PnlExtractionRecord
from app.models.source import EvidenceRef, SourceDocument, SourceManifest, SourceSegment

SUPPORTED_GEMINI_METRICS = (
    "revenue",
    "direct_costs",
    "gross_profit",
    "operating_expenses",
    "ebitda",
    "adjusted_ebitda",
    "customer_concentration_pct",
    "employee_count",
)


class GeminiEvidenceCandidate(BaseModel):
    """Structured evidence candidate returned by Gemini."""

    locator_label: str = Field(description="Exact locator label from the provided context when available.")
    quote: str = Field(description="Short verbatim quote or row text supporting the value.")
    page_number: Optional[int] = Field(default=None, description="Page number if the source is a PDF page.")
    sheet_name: Optional[str] = Field(default=None, description="Sheet or section name if available.")
    cell_range: Optional[str] = Field(default=None, description="Cell range if available.")
    section_name: Optional[str] = Field(default=None, description="Paragraph or section name if available.")
    confidence: float = Field(default=0.7, ge=0.0, le=1.0, description="Confidence in the evidence match.")


class GeminiMetricCandidate(BaseModel):
    """Structured metric candidate returned by Gemini."""

    raw_value: str = Field(description="Raw value string exactly as reported in the source.")
    normalized_value: Optional[float] = Field(
        default=None,
        description="Normalized numeric value after applying obvious unit handling.",
    )
    unit_scale: Literal["ones", "thousands", "millions", "percent", "count"] = Field(
        description="Unit scale for the normalized numeric value.",
    )
    currency: Optional[str] = Field(default="USD", description="Currency when the metric is monetary.")
    confidence: float = Field(default=0.8, ge=0.0, le=1.0, description="Confidence in the metric extraction.")
    evidence: list[GeminiEvidenceCandidate] = Field(
        default_factory=list,
        description="Supporting evidence references for this metric.",
    )


class GeminiPnlRecordCandidate(BaseModel):
    """One period-level P&L record candidate emitted by Gemini."""

    period_label: Optional[str] = Field(default=None, description="Human-readable period label like FY2024.")
    period_key: Optional[str] = Field(
        default=None,
        description="Stable period key like FY2024, Q1 2025, 2024-01, or UNDATED.",
    )
    period_start: Optional[str] = Field(default=None, description="ISO date when confidently inferable.")
    period_end: Optional[str] = Field(default=None, description="ISO date when confidently inferable.")
    period_granularity: Literal["month", "quarter", "year", "ltm", "unknown"] = Field(
        default="unknown",
        description="Best-fit period granularity.",
    )
    revenue: Optional[GeminiMetricCandidate] = None
    direct_costs: Optional[GeminiMetricCandidate] = None
    gross_profit: Optional[GeminiMetricCandidate] = None
    operating_expenses: Optional[GeminiMetricCandidate] = None
    ebitda: Optional[GeminiMetricCandidate] = None
    adjusted_ebitda: Optional[GeminiMetricCandidate] = None
    customer_concentration_pct: Optional[GeminiMetricCandidate] = None
    employee_count: Optional[GeminiMetricCandidate] = None
    notes: list[str] = Field(default_factory=list, description="Document-specific extraction notes.")
    uncertainty: list[str] = Field(
        default_factory=list,
        description="Ambiguities, conflicts, or extraction caveats that require human review.",
    )


class GeminiExtractionResponse(BaseModel):
    """Top-level structured extraction payload returned by Gemini."""

    assumptions: list[str] = Field(default_factory=list)
    records: list[GeminiPnlRecordCandidate] = Field(default_factory=list)


def extract_statement_facts_with_gemini(
    manifest: SourceManifest,
    segments: list[SourceSegment],
) -> ExtractionBundle:
    """Use Gemini to extract structured P&L records from the current source set."""

    settings = get_settings()
    if not settings.gemini_api_key:
        raise RuntimeError(
            "ANGELIC_GEMINI_API_KEY is not configured. Set it in .env or the shell before using Gemini extraction."
        )

    try:
        from google import genai
    except ImportError as exc:  # pragma: no cover - depends on local install state
        raise RuntimeError(
            "The google-genai package is not installed. Reinstall the project so Gemini extraction is available."
        ) from exc

    client = genai.Client(api_key=settings.gemini_api_key)
    segments_by_source: dict[str, list[SourceSegment]] = {}
    for segment in segments:
        segments_by_source.setdefault(segment.source_id, []).append(segment)

    records: list[PnlExtractionRecord] = []
    assumptions = [
        "Gemini extraction is restricted to structured JSON output only.",
        "Downstream normalization, formula execution, and workbook writing remain deterministic.",
        "Gemini should not derive final workbook numbers; derived metrics are handled later in Python.",
        "Gemini is allowed to interpret spreadsheet layouts as well as document-style sources.",
    ]

    for document in manifest.documents:
        document_segments = segments_by_source.get(document.source_id, [])
        try:
            response = _extract_document_with_gemini(
                client=client,
                model_name=settings.gemini_model,
                document=document,
                segments=document_segments,
            )
        except Exception as exc:
            assumptions.append(
                f"Gemini extraction failed for {document.file_name}: {_summarize_gemini_error(str(exc))}"
            )
            continue
        assumptions.extend(response.assumptions)
        records.extend(_convert_response_records(document, document_segments, response.records))

    return ExtractionBundle(
        schema_name="pnl_v1",
        record_count=len(records),
        records=records,
        assumptions=_dedupe_strings(assumptions),
    )


def _extract_document_with_gemini(
    client,
    model_name: str,
    document: SourceDocument,
    segments: list[SourceSegment],
) -> GeminiExtractionResponse:
    """Run Gemini on one document and return validated structured output."""

    prompt = _build_document_prompt(document=document, segments=segments)
    contents = _build_contents(client=client, document=document, prompt=prompt, segments=segments)
    response = client.models.generate_content(
        model=model_name,
        contents=contents,
        config={
            "temperature": 0,
            "response_mime_type": "application/json",
            "response_json_schema": GeminiExtractionResponse.model_json_schema(),
        },
    )

    text = getattr(response, "text", None)
    if not text:
        raise RuntimeError(f"Gemini returned an empty extraction response for {document.file_name}.")

    return GeminiExtractionResponse.model_validate_json(text)


def _build_contents(client, document: SourceDocument, prompt: str, segments: list[SourceSegment]):
    """Build Gemini contents for the target document."""

    if document.file_type == "pdf":
        uploaded_file = client.files.upload(file=document.absolute_path)
        return [uploaded_file, prompt]

    return prompt


def _build_document_prompt(document: SourceDocument, segments: list[SourceSegment]) -> str:
    """Build a deterministic extraction prompt for one source document."""

    context = _build_segment_context(segments)
    instructions = [
        "You are extracting structured P&L facts from one source document for a private equity databook pilot.",
        "Return JSON only and follow the provided schema exactly.",
        "Only extract values that are explicitly present in the source.",
        "Do not derive gross profit, EBITDA, margins, or any other formulas unless the document explicitly reports them.",
        "Keep separate reporting periods as separate records.",
        "Use the exact locator_label strings provided in the context when citing evidence.",
        "If a metric is not explicitly reported, leave it null.",
        "If the document is irrelevant to the supported metrics, return an empty records array and explain why in assumptions.",
        "Supported metrics are revenue, direct_costs, gross_profit, operating_expenses, ebitda, adjusted_ebitda, customer_concentration_pct, and employee_count.",
        "Use unit_scale values of ones, thousands, millions, percent, or count.",
        "Use period_key values like FY2024, Q1 2025, 2024-01, LTM 2024, or UNDATED when no period is explicit.",
        "For spreadsheet-style context, use row_values and header_values to determine which cells are labels, which cells are periods, and which cells are values.",
        "Do not assume the metric label is always in the first visible column.",
        "When a spreadsheet provides subtotal or total rows such as Total Revenue or Total COGS, prefer those rows as the canonical reported metric if they are explicitly present.",
        "Spreadsheet parser output may show formula-driven subtotal rows with blank numeric cells when cached workbook values are unavailable.",
        "If subtotal rows are blank but the sheet clearly contains component rows under a section like Revenue, Cost of Goods Sold, or Operating Expenses, you may aggregate those explicit component values into revenue, direct_costs, or operating_expenses for each period.",
        "Do not use this aggregation rule for gross_profit, ebitda, adjusted_ebitda, or margins; those should remain explicitly reported or be derived later by deterministic code.",
        "If a workbook includes detail rows plus a reported total, do not emit duplicate records for both unless they represent different supported metrics.",
        "Ignore unsupported KPI rows like ARR, NRR, CapEx, or average contract value rather than forcing them into the supported P&L schema.",
        "If a row is purely decorative, a section header, or metadata, ignore it.",
    ]

    metadata = {
        "file_name": document.file_name,
        "document_role": document.document_role,
        "file_type": document.file_type,
        "parser_used": document.parser_used,
        "source_id": document.source_id,
    }

    parts = [
        "\n".join(instructions),
        "Document metadata:",
        json.dumps(metadata, indent=2),
    ]
    if context:
        parts.extend(["Source locator context:", context])
    elif document.file_type != "pdf":
        parts.append("No parsed source segments were available for this document. Return an empty record set unless the raw file content is enough to extract a supported metric.")

    return "\n\n".join(parts)


def _build_segment_context(segments: Iterable[SourceSegment], max_segments: int = 120, max_chars: int = 48_000) -> str:
    """Serialize source segments into a compact prompt context with stable locators."""

    rendered: list[str] = []
    current_chars = 0

    for index, segment in enumerate(segments, start=1):
        if index > max_segments:
            rendered.append("... additional source segments omitted for brevity ...")
            break

        snippet = segment.content.strip().replace("\r", " ").replace("\n", " ")
        if len(snippet) > 800:
            snippet = f"{snippet[:800]} ..."
        row_values = segment.metadata.get("row_values")
        header_values = segment.metadata.get("header_values")
        line = (
            f"- locator_label: {segment.locator_label}\n"
            f"  segment_type: {segment.segment_type}\n"
            f"  page_number: {segment.page_number}\n"
            f"  sheet_name: {segment.sheet_name}\n"
            f"  cell_range: {segment.cell_range}\n"
            f"  section_name: {segment.section_name}\n"
            f"  content: {snippet}"
        )
        if row_values:
            line += f"\n  row_values: {json.dumps(row_values[:20])}"
        if header_values:
            line += f"\n  header_values: {json.dumps(header_values[:20])}"
        if current_chars + len(line) > max_chars:
            rendered.append("... source context truncated to stay within prompt limits ...")
            break
        rendered.append(line)
        current_chars += len(line)

    return "\n".join(rendered)


def _convert_response_records(
    document: SourceDocument,
    segments: list[SourceSegment],
    records: list[GeminiPnlRecordCandidate],
) -> list[PnlExtractionRecord]:
    """Convert Gemini's structured response into deterministic extraction records."""

    if not records:
        return []

    segment_by_locator = {segment.locator_label: segment for segment in segments}
    page_fallbacks = {
        segment.page_number: segment
        for segment in segments
        if segment.page_number is not None
    }
    default_segment = segments[0] if segments else None

    converted: list[PnlExtractionRecord] = []
    for index, record in enumerate(records, start=1):
        extraction_record = PnlExtractionRecord(
            extraction_id=f"gemini-{document.source_id}-{index:03d}",
            source_id=document.source_id,
            source_file_name=document.file_name,
            period_label=record.period_label,
            period_key=record.period_key,
            period_start=_parse_date(record.period_start),
            period_end=_parse_date(record.period_end),
            period_granularity=record.period_granularity,
            notes=record.notes,
            uncertainty=record.uncertainty,
        )

        for metric_name in SUPPORTED_GEMINI_METRICS:
            candidate = getattr(record, metric_name)
            if candidate is None or candidate.normalized_value is None:
                continue
            metric = MetricValue(
                value=candidate.normalized_value,
                raw_value=candidate.raw_value,
                unit_scale=candidate.unit_scale,
                currency=candidate.currency,
                confidence=candidate.confidence,
                evidence_refs=[
                    _build_evidence_ref(
                        document=document,
                        evidence=evidence,
                        metric_name=metric_name,
                        segment_by_locator=segment_by_locator,
                        page_fallbacks=page_fallbacks,
                        default_segment=default_segment,
                    )
                    for evidence in candidate.evidence
                ],
            )
            setattr(extraction_record, metric_name, metric)

        converted.append(extraction_record)

    return converted


def _build_evidence_ref(
    document: SourceDocument,
    evidence: GeminiEvidenceCandidate,
    metric_name: str,
    segment_by_locator: dict[str, SourceSegment],
    page_fallbacks: dict[int, SourceSegment],
    default_segment: Optional[SourceSegment],
) -> EvidenceRef:
    """Convert Gemini evidence into a deterministic evidence reference."""

    matched_segment = segment_by_locator.get(evidence.locator_label)
    if matched_segment is None and evidence.page_number is not None:
        matched_segment = page_fallbacks.get(evidence.page_number)
    if matched_segment is None:
        matched_segment = default_segment

    locator_label = (
        matched_segment.locator_label
        if matched_segment is not None
        else evidence.locator_label or document.file_name
    )
    segment_id = matched_segment.segment_id if matched_segment else f"{document.source_id}-unmatched-{metric_name}"
    evidence_seed = f"{document.source_id}|{metric_name}|{locator_label}|{evidence.quote}"
    evidence_hash = sha256(evidence_seed.encode("utf-8")).hexdigest()[:12]
    page_number = (
        evidence.page_number
        if evidence.page_number is not None
        else matched_segment.page_number if matched_segment is not None else None
    )
    sheet_name = (
        evidence.sheet_name
        or matched_segment.sheet_name
        if matched_segment is not None
        else evidence.sheet_name
    )
    cell_range = (
        evidence.cell_range
        or matched_segment.cell_range
        if matched_segment is not None
        else evidence.cell_range
    )
    section_name = (
        evidence.section_name
        or matched_segment.section_name
        if matched_segment is not None
        else evidence.section_name
    )

    return EvidenceRef(
        evidence_id=f"evidence-{document.source_id}-{metric_name}-{evidence_hash}",
        source_id=document.source_id,
        segment_id=segment_id,
        locator_label=locator_label,
        quote=evidence.quote,
        file_name=document.file_name,
        page_number=page_number,
        sheet_name=sheet_name,
        cell_range=cell_range,
        section_name=section_name,
        extraction_method="llm",
        confidence=evidence.confidence,
    )


def _parse_date(value: Optional[str]) -> Optional[date]:
    """Parse an ISO date emitted by Gemini when present."""

    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _dedupe_strings(values: Iterable[str]) -> list[str]:
    """Return first-seen unique strings while preserving order."""

    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _summarize_gemini_error(message: str) -> str:
    """Return a short Gemini failure summary for run artifacts."""

    normalized = message.lower()
    if "503 unavailable" in normalized or "high demand" in normalized:
        return "Gemini was temporarily unavailable due to high demand."
    if "resource_exhausted" in normalized or "quota" in normalized:
        return "Gemini quota was exhausted for the configured key or project."
    if "gemini_api_key is not configured" in normalized:
        return "Gemini API key is missing from the backend environment."
    return message.strip()

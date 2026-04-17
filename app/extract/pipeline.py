"""Schema-dispatched extraction stage for structured P&L records."""

from __future__ import annotations

from typing import Optional

from app.extract.gemini import extract_statement_facts_with_gemini
from app.extract.pnl import extract_pnl_records
from app.models.extraction import ExtractionBundle
from app.models.run import ExtractionBackend
from app.models.source import SourceManifest
from app.models.source import SourceSegment


def extract_statement_facts(
    segments: list[SourceSegment],
    *,
    manifest: Optional[SourceManifest] = None,
    backend: ExtractionBackend = "deterministic",
) -> ExtractionBundle:
    """Extract normalized P&L-focused JSON records from parsed segments.

    The current pilot uses deterministic heuristics so the repo is runnable
    without an LLM. A future model-backed extractor should conform to the same
    `ExtractionBundle` contract.
    """

    if backend == "gemini":
        if manifest is None:
            raise ValueError("A source manifest is required for Gemini-backed extraction.")
        gemini_bundle = extract_statement_facts_with_gemini(manifest, segments)
        fallback_bundle = _build_structured_fallback_bundle(
            manifest=manifest,
            segments=segments,
            gemini_bundle=gemini_bundle,
        )

        records = list(gemini_bundle.records)
        assumptions = list(gemini_bundle.assumptions)

        if fallback_bundle is not None:
            records.extend(fallback_bundle.records)
            assumptions.extend(fallback_bundle.assumptions)
            assumptions.append(
                "Structured spreadsheet fallback was used only for sources where Gemini returned no P&L records."
            )

        assumptions.append(
            "Gemini is the primary extraction interpreter; deterministic logic remains responsible for normalization, formulas, validation, and workbook writing."
        )

        return ExtractionBundle(
            schema_name="pnl_v1",
            record_count=len(records),
            records=records,
            assumptions=_dedupe_assumptions(assumptions),
        )

    return extract_pnl_records(segments)


def _build_structured_fallback_bundle(
    *,
    manifest: SourceManifest,
    segments: list[SourceSegment],
    gemini_bundle: ExtractionBundle,
) -> Optional[ExtractionBundle]:
    """Fallback to deterministic spreadsheet extraction only when Gemini returned nothing for a structured source."""

    structured_source_ids = {
        document.source_id
        for document in manifest.documents
        if document.file_type in {"csv", "xlsx", "xls"}
    }
    if not structured_source_ids:
        return None

    gemini_source_ids = {record.source_id for record in gemini_bundle.records}
    fallback_source_ids = structured_source_ids - gemini_source_ids
    if not fallback_source_ids:
        return None

    fallback_segments = [
        segment for segment in segments if segment.source_id in fallback_source_ids
    ]
    if not fallback_segments:
        return None

    return extract_pnl_records(fallback_segments)


def _dedupe_assumptions(values: list[str]) -> list[str]:
    """Return first-seen assumptions while preserving order."""

    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped

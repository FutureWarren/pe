"""P&L-focused deterministic extraction helpers for the narrow pilot."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from typing import Literal, Optional

from app.models.extraction import ExtractionBundle, MetricValue, PnlExtractionRecord
from app.models.source import EvidenceRef, SourceSegment

MONTH_INDEX = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
METRIC_PATTERNS = {
    "revenue": [r"\brevenue\b", r"\bnet sales\b", r"\bsales revenue\b", r"\btotal sales\b", r"\bturnover\b"],
    "direct_costs": [r"\bcogs\b", r"\bcost of goods sold\b", r"\bcost of sales\b", r"\bdirect costs?\b"],
    "gross_profit": [r"\bgross profit\b"],
    "operating_expenses": [r"\boperating expenses?\b", r"\bopex\b", r"\bsg&a\b"],
    "adjusted_ebitda": [r"\badjusted ebitda\b"],
    "ebitda": [r"\bebitda\b"],
    "customer_concentration_pct": [
        r"\bcustomer concentration\b",
        r"\bclient concentration\b",
        r"\btop customer\b",
        r"\btop client\b",
    ],
    "employee_count": [r"\bemployee count\b", r"\bheadcount\b", r"\bemployees\b", r"\bfte\b"],
}
VALUE_PATTERN = re.compile(
    r"(?P<value>\(?\$?\-?\d[\d,]*\.?\d*\)?\s*(?:%|k|K|m|M|mm|MM)?)"
)


@dataclass
class CandidateValue:
    """Internal candidate metric value before record grouping."""

    source_id: str
    source_file_name: str
    metric_name: str
    period_label: Optional[str]
    period_key: Optional[str]
    period_start: Optional[date]
    period_end: Optional[date]
    period_granularity: str
    metric_value: MetricValue
    note: Optional[str]
    aggregation_role: Literal["reported", "component"] = "reported"


def extract_pnl_records(segments: list[SourceSegment]) -> ExtractionBundle:
    """Extract normalized P&L records from parsed source segments."""

    candidates: list[CandidateValue] = []
    structured_segments = [
        segment for segment in segments if segment.segment_type in {"sheet_row", "csv_row", "docx_table_row"}
    ]
    text_segments = [
        segment for segment in segments if segment.segment_type in {"page_text", "docx_paragraph", "text_section"}
    ]

    candidates.extend(_extract_candidates_from_structured_segments(structured_segments))
    for segment in text_segments:
        candidates.extend(_extract_from_text_segment(segment))

    records = _group_candidates_into_records(candidates)
    assumptions = [
        "Current extraction is heuristic and optimized for spreadsheet-like rows plus simple text patterns.",
        "Customer concentration and employee count may be captured without a precise reporting period.",
        "If no explicit units are present, extracted numeric values are treated as ones.",
    ]
    return ExtractionBundle(schema_name="pnl_v1", record_count=len(records), records=records, assumptions=assumptions)


def _extract_candidates_from_structured_segments(segments: list[SourceSegment]) -> list[CandidateValue]:
    """Extract candidates from structured rows while tracking section context."""

    if not segments:
        return []

    ordered_segments = sorted(
        segments,
        key=lambda segment: (
            segment.source_id,
            segment.sheet_name or "",
            segment.row_number or 0,
            segment.segment_id,
        ),
    )
    section_by_group: dict[tuple[str, str], Optional[str]] = {}
    candidates: list[CandidateValue] = []

    for segment in ordered_segments:
        group_key = (segment.source_id, segment.sheet_name or "")
        current_section = section_by_group.get(group_key)
        next_section = _detect_section_metric(segment, current_section)
        if next_section is not None:
            section_by_group[group_key] = next_section
            current_section = next_section

        candidates.extend(_extract_from_table_row(segment, current_section))

    return candidates


def _extract_from_table_row(segment: SourceSegment, current_section: Optional[str]) -> list[CandidateValue]:
    """Extract metrics from a structured row with optional header values."""

    row_values: list[str] = segment.metadata.get("row_values", [])
    header_values: list[str] = segment.metadata.get("header_values", [])
    if not row_values:
        return []

    label_index = _find_label_index(row_values, header_values)
    if label_index is None:
        return []

    label = str(row_values[label_index]).strip()
    if not label:
        return []

    metric_name = _match_metric(label)
    aggregation_role: Literal["reported", "component"] = "reported"
    if metric_name is None and current_section in {"revenue", "direct_costs", "operating_expenses"}:
        metric_name = current_section
        aggregation_role = "component"
    elif (
        metric_name is not None
        and current_section in {"revenue", "direct_costs", "operating_expenses"}
        and metric_name == current_section
        and not _is_total_like_label(label)
    ):
        aggregation_role = "component"
    if not metric_name:
        return []

    candidates: list[CandidateValue] = []
    for index in range(label_index + 1, len(row_values)):
        raw_value = row_values[index]
        raw_value_text = str(raw_value).strip()
        if not raw_value_text or _looks_like_period_header(raw_value_text):
            continue
        parsed_metric = _parse_metric_value(raw_value, metric_name)
        if parsed_metric is None:
            continue

        header = header_values[index] if index < len(header_values) else None
        period_info = _parse_period_info(header or label)
        evidence = _build_evidence_ref(segment, quote=f"{label} | {raw_value}")
        parsed_metric.evidence_refs.append(evidence)
        candidates.append(
            CandidateValue(
                source_id=segment.source_id,
                source_file_name=segment.metadata.get("file_name", ""),
                metric_name=metric_name,
                period_label=period_info["label"],
                period_key=period_info["key"],
                period_start=period_info["start"],
                period_end=period_info["end"],
                period_granularity=period_info["granularity"],
                metric_value=parsed_metric,
                note=(
                    None
                    if period_info["key"]
                    else "No explicit period found; stored as undated."
                ),
                aggregation_role=aggregation_role,
            )
        )

    if not candidates:
        inline_metric = _parse_metric_value(" ".join(row_values[1:]), metric_name)
        if inline_metric is None:
            return []
        evidence = _build_evidence_ref(segment, quote=segment.content[:200])
        inline_metric.evidence_refs.append(evidence)
        period_info = _parse_period_info(label)
        candidates.append(
            CandidateValue(
                source_id=segment.source_id,
                source_file_name=segment.metadata.get("file_name", ""),
                metric_name=metric_name,
                period_label=period_info["label"],
                period_key=period_info["key"],
                period_start=period_info["start"],
                period_end=period_info["end"],
                period_granularity=period_info["granularity"],
                metric_value=inline_metric,
                note=None,
                aggregation_role=aggregation_role,
            )
        )

    return candidates


def _extract_from_text_segment(segment: SourceSegment) -> list[CandidateValue]:
    """Extract metrics from free-form text using narrow regex heuristics."""

    candidates: list[CandidateValue] = []
    lines = [line.strip() for line in segment.content.splitlines() if line.strip()]
    for line in lines:
        metric_name = _match_metric(line)
        if not metric_name:
            continue
        metric_value = _parse_metric_value(line, metric_name)
        if metric_value is None:
            continue
        period_info = _parse_period_info(line)
        evidence = _build_evidence_ref(segment, quote=line[:200])
        metric_value.evidence_refs.append(evidence)
        candidates.append(
            CandidateValue(
                source_id=segment.source_id,
                source_file_name=segment.metadata.get("file_name", ""),
                metric_name=metric_name,
                period_label=period_info["label"],
                period_key=period_info["key"],
                period_start=period_info["start"],
                period_end=period_info["end"],
                period_granularity=period_info["granularity"],
                metric_value=metric_value,
                note=None if period_info["key"] else "Text metric had no explicit period and was stored as undated.",
            )
        )
    return candidates


def _group_candidates_into_records(candidates: list[CandidateValue]) -> list[PnlExtractionRecord]:
    """Group candidate values into one JSON record per source and period."""

    grouped: dict[tuple[str, str], PnlExtractionRecord] = {}
    metric_candidates: dict[tuple[str, str, str], list[CandidateValue]] = {}
    sequence = 0
    for candidate in candidates:
        period_key = candidate.period_key or "UNDATED"
        period_label = candidate.period_label or "Undated"
        key = (candidate.source_id, period_key)
        if key not in grouped:
            sequence += 1
            grouped[key] = PnlExtractionRecord(
                extraction_id=f"ext-{sequence:04d}",
                source_id=candidate.source_id,
                source_file_name=candidate.source_file_name,
                period_label=period_label,
                period_key=period_key,
                period_start=candidate.period_start,
                period_end=candidate.period_end,
                period_granularity=candidate.period_granularity if period_key != "UNDATED" else "unknown",
            )
        metric_candidates.setdefault((candidate.source_id, period_key, candidate.metric_name), []).append(candidate)
        if candidate.note and candidate.note not in grouped[key].notes:
            grouped[key].notes.append(candidate.note)

    for (source_id, period_key, metric_name), metric_group in metric_candidates.items():
        record = grouped[(source_id, period_key)]
        resolved_metric = _resolve_metric_group(metric_group)
        if resolved_metric is None:
            record.uncertainty.append(
                f"Multiple values found for {metric_name} in source {metric_group[0].source_file_name}."
            )
            record.conflicting_values.setdefault(
                metric_name,
                [candidate.metric_value.raw_value for candidate in metric_group],
            )
            continue
        setattr(record, metric_name, resolved_metric)

    return list(grouped.values())


def _resolve_metric_group(metric_group: list[CandidateValue]) -> Optional[MetricValue]:
    """Resolve one metric across one source-period group.

    Reported totals win. If only component rows exist for a summable family, sum
    them into one deterministic input.
    """

    if not metric_group:
        return None

    reported = [candidate for candidate in metric_group if candidate.aggregation_role == "reported"]
    component = [candidate for candidate in metric_group if candidate.aggregation_role == "component"]

    if reported:
        first_value = reported[0].metric_value.value
        if all(abs(candidate.metric_value.value - first_value) < 0.0001 for candidate in reported):
            metric = reported[0].metric_value.model_copy(deep=True)
            for candidate in reported[1:]:
                metric.evidence_refs.extend(candidate.metric_value.evidence_refs)
            return metric
        return None

    if component:
        total_value = sum(candidate.metric_value.value for candidate in component)
        metric = component[0].metric_value.model_copy(
            update={
                "value": total_value,
                "raw_value": str(int(total_value) if float(total_value).is_integer() else total_value),
                "evidence_refs": [],
                "confidence": min(candidate.metric_value.confidence for candidate in component),
            },
            deep=True,
        )
        for candidate in component:
            metric.evidence_refs.extend(candidate.metric_value.evidence_refs)
        return metric

    return None


def _match_metric(text: str) -> Optional[str]:
    """Return the metric key that best matches the provided text."""

    normalized = text.strip().lower()
    if _is_unsupported_kpi_label(normalized):
        return None
    for metric_name, patterns in METRIC_PATTERNS.items():
        if any(re.search(pattern, normalized) for pattern in patterns):
            return metric_name
    return None


def _find_label_index(row_values: list[str], header_values: list[str]) -> Optional[int]:
    """Return the index that most likely contains the row label."""

    for index, raw_value in enumerate(row_values):
        value = str(raw_value).strip()
        if not value:
            continue
        header = header_values[index].strip().lower() if index < len(header_values) and isinstance(header_values[index], str) else ""
        if _looks_like_period_header(value) or _looks_like_period_header(header):
            continue
        if _parse_metric_value(value, "revenue") is not None and _match_metric(value) is None:
            continue
        return index
    return None


def _detect_section_metric(segment: SourceSegment, current_section: Optional[str]) -> Optional[str]:
    """Detect a section header metric for structured spreadsheet rows."""

    row_values: list[str] = segment.metadata.get("row_values", [])
    text = " | ".join(str(value).strip() for value in row_values if str(value).strip()).lower()
    if not text:
        return current_section

    if "revenue" in text and "$" in text:
        return "revenue"
    if ("cogs" in text or "cost of goods sold" in text or "direct costs" in text) and "$" in text:
        return "direct_costs"
    if ("operating expenses" in text or "opex" in text) and "$" in text:
        return "operating_expenses"
    return current_section


def _looks_like_period_header(value: str) -> bool:
    """Return whether a cell looks like a period header rather than a row label."""

    info = _parse_period_info(value)
    return info["key"] is not None


def _is_total_like_label(label: str) -> bool:
    """Return whether a row label looks like a reported total/subtotal line."""

    normalized = label.strip().lower()
    return any(token in normalized for token in ("total", "subtotal", "reported"))


def _is_unsupported_kpi_label(normalized: str) -> bool:
    """Return whether a label is a KPI we intentionally exclude from the narrow P&L schema."""

    return any(
        token in normalized
        for token in (
            "net revenue retention",
            "nrr",
            "gross revenue retention",
            "grr",
            "customer churn",
            "logo churn",
            "arr",
            "annual recurring revenue",
            "capex",
            "capital expenditure",
            "capital expenditures",
        )
    )


def _parse_metric_value(raw_text: str, metric_name: str) -> Optional[MetricValue]:
    """Parse a numeric token from text into a normalized metric value."""

    matches = list(VALUE_PATTERN.finditer(raw_text))
    if not matches:
        return None

    raw_value = matches[-1].group("value").strip()
    cleaned = raw_value.replace("$", "").replace(",", "").replace(" ", "")
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    if negative:
        cleaned = cleaned[1:-1]

    unit_scale: str = "ones"
    lower_cleaned = cleaned.lower()
    if lower_cleaned.endswith("%"):
        unit_scale = "percent"
        cleaned = cleaned[:-1]
    elif lower_cleaned.endswith("mm"):
        unit_scale = "millions"
        cleaned = cleaned[:-2]
    elif lower_cleaned.endswith("m"):
        unit_scale = "millions"
        cleaned = cleaned[:-1]
    elif lower_cleaned.endswith("k"):
        unit_scale = "thousands"
        cleaned = cleaned[:-1]

    try:
        value = float(cleaned)
    except ValueError:
        return None

    if negative:
        value *= -1

    if unit_scale == "percent":
        value = value / 100
    elif unit_scale == "thousands":
        value = value * 1_000
    elif unit_scale == "millions":
        value = value * 1_000_000
    if metric_name == "employee_count":
        unit_scale = "count"

    return MetricValue(
        value=value,
        raw_value=raw_value,
        unit_scale=unit_scale,
        currency="USD" if metric_name != "employee_count" and unit_scale != "percent" else None,
        evidence_refs=[],
        confidence=0.7,
    )


def _build_evidence_ref(segment: SourceSegment, quote: str) -> EvidenceRef:
    """Build a stable evidence reference for a candidate extraction."""

    digest = sha256(f"{segment.segment_id}:{quote}".encode("utf-8")).hexdigest()[:12]
    return EvidenceRef(
        evidence_id=f"evidence-{digest}",
        source_id=segment.source_id,
        segment_id=segment.segment_id,
        locator_label=segment.locator_label,
        quote=quote,
        file_name=segment.metadata.get("file_name", ""),
        page_number=segment.page_number,
        sheet_name=segment.sheet_name,
        cell_range=segment.cell_range,
        section_name=segment.section_name,
        extraction_method="heuristic",
        confidence=0.7,
    )


def _parse_period_info(raw_text: Optional[str]) -> dict[str, object]:
    """Parse period metadata from a header or inline text."""

    if not raw_text:
        return {"label": None, "key": None, "start": None, "end": None, "granularity": "unknown"}

    text = raw_text.strip()
    lowered = text.lower()

    if match := re.search(r"fy\s*(\d{4})", lowered):
        year = int(match.group(1))
        return {
            "label": f"FY{year}",
            "key": f"FY{year}",
            "start": date(year, 1, 1),
            "end": date(year, 12, 31),
            "granularity": "year",
        }

    if match := re.search(r"\bq([1-4])\s*(\d{4})\b", lowered):
        quarter = int(match.group(1))
        year = int(match.group(2))
        quarter_month = (quarter - 1) * 3 + 1
        end_month = quarter_month + 2
        end_day = 31 if end_month in {3, 12} else 30
        if end_month == 6:
            end_day = 30
        if end_month == 9:
            end_day = 30
        return {
            "label": f"Q{quarter} {year}",
            "key": f"{year}-Q{quarter}",
            "start": date(year, quarter_month, 1),
            "end": date(year, end_month, end_day),
            "granularity": "quarter",
        }

    if match := re.search(
        r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*[\s\-_\/]+(\d{4})\b",
        lowered,
    ):
        month = MONTH_INDEX[match.group(1)]
        year = int(match.group(2))
        end_day = _month_end_day(year, month)
        label = f"{year:04d}-{month:02d}"
        return {
            "label": label,
            "key": label,
            "start": date(year, month, 1),
            "end": date(year, month, end_day),
            "granularity": "month",
        }

    if match := re.search(r"\b(\d{4})[-_/](\d{2})\b", lowered):
        year = int(match.group(1))
        month = int(match.group(2))
        end_day = _month_end_day(year, month)
        label = f"{year:04d}-{month:02d}"
        return {
            "label": label,
            "key": label,
            "start": date(year, month, 1),
            "end": date(year, month, end_day),
            "granularity": "month",
        }

    if match := re.search(r"\b(20\d{2})\b", lowered):
        year = int(match.group(1))
        return {
            "label": str(year),
            "key": str(year),
            "start": date(year, 1, 1),
            "end": date(year, 12, 31),
            "granularity": "year",
        }

    return {"label": None, "key": None, "start": None, "end": None, "granularity": "unknown"}


def _month_end_day(year: int, month: int) -> int:
    """Return the last day of a month without external dependencies."""

    if month == 2:
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        return 29 if leap else 28
    if month in {4, 6, 9, 11}:
        return 30
    return 31

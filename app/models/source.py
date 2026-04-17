"""Source tracking models used throughout the pilot pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

DocumentRole = Literal[
    "qoe",
    "audited_fs",
    "monthly_fs",
    "board_deck",
    "other",
]
FileType = Literal["pdf", "xlsx", "xls", "csv", "docx", "txt"]
SegmentType = Literal[
    "page_text",
    "sheet_row",
    "csv_row",
    "docx_paragraph",
    "docx_table_row",
    "text_section",
    "file_note",
]


class SourceDocument(BaseModel):
    """Represents a source file discovered in the data room."""

    source_id: str
    rel_path: str
    absolute_path: Path
    file_name: str
    extension: str
    file_type: FileType
    modified_timestamp: str
    content_fingerprint: str
    document_role: DocumentRole
    parser_used: str
    priority_rank: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceSegment(BaseModel):
    """Represents a trackable portion of a source document."""

    segment_id: str
    source_id: str
    segment_type: SegmentType
    page_number: Optional[int]
    sheet_name: Optional[str]
    cell_range: Optional[str]
    section_name: Optional[str]
    row_number: Optional[int]
    locator_label: str
    content: str
    parsed_artifact_path: Optional[Path]
    metadata: dict[str, Any] = Field(default_factory=dict)


class SkippedFile(BaseModel):
    """Represents a file that was discovered but intentionally skipped."""

    rel_path: str
    extension: str
    reason: str


class EvidenceRef(BaseModel):
    """Represents evidence used to support an extracted value."""

    evidence_id: str
    source_id: str
    segment_id: str
    locator_label: str
    quote: str
    file_name: str
    page_number: Optional[int] = None
    sheet_name: Optional[str] = None
    cell_range: Optional[str] = None
    section_name: Optional[str] = None
    extraction_method: Literal["heuristic", "parser", "llm"]
    confidence: float


class SourceManifest(BaseModel):
    """Represents the discovered source inputs for a pipeline run."""

    data_room_dir: Path
    indexed_at: str
    document_count: int
    skipped_count: int
    documents: list[SourceDocument]
    skipped_files: list[SkippedFile] = Field(default_factory=list)

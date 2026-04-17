"""Run request, payload, and run summary models for the pilot pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.models.canonical import ModelInputBundle
from app.models.extraction import ExtractionBundle
from app.models.mapping import ResolvedPnlPeriod, SourceMapEntry, WorkbookCellBinding
from app.models.source import SourceManifest
from app.models.validation import ValidationReport

ExtractionBackend = Literal["deterministic", "gemini"]


class RunRequest(BaseModel):
    """Input contract for a local pilot run."""

    data_room_dir: Path
    extraction_backend: ExtractionBackend = "deterministic"
    ai_workbook_path: Optional[Path] = None
    gold_workbook_path: Optional[Path] = None
    template_workbook_path: Optional[Path] = None
    output_root: Optional[Path] = None
    run_label: Optional[str] = Field(
        default=None,
        description="Optional human-readable label for this run (e.g. import batch name).",
    )


class PilotRunSummary(BaseModel):
    """High-level metadata describing one pipeline execution."""

    run_id: str
    created_at: str
    status: Literal["completed", "failed"]
    output_dir: Path
    workbook_path: Path
    extraction_backend: ExtractionBackend = "deterministic"
    artifact_paths: dict[str, Path] = Field(default_factory=dict)
    input_paths: dict[str, Optional[Path]] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    run_label: Optional[str] = None
    validation_status: Optional[Literal["pass", "warning", "fail"]] = None
    issue_count: int = 0
    document_count: int = 0


class PilotRunPayload(BaseModel):
    """Rich run payload for frontend and local console consumers."""

    summary: PilotRunSummary
    source_manifest: SourceManifest
    extraction_bundle: ExtractionBundle
    resolved_periods: list[ResolvedPnlPeriod]
    workbook_bindings: list[WorkbookCellBinding] = Field(default_factory=list)
    source_map_entries: list[SourceMapEntry] = Field(default_factory=list)
    validation_report: ValidationReport
    # Canonical analyst-facing output. The frontend consumes this directly
    # and never reaches past it into the raw extraction or resolved arrays.
    # Named ``analyst_bundle`` because pydantic v2 reserves the ``model_`` prefix.
    analyst_bundle: Optional[ModelInputBundle] = Field(default=None, alias="analyst_bundle")

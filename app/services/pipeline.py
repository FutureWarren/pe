"""End-to-end orchestration for the narrow P&L pilot pipeline."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Optional

from app.canonical.build import build_model_input_bundle
from app.config import get_settings
from app.export.analyst_workbook import write_analyst_workbook
from app.export.workbook import write_scaffold_workbook
from app.extract.pipeline import extract_statement_facts
from app.ingest.manifest import build_source_manifest
from app.ingest.parsers import parse_documents
from app.map.cell_rules import build_pnl_workbook_bindings
from app.models.canonical import ModelInputBundle
from app.models.extraction import ExtractionBundle
from app.models.mapping import ResolvedPnlPeriod, SourceMapEntry, WorkbookCellBinding
from app.models.run import PilotRunPayload, PilotRunSummary, RunRequest
from app.models.source import SourceManifest
from app.models.validation import ValidationReport
from app.normalize.pnl import resolve_statement_facts
from app.services.run_store import RUN_SUMMARY_FILE_NAME, create_run_directory, write_json, write_markdown
from app.validate.reporting import build_validation_report, render_validation_markdown


def run_pilot(request: RunRequest) -> PilotRunSummary:
    """Execute the current scaffold pipeline and persist run artifacts.

    The current implementation is intentionally narrow and deterministic. It is
    designed for a live demo walkthrough, not broad platform scope.
    """

    settings = get_settings()
    output_root = (request.output_root or settings.output_dir).resolve()
    run_id, run_dir = create_run_directory(output_root)

    manifest = build_source_manifest(request.data_room_dir)
    segments = parse_documents(manifest)
    extraction_bundle = extract_statement_facts(
        segments,
        manifest=manifest,
        backend=request.extraction_backend,
    )
    resolved_periods = resolve_statement_facts(extraction_bundle, manifest)
    cell_bindings, source_map_entries = build_pnl_workbook_bindings(resolved_periods)
    validation_report = build_validation_report(manifest, extraction_bundle, resolved_periods)

    # Canonical analyst-facing bundle. Drives Model_Input / Exceptions /
    # Source_Map and the copilot API. The UI consumes this and never the raw
    # extraction bundle.
    analyst_bundle = build_model_input_bundle(resolved_periods, manifest)

    registry_path = write_json(run_dir / "source_registry.json", manifest.model_dump(mode="json"))
    segments_path = write_json(
        run_dir / "source_segments.json",
        [segment.model_dump(mode="json") for segment in segments],
    )
    extraction_path = write_json(
        run_dir / "extracted_pnl.json",
        extraction_bundle.model_dump(mode="json"),
    )
    resolved_path = write_json(
        run_dir / "resolved_pnl.json",
        [value.model_dump(mode="json") for value in resolved_periods],
    )
    workbook_plan_path = write_json(
        run_dir / "workbook_bindings.json",
        [binding.model_dump(mode="json") for binding in cell_bindings],
    )
    source_map_path = write_json(
        run_dir / "source_map_entries.json",
        [entry.model_dump(mode="json") for entry in source_map_entries],
    )
    validation_json_path = write_json(
        run_dir / "validation_report.json",
        validation_report.model_dump(mode="json"),
    )
    validation_md_path = write_markdown(
        run_dir / "validation_report.md",
        render_validation_markdown(validation_report),
    )
    analyst_bundle_path = write_json(
        run_dir / "analyst_bundle.json",
        analyst_bundle.model_dump(mode="json"),
    )
    workbook_path = write_scaffold_workbook(
        run_dir / "legacy_workbook.xlsx",
        template_workbook_path=request.template_workbook_path,
        manifest=manifest,
        cell_bindings=cell_bindings,
        source_map_entries=source_map_entries,
        validation_report=validation_report,
    )
    analyst_workbook_path = write_analyst_workbook(
        run_dir / "generated_workbook.xlsx",
        bundle=analyst_bundle,
    )

    notes = [
        "Pilot pipeline completed.",
        "Workbook export is deterministic and source-linked.",
        f"Extraction backend used: {request.extraction_backend}.",
        "LLM use, when enabled, is limited to structured extraction only.",
        "Analyst-facing workbook follows Model_Input / Exceptions / Source_Map schema.",
        "No model is allowed to write workbook cells directly.",
    ]

    summary = PilotRunSummary(
        run_id=run_id,
        created_at=datetime.now(tz=timezone.utc).isoformat(),
        status="completed",
        output_dir=run_dir,
        workbook_path=analyst_workbook_path,
        extraction_backend=request.extraction_backend,
        run_label=request.run_label,
        validation_status=validation_report.status,
        issue_count=validation_report.issue_count,
        document_count=manifest.document_count,
        artifact_paths={
            "source_registry": registry_path,
            "source_segments": segments_path,
            "extracted_pnl": extraction_path,
            "resolved_pnl": resolved_path,
            "workbook_bindings": workbook_plan_path,
            "source_map_entries": source_map_path,
            "validation_json": validation_json_path,
            "validation_markdown": validation_md_path,
            "analyst_bundle": analyst_bundle_path,
            "legacy_workbook": workbook_path,
            "generated_workbook": analyst_workbook_path,
        },
        input_paths={
            "data_room_dir": request.data_room_dir,
            "ai_workbook_path": request.ai_workbook_path,
            "gold_workbook_path": request.gold_workbook_path,
            "template_workbook_path": request.template_workbook_path,
            "output_root": output_root,
        },
        notes=notes,
    )
    summary_path = run_dir / RUN_SUMMARY_FILE_NAME
    summary.artifact_paths["run_summary"] = summary_path
    write_json(summary_path, summary.model_dump(mode="json"))
    return summary


def build_run_payload(summary: PilotRunSummary) -> PilotRunPayload:
    """Load structured run artifacts into one frontend-friendly payload."""

    analyst_bundle_path = summary.artifact_paths.get("analyst_bundle")
    analyst_bundle: Optional[ModelInputBundle] = None
    if analyst_bundle_path is not None:
        path_obj = Path(analyst_bundle_path)
        if path_obj.exists():
            analyst_bundle = _load_model(path_obj, ModelInputBundle)

    return PilotRunPayload(
        summary=summary,
        source_manifest=_load_model(summary.artifact_paths["source_registry"], SourceManifest),
        extraction_bundle=_load_model(summary.artifact_paths["extracted_pnl"], ExtractionBundle),
        resolved_periods=_load_model_list(summary.artifact_paths["resolved_pnl"], ResolvedPnlPeriod),
        workbook_bindings=_load_model_list(
            summary.artifact_paths["workbook_bindings"],
            WorkbookCellBinding,
        ),
        source_map_entries=_load_model_list(
            summary.artifact_paths.get("source_map_entries"),
            SourceMapEntry,
        ),
        validation_report=_load_model(summary.artifact_paths["validation_json"], ValidationReport),
        analyst_bundle=analyst_bundle,
    )


def _load_model(path: Path, model_cls):
    """Read one JSON artifact and validate it into a typed model."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return model_cls.model_validate(payload)


def _load_model_list(path: Optional[Path], model_cls):
    """Read a JSON array artifact and validate it into typed models."""

    if path is None:
        return []

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return [model_cls.model_validate(item) for item in payload]

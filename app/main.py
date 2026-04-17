"""FastAPI entrypoint for the local Angelic pilot."""

from __future__ import annotations

from mimetypes import guess_type
from pathlib import Path
import re
from typing import Optional
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel
import uvicorn

from app.canonical.explain import QuestionType, explain
from app.config import get_settings
from app.models.run import ExtractionBackend, PilotRunPayload, PilotRunSummary, RunRequest
from app.services.console import render_console_html
from app.services.pipeline import build_run_payload, run_pilot
from app.services.run_store import list_run_summaries, load_run_summary


class ExplainRequest(BaseModel):
    """Input payload for the grounded copilot endpoint."""

    metric_key: str
    period_key: str
    question: QuestionType = "summary"


class ExplainResponse(BaseModel):
    """Output payload for the grounded copilot endpoint."""

    answer: str
    metric_key: str
    period_key: str
    question: QuestionType
    confidence_level: Optional[str] = None
    status: Optional[str] = None
    selected_source_file: Optional[str] = None
    selected_source_tab: Optional[str] = None
    selected_source_range: Optional[str] = None

app = FastAPI(
    title="Angelic Pilot API",
    version="0.1.0",
    summary="Local pilot API for deterministic PE databook P&L automation.",
)


@app.get("/")
def root() -> RedirectResponse:
    """Redirect the root path to the dedicated console URL."""

    return RedirectResponse(url="/angelic-pilot", status_code=307)


@app.get("/angelic-pilot", response_class=HTMLResponse)
def console() -> str:
    """Render the minimal local console."""

    settings = get_settings()
    return render_console_html(str(settings.default_data_room))


@app.get("/health")
def health() -> dict[str, str]:
    """Return a lightweight health payload for local smoke checks."""

    settings = get_settings()
    return {
        "status": "ok",
        "environment": settings.env,
        "output_dir": str(settings.output_dir),
    }


@app.get("/runs", response_model=list[PilotRunSummary])
def get_runs(limit: int = Query(100, ge=1, le=500)) -> list[PilotRunSummary]:
    """Return recent run summaries for the local console and history views."""

    settings = get_settings()
    return list_run_summaries(settings.output_dir.resolve(), limit=limit)


@app.get("/runs/{run_id}", response_model=PilotRunSummary)
def get_run(run_id: str) -> PilotRunSummary:
    """Return one run summary by run id."""

    settings = get_settings()
    summary = load_run_summary(settings.output_dir.resolve() / run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return summary


@app.get("/runs/{run_id}/payload", response_model=PilotRunPayload)
def get_run_payload(run_id: str) -> PilotRunPayload:
    """Return one run plus its structured artifacts."""

    settings = get_settings()
    summary = load_run_summary(settings.output_dir.resolve() / run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return build_run_payload(summary)


@app.post("/runs", response_model=PilotRunSummary)
def create_run(request: RunRequest) -> PilotRunSummary:
    """Execute a local pilot run.

    TODO: Add asynchronous execution and persistent run status tracking if the
    narrow pilot proves valuable and run times become noticeable.
    """

    return _run_request_or_http_error(request)


@app.post("/runs/upload", response_model=PilotRunPayload)
async def create_run_from_uploads(
    files: list[UploadFile] = File(...),
    import_label: str = Form("Uploaded dataroom"),
    extraction_backend: ExtractionBackend = Form("gemini"),
) -> PilotRunPayload:
    """Persist uploaded files locally, run the pilot, and return structured artifacts."""

    if not files:
        raise HTTPException(status_code=400, detail="At least one file is required.")

    settings = get_settings()
    staging_dir = _create_upload_staging_dir(settings.output_dir.resolve(), import_label)
    for upload in files:
        await _persist_upload(upload, staging_dir)

    summary = _run_request_or_http_error(
        RunRequest(
            data_room_dir=staging_dir,
            extraction_backend=extraction_backend,
            output_root=settings.output_dir.resolve(),
            run_label=import_label,
        ),
    )
    try:
        return build_run_payload(summary)
    except HTTPException:
        raise
    except Exception as exc:
        detail, status_code = _classify_pipeline_error(str(exc))
        raise HTTPException(status_code=status_code, detail=detail) from exc


@app.post("/runs/{run_id}/explain", response_model=ExplainResponse)
def explain_metric(run_id: str, payload: ExplainRequest) -> ExplainResponse:
    """Return a grounded, provenance-backed explanation for one metric/period.

    The answer is assembled entirely from the FinalMetricRecord in the run's
    analyst bundle. No free-form generation is involved.
    """

    settings = get_settings()
    summary = load_run_summary(settings.output_dir.resolve() / run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    run_payload = build_run_payload(summary)
    bundle = run_payload.analyst_bundle
    if bundle is None:
        raise HTTPException(status_code=404, detail="This run has no analyst bundle — rerun with the current pipeline.")

    match = next(
        (
            record
            for record in bundle.metrics
            if record.metric_key == payload.metric_key and record.period_key == payload.period_key
        ),
        None,
    )
    if match is None:
        raise HTTPException(
            status_code=404,
            detail=f"No canonical record found for metric={payload.metric_key} period={payload.period_key}.",
        )

    answer = explain(match, payload.question)
    return ExplainResponse(
        answer=answer,
        metric_key=match.metric_key,
        period_key=match.period_key,
        question=payload.question,
        confidence_level=match.confidence_level,
        status=match.status,
        selected_source_file=match.selected_source.file if match.selected_source else None,
        selected_source_tab=match.selected_source.tab if match.selected_source else None,
        selected_source_range=match.selected_source.range if match.selected_source else None,
    )


@app.get("/runs/{run_id}/artifacts/{artifact_key}")
def get_run_artifact(run_id: str, artifact_key: str) -> FileResponse:
    """Serve a run artifact for download or inline viewing."""

    settings = get_settings()
    summary = load_run_summary(settings.output_dir.resolve() / run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    if artifact_key not in summary.artifact_paths:
        raise HTTPException(status_code=404, detail="Artifact not found.")

    artifact_path = Path(summary.artifact_paths[artifact_key]).resolve()
    output_root = settings.output_dir.resolve()
    try:
        artifact_path.relative_to(output_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Artifact path is outside the output directory.") from exc

    if not artifact_path.exists():
        raise HTTPException(status_code=404, detail="Artifact file does not exist.")

    media_type = guess_type(artifact_path.name)[0] or "application/octet-stream"
    return FileResponse(artifact_path, media_type=media_type, filename=artifact_path.name)


def serve() -> None:
    """Run the local API with the configured host and port."""

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
    )


def _create_upload_staging_dir(output_root: Path, import_label: str) -> Path:
    """Create a stable local staging directory for browser-uploaded files."""

    slug = re.sub(r"[^a-z0-9]+", "-", import_label.lower()).strip("-") or "uploaded-dataroom"
    staging_dir = output_root / "_uploads" / f"{slug}-{uuid4().hex[:8]}"
    staging_dir.mkdir(parents=True, exist_ok=True)
    return staging_dir


async def _persist_upload(upload: UploadFile, staging_dir: Path) -> Path:
    """Write one uploaded file into the staging directory."""

    safe_name = Path(upload.filename or f"upload-{uuid4().hex[:6]}").name
    target_path = _dedupe_path(staging_dir / safe_name)
    target_path.write_bytes(await upload.read())
    await upload.close()
    return target_path


def _dedupe_path(path: Path) -> Path:
    """Return a non-conflicting path for duplicate file names."""

    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    index = 2

    while True:
        candidate = parent / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def _run_request_or_http_error(request: RunRequest) -> PilotRunSummary:
    """Run the pilot and convert backend failures into explicit API errors."""

    try:
        return run_pilot(request)
    except HTTPException:
        raise
    except Exception as exc:
        detail, status_code = _classify_pipeline_error(str(exc))
        raise HTTPException(status_code=status_code, detail=detail) from exc


def _classify_pipeline_error(message: str) -> tuple[str, int]:
    """Translate known pipeline failures into frontend-friendly API errors."""

    normalized = message.lower()

    if "gemini_api_key is not configured" in normalized:
        return (
            "The Python backend does not have a Gemini API key loaded. Update /Users/futurewarren/Desktop/Angelic/.env and restart `angelic-api`.",
            500,
        )

    if "503 unavailable" in normalized or "high demand" in normalized:
        return (
            "Gemini is temporarily unavailable due to high demand. Please wait a minute and run the import again.",
            503,
        )

    if "quota" in normalized or "resource_exhausted" in normalized:
        return (
            "Gemini request quota is exhausted for the current key or project. Check billing/quota in Google AI Studio and try again.",
            429,
        )

    return (message or "The backend pipeline failed unexpectedly.", 500)

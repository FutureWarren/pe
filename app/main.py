"""FastAPI entrypoint for the local Angelic pilot."""

from __future__ import annotations

import logging
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

logger = logging.getLogger("angelic.api")

# Upload guardrails. The endpoint accepts confidential financial files but must
# not let a caller exhaust memory/disk with an unbounded number or size of files.
MAX_UPLOAD_FILES = 200
MAX_UPLOAD_BYTES_PER_FILE = 100 * 1024 * 1024  # 100 MB
MAX_UPLOAD_BYTES_TOTAL = 500 * 1024 * 1024  # 500 MB
ALLOWED_UPLOAD_EXTENSIONS = {".csv", ".xlsx", ".xlsm", ".xls", ".pdf", ".docx", ".txt"}


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
    return _build_run_payload_or_http_error(summary)


@app.post("/runs", response_model=PilotRunSummary)
def create_run(request: RunRequest) -> PilotRunSummary:
    """Execute a local pilot run.

    TODO: Add asynchronous execution and persistent run status tracking if the
    narrow pilot proves valuable and run times become noticeable.
    """

    _validate_run_request_paths(request)
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
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(
            status_code=413,
            detail=f"Too many files ({len(files)}). The limit is {MAX_UPLOAD_FILES} per import.",
        )

    settings = get_settings()
    staging_dir = _create_upload_staging_dir(settings.output_dir.resolve(), import_label)
    total_bytes = 0
    for upload in files:
        ext = Path(upload.filename or "").suffix.lower()
        if ext not in ALLOWED_UPLOAD_EXTENSIONS:
            raise HTTPException(
                status_code=415,
                detail=f"Unsupported file type '{ext or upload.filename}'. Allowed: "
                + ", ".join(sorted(ALLOWED_UPLOAD_EXTENSIONS)),
            )
        total_bytes += await _persist_upload(upload, staging_dir, total_so_far=total_bytes)

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
    run_payload = _build_run_payload_or_http_error(summary)
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


async def _persist_upload(upload: UploadFile, staging_dir: Path, total_so_far: int = 0) -> int:
    """Stream one uploaded file into the staging directory; return its byte size.

    Reads in bounded chunks (never the whole file into memory at once) and
    enforces both a per-file and a cumulative size cap, deleting the partial
    file and raising 413 if either is exceeded.
    """

    safe_name = Path(upload.filename or f"upload-{uuid4().hex[:6]}").name
    target_path = _dedupe_path(staging_dir / safe_name)
    written = 0
    try:
        with target_path.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES_PER_FILE:
                    raise HTTPException(
                        status_code=413,
                        detail=f"'{safe_name}' exceeds the per-file limit of "
                        f"{MAX_UPLOAD_BYTES_PER_FILE // (1024 * 1024)} MB.",
                    )
                if total_so_far + written > MAX_UPLOAD_BYTES_TOTAL:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Total upload exceeds the limit of "
                        f"{MAX_UPLOAD_BYTES_TOTAL // (1024 * 1024)} MB.",
                    )
                handle.write(chunk)
    except HTTPException:
        target_path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
    return written


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


def _validate_run_request_paths(request: RunRequest) -> None:
    """Reject filesystem paths that escape the permitted local directories.

    The raw POST /runs endpoint takes filesystem paths. Unconstrained, a caller
    could point data_room_dir at /home or output_root at /etc — arbitrary file
    read (exfiltrated via /runs/{id}/payload) and arbitrary write. Confine every
    supplied path to the app working tree or the configured output directory.
    (The CLI calls run_pilot directly and is unaffected.)
    """

    settings = get_settings()
    # Permitted roots: the app working tree, the output directory (where uploads
    # are staged), and the configured data-room root. Anything else — /etc,
    # another user's home, an unrelated deal folder — is rejected.
    allowed_roots = [
        Path.cwd().resolve(),
        settings.output_dir.resolve(),
        settings.default_data_room.resolve(),
    ]

    def _check(value: Optional[Path], label: str) -> None:
        if value is None:
            return
        resolved = Path(value).resolve()
        if not any(_is_within(resolved, root) for root in allowed_roots):
            raise HTTPException(
                status_code=400,
                detail=f"{label} is outside the permitted directories.",
            )

    _check(request.data_room_dir, "data_room_dir")
    _check(request.output_root, "output_root")
    _check(request.template_workbook_path, "template_workbook_path")
    _check(request.ai_workbook_path, "ai_workbook_path")
    _check(request.gold_workbook_path, "gold_workbook_path")


def _is_within(path: Path, root: Path) -> bool:
    """Return True if ``path`` is ``root`` or a descendant of it."""

    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _build_run_payload_or_http_error(summary: PilotRunSummary) -> PilotRunPayload:
    """Assemble a run payload, converting missing/corrupt artifacts into a 4xx.

    Older or partially-deleted runs may lack an expected artifact key or file;
    without this guard those surface as an opaque 500 on a run the /runs list
    still links to.
    """

    try:
        return build_run_payload(summary)
    except HTTPException:
        raise
    except (KeyError, FileNotFoundError, ValueError) as exc:
        logger.warning("Run %s artifacts unavailable: %s", summary.run_id, exc)
        raise HTTPException(
            status_code=410,
            detail="This run's artifacts are unavailable or incomplete — rerun the import.",
        ) from exc


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
            "The backend does not have a Gemini API key configured. Set ANGELIC_GEMINI_API_KEY "
            "in the environment and restart the API.",
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

    # Do not echo the raw exception text to the client — it can leak filesystem
    # paths, library internals, or fragments of the confidential input. Log the
    # detail server-side; return a generic message.
    logger.warning("Pipeline failure: %s", message)
    return ("The backend pipeline failed while processing this data room.", 500)

"""Build deterministic source manifests from a local data room folder."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from app.models.source import SkippedFile, SourceDocument, SourceManifest

SUPPORTED_FILE_TYPES = {
    ".csv": "csv",
    ".docx": "docx",
    ".pdf": "pdf",
    ".txt": "txt",
    ".xls": "xls",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
}

ROLE_PRIORITY = {
    "qoe": 1,
    "audited_fs": 2,
    "monthly_fs": 3,
    "board_deck": 4,
    "other": 5,
}


def build_source_manifest(data_room_dir: Path) -> SourceManifest:
    """Scan a data room directory and return a stable source manifest.

    The pilot starts with a conservative manifest builder because reliable
    source tracking matters more than aggressive auto-detection.
    """

    data_room_dir = data_room_dir.resolve()
    documents: list[SourceDocument] = []
    skipped_files: list[SkippedFile] = []

    for path in sorted(p for p in data_room_dir.rglob("*") if p.is_file()):
        # rglob follows symlinks; a link pointing outside the data room (e.g. to
        # /etc/passwd or another deal's folder) would otherwise be ingested as
        # in-scope evidence. Reject anything whose real path escapes the room.
        try:
            resolved = path.resolve()
            resolved.relative_to(data_room_dir)
        except (ValueError, OSError):
            skipped_files.append(
                SkippedFile(
                    rel_path=path.name,
                    extension=path.suffix.lower(),
                    reason="path_escapes_data_room",
                )
            )
            continue

        file_type = SUPPORTED_FILE_TYPES.get(path.suffix.lower())
        if not file_type:
            skipped_files.append(
                SkippedFile(
                    rel_path=path.relative_to(data_room_dir).as_posix(),
                    extension=path.suffix.lower(),
                    reason="unsupported_extension",
                )
            )
            continue

        rel_path = path.relative_to(data_room_dir).as_posix()
        content_fingerprint = _compute_sha256(path)
        document_role = _infer_document_role(path.name)
        documents.append(
            SourceDocument(
                source_id=_build_source_id(rel_path, content_fingerprint),
                rel_path=rel_path,
                absolute_path=path,
                file_name=path.name,
                extension=path.suffix.lower(),
                file_type=file_type,
                modified_timestamp=datetime.fromtimestamp(
                    path.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
                content_fingerprint=content_fingerprint,
                document_role=document_role,
                parser_used=_default_parser_name(file_type),
                priority_rank=ROLE_PRIORITY[document_role],
                metadata={"file_size_bytes": path.stat().st_size},
            )
        )

    return SourceManifest(
        data_room_dir=data_room_dir,
        indexed_at=datetime.now(tz=timezone.utc).isoformat(),
        document_count=len(documents),
        skipped_count=len(skipped_files),
        documents=documents,
        skipped_files=skipped_files,
    )


def _compute_sha256(path: Path) -> str:
    """Return a stable SHA-256 hash for the given local file."""

    digest = sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8192):
            digest.update(chunk)
    return digest.hexdigest()


def _build_source_id(rel_path: str, content_fingerprint: str) -> str:
    """Return a stable source identifier for a relative path plus file content."""

    digest = sha256(f"{rel_path}:{content_fingerprint}".encode("utf-8")).hexdigest()
    return f"src-{digest[:12]}"


def _infer_document_role(file_name: str) -> str:
    """Infer a lightweight document role from the file name."""

    lowered = file_name.lower()
    if "qoe" in lowered or "quality of earnings" in lowered:
        return "qoe"
    if "audited" in lowered or "financial" in lowered or "historical" in lowered:
        return "audited_fs"
    if "monthly" in lowered or "flash" in lowered or "budget" in lowered:
        return "monthly_fs"
    if "board" in lowered or "deck" in lowered or "presentation" in lowered:
        return "board_deck"
    return "other"


def _default_parser_name(file_type: str) -> str:
    """Return the default parser label for a file type."""

    return {
        "csv": "csv_reader",
        "docx": "python_docx",
        "pdf": "pypdf",
        "txt": "text_reader",
        "xls": "pandas_xlrd",
        "xlsx": "openpyxl",
    }[file_type]

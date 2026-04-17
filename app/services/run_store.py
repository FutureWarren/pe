"""Helpers for run-scoped artifact directories and local file persistence."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.models.run import PilotRunSummary

RUN_SUMMARY_FILE_NAME = "run_summary.json"


def create_run_directory(output_root: Path) -> tuple[str, Path]:
    """Create and return a timestamped run directory."""

    run_id = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_id, run_dir


def write_json(path: Path, payload: Any) -> Path:
    """Write JSON to disk with stable indentation."""

    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def write_markdown(path: Path, content: str) -> Path:
    """Write markdown content to disk."""

    path.write_text(content, encoding="utf-8")
    return path


def load_run_summary(run_dir: Path) -> Optional[PilotRunSummary]:
    """Load a run summary from disk when present."""

    summary_path = run_dir / RUN_SUMMARY_FILE_NAME
    if not summary_path.exists():
        return None
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    return PilotRunSummary.model_validate(payload)


def list_run_summaries(output_root: Path, limit: int = 20) -> list[PilotRunSummary]:
    """Return recent run summaries sorted from newest to oldest."""

    if not output_root.exists():
        return []

    summaries: list[PilotRunSummary] = []
    for child in sorted(output_root.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        summary = load_run_summary(child)
        if summary is None:
            continue
        summaries.append(summary)
        if len(summaries) >= limit:
            break
    return summaries

"""Typer-based CLI entrypoint for the Angelic pilot."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from app.config import get_settings
from app.models.run import ExtractionBackend, RunRequest
from app.services.pipeline import run_pilot

cli = typer.Typer(help="Local PE databook pilot CLI.")


@cli.command("health")
def health() -> None:
    """Print a small health payload for local smoke checks."""

    settings = get_settings()
    payload = {
        "status": "ok",
        "environment": settings.env,
        "output_dir": str(settings.output_dir),
    }
    typer.echo(json.dumps(payload, indent=2))


@cli.command("run")
def run_command(
    data_room: Path = typer.Option(
        Path("samples/data_room"),
        exists=True,
        file_okay=False,
        dir_okay=True,
        help="Directory containing sample source documents.",
    ),
    ai_workbook: Optional[Path] = typer.Option(
        None,
        exists=False,
        help="Optional path to a low-quality AI-generated benchmark workbook.",
    ),
    gold_workbook: Optional[Path] = typer.Option(
        None,
        exists=False,
        help="Optional path to a VP-created gold-standard workbook.",
    ),
    template_workbook: Optional[Path] = typer.Option(
        None,
        exists=False,
        help="Optional path to an empty or lightly formatted workbook template.",
    ),
    output_dir: Optional[Path] = typer.Option(
        None,
        exists=False,
        help="Optional output directory override for this run.",
    ),
    extraction_backend: ExtractionBackend = typer.Option(
        "deterministic",
        help="Extraction backend: deterministic or gemini. Gemini is reserved for structured extraction only.",
    ),
    run_label: Optional[str] = typer.Option(
        None,
        help="Optional label for this run (shown in history and run summary).",
    ),
) -> None:
    """Execute the scaffolded pilot pipeline and print the run summary."""

    request = RunRequest(
        data_room_dir=data_room,
        extraction_backend=extraction_backend,
        ai_workbook_path=ai_workbook,
        gold_workbook_path=gold_workbook,
        template_workbook_path=template_workbook,
        output_root=output_dir,
        run_label=run_label,
    )
    result = run_pilot(request)
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    cli()

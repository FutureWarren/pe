"""Robustness regression tests for the ingestion parsers.

These lock in that a single malformed file cannot kill a whole data-room run,
that an empty CSV leaves a visible note (not silent nothing), and that a
non-comma delimiter is detected instead of collapsing the table into one column.
"""

from __future__ import annotations

from pathlib import Path

from app.ingest.parsers import _parse_csv_document, parse_documents
from app.models.source import SourceDocument, SourceManifest


def _doc(path: Path, file_type: str) -> SourceDocument:
    return SourceDocument(
        source_id="src-1",
        rel_path=path.name,
        absolute_path=path,
        file_name=path.name,
        extension=path.suffix.lower(),
        file_type=file_type,
        modified_timestamp="2026-03-30T00:00:00+00:00",
        content_fingerprint="abc",
        document_role="other",
        parser_used="csv_reader",
        priority_rank=5,
    )


def test_empty_csv_emits_a_visible_note(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    segments = _parse_csv_document(_doc(path, "csv"))
    assert len(segments) == 1
    assert segments[0].segment_type == "file_note"


def test_semicolon_delimiter_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "euro.csv"
    path.write_text("Konto;FY2023;FY2024\nUmsatz;1000;2000\n", encoding="utf-8")
    segments = _parse_csv_document(_doc(path, "csv"))
    data_rows = [s for s in segments if s.segment_type == "csv_row"]
    assert data_rows, "expected at least one data row"
    # If the delimiter were mis-detected as comma, the row would be a single cell.
    assert len(data_rows[0].metadata["row_values"]) >= 3


def test_one_bad_file_does_not_kill_the_run(tmp_path: Path) -> None:
    good = tmp_path / "good.csv"
    good.write_text("Metric,FY2024\nRevenue,1000\n", encoding="utf-8")
    # A file that claims to be xlsx but is not a valid zip → load_workbook raises
    # (BadZipFile). This exercises the per-file isolation without importing pypdf,
    # which panics in environments lacking the cffi backend.
    broken = tmp_path / "broken.xlsx"
    broken.write_text("this is not a workbook", encoding="utf-8")

    manifest = SourceManifest(
        data_room_dir=tmp_path,
        indexed_at="2026-03-30T00:00:00+00:00",
        document_count=2,
        skipped_count=0,
        documents=[_doc(good, "csv"), _doc(broken, "xlsx")],
    )
    segments = parse_documents(manifest)
    # The good CSV still produced a data row...
    assert any(s.segment_type == "csv_row" for s in segments)
    # ...and the broken PDF became a flagged note rather than raising.
    assert any(s.segment_type == "file_note" for s in segments)

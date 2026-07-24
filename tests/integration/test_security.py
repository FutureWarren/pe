"""Security regression tests for the API surface and workbook export."""

from __future__ import annotations

import io
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import get_settings
from app.export.safe_cell import sanitize_cell_text
from app.main import app


def test_post_runs_rejects_path_outside_permitted_roots(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANGELIC_OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.setenv("ANGELIC_DEFAULT_DATA_ROOM", str(tmp_path / "data_room"))
    get_settings.cache_clear()
    client = TestClient(app)

    response = client.post("/runs", json={"data_room_dir": "/etc"})
    assert response.status_code == 400


def test_upload_rejects_unsupported_extension(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANGELIC_OUTPUT_DIR", str(tmp_path / "outputs"))
    get_settings.cache_clear()
    client = TestClient(app)

    response = client.post(
        "/runs/upload",
        files={"files": ("payload.exe", io.BytesIO(b"MZ..."), "application/octet-stream")},
    )
    assert response.status_code == 415


def test_sanitize_cell_text_neutralizes_formula_leads_but_keeps_numbers() -> None:
    assert sanitize_cell_text('=HYPERLINK("http://evil",A1)').startswith("'=")
    assert sanitize_cell_text("@SUM(A1)").startswith("'@")
    assert sanitize_cell_text("+cmd|calc").startswith("'+")
    # Genuine negative numbers-as-text are preserved.
    assert sanitize_cell_text("-5") == "-5"
    assert sanitize_cell_text("-1,234.50") == "-1,234.50"
    # Plain text and real numbers untouched.
    assert sanitize_cell_text("Revenue") == "Revenue"
    assert sanitize_cell_text(1234) == 1234

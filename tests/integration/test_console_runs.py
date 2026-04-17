from pathlib import Path

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


def test_console_can_create_list_and_download_artifacts(monkeypatch, tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    data_room = tmp_path / "data_room"
    data_room.mkdir()
    (data_room / "demo.csv").write_text(
        "Metric,FY2024\nRevenue,120\nCOGS,50\nOperating Expenses,20\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("ANGELIC_OUTPUT_DIR", str(output_root))
    monkeypatch.setenv("ANGELIC_DEFAULT_DATA_ROOM", str(data_room))
    get_settings.cache_clear()

    client = TestClient(app)

    create_response = client.post("/runs", json={"data_room_dir": str(data_room)})
    assert create_response.status_code == 200
    run_summary = create_response.json()
    assert run_summary["run_id"]
    assert run_summary["created_at"]

    list_response = client.get("/runs")
    assert list_response.status_code == 200
    runs = list_response.json()
    assert len(runs) == 1
    assert runs[0]["run_id"] == run_summary["run_id"]

    artifact_response = client.get(f"/runs/{run_summary['run_id']}/artifacts/generated_workbook")
    assert artifact_response.status_code == 200
    assert artifact_response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

from pathlib import Path

from app.ingest.manifest import build_source_manifest


def test_build_source_manifest_discovers_supported_files(tmp_path: Path) -> None:
    data_room = tmp_path / "data_room"
    data_room.mkdir()
    (data_room / "financials.csv").write_text("month,revenue\n2024-01,10\n", encoding="utf-8")
    (data_room / "ignore.txt").write_text("ignore me\n", encoding="utf-8")
    (data_room / "photo.png").write_text("not supported\n", encoding="utf-8")

    manifest = build_source_manifest(data_room)

    assert manifest.document_count == 2
    assert manifest.documents[0].rel_path == "financials.csv"
    assert manifest.documents[0].content_fingerprint
    assert manifest.documents[0].source_id.startswith("src-")
    assert manifest.skipped_count == 1
    assert manifest.skipped_files[0].rel_path == "photo.png"

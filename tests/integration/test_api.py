from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


def test_health_endpoint() -> None:
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_console_page_renders() -> None:
    get_settings.cache_clear()
    client = TestClient(app)

    response = client.get("/angelic-pilot")

    assert response.status_code == 200
    assert "Angelic Pilot 操作台" in response.text

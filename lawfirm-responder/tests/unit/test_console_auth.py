"""控制台/ingest 鉴权：admin_token 配置后必须带 X-Admin-Token。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from responder.config import Settings
from responder.console.api import router as console_router
from responder.gateway.callback import router as callback_router
from responder.service import Pipeline
from responder.store.db import Store


def make_app(tmp_path, token: str) -> TestClient:
    settings = Settings(mode="shadow", db_path=str(tmp_path / "a.db"), admin_token=token)
    store = Store(settings.db_path)
    app = FastAPI()
    app.state.store = store
    app.state.pipeline = Pipeline(store, None, settings)
    app.include_router(console_router)
    app.include_router(callback_router)
    return TestClient(app)


def test_console_requires_token(tmp_path):
    c = make_app(tmp_path, "sec123")
    assert c.get("/console/todo").status_code == 401
    assert c.get("/console/todo", headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert c.get("/console/todo", headers={"X-Admin-Token": "sec123"}).status_code == 200


def test_ingest_requires_token(tmp_path):
    c = make_app(tmp_path, "sec123")
    body = {"msg_id": "m1", "group_id": "g", "sender_id": "u", "content": "在吗"}
    assert c.post("/ingest", json=body).status_code == 401
    assert c.post("/ingest", json=body, headers={"X-Admin-Token": "sec123"}).status_code == 200


def test_open_when_token_empty(tmp_path):
    c = make_app(tmp_path, "")
    assert c.get("/console/todo").status_code == 200

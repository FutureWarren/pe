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


def _console(tmp_path, token="sec123"):
    from responder.config import Settings
    settings = Settings(mode="shadow", db_path=str(tmp_path / "b.db"),
                        admin_token=token, public_base_url="")
    store = Store(settings.db_path)
    app = FastAPI()
    app.state.store = store
    app.state.pipeline = Pipeline(store, None, settings)
    app.include_router(console_router)
    return TestClient(app), settings


def test_public_base_url_learned_from_console_access(tmp_path):
    """首次有人从公网打开控制台 → 记下这个地址当对外基础地址。

    交接单的「看完整对话」深链和律师登录链接都要它。此前只能人工写进 .env，
    而运维侧未必够得着这台机器，结果链接一直发不出去。控制台被访问到的地址，
    恰恰就是律师能打开的那个地址。
    """
    c, settings = _console(tmp_path)
    c.get("/console/me", headers={"X-Admin-Token": "sec123", "Host": "ai.example.com"})
    assert settings.public_base_url == "http://ai.example.com"


def test_base_url_respects_reverse_proxy_scheme(tmp_path):
    """nginx 反代下要按 X-Forwarded-Proto 记 https，否则发出去的链接打不开。"""
    c, settings = _console(tmp_path)
    c.get("/console/me", headers={
        "X-Admin-Token": "sec123", "Host": "ai.example.com",
        "X-Forwarded-Proto": "https",
    })
    assert settings.public_base_url == "https://ai.example.com"


def test_localhost_access_does_not_poison_base_url(tmp_path):
    """管理员从本机调试时记下的地址，发给律师一个都打不开——不能采信。"""
    c, settings = _console(tmp_path)
    c.get("/console/me", headers={"X-Admin-Token": "sec123", "Host": "127.0.0.1:8020"})
    assert settings.public_base_url == ""


def test_explicit_config_is_never_overwritten(tmp_path):
    """显式配过就以配置为准，不被某次访问的 Host 顶掉。"""
    from responder.config import Settings
    settings = Settings(mode="shadow", db_path=str(tmp_path / "c.db"),
                        admin_token="sec123", public_base_url="https://ai.songhu.com")
    store = Store(settings.db_path)
    app = FastAPI()
    app.state.store = store
    app.state.pipeline = Pipeline(store, None, settings)
    app.include_router(console_router)
    TestClient(app).get("/console/me", headers={
        "X-Admin-Token": "sec123", "Host": "evil.example.com"})
    assert settings.public_base_url == "https://ai.songhu.com"

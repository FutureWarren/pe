"""远程升级：脱离进程组执行、命令写死、可开关。"""

import subprocess

from fastapi import FastAPI
from fastapi.testclient import TestClient

from responder import ops
from responder.config import Settings
from responder.console.api import router as console_router
from responder.service import Pipeline
from responder.store.db import Store


def make_app(tmp_path, **kw):
    settings = Settings(
        mode="shadow", db_path=str(tmp_path / "o.db"), admin_token="",
        update_repo_dir=str(tmp_path), update_pip=str(tmp_path / "pip"),
        update_log=str(tmp_path / "upd.log"), **kw,
    )
    store = Store(settings.db_path)
    app = FastAPI()
    app.state.store = store
    app.state.pipeline = Pipeline(store, None, settings)
    app.include_router(console_router)
    return TestClient(app), settings


def test_update_spawns_detached_process(tmp_path, monkeypatch):
    """升级脚本必须脱离服务进程组，否则 systemctl restart 会杀掉自己。"""
    # 记录所有 Popen 调用：current_commit 里的 subprocess.run 也会走到这里
    calls: list[tuple] = []

    class FakePopen:
        def __init__(self, cmd, **kw):
            calls.append((cmd, kw))

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    c, settings = make_app(tmp_path)
    body = c.post("/console/update").json()

    assert body["ok"] and body["started"]
    spawn = next(c for c, _ in calls if "bash" in c)
    kw = next(k for c, k in calls if "bash" in c)
    assert kw["start_new_session"] is True
    script = spawn[-1]
    text = open(script, encoding="utf-8").read()
    # 命令写死：只拉配置里的分支与目录，不含任何请求参数
    assert settings.update_branch in text
    assert str(tmp_path) in text
    assert "systemctl restart responder" in text


def test_update_can_be_disabled(tmp_path):
    c, _ = make_app(tmp_path, self_update_enabled=False)
    body = c.post("/console/update").json()
    assert body["ok"] is False and "关闭" in body["error"]


def test_update_log_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: None)
    c, settings = make_app(tmp_path)
    (tmp_path / "upd.log").write_text("line1\nline2\n", encoding="utf-8")
    body = c.get("/console/update/log").json()
    assert "line2" in body["log"]
    assert "commit" in body


def test_current_commit_of_non_repo_is_empty(tmp_path):
    assert ops.current_commit(str(tmp_path)) == ""

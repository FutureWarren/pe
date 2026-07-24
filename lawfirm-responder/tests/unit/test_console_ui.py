"""控制台网页：/ui 可公开访问（登录壳），数据接口仍受令牌保护。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from responder.config import Settings
from responder.console.api import router as console_router
from responder.console.api import ui_router
from responder.models import Reminder
from responder.service import Pipeline
from responder.store.db import Store


def test_ui_served_in_chinese():
    app = FastAPI()
    app.include_router(ui_router)
    r = TestClient(app).get("/ui")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    for anchor in ("控制台", "待办", "群管理", "X-Admin-Token"):
        assert anchor in r.text
    # 页面不应内嵌任何密钥/令牌，也不应使用原生对话框（规范 P0-3）
    assert "sk-" not in r.text
    for banned in ("alert(", "confirm(", "prompt("):
        assert banned not in r.text


def test_todo_done_then_reopen(tmp_path):
    """「标记已处理」的 5 秒撤销依赖 reopen 接口回滚。"""
    settings = Settings(mode="shadow", db_path=str(tmp_path / "u.db"), admin_token="")
    store = Store(settings.db_path)
    rid = store.save_reminder(
        Reminder(msg_id="m1", group_id="g1", to_userid="wei", summary="测试事项")
    )
    app = FastAPI()
    app.state.store = store
    app.state.pipeline = Pipeline(store, None, settings)
    app.include_router(console_router)
    c = TestClient(app)

    assert c.post(f"/console/todo/{rid}/done").json()["ok"]
    assert all(r["id"] != rid or r["status"] == "done" for r in store.pending_reminders())
    assert c.post(f"/console/todo/{rid}/reopen").json()["ok"]
    pending = [r for r in store.pending_reminders() if r["id"] == rid]
    assert pending and pending[0]["status"] == "pending"

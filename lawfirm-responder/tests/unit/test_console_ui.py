"""控制台网页：/ui 可公开访问（登录壳），数据接口仍受令牌保护。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from responder.console.api import ui_router


def test_ui_served_in_chinese():
    app = FastAPI()
    app.include_router(ui_router)
    r = TestClient(app).get("/ui")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    for anchor in ("控制台", "待办", "群管理", "X-Admin-Token"):
        assert anchor in r.text
    # 页面不应内嵌任何密钥/令牌
    assert "sk-" not in r.text

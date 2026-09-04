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


# ---------------------------------------------------------- 令牌可改成一句话
# 律所方的诉求：一串随机字符记不住。允许改成短语的前提是有连续输错锁定——
# 否则为了扛住每秒几千次猜测，就只能逼人抄那串字符，而它最后一定会被抄进
# 某个记事本或聊天记录里，反而更不安全。
def _auth_app(tmp_path, token="tok-original-123"):
    from fastapi import FastAPI

    from responder.config import Settings
    from responder.console import api as console_api
    from responder.console.api import router as console_router
    from responder.service import Pipeline
    from responder.store.db import Store

    console_api._fails.clear()  # 用例之间不共享锁定状态
    db = str(tmp_path / "auth.db")
    settings = Settings(mode="shadow", db_path=db, admin_token=token)
    store = Store(db)
    app = FastAPI()
    app.state.store = store
    app.state.pipeline = Pipeline(store, None, settings)
    app.include_router(console_router)
    return app, settings


def test_token_can_be_changed_to_a_memorable_phrase(tmp_path):
    from fastapi.testclient import TestClient

    app, settings = _auth_app(tmp_path)
    c = TestClient(app)
    # 刻意不用跟律所沾边的词：所址「九峰路」印在每一条邀约话术里，
    # 客户和爬虫都读得到，拿它当口令等于把答案写在门上（2026-08-12 收紧）。
    phrase = "mabuteng-7-hetong"
    r = c.post("/console/admin-token", json={"token": phrase},
               headers={"X-Admin-Token": "tok-original-123"})
    assert r.status_code == 200
    assert settings.admin_token == phrase
    # 新令牌立即生效，旧的立即失效
    assert c.get("/console/me", headers={"X-Admin-Token": phrase}).status_code == 200


def test_weak_tokens_are_rejected(tmp_path):
    """songhu123 是攻击者会试的第一个——律所域名就是 songhulaw。"""
    from fastapi.testclient import TestClient

    app, _ = _auth_app(tmp_path)
    c = TestClient(app)
    h = {"X-Admin-Token": "tok-original-123"}
    # 「够长就放行」的口子已经堵掉：律所名/所址/示例口令一律拒，不看长度。
    # 旧判据 `含弱词 and len < 16` 恰好放行了文档里那句 songhu-jiufeng-88（17 位），
    # 而那是最多人会照抄的一句。
    for bad in ("songhu123", "12345678", "songhulaw2026", "abcdefghijkl",
                "songhu-jiufeng-88", "jiufeng-88-pinggao"):
        r = c.post("/console/admin-token", json={"token": bad}, headers=h)
        assert r.status_code == 400, bad


def test_token_cannot_be_emptied(tmp_path):
    """取消令牌 = 公网上任何人都能读全部客户咨询原文，还能触发升级（代码执行）。"""
    from fastapi.testclient import TestClient

    app, settings = _auth_app(tmp_path)
    r = TestClient(app).post("/console/admin-token", json={"token": ""},
                             headers={"X-Admin-Token": "tok-original-123"})
    assert r.status_code == 400
    assert settings.admin_token == "tok-original-123"


def test_repeated_wrong_tokens_get_locked_out(tmp_path):
    """短语能当密码用，靠的就是这条：在线爆破 15 分钟只有 8 次机会。"""
    from fastapi.testclient import TestClient

    app, _ = _auth_app(tmp_path)
    c = TestClient(app)
    for _ in range(8):
        assert c.get("/console/me", headers={"X-Admin-Token": "guess"}).status_code == 401
    r = c.get("/console/me", headers={"X-Admin-Token": "guess"})
    assert r.status_code == 429
    # 锁定期间连正确令牌也挡——否则攻击者可以边试边看是不是「只有这个不报 429」
    assert c.get("/console/me", headers={"X-Admin-Token": "tok-original-123"}).status_code == 429


def test_successful_login_clears_the_counter(tmp_path):
    """输错几次又想起来了，不该被自己的手滑锁在门外。"""
    from fastapi.testclient import TestClient

    app, _ = _auth_app(tmp_path)
    c = TestClient(app)
    for _ in range(5):
        c.get("/console/me", headers={"X-Admin-Token": "guess"})
    assert c.get("/console/me", headers={"X-Admin-Token": "tok-original-123"}).status_code == 200
    for _ in range(5):
        c.get("/console/me", headers={"X-Admin-Token": "guess"})
    assert c.get("/console/me", headers={"X-Admin-Token": "tok-original-123"}).status_code == 200

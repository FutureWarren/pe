"""管道分段计数：客户的消息在哪一段没了，要一眼看得出。

2026-08-11 真机：客户在微信客服里连发多条消息，AI 一条都没回——
而 `/health` 显示服务器一切正常（后台线程活着、队列空、客服通道配置有效）。
当时**分不清**三件事，而它们的症状完全一样、修法天差地别：

  · 企微根本没把消息推给我们   → 去查回调地址 / 可信 IP
  · 推了但签名验不过           → 去核对 Token / EncodingAESKey
  · 验过了但一条也拉不回来     → 游标卡住或 Token 过期

没有这组计数器，只能一轮轮截图问。
"""


from fastapi import FastAPI
from fastapi.testclient import TestClient

from responder.config import Settings
from responder.gateway import callback as cb
from responder.service import Pipeline
from responder.store.db import Store


class FakeCrypto:
    """签名结果可控的加解密桩：只用来驱动分支，不测真加密。"""

    def __init__(self, ok=True, payload="<xml><Event>kf_msg_or_event</Event>"
                                        "<Token>t</Token><OpenKfId>wk1</OpenKfId></xml>"):
        self.ok, self.payload = ok, payload

    def verify(self, *a):
        return self.ok

    def decrypt(self, _):
        return self.payload


def _app(tmp_path, crypto):
    s = Settings(mode="live", db_path=str(tmp_path / "t.db"), llm_provider="none",
                 callback_async=False)
    store = Store(s.db_path)
    app = FastAPI()
    app.state.store = store
    app.state.pipeline = Pipeline(store, None, s)
    app.state.worker = None
    app.include_router(cb.router)
    app.dependency_overrides[cb.get_crypto] = lambda: crypto
    app.dependency_overrides[cb.get_pipeline] = lambda: app.state.pipeline
    return TestClient(app), store


ENVELOPE = "<xml><Encrypt>abc</Encrypt></xml>"
Q = {"msg_signature": "s", "timestamp": "1", "nonce": "n"}


def test_every_callback_is_counted_even_before_we_understand_it(tmp_path):
    """总数必须在**签名校验之前**就记。否则「没推给我们」和「推了但验不过」
    在计数上长得一样，而那正是要分开的两件事。"""
    c, store = _app(tmp_path, FakeCrypto())
    c.post("/wecom/callback", params=Q, content=ENVELOPE)
    assert store.counters()["kf_cb_total"]["n"] == 1


def test_a_bad_signature_is_its_own_number(tmp_path):
    """这个数在涨 = Token / EncodingAESKey 跟企微后台填的不一致。
    看到它就别去查判断层了。"""
    c, store = _app(tmp_path, FakeCrypto(ok=False))
    r = c.post("/wecom/callback", params=Q, content=ENVELOPE)
    assert r.status_code == 403
    n = store.counters()
    assert n["kf_cb_total"]["n"] == 1
    assert n["kf_cb_bad_signature"]["n"] == 1
    assert "kf_cb_event" not in n, "验都没过，不该算成收到了客服事件"


def test_a_kf_event_is_counted_separately(tmp_path):
    c, store = _app(tmp_path, FakeCrypto())
    c.post("/wecom/callback", params=Q, content=ENVELOPE)
    assert store.counters()["kf_cb_event"]["n"] == 1
    assert store.get_note("kf_cb_last") == "-/kf_msg_or_event"


def test_we_remember_what_we_could_not_understand(tmp_path):
    """认不出的回调也要留证据。企微改个字段名，我们就哑了——
    而「哑了」在客户那头跟「服务器挂了」长得一模一样。"""
    c, store = _app(tmp_path, FakeCrypto(payload="<xml><MsgType>image</MsgType></xml>"))
    c.post("/wecom/callback", params=Q, content=ENVELOPE)
    assert store.get_note("kf_cb_last") == "image/-"


def test_synced_message_count_separates_a_stuck_cursor(tmp_path):
    """回调来了（kf_cb_event 在涨）但 kf_synced 一直是 0
    = 游标卡住或 Token 过期。跟「回调没来」完全是两回事。"""
    from responder.worker import KfSyncJob, Worker

    s = Settings(mode="live", db_path=str(tmp_path / "w.db"), llm_provider="none")
    store = Store(s.db_path)

    class DeadSync:
        def available(self):
            return True

        def sync_msg(self, token, kfid, cursor, limit=1000):
            return {"msg_list": [], "next_cursor": "", "has_more": 0}

    w = Worker(Pipeline(store, None, s), store, kf_client=DeadSync())
    w.process_kf(KfSyncJob(token="t", open_kfid="wk1"))
    assert store.counters().get("kf_synced", {}).get("n", 0) == 0


def test_an_unknown_enter_event_is_written_down(tmp_path):
    """进线问候挂在事件名白名单上。企微换个名字，问候整条不触发，
    客户扫码进来看到的是空窗口——而后台什么异常都没有。"""
    from responder.worker import Worker

    s = Settings(mode="live", db_path=str(tmp_path / "e.db"), llm_provider="none")
    store = Store(s.db_path)
    w = Worker(Pipeline(store, None, s), store, kf_client=None)
    w._handle_kf_message({
        "msgtype": "event", "origin": 4,
        "open_kfid": "wk1", "external_userid": "c1", "msgid": "e1",
        "event": {"event_type": "some_new_name_tencent_invented"},
    })
    assert store.get_note("kf_unknown_event") == "some_new_name_tencent_invented"


def test_the_counters_never_become_the_outage(tmp_path):
    """计数器是用来排障的，不能反过来成为故障源。"""
    store = Store(str(tmp_path / "x.db"))
    store.bump("k")
    store.path = "/nonexistent/dir/x.db"
    store.bump("k")  # 不该抛


# ------------------------------------------- 拿浏览器戳一下这个地址会看到什么
def _get_app(tmp_path, crypto=None):
    s = Settings(mode="live", db_path=str(tmp_path / "g.db"), llm_provider="none")
    store = Store(s.db_path)
    app = FastAPI()
    app.state.store = store
    app.state.pipeline = Pipeline(store, None, s)
    app.include_router(cb.router)
    app.dependency_overrides[cb.get_crypto] = lambda: crypto or FakeCrypto()
    return TestClient(app), store


def test_a_human_opening_the_callback_url_gets_a_verdict(tmp_path):
    """排查「客户消息为什么没进来」时，第一件事就是拿浏览器戳一下这个地址。
    FastAPI 默认回一屏 `{"detail":[{"type":"missing"...}]}`——那对律所方
    等于乱码，而它其实是**好消息**：地址通了、路由到了我们。
    """
    c, _ = _get_app(tmp_path)
    r = c.get("/wecom/callback")
    assert r.status_code == 200
    assert "missing" not in r.text
    assert "从公网是通的" in r.text
    # 一条回调都没收到过 → 直接说该去后台填哪一项
    assert "从来没有往这个地址推过消息" in r.text
    assert "接收事件服务器" in r.text


def test_the_page_names_the_signature_problem_when_that_is_the_problem(tmp_path):
    c, store = _get_app(tmp_path)
    store.bump("kf_cb_total", 5)
    store.bump("kf_cb_bad_signature", 5)
    assert "签名一直对不上" in c.get("/wecom/callback").text


def test_the_page_names_a_stuck_pull_when_that_is_the_problem(tmp_path):
    """通知收到了却一条也拉不回来——跟「没收到通知」完全是两回事。"""
    c, store = _get_app(tmp_path)
    store.bump("kf_cb_total", 3)
    store.bump("kf_cb_event", 3)
    assert "一条消息也拉不回来" in c.get("/wecom/callback").text


def test_the_page_says_so_when_everything_works(tmp_path):
    c, store = _get_app(tmp_path)
    store.bump("kf_cb_total", 3)
    store.bump("kf_cb_event", 3)
    store.bump("kf_synced", 7)
    assert "消息进得来" in c.get("/wecom/callback").text


def test_the_real_verification_handshake_still_works(tmp_path):
    """企微配置回调地址时先发一个挑战包，回显不对就配不上——
    这条页面改动绝不能把它弄坏。"""
    c, store = _get_app(tmp_path, FakeCrypto(payload="plain-echo"))
    r = c.get("/wecom/callback", params={
        "msg_signature": "s", "timestamp": "1", "nonce": "n", "echostr": "e"})
    assert r.status_code == 200 and r.text == "plain-echo"
    assert store.counters()["kf_verify_ok"]["n"] == 1


def test_a_failed_verification_is_counted_too(tmp_path):
    """企微配置时验证失败，律所方看到的只是后台一句「保存失败」。
    这个数让我们这边也看得见。"""
    c, store = _get_app(tmp_path, FakeCrypto(ok=False))
    r = c.get("/wecom/callback", params={
        "msg_signature": "s", "timestamp": "1", "nonce": "n", "echostr": "e"})
    assert r.status_code == 403
    assert store.counters()["kf_verify_failed"]["n"] == 1

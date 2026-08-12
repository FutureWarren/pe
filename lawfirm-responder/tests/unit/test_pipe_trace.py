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

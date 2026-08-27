"""公众号回调入口：配置验证 + 收消息。

这是酷机时代那条主通道的第一米。两件事必须对，否则后面全白搭：

1. **配置验证要通。** 在微信开发者平台填完回调地址点提交时，微信发一个 GET
   过来，原样回显 `echostr` 才算配置成功——这一步不通，一条消息都收不到。
   而它的验签算法与企微**完全不同**（token/timestamp/nonce 排序取 SHA1，
   没有 msg_signature 那一套）。照抄企微的写法必然失败，
   而失败的现象只是后台一句「配置失败」，不告诉你为什么。
2. **任何情况下都要立刻回 200。** 微信收不到就重推，三次之后认为服务挂了。
"""

import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from responder.config import Settings
from responder.gateway.callback import router as callback_router
from responder.gateway.sender import WeComSender
from responder.service import Pipeline
from responder.store.db import Store
from responder.worker import Worker

TOKEN = "kuji-mp-token"


def sign(token: str, ts: str, nonce: str) -> str:
    return hashlib.sha1("".join(sorted([token, ts, nonce])).encode()).hexdigest()


def app_for(tmp_path, token: str = TOKEN) -> TestClient:
    db = str(tmp_path / "t.db")
    s = Settings(mode="shadow", db_path=db, callback_async=False,
                 mp_app_id="wxtest", mp_app_secret="secret",
                 mp_callback_token=token)
    store = Store(db)
    pipeline = Pipeline(store, sender=WeComSender(s), settings=s)
    app = FastAPI()
    app.state.store = store
    app.state.pipeline = pipeline
    app.state.worker = Worker(pipeline, store, WeComSender(s))
    app.include_router(callback_router)
    return TestClient(app)


@pytest.fixture()
def client(tmp_path):
    return app_for(tmp_path)


def qs(token: str = TOKEN, echo: str = "") -> dict:
    ts, nonce = "1756200000", "abc123"
    p = {"signature": sign(token, ts, nonce), "timestamp": ts, "nonce": nonce}
    if echo:
        p["echostr"] = echo
    return p


TEXT_XML = """<xml>
  <ToUserName><![CDATA[gh_kuji]]></ToUserName>
  <FromUserName><![CDATA[oCUST001]]></FromUserName>
  <CreateTime>1756200000</CreateTime>
  <MsgType><![CDATA[text]]></MsgType>
  <Content><![CDATA[我那台什么时候能到啊]]></Content>
  <MsgId>24000001</MsgId>
</xml>"""


# ------------------------------------------------------------ ① 配置验证
def test_the_setup_verification_echoes_the_challenge(client):
    """**这一步不通，后面一条消息都收不到。**"""
    r = client.get("/mp/callback", params=qs(echo="1234567890"))
    assert r.status_code == 200
    assert r.text == "1234567890"


def test_a_forged_signature_is_refused(client):
    r = client.get("/mp/callback", params={
        "signature": "deadbeef", "timestamp": "1756200000",
        "nonce": "abc123", "echostr": "x",
    })
    assert r.status_code == 403


# ------------------------------------------------------------ ② 收消息
def test_a_real_message_is_accepted_and_answered_with_success(client):
    """**任何情况下都立刻回 success。**

    微信收不到 200 会重推三次，三次之后它认为我们的服务挂了。
    """
    r = client.post("/mp/callback", params=qs(), content=TEXT_XML)
    assert r.status_code == 200
    assert r.text == "success"


def test_unparseable_body_still_returns_success(client):
    """一条读不懂的消息不值得把整条通道拖下水。"""
    r = client.post("/mp/callback", params=qs(), content="这不是 XML")
    assert r.status_code == 200
    assert r.text == "success"


def test_a_forged_post_is_refused_and_counted(client):
    """伪造的报文要拒，而且要记数——这个计数器是排查「回调为什么没进来」
    时第一个要看的东西（与企微那条 kf_cb_bad_signature 同源）。"""
    r = client.post("/mp/callback", params={
        "signature": "deadbeef", "timestamp": "1756200000", "nonce": "abc123",
    }, content=TEXT_XML)
    assert r.status_code == 403


def test_receiving_leaves_a_trace_while_the_pipeline_is_not_wired_yet(client):
    """**分期上线中的那一段最容易「静默消失」。**

    通道通了但零售链路还没启用（`RESPONDER_RETAIL_MODE=off`，也就是本仓库的
    默认值）时，消息不能无声地被丢掉。这条运维小记是「回调通了但还没处理」
    与「回调根本没通」的分界，排查时这一句能省掉半小时。
    """
    client.post("/mp/callback", params=qs(), content=TEXT_XML)
    store = client.app.state.store
    assert int((store.counters().get("mp_cb_event") or {}).get("n", 0)) >= 1
    client.app.state.worker.drain()
    assert "未启用" in store.get_note("retail_unwired")


def test_a_message_reaches_the_retail_pipeline_when_it_is_on(tmp_path):
    """回调 → 队列 → 零售链路，整条接通。

    这一条是本轮之前缺的那一段：`responder/retail/` 每一块都测得过，
    合起来一条真实消息也处理不了——因为没有人调用它。
    """
    from responder.retail.pipeline import RetailPipeline

    c = app_for(tmp_path)
    c.app.state.worker.retail = RetailPipeline(c.app.state.store, mode="shadow")
    c.post("/mp/callback", params=qs(), content=TEXT_XML)
    c.app.state.worker.drain()
    replies = c.app.state.store.list_replies(limit=5)
    assert replies, "消息进来了却一条回复记录都没有"
    assert replies[0]["group_id"] == "mp:oCUST001"


# ------------------------------------------------------------ ③ 默认拒绝
def test_no_token_configured_shuts_the_door(tmp_path):
    """**留空 = 接入口关闭，不是放行。**

    与抖音、外部渠道接入口口径一致：不验签等于把公网地址敞开，
    任何人都能伪造客户消息灌进来、骗走客服消息额度。
    """
    c = app_for(tmp_path, token="")
    assert c.get("/mp/callback", params=qs(echo="x")).status_code == 403
    assert c.post("/mp/callback", params=qs(), content=TEXT_XML).status_code == 403

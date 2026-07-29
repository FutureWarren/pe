"""群聊 @ 助手：正文清洗、免等待、不沉默，以及机器人回调全链路。"""

import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from responder.config import Settings
from responder.gateway.callback import get_bot_crypto, get_crypto
from responder.gateway.callback import router as callback_router
from responder.gateway.mention import strip_mentions
from responder.gateway.wecom_crypto import WeComCrypto
from responder.models import ClientStatus, GroupProfile, IncomingMessage
from responder.service import Pipeline
from responder.store.db import Store
from responder.worker import Worker

BOT_TOKEN = "bottoken"
BOT_AES = base64.b64encode(b"b" * 32).decode()[:43]
CORP = "wwcorp"
GID = "chat_pilot_01"


# ---------------------------------------------------------------- 正文清洗
@pytest.mark.parametrize("raw,expect,flag", [
    ("@松沪助理 拖欠工资多久可以仲裁？", "拖欠工资多久可以仲裁？", True),
    ("@助理　我想咨询离婚", "我想咨询离婚", True),          # 全角空格
    ("@A @B 帮我看看", "帮我看看", True),                   # 连续多个 @
    ("拖欠工资怎么办", "拖欠工资怎么办", False),
    ("我跟 @张三 说过了", "我跟 @张三 说过了", False),      # 句中 @ 属于内容，保留
])
def test_strip_mentions(raw, expect, flag):
    assert strip_mentions(raw) == (expect, flag)


# ---------------------------------------------------------------- 管道行为
class Snd:
    def __init__(self):
        self.robot: list[str] = []
        self.direct: list[str] = []

    def send_robot_text(self, webhook, text):
        self.robot.append(text)
        return True

    def send_group_text(self, chat_id, text):
        self.robot.append(text)
        return True

    def send_direct_text(self, userid, text):
        self.direct.append(text)
        return True


def make(tmp_path):
    db = str(tmp_path / "b.db")
    store = Store(db)
    store.upsert_group(GroupProfile(
        group_id=GID, name="劳动仲裁咨询群", client_status=ClientStatus.PROSPECT,
        case_type="劳动仲裁", lawyer_name="魏", lawyer_userid="future",
        robot_webhook="rk-1",
    ))
    settings = Settings(mode="live", db_path=db, split_delay_seconds=0,
                        wecom_bot_token=BOT_TOKEN, wecom_bot_aes_key=BOT_AES,
                        wecom_corp_id=CORP)
    snd = Snd()
    return store, snd, Pipeline(store, snd, settings), settings


def _msg(content, mid, mentioned=True):
    return IncomingMessage(msg_id=mid, group_id=GID, sender_id="c1",
                           content=content, mentioned_bot=mentioned)


def test_mention_skips_wait_gate(tmp_path):
    """被 @ 点名＝客户直接找助手，不该再等 2.5 分钟。"""
    store, snd, p, _ = make(tmp_path)
    p.handle(_msg("拖欠工资多久可以申请劳动仲裁？", "m1"))
    assert snd.robot, "被 @ 的消息应立即回复"
    reasons = [d["reasons"] for d in store.list_decisions(GID)]
    assert not any("gate:waiting" in r for r in reasons)


def test_unmentioned_group_message_still_waits(tmp_path):
    """群里没 @ 助手的消息维持原有补位逻辑（人工优先）。"""
    store, snd, p, _ = make(tmp_path)
    p.handle(_msg("拖欠工资多久可以申请劳动仲裁？", "m2", mentioned=False))
    assert snd.robot == []
    reasons = [d["reasons"] for d in store.list_decisions(GID)]
    assert any("gate:waiting" in r for r in reasons)


def test_mention_of_unclear_intent_gets_opener(tmp_path):
    """@ 了但没说清楚 → 引导而非沉默：客户明确在叫助手，不吭声比答错更伤。"""
    store, snd, p, _ = make(tmp_path)
    p.handle(_msg("在吗", "m3"))
    d = store.list_decisions(GID)[0]
    assert d["action"] == "answer" and d["category"] == "greeting"
    assert snd.robot


def test_lead_flows_from_group_mention(tmp_path):
    """群里留电话同样进线索闭环。"""
    store, snd, p, _ = make(tmp_path)
    p.handle(_msg("我的电话是17721275495，麻烦律师联系我", "m4"))
    lead = store.get_lead(GID)
    assert lead and lead["intent"] == "hot" and lead["contact"] == "17721275495"


# ---------------------------------------------------------------- 回调链路
def test_bot_callback_end_to_end(tmp_path):
    """加密回调 → 验签解密 → 剥离 @ → 判断 → 群机器人发言。"""
    store, snd, pipeline, settings = make(tmp_path)
    crypto = WeComCrypto(BOT_TOKEN, BOT_AES, CORP)
    app = FastAPI()
    app.state.store = store
    app.state.pipeline = pipeline
    app.state.worker = Worker(pipeline, store, snd)
    settings.callback_async = False
    app.include_router(callback_router)
    app.dependency_overrides[get_bot_crypto] = lambda: crypto
    app.dependency_overrides[get_crypto] = lambda: crypto
    client = TestClient(app)

    plain = (f"<xml><MsgId>bm1</MsgId><ChatId>{GID}</ChatId>"
             f"<From><UserId>client_a</UserId></From><MsgType>text</MsgType>"
             f"<Content>@松沪助理 拖欠工资多久可以申请劳动仲裁？</Content></xml>")
    enc = crypto.encrypt(plain)
    ts, nonce = "1753000100", "bn1"
    sig = crypto.signature(ts, nonce, enc)
    r = client.post(
        f"/wecom/bot/callback?msg_signature={sig}&timestamp={ts}&nonce={nonce}",
        content=f"<xml><Encrypt><![CDATA[{enc}]]></Encrypt></xml>",
        headers={"content-type": "text/xml"},
    )
    assert r.status_code == 200 and r.text == "success"
    # @ 前缀已剥离，入库的是干净正文
    assert store.get_message("bm1").content == "拖欠工资多久可以申请劳动仲裁？"
    assert snd.robot, "应经群机器人 webhook 发言"


def test_bot_callback_url_verification(tmp_path):
    store, snd, pipeline, _ = make(tmp_path)
    crypto = WeComCrypto(BOT_TOKEN, BOT_AES, CORP)
    app = FastAPI()
    app.state.store = store
    app.state.pipeline = pipeline
    app.include_router(callback_router)
    app.dependency_overrides[get_bot_crypto] = lambda: crypto
    echostr = crypto.encrypt("bot-echo-1")
    ts, nonce = "1753000101", "bn2"
    sig = crypto.signature(ts, nonce, echostr)
    r = TestClient(app).get("/wecom/bot/callback", params={
        "msg_signature": sig, "timestamp": ts, "nonce": nonce, "echostr": echostr})
    assert r.status_code == 200 and r.text == "bot-echo-1"


def test_bot_callback_rejects_bad_signature(tmp_path):
    store, snd, pipeline, _ = make(tmp_path)
    crypto = WeComCrypto(BOT_TOKEN, BOT_AES, CORP)
    app = FastAPI()
    app.state.store = store
    app.state.pipeline = pipeline
    app.include_router(callback_router)
    app.dependency_overrides[get_bot_crypto] = lambda: crypto
    enc = crypto.encrypt("<xml><MsgType>text</MsgType><Content>x</Content></xml>")
    r = TestClient(app).post(
        "/wecom/bot/callback?msg_signature=bad&timestamp=1&nonce=1",
        content=f"<xml><Encrypt><![CDATA[{enc}]]></Encrypt></xml>")
    assert r.status_code == 403
    assert store.list_decisions() == []

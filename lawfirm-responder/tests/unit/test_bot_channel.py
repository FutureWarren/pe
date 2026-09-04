"""群聊助手闭环：回调自带的会话 webhook → 自动建档 → 用它发言 → 过期回落。

这条链路的价值在于「员工零配置」：员工只要把机器人拉进群，剩下的建档与发送地址
都由回调自己带来。测试守住的正是这个承诺，以及它失效时的回落路径。
"""

import base64
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from responder.config import Settings
from responder.console.api import router as console_router
from responder.gateway import bot
from responder.gateway.callback import get_bot_crypto
from responder.gateway.callback import router as callback_router
from responder.gateway.wecom_crypto import WeComCrypto
from responder.models import ClientStatus, GroupProfile
from responder.service import Pipeline
from responder.store.db import Store
from responder.worker import Worker

BOT_TOKEN = "bottoken"
BOT_AES = base64.b64encode(b"b" * 32).decode()[:43]
CORP = "wwcorp"
GID = "chat_bot_01"
HOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=callback-issued"
ADMIN = "tok-admin"


class Snd:
    """记录发到哪个 webhook——通道优先级是本文件的被测对象。"""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.direct: list[str] = []

    def send_robot_text(self, webhook, text):
        self.calls.append((webhook, text))
        return True

    def send_group_text(self, chat_id, text):
        self.calls.append(("appchat", text))
        return True

    def send_direct_text(self, userid, text):
        self.direct.append(text)
        return True


def make(tmp_path, **over):
    db = str(tmp_path / "bc.db")
    store = Store(db)
    settings = Settings(
        mode="live", db_path=db, split_delay_seconds=0, admin_token=ADMIN,
        wecom_bot_token=BOT_TOKEN, wecom_bot_aes_key=BOT_AES, wecom_corp_id=CORP,
        bot_default_notify_userid="future", **over,
    )
    snd = Snd()
    pipeline = Pipeline(store, snd, settings)
    return store, snd, pipeline, settings


def _xml(*, chat_id=GID, content="拖欠工资多久可以申请劳动仲裁？", webhook=HOOK,
         chat_type="group", msg_id="bm1", event=""):
    if event:
        return (f"<xml><MsgType>event</MsgType><ChatId>{chat_id}</ChatId>"
                f"<ChatType>{chat_type}</ChatType><WebhookUrl>{webhook}</WebhookUrl>"
                f"<From><UserId>client_a</UserId><Name>客户甲</Name></From>"
                f"<Event><EventType>{event}</EventType></Event></xml>")
    return (f"<xml><MsgId>{msg_id}</MsgId><ChatId>{chat_id}</ChatId>"
            f"<ChatType>{chat_type}</ChatType><WebhookUrl>{webhook}</WebhookUrl>"
            f"<From><UserId>client_a</UserId><Name>客户甲</Name></From>"
            f"<MsgType>text</MsgType><Text><Content>{content}</Content></Text></xml>")


def post_callback(client, crypto, plain, *, nonce="bn1"):
    enc = crypto.encrypt(plain)
    ts = "1753000100"
    sig = crypto.signature(ts, nonce, enc)
    return client.post(
        f"/wecom/bot/callback?msg_signature={sig}&timestamp={ts}&nonce={nonce}",
        content=f"<xml><Encrypt><![CDATA[{enc}]]></Encrypt></xml>",
        headers={"content-type": "text/xml"},
    )


def app_for(store, pipeline, snd, settings):
    settings.callback_async = False
    app = FastAPI()
    app.state.store = store
    app.state.pipeline = pipeline
    app.state.worker = Worker(pipeline, store, snd)
    app.include_router(callback_router)
    app.include_router(console_router)
    app.dependency_overrides[get_bot_crypto] = lambda: WeComCrypto(BOT_TOKEN, BOT_AES, CORP)
    return TestClient(app)


# ---------------------------------------------------------------- 报文解析
def test_parse_extracts_session_webhook_and_nested_content():
    env = bot.parse(ET.fromstring(_xml()), fallback_msg_id="fb")
    assert env.webhook_url == HOOK
    assert env.chat_id == GID and not env.is_single
    assert env.sender_id == "client_a" and env.sender_name == "客户甲"
    assert env.msg.content == "拖欠工资多久可以申请劳动仲裁？"
    # 群里机器人只收得到被 @ 的消息 → 恒视为点名，不依赖企微是否保留 @ 前缀
    assert env.msg.mentioned_bot is True


def test_parse_strips_mention_prefix():
    env = bot.parse(ET.fromstring(_xml(content="@松沪助理 离婚怎么分财产")), fallback_msg_id="fb")
    assert env.msg.content == "离婚怎么分财产"


def test_parse_single_chat_falls_back_to_sender_scoped_id():
    env = bot.parse(
        ET.fromstring(_xml(chat_id="", chat_type="single")), fallback_msg_id="fb"
    )
    assert env.is_single and env.group_id == "bot:client_a"


def test_parse_ignores_unsupported_type():
    xml = ET.fromstring("<xml><MsgType>image</MsgType></xml>")
    assert bot.parse(xml, fallback_msg_id="fb") is None


# ---------------------------------------------------------------- 自动建档
def test_first_mention_creates_profile_and_replies_via_callback_webhook(tmp_path):
    """员工零配置：群档案与发送地址都由回调自己带来。"""
    store, snd, pipeline, settings = make(tmp_path)
    client = app_for(store, pipeline, snd, settings)

    assert post_callback(client, WeComCrypto(BOT_TOKEN, BOT_AES, CORP), _xml()).text == "success"

    g = store.get_group(GID)
    assert g is not None, "首次 @ 应自动建档，人工才有地方补承办律师"
    assert g.bot_webhook == HOOK and g.bot_webhook_at is not None
    assert g.client_status == ClientStatus.PROSPECT
    assert g.notify_userid == "future", "没有接待人可查的群要用兜底接收人，否则简报无人可推"
    assert g.lawyer_userid == "", (
        "建档时不写数据归属——写了等于把全所会话的可见权发给那个人"
    )
    assert snd.calls and snd.calls[0][0] == HOOK


def test_add_to_chat_event_creates_profile_without_speaking(tmp_path):
    """机器人被拉进群：先建档让人工能提前配好，但不主动说话。"""
    store, snd, pipeline, settings = make(tmp_path)
    client = app_for(store, pipeline, snd, settings)

    post_callback(client, WeComCrypto(BOT_TOKEN, BOT_AES, CORP),
                  _xml(event=bot.EVENT_ADD_TO_CHAT))

    assert store.get_group(GID) is not None
    assert snd.calls == [] and store.list_decisions() == []


def test_webhook_refreshed_on_each_callback(tmp_path):
    store, snd, pipeline, settings = make(tmp_path)
    client = app_for(store, pipeline, snd, settings)
    crypto = WeComCrypto(BOT_TOKEN, BOT_AES, CORP)

    post_callback(client, crypto, _xml(msg_id="r1"), nonce="n1")
    post_callback(client, crypto, _xml(msg_id="r2", webhook=HOOK + "-new"), nonce="n2")
    assert store.get_group(GID).bot_webhook == HOOK + "-new"


# ---------------------------------------------------------------- 通道回落
def test_stale_session_webhook_falls_back_to_manual(tmp_path):
    """会话 webhook 过期后不能硬发——发失败等于客户没收到回复。"""
    store, snd, pipeline, settings = make(tmp_path)
    store.upsert_group(GroupProfile(
        group_id=GID, name="试点群", lawyer_userid="future",
        robot_webhook="manual-key",
        bot_webhook=HOOK,
        bot_webhook_at=datetime.now() - timedelta(seconds=settings.bot_webhook_ttl_seconds + 60),
    ))
    assert pipeline._reply_webhook(store.get_group(GID)) == "manual-key"


def test_fresh_session_webhook_wins_over_manual(tmp_path):
    store, _, pipeline, _ = make(tmp_path)
    store.upsert_group(GroupProfile(
        group_id=GID, robot_webhook="manual-key",
        bot_webhook=HOOK, bot_webhook_at=datetime.now(),
    ))
    assert pipeline._reply_webhook(store.get_group(GID)) == HOOK


# ---------------------------------------------------------------- 编辑不破坏通道
def test_console_edit_preserves_session_webhook(tmp_path):
    """改一次「承办律师」不该把自动拿到的发送地址抹掉。"""
    store, snd, pipeline, settings = make(tmp_path)
    client = app_for(store, pipeline, snd, settings)
    post_callback(client, WeComCrypto(BOT_TOKEN, BOT_AES, CORP), _xml())

    r = client.put(
        f"/console/groups/{GID}",
        json={"group_id": GID, "name": "劳动仲裁试点群", "lawyer_name": "魏",
              "lawyer_userid": "mr.Li"},
        headers={"x-admin-token": ADMIN},
    )
    assert r.status_code == 200
    g = store.get_group(GID)
    assert g.lawyer_userid == "mr.Li" and g.bot_webhook == HOOK


def test_diagnostics_flags_group_without_send_channel(tmp_path):
    store, snd, pipeline, settings = make(tmp_path)
    client = app_for(store, pipeline, snd, settings)
    store.upsert_group(GroupProfile(group_id=GID, lawyer_userid="future"))

    d = client.get("/console/diagnostics", headers={"x-admin-token": ADMIN}).json()
    assert d["bot"]["chats"] == 1 and d["bot"]["sendable"] == 0
    assert d["bot"]["hint"], "拿不到发送地址是静默失败，必须在自检里说清楚"

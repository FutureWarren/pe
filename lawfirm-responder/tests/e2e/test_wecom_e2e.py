"""端到端测试：模拟企业微信真实链路。

覆盖：加密回调进 → 签名校验/解密 → 判断引擎 → 合规闸门 → 机器人 webhook 出 →
律师提醒 → 人工接管 → 追问去重升级。全程走真实 HTTP 层与真实加解密，仅发送端为记录桩。
"""

import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from responder.config import Settings
from responder.console.api import router as console_router
from responder.gateway.callback import get_crypto
from responder.gateway.callback import router as callback_router
from responder.gateway.wecom_crypto import WeComCrypto
from responder.models import ClientStatus, GroupProfile
from responder.service import Pipeline
from responder.store.db import Store

TOKEN = "e2etoken"
AES_KEY = base64.b64encode(b"k" * 32).decode()[:43]
CORP_ID = "wwe2ecorp"


class RecordingSender:
    """发送端记录桩：接口与 WeComSender 一致，不出网。"""

    def __init__(self):
        self.robot: list[tuple[str, str]] = []
        self.group: list[tuple[str, str]] = []
        self.direct: list[tuple[str, str]] = []

    def send_robot_text(self, webhook, text):
        self.robot.append((webhook, text))
        return True

    def send_group_text(self, chat_id, text):
        self.group.append((chat_id, text))
        return True

    def send_direct_text(self, userid, text):
        self.direct.append((userid, text))
        return True


@pytest.fixture
def env(tmp_path):
    settings = Settings(mode="live", db_path=str(tmp_path / "e2e.db"))
    store = Store(settings.db_path)
    store.upsert_group(
        GroupProfile(
            group_id="chat_labor_01", name="劳动仲裁咨询群",
            client_status=ClientStatus.PROSPECT, case_type="劳动仲裁",
            lawyer_name="王", lawyer_userid="wang", backup_userid="li",
            robot_webhook="robot-key-001",
        )
    )
    sender = RecordingSender()
    crypto = WeComCrypto(TOKEN, AES_KEY, CORP_ID)

    app = FastAPI()
    app.state.store = store
    app.state.pipeline = Pipeline(store, sender, settings)
    app.include_router(callback_router)
    app.include_router(console_router)
    app.dependency_overrides[get_crypto] = lambda: crypto

    return TestClient(app), store, sender, crypto


def _post_encrypted(client, crypto, msg_id, content, chat_id="chat_labor_01", sender="client_a"):
    """构造一条企微加密回调消息并 POST，走真实签名与 AES 链路。"""
    plain = (
        f"<xml><MsgId>{msg_id}</MsgId><ChatId>{chat_id}</ChatId>"
        f"<FromUserName>{sender}</FromUserName><MsgType>text</MsgType>"
        f"<Content>{content}</Content></xml>"
    )
    encrypt = crypto.encrypt(plain)
    timestamp, nonce = "1753000000", f"n{msg_id}"
    sig = crypto.signature(timestamp, nonce, encrypt)
    return client.post(
        f"/wecom/callback?msg_signature={sig}&timestamp={timestamp}&nonce={nonce}",
        content=f"<xml><Encrypt><![CDATA[{encrypt}]]></Encrypt></xml>",
        headers={"content-type": "text/xml"},
    )


def test_url_verification_echo(env):
    client, _, _, crypto = env
    echostr = crypto.encrypt("echo-plain-123")
    ts, nonce = "1753000001", "vn1"
    sig = crypto.signature(ts, nonce, echostr)
    r = client.get(
        "/wecom/callback",
        params={"msg_signature": sig, "timestamp": ts, "nonce": nonce, "echostr": echostr},
    )
    assert r.status_code == 200 and r.text == "echo-plain-123"


def test_bad_signature_rejected(env):
    client, store, _, crypto = env
    plain = "<xml><MsgId>x</MsgId><MsgType>text</MsgType><Content>在吗</Content></xml>"
    encrypt = crypto.encrypt(plain)
    r = client.post(
        "/wecom/callback?msg_signature=badsig&timestamp=1&nonce=1",
        content=f"<xml><Encrypt><![CDATA[{encrypt}]]></Encrypt></xml>",
    )
    assert r.status_code == 403
    assert store.list_decisions() == []


def test_urgent_message_immediate_robot_reply_and_escalating_reminder(env):
    client, store, sender, crypto = env
    r = _post_encrypted(client, crypto, "m-urgent", "公司把我辞退了我要投诉你们这样处理")
    assert r.status_code == 200 and r.text == "success"

    # 紧急：免等待，机器人立即在群里安抚（话术为稳定变体之一）
    assert len(sender.robot) == 1
    webhook, text = sender.robot[0]
    assert webhook == "robot-key-001"
    assert "别急" in text or "别慌" in text
    # 加急单聊提醒承办律师，附原文与 AI 已回复内容
    assert sender.direct and "加急" in sender.direct[0][1]
    assert "投诉" in sender.direct[0][1]


def test_chitchat_stays_silent_but_logged(env):
    client, store, sender, crypto = env
    _post_encrypted(client, crypto, "m-chat", "谢谢王律师，辛苦了")
    assert sender.robot == [] and sender.direct == []
    decisions = store.list_decisions("chat_labor_01")
    assert decisions and decisions[0]["action"] == "silence"


def test_general_question_answered_after_wait_via_ingest(env):
    client, _, sender, _ = env
    # 调度器视角：等待时长已到（白天 150s），通用劳动法问题 → 直接回答路径
    r = client.post(
        "/ingest?seconds_unanswered=300",
        json={
            "msg_id": "m-general", "group_id": "chat_labor_01",
            "sender_id": "client_b", "content": "拖欠工资多久可以去劳动仲裁？",
        },
    )
    data = r.json()
    assert data["action"] == "answer" and data["should_speak"]
    assert len(sender.robot) == 1
    # 未成交群：销售顾问定位，收尾引导面谈
    assert "约个时间" in sender.robot[0][1]


def test_staff_takeover_suppresses_ai(env):
    client, store, sender, _ = env
    client.post(
        "/ingest",
        json={
            "msg_id": "m-staff", "group_id": "chat_labor_01",
            "sender_id": "wang", "content": "我来说明一下仲裁流程",
            "sender_is_staff": True,
        },
    )
    r = client.post(
        "/ingest?seconds_unanswered=300",
        json={
            "msg_id": "m-after-staff", "group_id": "chat_labor_01",
            "sender_id": "client_a", "content": "我的案子到哪一步了？",
        },
    )
    data = r.json()
    assert not data["should_speak"] and "gate:human-takeover" in data["reasons"]
    assert sender.robot == []  # 群里没有 AI 发言
    # 草稿仍入库供控制台复核
    replies = store.list_replies("chat_labor_01")
    assert replies and replies[0]["mode"] == "shadow"


def test_followup_policy_second_touch_then_suppress(env):
    client, store, sender, crypto = env
    for i in range(3):
        _post_encrypted(client, crypto, f"m-fu-{i}", "我要投诉你们这个服务态度")
    # 第 1 次正常话术，第 2 次二次安抚（不复读），第 3 次群内静默 + 升级
    assert len(sender.robot) == 2
    assert sender.robot[0][1] != sender.robot[1][1]
    assert "久等" in sender.robot[1][1]
    decisions = store.list_decisions("chat_labor_01")
    reasons = [d["reasons"] for d in decisions]
    assert any("followup:second-touch" in r for r in reasons)
    assert any("followup:suppressed-escalated" in r for r in reasons)
    assert len(sender.direct) == 3  # 每次都提醒了律师


def test_console_reflects_full_loop(env):
    client, _, _, crypto = env
    _post_encrypted(client, crypto, "m-c1", "你们收费标准是怎样的？")
    todo = client.get("/console/todo").json()
    assert todo and "收费标准" in todo[0]["summary"]
    metrics = client.get("/console/metrics").json()
    assert metrics["decisions_total"] >= 1 and metrics["compliance_blocked"] == 0

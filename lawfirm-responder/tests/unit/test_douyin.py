"""抖音企业号私信通道：回调解析 → 建档 → 判断 → 回复，以及平台配额约束。

这条通道真正的难点不是「怎么把字发出去」，而是平台的两条硬限制：
  ① 客户发言后 24 小时内才允许回复；
  ② 同一窗口内、客户下次开口之前最多 6 条（算的是**分条后**的真实条数）。
超发不是「多发了一条」，是接口报错 + 应用被平台标记。本文件主要测这两条。

不出网：以记录桩替代 DouyinClient。
"""

import hashlib
from datetime import datetime, timedelta

from responder.config import Settings
from responder.gateway import douyin
from responder.models import ClientStatus, GroupProfile, IncomingMessage
from responder.service import Pipeline
from responder.store.db import Store
from responder.worker import Worker

OPEN_ID = "_000abcOpenIdXyz789"
GID = f"dyim:{OPEN_ID}"


class FakeDouyin:
    def __init__(self, ok=True):
        self.sent: list[tuple[str, str]] = []
        self.ok = ok

    def available(self):
        return True

    def send_text(self, open_id, text):
        self.sent.append((open_id, text))
        return self.ok

    def texts(self) -> str:
        return "\n".join(t for _, t in self.sent)


class DirectSender:
    def __init__(self):
        self.direct: list[tuple[str, str]] = []

    def send_direct_text(self, userid, text):
        self.direct.append((userid, text))
        return True


def make_env(tmp_path, **over):
    db = str(tmp_path / "dy.db")
    store = Store(db)
    cfg = dict(
        mode="live", db_path=db, split_delay_seconds=0,
        douyin_client_key="ck", douyin_client_secret="cs",
        kf_default_lawyer_name="魏", llm_answer_enabled=False,
        llm_refine_enabled=False, lead_brief_enabled=False,
    )
    cfg.update(over)
    settings = Settings(**cfg)
    dy = FakeDouyin()
    sender = DirectSender()
    pipeline = Pipeline(store, sender=sender, settings=settings, douyin_client=dy)
    worker = Worker(pipeline, store, sender, douyin_client=dy)
    return store, dy, worker


def dy_msg(msg_id, text, *, content_as_str=False):
    content = {
        "server_message_id": msg_id,
        "message_type": "text",
        "text": text,
        "conversation_short_id": "conv-1",
        "nickname": "小王",
    }
    import json as _json
    return {
        "event": "imReceiveMsg",
        "from_user_id": OPEN_ID,
        "content": _json.dumps(content) if content_as_str else content,
    }


def dy_enter():
    return {
        "event": "imEnterDirectMessage",
        "from_user_id": OPEN_ID,
        "content": {"conversation_short_id": "conv-1", "nickname": "小王"},
    }


# ---------------------------------------------------------------- 报文解析
def test_parse_message_with_object_content():
    env = douyin.parse(dy_msg("m1", "公司拖欠我三个月工资"))
    assert env is not None and env.msg is not None
    assert env.msg.content == "公司拖欠我三个月工资"
    assert env.msg.msg_id == "m1"
    assert env.group_id == GID
    assert env.nickname == "小王"


def test_parse_message_with_string_content():
    """抖音常把 content 塞成 JSON 字符串——只支持对象的话线上直接哑掉。"""
    env = douyin.parse(dy_msg("m2", "仲裁要多久", content_as_str=True))
    assert env is not None and env.msg is not None
    assert env.msg.content == "仲裁要多久"


def test_parse_enter_event():
    env = douyin.parse(dy_enter())
    assert env is not None and env.is_enter and env.msg is None


def test_parse_verify_challenge():
    """配置回调地址时的挑战包：回显不对就什么都收不到。"""
    env = douyin.parse({"event": "verify_webhook", "content": {"challenge": 4213}})
    assert env is not None and env.challenge == 4213


def test_parse_ignores_unknown_event():
    assert douyin.parse({"event": "video_publish", "from_user_id": OPEN_ID}) is None


def test_parse_non_text_still_delivered():
    """图片/语音没有文字可判断，但必须建档转人工，不能静默丢。"""
    env = douyin.parse({
        "event": "imReceiveMsg", "from_user_id": OPEN_ID,
        "content": {"server_message_id": "m9", "message_type": "image"},
    })
    assert env is not None and env.msg is not None
    assert env.msg.msg_type == "image" and env.msg.content == ""


def test_conversation_id_does_not_collide_with_imported_leads():
    """导入客资用 dy:{手机号}，活对话用 dyim:{open_id}，两个命名空间不能撞。"""
    assert douyin.conversation_id(OPEN_ID).startswith("dyim:")
    assert not douyin.conversation_id(OPEN_ID).startswith("dy:1")


# ---------------------------------------------------------------- 签名
def test_signature_accepts_valid_and_rejects_forged():
    token, body = "tok-123", b'{"event":"imReceiveMsg"}'
    sig = hashlib.sha1(
        "".join(sorted([token, "1700000000", "abc", body.decode()])).encode()
    ).hexdigest()
    headers = {
        "x-douyin-signature": sig,
        "x-douyin-timestamp": "1700000000",
        "x-douyin-nonce": "abc",
    }
    assert douyin.verify_signature(token, headers, body)
    assert not douyin.verify_signature("wrong-token", headers, body)
    assert not douyin.verify_signature(token, {}, body)  # 没签名头一律拒


# ---------------------------------------------------------------- 端到端
def test_message_creates_profile_and_replies(tmp_path):
    store, dy, worker = make_env(tmp_path)
    worker.process_douyin(douyin.parse(dy_msg("m1", "我老公被拘留了怎么办")))

    g = store.get_group(GID)
    assert g is not None
    assert g.client_status == ClientStatus.PROSPECT
    assert g.douyin_open_id == OPEN_ID and g.is_douyin and g.is_kf
    assert "抖音私信" in g.name and "小王" in g.name
    assert dy.sent and dy.sent[0][0] == OPEN_ID
    assert "别急" in dy.texts() or "别慌" in dy.texts()


def test_enter_event_sends_welcome_once(tmp_path):
    store, dy, worker = make_env(tmp_path)
    worker.process_douyin(douyin.parse(dy_enter()))
    worker.process_douyin(douyin.parse(dy_enter()))  # 平台重复推送
    assert len(dy.sent) == 1
    assert "上海松沪律师事务所" in dy.sent[0][1]


def test_welcome_skipped_for_returning_customer(tmp_path):
    store, dy, worker = make_env(tmp_path)
    worker.process_douyin(douyin.parse(dy_msg("m1", "拖欠工资多久可以申请劳动仲裁？")))
    before = len(dy.sent)
    worker.process_douyin(douyin.parse(dy_enter()))
    assert len(dy.sent) == before, "老客户回访不再自我介绍"


def test_duplicate_message_ignored(tmp_path):
    store, dy, worker = make_env(tmp_path)
    env = dy_msg("same-id", "我的案子到哪一步了")
    worker.process_douyin(douyin.parse(env))
    n = len(dy.sent)
    worker.process_douyin(douyin.parse(env))
    assert len(dy.sent) == n


def test_disabled_channel_is_silent(tmp_path):
    store, dy, worker = make_env(tmp_path, douyin_enabled=False)
    worker.process_douyin(douyin.parse(dy_msg("m1", "我老公被拘留了")))
    assert dy.sent == []


def test_shadow_mode_never_sends(tmp_path):
    store, dy, worker = make_env(tmp_path, mode="shadow")
    worker.process_douyin(douyin.parse(dy_msg("m1", "我老公被拘留了")))
    assert dy.sent == []
    assert store.list_replies(GID), "影子模式仍要留草稿"


# ---------------------------------------------------------------- 平台配额
def _pipeline(tmp_path, **over):
    store, dy, worker = make_env(tmp_path, **over)
    return store, dy, worker.pipeline


def test_split_capped_for_douyin(tmp_path):
    """分条上限在抖音要收敛：一条回复最多拆 2 条，不能按微信的 3 条来。"""
    store, dy, pipe = _pipeline(tmp_path, split_max_parts=3, douyin_split_max_parts=2)
    store.upsert_group(GroupProfile(group_id=GID, douyin_open_id=OPEN_ID, lawyer_name="魏"))
    store.save_message(IncomingMessage(
        msg_id="c1", group_id=GID, sender_id=OPEN_ID, content="客户开口了"))
    ok, parts = pipe._send_group(store.get_group(GID), GID, "第一句。第二句。第三句。")
    assert ok and parts <= 2


def test_quota_blocks_send_when_exhausted(tmp_path):
    """窗口内已发满 6 条 → 不再发，宁可少说一句也不要把通道打死。"""
    store, dy, pipe = _pipeline(tmp_path)
    store.upsert_group(GroupProfile(group_id=GID, douyin_open_id=OPEN_ID))
    store.save_message(IncomingMessage(
        msg_id="c1", group_id=GID, sender_id=OPEN_ID, content="客户开口了"))
    store.save_reply("c1", GID, "已发", "live", True, parts=6)

    assert pipe._douyin_budget(GID) == 0
    ok, parts = pipe._send_group(store.get_group(GID), GID, "还想再说一句")
    assert not ok and parts == 0 and dy.sent == []


def test_quota_counts_split_parts_not_reply_rows(tmp_path):
    """限额算的是真实消息条数：3 行回复各拆 2 条 = 6 条，已经满了。"""
    store, dy, pipe = _pipeline(tmp_path)
    store.upsert_group(GroupProfile(group_id=GID, douyin_open_id=OPEN_ID))
    store.save_message(IncomingMessage(
        msg_id="c1", group_id=GID, sender_id=OPEN_ID, content="客户开口了"))
    for i in range(3):
        store.save_reply(f"r{i}", GID, "回复", "live", True, parts=2)
    assert pipe._douyin_budget(GID) == 0


def test_quota_counts_partially_failed_sends(tmp_path):
    """发了一半失败：前半截平台已经收下并计了数，不能不算。"""
    store, dy, pipe = _pipeline(tmp_path)
    store.upsert_group(GroupProfile(group_id=GID, douyin_open_id=OPEN_ID))
    store.save_message(IncomingMessage(
        msg_id="c1", group_id=GID, sender_id=OPEN_ID, content="客户开口了"))
    store.save_reply("r1", GID, "发了一半", "failed", True, parts=4)
    assert pipe._douyin_budget(GID) == 2


def test_shadow_drafts_do_not_consume_quota(tmp_path):
    """影子模式没有真的发出去，不该占配额。"""
    store, dy, pipe = _pipeline(tmp_path)
    store.upsert_group(GroupProfile(group_id=GID, douyin_open_id=OPEN_ID))
    store.save_message(IncomingMessage(
        msg_id="c1", group_id=GID, sender_id=OPEN_ID, content="客户开口了"))
    store.save_reply("r1", GID, "草稿", "shadow", True, parts=3)
    assert pipe._douyin_budget(GID) == 6


def test_quota_resets_when_customer_speaks_again(tmp_path):
    """客户再次开口 → 新窗口，配额重置。"""
    store, dy, pipe = _pipeline(tmp_path)
    store.upsert_group(GroupProfile(group_id=GID, douyin_open_id=OPEN_ID))
    store.save_message(IncomingMessage(
        msg_id="c1", group_id=GID, sender_id=OPEN_ID, content="第一句",
        created_at=datetime.now() - timedelta(minutes=10)))
    store.save_reply("r1", GID, "回复", "live", True, parts=6)
    assert pipe._douyin_budget(GID) == 0

    store.save_message(IncomingMessage(
        msg_id="c2", group_id=GID, sender_id=OPEN_ID, content="第二句"))
    assert pipe._douyin_budget(GID) == 6


def test_no_send_outside_24h_window(tmp_path):
    """客户最后一次发言已超 24 小时 → 平台不允许回复，接口会直接拒。"""
    store, dy, pipe = _pipeline(tmp_path)
    store.upsert_group(GroupProfile(group_id=GID, douyin_open_id=OPEN_ID))
    store.save_message(IncomingMessage(
        msg_id="c1", group_id=GID, sender_id=OPEN_ID, content="很久以前说的",
        created_at=datetime.now() - timedelta(hours=25)))
    assert pipe._douyin_budget(GID) == 0


def test_no_send_before_customer_ever_speaks(tmp_path):
    """客户一句没说过 → 只能回复、不能主动发起。"""
    store, dy, pipe = _pipeline(tmp_path)
    store.upsert_group(GroupProfile(group_id=GID, douyin_open_id=OPEN_ID))
    assert pipe._douyin_budget(GID) == 0


# ---------------------------------------------------------------- 回调路由
def _client(tmp_path, **over):
    """真正跑一遍 HTTP 路由：回调配置是律所侧第一步，也是最容易卡住的一步。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from responder.gateway.callback import router as callback_router

    cfg = dict(mode="shadow", db_path=str(tmp_path / "r.db"), callback_async=False)
    cfg.update(over)
    settings = Settings(**cfg)
    store = Store(settings.db_path)
    app = FastAPI()
    app.state.store = store
    app.state.pipeline = Pipeline(store, None, settings)
    app.include_router(callback_router)
    return TestClient(app)


def test_callback_echoes_challenge(tmp_path):
    """配置回调地址时平台先发挑战包，回显不对后面什么都收不到。"""
    c = _client(tmp_path)
    r = c.post("/douyin/callback",
               json={"event": "verify_webhook", "content": {"challenge": 4213}})
    assert r.status_code == 200 and r.json() == {"challenge": 4213}


def test_callback_rejects_bad_signature(tmp_path):
    c = _client(tmp_path, douyin_callback_token="tok-123")
    r = c.post("/douyin/callback", json=dy_msg("m1", "你好"),
               headers={"x-douyin-signature": "deadbeef"})
    assert r.status_code == 403


def test_callback_accepts_signed_payload(tmp_path):
    import json as _json

    c = _client(tmp_path, douyin_callback_token="tok-123")
    body = _json.dumps(dy_msg("m1", "拖欠工资怎么办")).encode()
    sig = hashlib.sha1(
        "".join(sorted(["tok-123", "1700000000", "n1", body.decode()])).encode()
    ).hexdigest()
    r = c.post("/douyin/callback", content=body, headers={
        "content-type": "application/json",
        "x-douyin-signature": sig,
        "x-douyin-timestamp": "1700000000",
        "x-douyin-nonce": "n1",
    })
    assert r.status_code == 200 and r.json()["err_no"] == 0


def test_callback_always_200_on_junk(tmp_path):
    """认不出的报文也要立刻回 200，否则平台按超时反复重推。"""
    c = _client(tmp_path)
    r = c.post("/douyin/callback", json={"event": "video_publish"})
    assert r.status_code == 200


def test_enter_event_marker_does_not_open_the_window(tmp_path):
    """进会话事件只是个占位，不是客户发言，不能拿它当窗口起点。"""
    store, dy, pipe = _pipeline(tmp_path)
    store.upsert_group(GroupProfile(group_id=GID, douyin_open_id=OPEN_ID))
    store.save_message(IncomingMessage(
        msg_id="dy-enter-x", group_id=GID, sender_id=OPEN_ID,
        content="", msg_type="event"))
    assert pipe._douyin_budget(GID) == 0

"""外部渠道接入口：给 RPA 一类工具用的通用进出口。

这一层存在的理由是「平台不给 API，但客户在那儿」——美团、点评这些既没有
开放接口，也不允许把人引到站外。RPA 能替人点那些按钮。

设计上有三条取舍写在测试里，因为它们最容易在后续改动中被推翻：

1. **RPA 只是一只手，不是一个脑子。** 判断、生成、合规、评分、派单全在我们
   这一侧；对接方只做搬运。所以外部渠道的会话必须与微信客服**同构**——
   多接一个渠道，不该多一个合规缺口。
2. **回复先落发件箱，不靠 HTTP 同步返回。** 那头一次超时，那句话就永远消失了，
   而客户还在等。销账要等对方明确 ack：宁可重发一句，不可丢一句。
3. **没有动静本身要能报警。** RPA 那台机器卡住是静默失败：客户在等，
   我们后台一片安静、日志里连一行错误都没有。
"""

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from responder.config import Settings
from responder.gateway.channel import router as channel_router
from responder.models import ClientStatus
from responder.service import Pipeline
from responder.store.db import Store
from responder.worker import Worker

TOKEN = "chan-secret-123"
HEAD = {"X-Channel-Token": TOKEN}


class Snd:
    def __init__(self):
        self.direct = []

    def send_direct_text(self, userid, text):
        self.direct.append((userid, text))
        return True

    def send_group_text(self, *a, **k):
        return True

    def send_robot_text(self, *a, **k):
        return True


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = Settings(
        mode="live", db_path=str(tmp_path / "ch.db"), channel_token=TOKEN,
        default_notify_userid="wei", office_name="上海松沪律师事务所",
        split_messages=False, llm_provider="none",
    )
    monkeypatch.setattr("responder.gateway.channel.get_settings", lambda: settings)
    store = Store(settings.db_path)
    pipeline = Pipeline(store, Snd(), settings)
    app = FastAPI()
    app.state.store = store
    app.state.pipeline = pipeline
    app.include_router(channel_router)
    return TestClient(app), store, settings


def send(c, **kw):
    body = {"channel": "meituan", "external_id": "u-1", "content": "在吗"}
    body.update(kw)
    return c.post("/channel/inbound", json=body, headers=HEAD)


# ---------------------------------------------------------------- 鉴权
def test_token_is_required(env):
    c, _, _ = env
    assert c.post("/channel/inbound", json={"channel": "meituan",
                                            "external_id": "u"}).status_code == 401
    assert send(c, **{}).status_code == 200


def test_channel_token_is_not_the_admin_token(tmp_path, monkeypatch):
    """RPA 跑在一台随时可能被人碰的桌面电脑上，令牌等于摊在那儿。
    和控制台共用一个，控制台就跟着一起丢了。"""
    settings = Settings(db_path=str(tmp_path / "x.db"), admin_token="admin-secret",
                        channel_token="")
    monkeypatch.setattr("responder.gateway.channel.get_settings", lambda: settings)
    store = Store(settings.db_path)
    app = FastAPI()
    app.state.store = store
    app.state.pipeline = Pipeline(store, Snd(), settings)
    app.include_router(channel_router)
    c = TestClient(app)
    # 没配渠道令牌 = 接入口关闭。**不许**回落到 admin_token
    r = c.post("/channel/inbound", json={"channel": "meituan", "external_id": "u"},
               headers={"X-Channel-Token": "admin-secret"})
    assert r.status_code == 403


def test_channel_id_must_be_a_clean_slug(env):
    """渠道标识要进 group_id 主键。中文名和空格进去，后面每一处按前缀
    解析的地方都要遭殃。"""
    c, _, _ = env
    assert send(c, channel="美团").status_code == 400
    assert send(c, channel="mei tuan").status_code == 400
    assert send(c, channel="meituan-jingan").status_code == 200


# ---------------------------------------------------------------- 建档与同构
def test_first_message_creates_a_profile(env):
    """客户在美团上问一句就是个陌生人。要求谁先去后台手工建一条记录，
    那一步没人会做。"""
    c, store, _ = env
    r = send(c, content="公司拖欠我三个月工资", name="张先生")
    gid = r.json()["group_id"]
    assert gid == "ch:meituan:u-1"
    g = store.get_group(gid)
    assert g is not None
    assert g.client_status is ClientStatus.PROSPECT
    assert g.ext_channel == "meituan" and g.ext_user_id == "u-1"


def test_profile_always_gets_a_notify_target(env):
    """名册为空 + 兜底接收人为空 = 线索照样入库评分，但那张交接单一个人
    也收不到，而控制台里看什么都正常。静默失败是最贵的 bug。"""
    c, store, _ = env
    send(c, content="被辞退了想咨询")
    g = store.get_group("ch:meituan:u-1")
    assert g.notify_userid == "wei"
    assert g.lawyer_userid == "", "提醒接收人 ≠ 数据归属，建档只落前者"


def test_external_sessions_are_one_on_one_like_wechat_kf(env):
    """判断层同构是整套多渠道方案的地基：多接一个渠道，
    不该多一个合规缺口，也不该多一套话术。"""
    c, store, _ = env
    send(c, content="你好")
    assert store.get_group("ch:meituan:u-1").is_kf is True


def test_replies_never_mention_a_group_chat(env):
    """「在群里」只对群聊说。外部渠道是一对一窗口，没有群。"""
    c, store, _ = env
    send(c, content="公司拖欠我三个月工资，还把我辞退了")
    texts = " ".join(x["text"] for x in store.pending_outbound("ch:meituan:u-1", 20))
    assert "群" not in texts


# ---------------------------------------------------------------- 发件箱
def test_reply_lands_in_the_outbox_not_a_push(env):
    c, store, _ = env
    r = send(c, content="公司拖欠我三个月工资，还把我辞退了")
    replies = r.json()["replies"]
    assert replies, "该说的话要么在返回里，要么在发件箱里，不能凭空消失"
    assert store.pending_outbound("ch:meituan:u-1")


def test_unacked_replies_come_back_next_time(env):
    """那头一次超时，这句话不能就此消失——客户还在等。"""
    c, store, _ = env
    first = send(c, content="公司拖欠我三个月工资").json()["replies"]
    assert first
    again = c.get("/channel/outbox", params={"channel": "meituan", "external_id": "u-1"},
                  headers=HEAD).json()["replies"]
    assert [x["id"] for x in again] == [x["id"] for x in first]


def test_acked_replies_do_not_come_back(env):
    c, store, _ = env
    ids = [x["id"] for x in send(c, content="公司拖欠我三个月工资").json()["replies"]]
    r = c.post("/channel/ack", json={"ids": ids, "channel": "meituan"}, headers=HEAD)
    assert r.json()["acked"] == len(ids)
    assert c.get("/channel/outbox",
                 params={"channel": "meituan", "external_id": "u-1"},
                 headers=HEAD).json()["replies"] == []


def test_duplicate_delivery_is_not_answered_twice(env):
    """那头重试是常态，不是异常。"""
    c, store, _ = env
    send(c, content="公司拖欠我三个月工资", msg_id="m-1")
    n1 = len(store.pending_outbound("ch:meituan:u-1", 50))
    send(c, content="公司拖欠我三个月工资", msg_id="m-1")
    assert len(store.pending_outbound("ch:meituan:u-1", 50)) == n1


def test_a_broken_pipeline_still_acknowledges_the_message(env, monkeypatch):
    """判断链炸了不能让那头以为消息没送到——它会一直重试同一条，
    于是一次故障变成一场风暴。"""
    c, store, _ = env

    def boom(*a, **k):
        raise RuntimeError("模型挂了")

    monkeypatch.setattr(c.app.state.pipeline, "handle", boom)
    assert send(c, content="在吗").status_code == 200


# ---------------------------------------------------------------- 心跳
def test_heartbeat_records_liveness_without_traffic(env):
    """「今天没客户」和「三天前就挂了」在数据上必须长得不一样。"""
    c, store, _ = env
    c.post("/channel/heartbeat", json={"channel": "meituan", "label": "美团-静安"},
           headers=HEAD)
    row = store.list_channel_state()[0]
    assert row["last_seen_at"] and row["last_inbound_at"] is None
    assert row["label"] == "美团-静安"


def test_inbound_counts_as_both(env):
    c, store, _ = env
    send(c, content="在吗")
    row = store.list_channel_state()[0]
    assert row["last_inbound_at"] and row["inbound_total"] == 1


# ---------------------------------------------------------------- 告警
def _worker(store, settings, snd):
    return Worker(Pipeline(store, snd, settings), store, sender=snd)


def test_alerts_when_replies_pile_up_undelivered(env):
    """RPA 卡住是静默失败：客户在等，我们后台一片安静。"""
    c, store, settings = env
    snd = Snd()
    store.queue_outbound("ch:meituan:u-9", ["您好"], channel="meituan")
    with store._conn() as conn:  # 造一条排了很久的
        conn.execute("UPDATE outbox SET created_at=?",
                     ((datetime.now() - timedelta(hours=2)).isoformat(),))
    store.touch_channel("meituan", inbound=True)

    _worker(store, settings, snd)._sweep_channel_health(datetime.now())

    assert snd.direct and "发不出去" in snd.direct[0][1]
    assert snd.direct[0][0] == "wei"


def test_alerts_when_a_channel_goes_completely_silent(env):
    c, store, settings = env
    snd = Snd()
    store.touch_channel("meituan", label="美团-静安")
    with store._conn() as conn:
        conn.execute("UPDATE channel_state SET last_seen_at=?",
                     ((datetime.now() - timedelta(hours=20)).isoformat(),))

    _worker(store, settings, snd)._sweep_channel_health(datetime.now())

    assert snd.direct and "没有任何动静" in snd.direct[0][1]


def test_does_not_alert_twice_for_the_same_outage(env):
    """每 10 秒轰炸一次，很快就没人看了——而下一次真出事时也一样没人看。"""
    c, store, settings = env
    snd = Snd()
    store.touch_channel("meituan", label="美团-静安")
    with store._conn() as conn:
        conn.execute("UPDATE channel_state SET last_seen_at=?",
                     ((datetime.now() - timedelta(hours=20)).isoformat(),))
    w = _worker(store, settings, snd)

    w._sweep_channel_health(datetime.now())
    w._sweep_channel_health(datetime.now())

    assert len(snd.direct) == 1


def test_recovery_rearms_the_alarm(env):
    """报过一次就永远不再报，等于第二次故障没人知道。"""
    c, store, settings = env
    snd = Snd()
    store.touch_channel("meituan", label="美团-静安")
    with store._conn() as conn:
        conn.execute("UPDATE channel_state SET last_seen_at=?",
                     ((datetime.now() - timedelta(hours=20)).isoformat(),))
    w = _worker(store, settings, snd)
    w._sweep_channel_health(datetime.now())

    store.touch_channel("meituan", inbound=True)  # 恢复了
    assert store.list_channel_state()[0]["alerted_at"] is None


def test_no_alert_when_nobody_can_receive_it(env):
    """没人可发就别空转——也别把「已报警」记下来，配好了要能补报。"""
    c, store, settings = env
    settings.default_notify_userid = ""
    settings.bot_default_notify_userid = ""
    snd = Snd()
    store.touch_channel("meituan")
    with store._conn() as conn:
        conn.execute("UPDATE channel_state SET last_seen_at=?",
                     ((datetime.now() - timedelta(hours=20)).isoformat(),))

    _worker(store, settings, snd)._sweep_channel_health(datetime.now())

    assert snd.direct == []
    assert store.list_channel_state()[0]["alerted_at"] is None


# ---------------------------------------------------------------- 来源归因
def test_export_shows_the_real_channel(env):
    """所有从微信客服进来的人在库里长得一模一样，光看通道只能答出
    「微信客服」——而视频号来的、官网来的、名片扫的是三笔不同的生意。"""
    from responder import exporter

    c, store, settings = env
    send(c, content="我想找律师，公司拖欠我三个月工资，电话13712345678")
    rows = exporter.build_rows(store, store.leads_in_range(None, None),
                               settings=settings)
    assert len(rows) > 1, "这条该已经被评为线索"
    header, first = rows[0], rows[1]
    assert first[header.index("来源")] == "美团"


# ---------------------------------------------------------------- 主动发起
def test_pending_lists_who_is_waiting_to_be_spoken_to(env):
    """主动发起的话全靠这个接口。客户聊一半不说话了，系统会生成一句挽留——
    可那句话排进发件箱之后没有任何人会来取，除非客户自己再开口，
    而他要是再开口，挽留本身就没意义了。"""
    c, store, _ = env
    send(c, content="公司拖欠我三个月工资")  # 建档并产生回复
    c.post("/channel/ack", json={"ids": [x["id"] for x in store.pending_outbound(
        "ch:meituan:u-1", 50)], "channel": "meituan"}, headers=HEAD)
    # 之后系统主动生成一句（挽留/跟进），客户并没有再说话
    store.queue_outbound("ch:meituan:u-1", ["刚才的事您还需要了解吗"],
                         channel="meituan")

    rows = c.get("/channel/pending", params={"channel": "meituan"},
                 headers=HEAD).json()["conversations"]

    assert [r["external_id"] for r in rows] == ["u-1"]
    assert rows[0]["count"] == 1


def test_pending_skips_conversations_with_no_external_id(env):
    """老数据可能没有渠道字段。跳过一条，别让那头拿到一个发不出去的目标。"""
    c, store, _ = env
    store.queue_outbound("kf:wk:someone", ["您好"], channel="meituan")
    rows = c.get("/channel/pending", params={"channel": "meituan"},
                 headers=HEAD).json()["conversations"]
    assert rows == []


def test_pending_requires_the_token(env):
    c, _, _ = env
    assert c.get("/channel/pending", params={"channel": "meituan"}).status_code == 401

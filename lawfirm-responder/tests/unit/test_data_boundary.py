"""律师个人令牌只看得到自己的客户（2026-08-12 体检最严重一条）。

原来「该通知谁」和「这是谁的数据」共用 `lawyer_userid` 一个字段。
微信客服会话建档时把企微返回的**第一位接待人**填了进去，而律师工作台
正是按这个字段放行的——于是名册里排第一的那位普通律师，个人链接一点开
就能看到全所每一通咨询原文、每一条 AI 回复、全部会话档案，
还能把任意客户的会话「转给我」。

而这批人往往正是可能跳槽带客户走的人。护栏文档上写着「律师只看自己名下数据」，
所以没有人会去查——**这正是最贵的那种坏：看起来一切正常。**

修法：拆成两个字段。`lawyer_userid` 只在分案引擎真的派单、或人工指定承办律师时才写；
建档一律只落 `notify_userid`。
"""

from datetime import datetime

from responder.config import Settings
from responder.models import ClientStatus, GroupProfile
from responder.store.db import Store
from responder.worker import KfSyncJob, Worker

OPEN_KFID = "wk-bound"
GID = f"kf:{OPEN_KFID}:wmOther"


class Kf:
    """两位接待人：wei 排第一（旧版会把全所会话判成他的）。"""

    def __init__(self):
        self.sent = []

    def available(self):
        return True

    def servicer_list(self, kfid):
        return ["wei", "zhang"]

    def sync_msg(self, token, cursor, open_kfid):
        return {
            "msg_list": [{
                "msgid": "mm-1", "open_kfid": OPEN_KFID, "external_userid": "wmOther",
                "origin": 3, "msgtype": "text", "text": {"content": "我被公司辞退了"},
                "send_time": int(datetime.now().timestamp()),
            }],
            "next_cursor": "c1", "has_more": 0,
        }

    def send_text(self, kfid, ext, text):
        self.sent.append(text)
        return True

    def service_state(self, kfid, ext):
        return 1

    def to_robot(self, kfid, ext):
        return True

    def transfer(self, kfid, ext, userid):
        return True


class Snd:
    def send_direct_text(self, userid, text):
        return True


def make(tmp_path, **over):
    from responder.service import Pipeline

    db = str(tmp_path / "b.db")
    store = Store(db)
    cfg = dict(
        mode="live", db_path=db, wecom_kf_secret="s", admin_token="",
        llm_answer_enabled=False, llm_refine_enabled=False,
        split_messages=False, split_delay_seconds=0,
    )
    cfg.update(over)
    settings = Settings(**cfg)
    kf = Kf()
    p = Pipeline(store, sender=Snd(), settings=settings, kf_client=kf)
    return store, kf, p, Worker(p, store, sender=Snd(), kf_client=kf)


def test_the_first_servicer_does_not_inherit_every_conversation(tmp_path):
    """**本组的核心。** 建档只落「该通知谁」，不落「这是谁的数据」。"""
    store, _, _, w = make(tmp_path)

    w.process_kf(KfSyncJob(token="tk", open_kfid=OPEN_KFID))

    g = store.get_group(GID)
    assert g is not None
    assert g.notify_userid == "wei", "交接单还是得有人收——这一条不能因为修权限而丢"
    assert g.lawyer_userid == "", "排第一的接待人不该因此拿到这通对话的归属"
    assert store.own_group_ids("wei") == set(), (
        "他没被派到这单，工作台里就不该出现它"
    )


def test_assignment_is_what_grants_ownership(tmp_path):
    """真派给他之后，才该看得到——这条不能因为修权限而误伤。"""
    from responder import assignment

    store, _, _, w = make(tmp_path)
    store.upsert_lawyer("zhang", {"name": "张", "role": "lawyer",
                                  "on_duty": True, "active": True})
    w.process_kf(KfSyncJob(token="tk", open_kfid=OPEN_KFID))
    group = store.get_group(GID)
    store.upsert_lead(GID, {"intent": "hot"})

    assignment.assign(store, group, GID, store.get_lawyer("zhang"))

    assert store.own_group_ids("zhang") == {GID}
    assert store.own_group_ids("wei") == set()


def test_reminders_still_reach_someone_when_nobody_is_assigned(tmp_path):
    """拆字段之后最容易砸掉的就是这条：单子还没派人时，交接单不能没人收。"""
    from responder import lead

    store, _, p, w = make(tmp_path, default_notify_userid="")
    w.process_kf(KfSyncJob(token="tk", open_kfid=OPEN_KFID))
    group = store.get_group(GID)

    sent = []

    class Rec:
        def send_direct_text(self, userid, text):
            sent.append((userid, text))
            return True

    lead.dispatch(
        store, group,
        [{"content": "我想委托你们，电话13800138000", "sender_is_staff": False,
          "created_at": datetime.now().isoformat()}],
        Rec(), settings=p.settings, force=True,
    )
    assert sent and sent[0][0] == "wei", "没派人时交接单该走建档时落的提醒接收人"


def test_signed_group_owner_is_still_the_owner(tmp_path):
    """群聊里人工维护的承办律师是真归属，不受这次拆分影响。"""
    store, _, _, _ = make(tmp_path)
    store.upsert_group(GroupProfile(
        group_id="g-signed", client_status=ClientStatus.SIGNED,
        lawyer_userid="mr.Li", lawyer_name="李",
    ))
    assert store.own_group_ids("mr.Li") == {"g-signed"}


def test_backfill_endpoint_fills_the_notify_field_not_the_owner_field(tmp_path):
    """控制台那个「回填接待人」按钮同样不能发出去数据权限。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from responder.console.api import router

    store, kf, p, _ = make(tmp_path)
    store.upsert_group(GroupProfile(
        group_id=GID, client_status=ClientStatus.PROSPECT,
        kf_open_kfid=OPEN_KFID, kf_external_userid="wmOther",
    ))
    app = FastAPI()
    app.include_router(router)
    app.state.store, app.state.pipeline = store, p

    r = TestClient(app).post("/console/kf/sync-servicers")

    assert r.status_code == 200
    g = store.get_group(GID)
    assert g.notify_userid == "wei" and g.lawyer_userid == ""


def test_the_example_shown_to_users_is_itself_rejected(tmp_path):
    """文档和控制台上写出来的那句示例口令，写出来的那一刻就不再是秘密。

    旧判据是「含律所名且短于 16 位才拒」，而当时的示例 `songhu-jiufeng-88`
    恰好 17 位——它同时是最多人会照抄的一句，等于在门上留了条现成的路。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from responder.console.api import router

    store, _, p, _ = make(tmp_path)
    app = FastAPI()
    app.include_router(router)
    app.state.store, app.state.pipeline = store, p
    client = TestClient(app)

    for weak in ("songhu-jiufeng-88", "qingchen-6-lubiao", "songhu-law-2026"):
        r = client.post("/console/admin-token", json={"token": weak})
        assert r.status_code == 400, weak
    assert client.post(
        "/console/admin-token", json={"token": "mabuteng-7-hetong"}
    ).status_code == 200


def test_the_console_and_the_rule_cannot_drift_apart():
    """控制台页面上印的那句示例，必须正是服务端拒绝的那一句。

    换示例时只改一边，就等于重新把那条路留出来——而没有任何地方会提示。
    """
    from pathlib import Path

    import responder
    from responder.console.api import EXAMPLE_TOKEN

    root = Path(responder.__file__).resolve().parent.parent
    html = (root / "responder/console/static/index.html").read_text()
    assert EXAMPLE_TOKEN in html, "控制台展示的示例与服务端拒绝的那句对不上了"
    assert EXAMPLE_TOKEN in (root / "docs/deploy.md").read_text()

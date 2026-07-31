"""成熟化批次的行为契约：单一通知、非文本承接、检索分页、备注、批量分派。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from responder.config import Settings
from responder.console.api import router as console_router
from responder.models import ClientStatus, GroupProfile, IncomingMessage
from responder.service import Pipeline
from responder.store.db import Store
from responder.worker import Worker

ADMIN = "adm"


class Snd:
    def __init__(self):
        self.direct: list[tuple[str, str]] = []
        self.robot: list[str] = []

    def send_direct_text(self, userid, text):
        self.direct.append((userid, text))
        return True

    def send_robot_text(self, w, t):
        self.robot.append(t)
        return True

    def send_group_text(self, c, t):
        self.robot.append(t)
        return True


class KfSnd:
    def __init__(self):
        self.sent: list[str] = []

    def available(self):
        return True

    def send_text(self, kfid, ext, text):
        self.sent.append(text)
        return True


def make(tmp_path, **over):
    db = str(tmp_path / "m.db")
    store = Store(db)
    kw = dict(
        mode="live", db_path=db, admin_token=ADMIN, split_delay_seconds=0,
        llm_refine_enabled=False, default_notify_userid="reception",
    )
    kw.update(over)
    settings = Settings(**kw)
    snd = Snd()
    kf = KfSnd()
    pipeline = Pipeline(store, snd, settings, kf_client=kf)
    return store, snd, kf, pipeline, settings


def client_for(store, pipeline, snd):
    app = FastAPI()
    app.state.store = store
    app.state.pipeline = pipeline
    app.state.worker = Worker(pipeline, store, snd)
    app.include_router(console_router)
    return TestClient(app)


def H(t=ADMIN):
    return {"x-admin-token": t}


# ---------------------------------------------------------------- 单一通知
def test_group_hot_message_sends_exactly_one_dm(tmp_path):
    """群里客户留电话：交接单推一条 DM，逐条提醒不再叠加第二条。"""
    store, snd, _, pipeline, _ = make(tmp_path)
    store.upsert_group(GroupProfile(
        group_id="grp1", name="试点群", lawyer_userid="wei",
        robot_webhook="rk", client_status=ClientStatus.PROSPECT,
    ))
    pipeline.handle(IncomingMessage(
        msg_id="m1", group_id="grp1", sender_id="c",
        content="我想委托你们，电话17721275495", mentioned_bot=True,
    ))
    assert len(snd.direct) == 1, f"应只有一条 DM，实际 {len(snd.direct)}"
    assert "客户诉求" in snd.direct[0][1]  # 是交接单而不是逐条提醒
    assert store.pending_reminders() == []


def test_shadow_mode_hot_message_lands_in_lead_queue_unpushed(tmp_path):
    """影子模式：一条 DM 都不出；转化信号进线索队列并标「未推送」，复核链在控制台。"""
    store, snd, _, pipeline, _ = make(tmp_path, mode="shadow")
    store.upsert_group(GroupProfile(
        group_id="grp1", name="试点群", lawyer_userid="wei", robot_webhook="rk",
    ))
    pipeline.handle(IncomingMessage(
        msg_id="m1", group_id="grp1", sender_id="c",
        content="我想委托你们，电话17721275495", mentioned_bot=True,
    ))
    assert snd.direct == []
    row = store.get_lead("grp1")
    assert row and row["contact"] == "17721275495"
    assert not row["notified_at"]  # 控制台线索卡上会亮「未推送」，人工可补推


# ---------------------------------------------------------------- 非文本承接
def kf_group(store):
    g = GroupProfile(
        group_id="kf:a:c1", name="微信客服 · 客户c1", lawyer_userid="wei",
        kf_open_kfid="a", kf_external_userid="c1",
        client_status=ClientStatus.PROSPECT,
    )
    store.upsert_group(g)
    return g


def test_kf_voice_message_gets_ack_not_silence(tmp_path):
    """客服会话里发语音：客户必须收到承接话术，而不是被晾着。"""
    store, snd, kf, pipeline, _ = make(tmp_path)
    kf_group(store)
    d = pipeline.handle(IncomingMessage(
        msg_id="v1", group_id="kf:a:c1", sender_id="c1",
        content="", msg_type="voice",
    ))
    assert d.action.value == "handoff" and "kf:non-text-handoff" in d.reasons
    assert kf.sent, "客户应收到承接回复"
    # 提醒里说清是语音、去哪看，而不是留空让律师猜
    todo = store.pending_reminders()
    assert todo and "语音" in todo[0]["question"]


def test_group_nontext_stays_silent(tmp_path):
    """群里的图片/表情满天飞，AI 不该对每张图都出声——仅客服会话承接非文本。"""
    store, snd, _, pipeline, _ = make(tmp_path)
    store.upsert_group(GroupProfile(group_id="grp1", robot_webhook="rk"))
    d = pipeline.handle(IncomingMessage(
        msg_id="i1", group_id="grp1", sender_id="c", content="", msg_type="image",
    ))
    assert d.action.value == "silence"
    assert snd.robot == []


# ---------------------------------------------------------------- 检索与分页
def seed_leads(store):
    for i, (gid, case, contact) in enumerate([
        ("dy:13800000001", "劳动仲裁", "13800000001"),
        ("dy:13800000002", "婚姻家事", "13800000002"),
        ("kf:a:x1", "交通事故", "13800000003"),
    ]):
        store.upsert_group(GroupProfile(group_id=gid, case_type=case))
        store.upsert_lead(gid, {
            "intent": "hot", "contact": contact, "summary": f"{case}咨询",
            "case_type": case, "priority": "P1" if i else "P0",
            "score": 40 if i else 75,
        })


def test_lead_endpoint_paginates_with_total(tmp_path):
    store, snd, _, pipeline, _ = make(tmp_path)
    seed_leads(store)
    client = client_for(store, pipeline, snd)
    r = client.get("/console/leads?limit=2", headers=H()).json()
    assert r["total"] == 3 and len(r["items"]) == 2
    r2 = client.get("/console/leads?limit=2&offset=2", headers=H()).json()
    assert len(r2["items"]) == 1
    # 三页拼起来无重复
    ids = {x["id"] for x in r["items"]} | {x["id"] for x in r2["items"]}
    assert len(ids) == 3


def test_lead_search_by_phone_and_keyword(tmp_path):
    store, snd, _, pipeline, _ = make(tmp_path)
    seed_leads(store)
    client = client_for(store, pipeline, snd)
    assert client.get("/console/leads?q=13800000002", headers=H()).json()["total"] == 1
    assert client.get("/console/leads?q=婚姻", headers=H()).json()["total"] == 1
    assert client.get("/console/leads?q=不存在的词", headers=H()).json()["total"] == 0


def test_lead_filter_by_source_and_priority(tmp_path):
    store, snd, _, pipeline, _ = make(tmp_path)
    seed_leads(store)
    client = client_for(store, pipeline, snd)
    assert client.get("/console/leads?source=dy", headers=H()).json()["total"] == 2
    assert client.get("/console/leads?source=kf", headers=H()).json()["total"] == 1
    assert client.get("/console/leads?priority=P0", headers=H()).json()["total"] == 1


# ---------------------------------------------------------------- 跟进备注
def test_lead_notes_roundtrip_and_scoping(tmp_path):
    store, snd, _, pipeline, _ = make(tmp_path)
    seed_leads(store)
    client = client_for(store, pipeline, snd)
    lid = store.list_leads(limit=1)[0]["id"]
    r = client.post(f"/console/leads/{lid}/notes",
                    json={"notes": "已通话，周五来所面谈"}, headers=H())
    assert r.status_code == 200
    assert store.get_lead_by_id(lid)["notes"] == "已通话，周五来所面谈"
    # 备注可被搜索命中（律师用自己的话找单）
    assert client.get("/console/leads?q=周五来所", headers=H()).json()["total"] == 1


def test_lawyer_cannot_note_others_lead(tmp_path):
    import hashlib

    store, snd, _, pipeline, _ = make(tmp_path)
    seed_leads(store)
    store.upsert_lawyer("wei", {"name": "魏", "role": "lawyer",
                                "on_duty": True, "active": True})
    store.set_lawyer_token_hash("wei", hashlib.sha256(b"tok-wei").hexdigest())
    client = client_for(store, pipeline, snd)
    lid = store.list_leads(limit=1)[0]["id"]  # 未指派给 wei
    r = client.post(f"/console/leads/{lid}/notes",
                    json={"notes": "x"}, headers=H("tok-wei"))
    assert r.status_code == 404


# ---------------------------------------------------------------- 批量分派
def test_bulk_assign_unrouted(tmp_path):
    """先导客资后建名册的典型场景：一键把存量线索按专长分下去。"""
    store, snd, _, pipeline, _ = make(tmp_path)
    seed_leads(store)
    for uid, name, spec in [("wei", "魏来", "劳动仲裁"), ("zhang", "张", "婚姻家事")]:
        store.upsert_lawyer(uid, {"name": name, "specialties": spec,
                                  "role": "lawyer", "on_duty": True, "active": True})
    client = client_for(store, pipeline, snd)
    r = client.post("/console/leads/assign-unrouted", headers=H()).json()
    assert r["assigned"] == 3
    assert store.get_lead("dy:13800000001")["assigned_userid"] == "wei"
    assert store.get_lead("dy:13800000002")["assigned_userid"] == "zhang"
    assert snd.direct == [], "批量分派不推送企微消息"
    # 幂等：再跑一次没有可分派的
    assert client.post("/console/leads/assign-unrouted", headers=H()).json()["assigned"] == 0


def test_bulk_assign_without_roster_is_graceful(tmp_path):
    store, snd, _, pipeline, _ = make(tmp_path)
    seed_leads(store)
    client = client_for(store, pipeline, snd)
    r = client.post("/console/leads/assign-unrouted", headers=H()).json()
    assert r["assigned"] == 0 and "名册" in r["hint"]

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


# ---------------------------------------------------------------- 数据完整性
def test_repeat_customer_does_not_lose_phone_or_score(tmp_path):
    """回头客隔周只说一句「在吗」，不能把上次辛苦拿到的电话和评分抹掉。"""
    store, _, _, pipeline, _ = make(tmp_path)
    store.upsert_group(GroupProfile(
        group_id="kf:a:c9", kf_open_kfid="a", kf_external_userid="c9",
        client_status=ClientStatus.PROSPECT,
    ))
    pipeline.handle(IncomingMessage(
        msg_id="r1", group_id="kf:a:c9", sender_id="c9",
        content="我想委托你们处理，电话17721275495"))
    first = store.get_lead("kf:a:c9")
    assert first["contact"] == "17721275495" and first["score"] >= 60

    pipeline.handle(IncomingMessage(
        msg_id="r2", group_id="kf:a:c9", sender_id="c9", content="在吗"))
    again = store.get_lead("kf:a:c9")
    assert again["contact"] == "17721275495", "电话不能被空值覆盖"
    assert again["score"] == first["score"], "评分取历史最高，不因一句闲聊降级"
    assert again["priority"] == first["priority"]


def test_group_silent_phone_message_still_creates_lead(tmp_path):
    """群里客户单发一句「我电话138…你们联系我」：AI 按规则沉默，
    但这是最强的转化信号，必须进线索通道。"""
    store, snd, _, pipeline, _ = make(tmp_path)
    store.upsert_group(GroupProfile(
        group_id="grpX", robot_webhook="rk", client_status=ClientStatus.PROSPECT,
        lawyer_userid="wei",
    ))
    d = pipeline.handle(IncomingMessage(
        msg_id="s1", group_id="grpX", sender_id="c",
        content="13800138888 你们联系我"))
    assert d.action.value == "silence"          # 群里不用接话
    row = store.get_lead("grpX")
    assert row and row["contact"] == "13800138888"   # 但线索不能漏


def test_signed_group_keeps_its_lawyer(tmp_path):
    """已成交客户的服务群有固定承办律师，自动派单一律不碰——
    改派会让 AI 在群里点名一个客户从没见过的人。"""
    store, snd, _, pipeline, _ = make(tmp_path)
    store.upsert_lawyer("other", {"name": "另一位", "specialties": "劳动仲裁",
                                  "role": "lawyer", "on_duty": True, "active": True})
    store.upsert_group(GroupProfile(
        group_id="signed1", client_status=ClientStatus.SIGNED,
        lawyer_userid="mr.Li", lawyer_name="李", case_type="劳动仲裁",
        robot_webhook="rk",
    ))
    pipeline.handle(IncomingMessage(
        msg_id="g1", group_id="signed1", sender_id="c",
        content="我想委托你们，电话17721275495"))
    g = store.get_group("signed1")
    assert (g.lawyer_userid, g.lawyer_name) == ("mr.Li", "李")


def test_send_failure_is_not_counted_as_replied(tmp_path):
    """发送失败还按 live 记账，追问策略会误判「已经答过了」，客户永远拿不到首答。"""
    store, snd, _, pipeline, _ = make(tmp_path)

    class Dead:
        def send_robot_text(self, w, t):
            return False

        def send_group_text(self, c, t):
            return False

        def send_direct_text(self, u, t):
            return True

    pipeline._sender = Dead()
    store.upsert_group(GroupProfile(
        group_id="grpF", robot_webhook="rk", client_status=ClientStatus.PROSPECT,
        lawyer_userid="wei",
    ))
    d = pipeline.handle(IncomingMessage(
        msg_id="f1", group_id="grpF", sender_id="c", mentioned_bot=True,
        content="拖欠工资多久可以申请劳动仲裁？"))
    assert "send:failed" in d.reasons
    rows = store.list_replies("grpF")
    assert rows and rows[0]["mode"] == "failed"
    assert store.count_recent_live("grpF", d.category.value, 3600) == 0


# ---------------------------------------------------------------- 越权
def _lawyer_token(store, uid="wei", tok="tok-wei"):
    import hashlib

    store.upsert_lawyer(uid, {"name": uid, "role": "lawyer",
                              "on_duty": True, "active": True})
    store.set_lawyer_token_hash(uid, hashlib.sha256(tok.encode()).hexdigest())
    return tok


def test_lawyer_cannot_close_others_todo(tmp_path):
    from responder.models import Reminder

    store, snd, _, pipeline, _ = make(tmp_path)
    tok = _lawyer_token(store)
    rid = store.save_reminder(Reminder(
        msg_id="x", group_id="g", to_userid="someone-else", summary="s"))
    client = client_for(store, pipeline, snd)
    assert client.post(f"/console/todo/{rid}/done", headers=H(tok)).status_code == 404
    assert client.post(f"/console/todo/{rid}/reopen", headers=H(tok)).status_code == 404
    # 自己的可以关
    mine = store.save_reminder(Reminder(
        msg_id="y", group_id="g", to_userid="wei", summary="s"))
    assert client.post(f"/console/todo/{mine}/done", headers=H(tok)).status_code == 200


def test_lawyer_cannot_rate_others_reply(tmp_path):
    store, snd, _, pipeline, _ = make(tmp_path)
    tok = _lawyer_token(store)
    store.upsert_group(GroupProfile(group_id="gz", lawyer_userid="zhang"))
    rid = store.save_reply("m1", "gz", "text", "live", True, category="general_law")
    client = client_for(store, pipeline, snd)
    r = client.post(f"/console/replies/{rid}/feedback",
                    json={"feedback": "good"}, headers=H(tok))
    assert r.status_code == 404


def test_feedback_value_is_validated(tmp_path):
    store, snd, _, pipeline, _ = make(tmp_path)
    store.upsert_group(GroupProfile(group_id="gz"))
    rid = store.save_reply("m1", "gz", "text", "live", True, category="general_law")
    client = client_for(store, pipeline, snd)
    assert client.post(f"/console/replies/{rid}/feedback",
                       json={"feedback": "随便写"}, headers=H()).status_code == 400


# ---------------------------------------------------------------- 待办联动
def test_marking_contacted_closes_related_todos(tmp_path):
    """人已经联系过了，督办还去升级第二责任人纯属制造无效打扰。"""
    from responder.models import Reminder

    store, snd, _, pipeline, _ = make(tmp_path)
    seed_leads(store)
    row = store.list_leads(limit=1)[0]
    store.save_reminder(Reminder(
        msg_id="t1", group_id=row["group_id"], to_userid="wei",
        urgent=True, summary="s"))
    client = client_for(store, pipeline, snd)
    r = client.post(f"/console/leads/{row['id']}/status",
                    json={"status": "contacted"}, headers=H()).json()
    assert r["todos_closed"] == 1
    assert store.pending_reminders() == []


def test_search_escapes_like_wildcards(tmp_path):
    """搜「100%」时 % 是字面量，不是「匹配任意」。"""
    store, snd, _, pipeline, _ = make(tmp_path)
    store.upsert_group(GroupProfile(group_id="g%1"))
    store.upsert_lead("g%1", {"intent": "hot", "contact": "13900000001",
                              "summary": "承诺100%胜诉的对方"})
    store.upsert_group(GroupProfile(group_id="g2"))
    store.upsert_lead("g2", {"intent": "hot", "contact": "13900000002",
                             "summary": "普通咨询"})
    client = client_for(store, pipeline, snd)
    assert client.get("/console/leads?q=100%25", headers=H()).json()["total"] == 1


def test_model_claim_contradicting_extracted_phone_is_dropped():
    """卡片并排显示「未提供联系方式」和一个手机号，律师会当场不信这张单。"""
    from responder.lead import _drop_contradicting_facts

    facts = ["拖欠工资 4 万", "客户未提供联系方式", "昨日被辞退"]
    assert _drop_contradicting_facts(facts, "17721275495") == [
        "拖欠工资 4 万", "昨日被辞退"]
    # 真没号时照原样保留——那条要点是有效信息
    assert _drop_contradicting_facts(facts, "") == facts

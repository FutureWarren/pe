"""律师账号体系：签发登录、数据隔离（服务端强制）、团队管理、SLA 督办。"""

from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from responder.config import Settings
from responder.console.api import router as console_router
from responder.models import GroupProfile, IncomingMessage
from responder.service import Pipeline
from responder.store.db import Store
from responder.worker import Worker

ADMIN = "master-token"


class Snd:
    def __init__(self):
        self.direct: list[tuple[str, str]] = []

    def send_direct_text(self, userid, text):
        self.direct.append((userid, text))
        return True

    def send_robot_text(self, webhook, text):
        return True

    def send_group_text(self, chat_id, text):
        return True


def make(tmp_path, **over):
    db = str(tmp_path / "t.db")
    store = Store(db)
    settings = Settings(
        mode="live", db_path=db, admin_token=ADMIN, split_delay_seconds=0,
        llm_refine_enabled=False, default_notify_userid="reception", **over,
    )
    snd = Snd()
    pipeline = Pipeline(store, snd, settings)
    app = FastAPI()
    app.state.store = store
    app.state.pipeline = pipeline
    app.state.worker = Worker(pipeline, store, snd)
    app.include_router(console_router)
    return store, snd, pipeline, TestClient(app), app


def H(tok):
    return {"x-admin-token": tok}


def create_lawyer_with_login(client, userid, name, specialties=""):
    r = client.put(f"/console/lawyers/{userid}",
                   json={"name": name, "specialties": specialties}, headers=H(ADMIN))
    assert r.status_code == 200
    r = client.post(f"/console/lawyers/{userid}/token", headers=H(ADMIN))
    assert r.status_code == 200
    return r.json()["token"]


# ---------------------------------------------------------------- 登录与身份
def test_lawyer_token_logs_in_scoped(tmp_path):
    _, _, _, client, _ = make(tmp_path)
    tok = create_lawyer_with_login(client, "wei", "魏")
    me = client.get("/console/me", headers=H(tok)).json()
    assert me == {"role": "lawyer", "userid": "wei", "name": "魏"}
    assert client.get("/console/me", headers=H(ADMIN)).json()["role"] == "admin"


def test_bad_token_rejected(tmp_path):
    _, _, _, client, _ = make(tmp_path)
    assert client.get("/console/me", headers=H("wrong")).status_code == 401


def test_token_rotation_invalidates_old(tmp_path):
    """重发登录链接＝旧链接作废（转发出去的旧消息不构成长期风险）。"""
    _, _, _, client, _ = make(tmp_path)
    old = create_lawyer_with_login(client, "wei", "魏")
    new = client.post("/console/lawyers/wei/token", headers=H(ADMIN)).json()["token"]
    assert client.get("/console/me", headers=H(old)).status_code == 401
    assert client.get("/console/me", headers=H(new)).status_code == 200


def test_deactivated_lawyer_cannot_login(tmp_path):
    _, _, _, client, _ = make(tmp_path)
    tok = create_lawyer_with_login(client, "wei", "魏")
    client.put("/console/lawyers/wei",
               json={"name": "魏", "active": False}, headers=H(ADMIN))
    assert client.get("/console/me", headers=H(tok)).status_code == 401


def test_lawyer_admin_role_gets_admin_view(tmp_path):
    """老板的个人令牌可带管理权限，不必共用主令牌。"""
    _, _, _, client, _ = make(tmp_path)
    client.put("/console/lawyers/boss",
               json={"name": "主任", "role": "admin"}, headers=H(ADMIN))
    tok = client.post("/console/lawyers/boss/token", headers=H(ADMIN)).json()["token"]
    assert client.get("/console/me", headers=H(tok)).json()["role"] == "admin"


def test_send_login_dm_contains_hash_fragment_link(tmp_path):
    store, snd, _, client, _ = make(tmp_path)
    client.put("/console/lawyers/wei", json={"name": "魏"}, headers=H(ADMIN))
    r = client.post("/console/lawyers/wei/send-login", headers=H(ADMIN))
    assert r.status_code == 200
    to, text = snd.direct[-1]
    assert to == "wei" and "/ui#t=" in text


# ---------------------------------------------------------------- 数据隔离
def seed_two_lawyers_with_leads(store, snd, pipeline, client):
    wei = create_lawyer_with_login(client, "wei", "魏", "劳动仲裁")
    zhang = create_lawyer_with_login(client, "zhang", "张", "婚姻家事")
    for gid, case, text in [
        ("kf:a:c1", "劳动仲裁", "拖欠工资，想委托你们，电话17721275495"),
        ("kf:a:c2", "婚姻家事", "想离婚，约时间面谈，电话13912345678"),
    ]:
        store.upsert_group(GroupProfile(
            group_id=gid, case_type=case, kf_open_kfid="a",
            kf_external_userid=gid[-2:],
        ))
        pipeline.handle(IncomingMessage(
            msg_id=f"m-{gid}", group_id=gid, sender_id="c", content=text))
    return wei, zhang


def test_lawyer_sees_only_own_leads_and_conversations(tmp_path):
    store, snd, pipeline, client, _ = make(tmp_path)
    wei, zhang = seed_two_lawyers_with_leads(store, snd, pipeline, client)

    mine = client.get("/console/leads", headers=H(wei)).json()
    assert mine["total"] == 1
    assert [x["group_id"] for x in mine["items"]] == ["kf:a:c1"]
    assert client.get("/console/leads", headers=H(ADMIN)).json()["total"] == 2

    # 会话原文：自己的可看，别人的按不存在处理
    assert client.get("/console/conversation?group_id=kf:a:c1", headers=H(wei)).status_code == 200
    assert client.get("/console/conversation?group_id=kf:a:c2", headers=H(wei)).status_code == 404


def test_lawyer_cannot_touch_others_lead_status(tmp_path):
    store, snd, pipeline, client, _ = make(tmp_path)
    wei, _ = seed_two_lawyers_with_leads(store, snd, pipeline, client)
    other = next(x for x in store.list_leads(limit=10)
                 if x["assigned_userid"] == "zhang")
    r = client.post(f"/console/leads/{other['id']}/status",
                    json={"status": "contacted"}, headers=H(wei))
    assert r.status_code == 404  # 不泄露存在性
    mine = next(x for x in store.list_leads(limit=10)
                if x["assigned_userid"] == "wei")
    assert client.post(f"/console/leads/{mine['id']}/status",
                       json={"status": "contacted"}, headers=H(wei)).status_code == 200


def test_lawyer_blocked_from_admin_endpoints(tmp_path):
    store, snd, pipeline, client, _ = make(tmp_path)
    wei = create_lawyer_with_login(client, "wei", "魏")
    for method, path, body in [
        ("get", "/console/lawyers", None),
        ("put", "/console/lawyers/x", {"name": "x"}),
        ("post", "/console/mode", {"mode": "shadow"}),
        ("post", "/console/update", None),
        ("get", "/console/diagnostics", None),
        ("put", "/console/groups/g", {"group_id": "g"}),
    ]:
        r = getattr(client, method)(
            path, **({"json": body} if body is not None else {}), headers=H(wei)
        )
        assert r.status_code == 403, path


def test_metrics_scoped_for_lawyer_full_for_admin(tmp_path):
    store, snd, pipeline, client, _ = make(tmp_path)
    wei, _ = seed_two_lawyers_with_leads(store, snd, pipeline, client)
    m = client.get("/console/metrics", headers=H(wei)).json()
    assert m["scope"] == "mine" and m["leads_total"] == 1
    a = client.get("/console/metrics", headers=H(ADMIN)).json()
    assert a["scope"] == "all" and a["leads_total"] == 2
    assert {x["userid"] for x in a["by_lawyer"]} == {"wei", "zhang"}


def test_todo_scoped_by_recipient(tmp_path):
    store, snd, pipeline, client, _ = make(tmp_path)
    wei, zhang = seed_two_lawyers_with_leads(store, snd, pipeline, client)
    # 非客服群的普通承接会走逐条提醒；这里直接种数据验证过滤
    from responder.models import Reminder
    store.save_reminder(Reminder(
        msg_id="r1", group_id="g", to_userid="wei", summary="s", question="q"))
    store.save_reminder(Reminder(
        msg_id="r2", group_id="g", to_userid="zhang", summary="s", question="q"))
    assert {t["to_userid"] for t in client.get("/console/todo", headers=H(ADMIN)).json()} \
        == {"wei", "zhang"}
    assert {t["to_userid"] for t in client.get("/console/todo", headers=H(wei)).json()} \
        == {"wei"}


def test_admin_reassign_moves_lead_and_notifies(tmp_path):
    store, snd, pipeline, client, _ = make(tmp_path)
    wei, zhang = seed_two_lawyers_with_leads(store, snd, pipeline, client)
    row = next(x for x in store.list_leads(limit=10)
               if x["assigned_userid"] == "wei")
    snd.direct.clear()
    r = client.post(f"/console/leads/{row['id']}/assign",
                    json={"userid": "zhang"}, headers=H(ADMIN))
    assert r.status_code == 200
    assert store.get_lead(row["group_id"])["assigned_userid"] == "zhang"
    assert store.get_group(row["group_id"]).lawyer_userid == "zhang"
    assert snd.direct and snd.direct[0][0] == "zhang" and "改派" in snd.direct[0][1]


# ---------------------------------------------------------------- SLA 督办
def test_p0_overdue_lead_gets_nudge_once(tmp_path):
    store, snd, pipeline, client, app = make(tmp_path)
    wei, _ = seed_two_lawyers_with_leads(store, snd, pipeline, client)
    row = next(x for x in store.list_leads(limit=10) if x["priority"] == "P0")
    # 把通知时间拨回 2 小时前，模拟超时未联系
    with store._conn() as conn:
        conn.execute("UPDATE leads SET notified_at=? WHERE id=?",
                     ((datetime.now() - timedelta(hours=2)).isoformat(), row["id"]))
    snd.direct.clear()
    worker = app.state.worker
    worker.tick()
    nudges = [d for d in snd.direct if "督办" in d[1]]
    assert nudges and nudges[0][0] == row["assigned_userid"]
    snd.direct.clear()
    worker.tick()
    assert not [d for d in snd.direct if "督办" in d[1]], "每单只追一次"


def test_contacted_lead_not_nudged(tmp_path):
    store, snd, pipeline, client, app = make(tmp_path)
    wei, _ = seed_two_lawyers_with_leads(store, snd, pipeline, client)
    row = next(x for x in store.list_leads(limit=10) if x["priority"] == "P0")
    store.set_lead_status(row["id"], "contacted")
    with store._conn() as conn:
        conn.execute("UPDATE leads SET notified_at=? WHERE id=?",
                     ((datetime.now() - timedelta(hours=2)).isoformat(), row["id"]))
    snd.direct.clear()
    app.state.worker.tick()
    assert not [d for d in snd.direct if "督办" in d[1]]


def test_team_page_shows_whether_each_lawyer_can_receive_transfers():
    """「他是不是微信客服接待人」必须画在他本人那张卡上。

    律所方原话：「EZID 都配对了，也收得到每日 update，就是收不到转接」。
    这两件事不矛盾——**它们查的是两份互不相干的名单**：日报走自建应用的
    可见范围，转接走客服账号的接待人列表。把这个区别藏在一个要点右上角
    才能打开的面板里，等于没有说。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from responder.console.api import ui_router

    app = FastAPI()
    app.include_router(ui_router)
    html = TestClient(app).get("/ui").text

    assert 'data-sv="' in html, "团队卡片要有画接待人状态的位置"
    assert "paintServicerBadges" in html
    assert "还不是接待人" in html
    # 停用但名下还压着单的律师：那些单一样转不过去，必须说出来
    assert "转接不过去" in html
    # 判据要用接待人全集，不能用 probe 的 missing（它只算在职律师）
    assert "a.servicers" in html
    # 多个客服账号时逐个算，不取交集：一个没配好的账号会把所有人都染红，
    # 而客户实际走的那个入口可能完全是通的（真机 2026-08-09：律所有两个账号）
    assert "从那个入口进来的客户转接不到他" in html
    assert "不取交集" in html


def _forget_app(tmp_path, kf=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from responder.config import Settings
    from responder.console.api import router
    from responder.service import Pipeline
    from responder.store.db import Store

    s = Settings(mode="live", db_path=str(tmp_path / "f.db"), admin_token="",
                 llm_provider="none")
    store = Store(s.db_path)
    app = FastAPI()
    app.state.store = store
    app.state.pipeline = Pipeline(store, None, s, kf_client=kf)
    app.include_router(router)
    return TestClient(app), store


def test_forgetting_a_customer_makes_the_next_visit_look_brand_new(tmp_path):
    """律所方原话：「我每次跑测试都得用新的微信号，有点麻烦」。

    而这不只是麻烦——**每次换号，测到的永远是「新客户」**：跨会话记忆、
    二次问候、再推送判据这些只在老客户身上发生的事，用新号一次也测不到。
    """
    from responder.models import ClientStatus, GroupProfile, IncomingMessage

    c, store = _forget_app(tmp_path)
    gid = "kf:wk1:cust"
    store.upsert_group(GroupProfile(
        group_id=gid, kf_open_kfid="wk1", kf_external_userid="cust",
        client_status=ClientStatus.PROSPECT, case_type="工伤",
    ))
    store.save_message(IncomingMessage(
        msg_id="m1", group_id=gid, sender_id="cust", content="我被辞退了"))
    store.upsert_lead(gid, {"intent": "hot", "contact": "13800001111"})
    store.set_memory(gid, "上次聊过工伤")
    store.set_handoff(gid, "wei")

    r = c.post(f"/console/groups/{gid}/forget")
    assert r.status_code == 200, r.text
    assert not store.recent_messages(gid, 10)
    assert store.get_lead(gid) is None
    g = store.get_group(gid)
    assert g is not None, "档案本身要留着——案由和承办律师是人配的"
    assert g.case_type == "工伤"
    assert g.memory == "" and g.handoff_userid == ""


def test_a_signed_clients_history_is_not_a_thing_you_can_wipe(tmp_path):
    """已委托客户的聊天记录是案件台账。「我以为那是测试号」是这类误删
    最常见的开场白，所以服务端直接拒，不靠人小心。"""
    from responder.models import ClientStatus, GroupProfile

    c, store = _forget_app(tmp_path)
    gid = "kf:wk1:signed"
    store.upsert_group(GroupProfile(
        group_id=gid, kf_open_kfid="wk1", kf_external_userid="signed",
        client_status=ClientStatus.SIGNED,
    ))
    r = c.post(f"/console/groups/{gid}/forget")
    assert r.status_code == 400
    assert "台账" in r.json()["detail"]


def test_forget_also_hands_the_wecom_session_back_to_ai(tmp_path):
    """只清我们库里的是假动作：会话还挂在「人工接待」或「已结束」上，
    下次扫码进来照样没人接（2026-08-08 那条真因）。"""
    from responder.models import ClientStatus, GroupProfile

    class Kf:
        def __init__(self): self.robot = []
        def available(self): return True
        def to_robot(self, kfid, ext):
            self.robot.append((kfid, ext))
            return True

    kf = Kf()
    c, store = _forget_app(tmp_path, kf)
    gid = "kf:wk1:cust"
    store.upsert_group(GroupProfile(
        group_id=gid, kf_open_kfid="wk1", kf_external_userid="cust",
        client_status=ClientStatus.PROSPECT,
    ))
    c.post(f"/console/groups/{gid}/forget")
    assert kf.robot == [("wk1", "cust")]

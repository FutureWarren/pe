"""真机第三轮（2026-08-08）：转接之后的三件事。

律所方原话：「把用户改派到魏律师之后，魏律师的企业微信后台却不显示这个对话」
「用户在接入人工客服之后，AI 好像就不能发消息了」「没法删除律师」。
"""

from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from responder.config import Settings
from responder.console.api import router
from responder.engine.decision import decide
from responder.models import Action, ClientStatus, GroupProfile, IncomingMessage
from responder.service import Pipeline
from responder.store.db import Store


class FakeKf:
    def __init__(self, servicers=("wang", "wei")):
        self._servicers = list(servicers)
        self.transfers = []

    def available(self):
        return True

    def servicer_list(self, open_kfid):
        return list(self._servicers)

    def transfer(self, open_kfid, external_userid, servicer_userid):
        self.transfers.append((open_kfid, external_userid, servicer_userid))
        return True


def _console(tmp_path, kf=None):
    s = Settings(mode="live", db_path=str(tmp_path / "h.db"), admin_token="",
                 llm_provider="none")
    store = Store(s.db_path)
    app = FastAPI()
    app.state.store = store
    pipeline = Pipeline(store, None, s, kf_client=kf)
    app.state.pipeline = pipeline
    app.include_router(router)
    return TestClient(app), store, s


# ------------------------------------------------------ 一、改派要搬会话
def test_reassign_moves_the_wecom_session_to_the_new_lawyer(tmp_path):
    """原来只改了我们库里的 assigned_userid，企微那边的会话状态一动没动——
    新律师的「微信客服」工作台里根本看不到这个人，老律师那边倒还在。
    单子看着交接完了，客户实际上还挂在原来那个人名下。"""
    kf = FakeKf()
    c, store, _ = _console(tmp_path, kf)
    gid = "kf:wk05:cust"
    store.upsert_group(GroupProfile(
        group_id=gid, kf_open_kfid="wk05", kf_external_userid="cust",
        client_status=ClientStatus.PROSPECT,
    ))
    store.upsert_lawyer("wang", {"name": "王律师", "active": True})
    store.upsert_lawyer("wei", {"name": "魏律师", "active": True})
    store.upsert_lead(gid, {"intent": "hot", "contact": "13800000000",
                            "priority": "P0", "score": 80})
    store.assign_lead(gid, "wang")
    store.set_handoff(gid, "wang")
    lead_id = store.get_lead(gid)["id"]

    r = c.post(f"/console/leads/{lead_id}/assign", json={"userid": "wei"})

    assert r.status_code == 200, r.text
    assert r.json()["session_moved"] is True
    assert kf.transfers == [("wk05", "cust", "wei")]
    assert store.get_group(gid).handoff_userid == "wei"


def test_reassign_says_so_when_the_new_lawyer_is_not_a_servicer(tmp_path):
    """企微会当场拒。静默失败的话，管理员以为搬过去了，客户其实还在原处。"""
    kf = FakeKf(servicers=("wang",))
    c, store, _ = _console(tmp_path, kf)
    gid = "kf:wk05:cust"
    store.upsert_group(GroupProfile(
        group_id=gid, kf_open_kfid="wk05", kf_external_userid="cust",
        client_status=ClientStatus.PROSPECT,
    ))
    store.upsert_lawyer("wang", {"name": "王", "active": True})
    store.upsert_lawyer("wei", {"name": "魏", "active": True})
    store.upsert_lead(gid, {"intent": "hot", "priority": "P0", "score": 80})
    store.assign_lead(gid, "wang")
    store.set_handoff(gid, "wang")

    r = c.post(f"/console/leads/{store.get_lead(gid)['id']}/assign",
               json={"userid": "wei"})

    assert r.json()["session_moved"] is False
    assert "接待人" in r.json()["hint"]
    assert kf.transfers == []


def test_a_conversation_still_with_the_ai_is_not_force_moved(tmp_path):
    """还在 AI 手上的会话本就没有归属。强行转过去会让 AI 当场闭嘴，
    而新律师未必这会儿就要接。"""
    kf = FakeKf()
    c, store, _ = _console(tmp_path, kf)
    gid = "kf:wk05:cust"
    store.upsert_group(GroupProfile(
        group_id=gid, kf_open_kfid="wk05", kf_external_userid="cust",
        client_status=ClientStatus.PROSPECT,
    ))
    store.upsert_lawyer("wei", {"name": "魏", "active": True})
    store.upsert_lead(gid, {"intent": "hot", "priority": "P1", "score": 40})

    c.post(f"/console/leads/{store.get_lead(gid)['id']}/assign", json={"userid": "wei"})

    assert kf.transfers == []


# ------------------------------------------------------ 二、转过去没人接
def _decide_after_handoff(handoff_ago_seconds, staff_spoke_ago=None, grace=90):
    s = Settings(handoff_grace_seconds=grace, handoff_reclaim_seconds=1800,
                 kf_wait_seconds=0)
    g = GroupProfile(group_id="kf:a:b", kf_open_kfid="a", kf_external_userid="b",
                     client_status=ClientStatus.PROSPECT, handoff_userid="wei",
                     handoff_at=datetime.now() - timedelta(seconds=handoff_ago_seconds))
    msg = IncomingMessage(msg_id="m", group_id=g.group_id, sender_id="b", content="人呢")
    return decide(msg, g, seconds_since_last_staff_reply=staff_spoke_ago,
                  settings=s,
                  classification=(Action.HANDOFF, "contact", False, ["contact:在吗"]))


def test_ai_stays_quiet_right_after_the_handoff():
    """刚转过去那一会儿必须闭嘴，否则会抢在律师前面说话。"""
    d = _decide_after_handoff(10)
    assert d.should_speak is False
    assert any("gate:handed-off" in r for r in d.reasons)


def test_ai_takes_over_again_when_nobody_shows_up():
    """真机实测：转人工后客户连发「你好」「人呢」「你好？」，AI 全程沉默，
    直到企微把会话判成「已结束聊天」。转接本来是为了让他更快见到人，
    结果是被晾在一间空屋子里——比不转还糟。"""
    d = _decide_after_handoff(300)
    assert d.should_speak is True
    assert "handoff:no-show" in d.reasons


def test_ai_shuts_up_again_once_the_lawyer_speaks():
    """律师一开口，AI 立刻让开——不管过了多久。"""
    d = _decide_after_handoff(600, staff_spoke_ago=20)
    assert d.should_speak is False
    assert any("gate:handed-off" in r for r in d.reasons)


def test_the_no_show_grace_does_not_clear_the_handoff():
    """AI 接着陪 ≠ 转接被撤销。律师随时还能接手。"""
    d = _decide_after_handoff(300)
    assert "handoff:reclaimed" not in d.reasons


# ------------------------------------------------------ 三、移除律师
def test_a_lawyer_with_open_leads_cannot_be_removed(tmp_path):
    """删掉之后那些线索的负责人就成了一个查无此人的 id：交接单推不出去、
    督办找不到人，而没有任何地方会告诉你为什么。"""
    c, store, _ = _console(tmp_path)
    store.upsert_lawyer("wang", {"name": "王律师", "active": True})
    store.upsert_group(GroupProfile(group_id="kf:a:b", kf_open_kfid="a",
                                    kf_external_userid="b"))
    store.upsert_lead("kf:a:b", {"intent": "hot", "priority": "P0", "score": 80})
    store.assign_lead("kf:a:b", "wang")

    r = c.delete("/console/lawyers/wang")

    assert r.status_code == 400
    assert "改派" in r.json()["detail"]
    assert store.get_lawyer("wang") is not None


def test_a_lawyer_without_open_leads_can_be_removed(tmp_path):
    c, store, _ = _console(tmp_path)
    store.upsert_lawyer("wang", {"name": "王律师", "active": True})

    assert c.delete("/console/lawyers/wang").status_code == 200
    assert store.get_lawyer("wang") is None


def test_removing_an_unknown_lawyer_is_a_404(tmp_path):
    c, _, _ = _console(tmp_path)
    assert c.delete("/console/lawyers/nobody").status_code == 404


# ------------------------------------------------------ 四、别让窗口死掉
def test_the_lawyer_speaking_once_does_not_silence_the_ai_forever():
    """原判据是「律师在转接之后说过话」，而它一旦成立就**永远成立**——
    转接越久 waited 越大，差值只会越拉越开。真机后果：律师接手时说了一句，
    第二天客户再来发「你好」，AI 依然一个字不回，对着一个死掉的窗口。"""
    d = _decide_after_handoff(86400, staff_spoke_ago=3600)   # 一天前转的，一小时前律师说过话
    assert d.should_speak is True, d.reasons


def test_the_ai_still_yields_while_the_lawyer_is_actually_there():
    """刚说过话就是「正在跟」，这时候 AI 必须让开。"""
    d = _decide_after_handoff(86400, staff_spoke_ago=60)
    assert d.should_speak is False


def test_release_button_hands_the_conversation_back_to_the_ai(tmp_path):
    """转接是自动发生的，一旦卡住客户看到的就是个死窗口。等三十分钟超时回收
    对一个正在打字的客户太久了——手上得有个当场能救的开关。"""
    c, store, _ = _console(tmp_path)
    gid = "kf:wk:cust"
    store.upsert_group(GroupProfile(group_id=gid, kf_open_kfid="wk",
                                    kf_external_userid="cust"))
    store.set_handoff(gid, "wei")

    assert c.post(f"/console/groups/{gid}/release").status_code == 200
    assert store.get_group(gid).handoff_userid == ""


# ------------------------------------------------------ 五、被抹掉的会话要自愈
def test_a_wiped_kf_session_heals_itself_from_the_group_id(tmp_path):
    """「保存群档案」曾经把 kf_open_kfid 一起清成空串。那个 bug 堵上了，
    但**已经被点坏的会话不会自己好**——is_kf 变假，回复被发往一个不存在的群，
    客户从此一句话也收不到，而控制台里看着一切正常。
    好在 group_id 本身就是 kf:{open_kfid}:{external_userid}。"""
    from responder.store.db import Store

    store = Store(str(tmp_path / "heal.db"))
    gid = "kf:wk05XzXg:wm05XzXg"
    store.upsert_group(GroupProfile(group_id=gid, kf_open_kfid="wk05XzXg",
                                    kf_external_userid="wm05XzXg"))
    with store._conn() as conn:
        conn.execute("UPDATE groups SET kf_open_kfid='', kf_external_userid=''"
                     " WHERE group_id=?", (gid,))

    g = store.get_group(gid)
    assert g.is_kf is True
    assert (g.kf_open_kfid, g.kf_external_userid) == ("wk05XzXg", "wm05XzXg")


def test_a_wiped_external_channel_heals_too(tmp_path):
    from responder.store.db import Store

    store = Store(str(tmp_path / "heal2.db"))
    store.upsert_group(GroupProfile(group_id="ch:meituan:u-9527"))
    with store._conn() as conn:
        conn.execute("UPDATE groups SET ext_channel='', ext_user_id=''")

    g = store.get_group("ch:meituan:u-9527")
    assert (g.ext_channel, g.ext_user_id) == ("meituan", "u-9527")
    assert g.is_external is True


def test_group_chats_are_not_touched_by_the_healing(tmp_path):
    from responder.store.db import Store

    store = Store(str(tmp_path / "heal3.db"))
    store.upsert_group(GroupProfile(group_id="wrOabc123", name="客户群"))
    g = store.get_group("wrOabc123")
    assert g.is_kf is False and g.kf_open_kfid == ""


# ------------------------------------------------------ 六、一键自检
def test_diagnose_names_the_gate_in_plain_chinese(tmp_path):
    """AI 不说话有十几种可能，而它们在客户那头长得一模一样——一片空白。"""
    c, store, _ = _console(tmp_path)
    gid = "kf:wk:cust"
    store.upsert_group(GroupProfile(group_id=gid, kf_open_kfid="wk",
                                    kf_external_userid="cust", ai_enabled=False))
    r = c.get(f"/console/diagnose?group_id={gid}").json()
    assert "开关" in r["verdict"], r


def test_diagnose_flags_shadow_mode(tmp_path):
    c, store, s = _console(tmp_path)
    s.mode = "shadow"
    gid = "kf:wk:cust"
    store.upsert_group(GroupProfile(group_id=gid, kf_open_kfid="wk",
                                    kf_external_userid="cust"))
    assert "影子模式" in c.get(f"/console/diagnose?group_id={gid}").json()["verdict"]


def test_diagnose_says_when_the_conversation_never_arrived(tmp_path):
    c, _, _ = _console(tmp_path)
    r = c.get("/console/diagnose?group_id=kf:nope:nope").json()
    assert r["ok"] is False and "不存在" in r["verdict"]

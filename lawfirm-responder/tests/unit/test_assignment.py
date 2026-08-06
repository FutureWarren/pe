"""派单引擎：粘性 → 专长匹配 → 负载均衡 → 名册为空回落旧链路。"""

from responder import lead
from responder.config import Settings
from responder.models import ClientStatus, GroupProfile
from responder.store.db import Store


def make(tmp_path):
    store = Store(str(tmp_path / "a.db"))
    settings = Settings(
        db_path=str(tmp_path / "a.db"), default_notify_userid="reception",
        llm_refine_enabled=False,
    )
    return store, settings


def add_lawyer(store, userid, name="", specialties="", on_duty=True, active=True):
    store.upsert_lawyer(userid, {
        "name": name or userid, "specialties": specialties,
        "role": "lawyer", "on_duty": on_duty, "active": active,
    })


def _hist(text="我想委托你们，电话17721275495"):
    return [{"content": text, "sender_is_staff": False,
             "created_at": "2026-07-29T10:00:00"}]


class Snd:
    def __init__(self):
        self.direct = []

    def send_direct_text(self, userid, text):
        self.direct.append((userid, text))
        return True


def test_empty_roster_falls_back_to_legacy_target(tmp_path):
    """名册为空＝功能未启用：目标仍是会话承办人，升级部署行为不变。"""
    store, settings = make(tmp_path)
    group = GroupProfile(client_status=ClientStatus.PROSPECT, group_id="g1", lawyer_userid="mr.Li")
    snd = Snd()
    lead.dispatch(store, group, _hist(), snd, settings=settings)
    assert snd.direct and snd.direct[0][0] == "mr.Li"
    assert store.get_lead("g1")["assigned_userid"] == ""


def test_specialty_match_beats_load(tmp_path):
    store, settings = make(tmp_path)
    add_lawyer(store, "wei", "魏", "劳动仲裁、工伤")
    add_lawyer(store, "zhang", "张", "婚姻家事")
    group = GroupProfile(client_status=ClientStatus.PROSPECT, group_id="g1", case_type="劳动仲裁")
    snd = Snd()
    lead.dispatch(store, group, _hist(), snd, settings=settings)
    row = store.get_lead("g1")
    assert row["assigned_userid"] == "wei"
    assert snd.direct[0][0] == "wei"
    # 会话档案承办律师同步换成被派律师（提醒/话术点名跟着走）
    g = store.get_group("g1")
    assert (g.lawyer_userid, g.lawyer_name) == ("wei", "魏")


def test_load_balance_picks_least_busy(tmp_path):
    store, settings = make(tmp_path)
    add_lawyer(store, "a", specialties="劳动仲裁")
    add_lawyer(store, "b", specialties="劳动仲裁")
    store.upsert_lead("busy1", {"intent": "hot"})
    store.assign_lead("busy1", "a")
    group = GroupProfile(client_status=ClientStatus.PROSPECT, group_id="g2", case_type="劳动仲裁")
    lead.dispatch(store, group, _hist(), Snd(), settings=settings)
    assert store.get_lead("g2")["assigned_userid"] == "b"


def test_no_specialty_match_falls_back_to_all_on_duty(tmp_path):
    """宁可专长不对口，不能没人管。"""
    store, settings = make(tmp_path)
    add_lawyer(store, "a", specialties="婚姻家事")
    group = GroupProfile(client_status=ClientStatus.PROSPECT, group_id="g3", case_type="刑事辩护")
    lead.dispatch(store, group, _hist(), Snd(), settings=settings)
    assert store.get_lead("g3")["assigned_userid"] == "a"


def test_off_duty_lawyer_not_assigned(tmp_path):
    store, settings = make(tmp_path)
    add_lawyer(store, "resting", specialties="劳动仲裁", on_duty=False)
    add_lawyer(store, "working", specialties="婚姻家事")
    group = GroupProfile(client_status=ClientStatus.PROSPECT, group_id="g4", case_type="劳动仲裁")
    lead.dispatch(store, group, _hist(), Snd(), settings=settings)
    assert store.get_lead("g4")["assigned_userid"] == "working"


def test_all_off_duty_falls_back_to_legacy(tmp_path):
    store, settings = make(tmp_path)
    add_lawyer(store, "resting", on_duty=False)
    group = GroupProfile(client_status=ClientStatus.PROSPECT, group_id="g5", lawyer_userid="mr.Li")
    snd = Snd()
    lead.dispatch(store, group, _hist(), snd, settings=settings)
    assert store.get_lead("g5")["assigned_userid"] == ""
    assert snd.direct[0][0] == "mr.Li"


def test_sticky_assignment_survives_new_messages(tmp_path):
    """已派过的客户再进线不换人——背景不能作废。"""
    store, settings = make(tmp_path)
    add_lawyer(store, "a", specialties="劳动仲裁")
    add_lawyer(store, "b", specialties="劳动仲裁")
    group = GroupProfile(client_status=ClientStatus.PROSPECT, group_id="g6", case_type="劳动仲裁")
    lead.dispatch(store, group, _hist(), Snd(), settings=settings)
    first = store.get_lead("g6")["assigned_userid"]
    # 给已派律师塞满在办单，若重新派单会换人；粘性要求不换
    for i in range(3):
        store.upsert_lead(f"busy{i}", {"intent": "warm"})
        store.assign_lead(f"busy{i}", first)
    lead.dispatch(
        store, store.get_group("g6"),
        _hist("我明天有空，随时联系17721275495"), Snd(), settings=settings,
    )
    assert store.get_lead("g6")["assigned_userid"] == first


def test_inactive_assignee_triggers_reroute(tmp_path):
    """离职律师手上的新动态要改派，不能推给一个不存在的人。"""
    store, settings = make(tmp_path)
    add_lawyer(store, "gone", specialties="劳动仲裁")
    group = GroupProfile(client_status=ClientStatus.PROSPECT, group_id="g7", case_type="劳动仲裁")
    lead.dispatch(store, group, _hist(), Snd(), settings=settings)
    assert store.get_lead("g7")["assigned_userid"] == "gone"
    add_lawyer(store, "gone", specialties="劳动仲裁", active=False)
    add_lawyer(store, "next", specialties="劳动仲裁")
    snd = Snd()
    lead.dispatch(
        store, store.get_group("g7"),
        _hist("怎么还没人联系我？电话17721275495"), snd, settings=settings, force=True,
    )
    assert store.get_lead("g7")["assigned_userid"] == "next"


def test_notification_carries_priority_and_factors(tmp_path):
    """交接单首行是层级+时限，随后是评分依据——可解释的排序才会被执行。"""
    store, settings = make(tmp_path)
    add_lawyer(store, "wei", "魏", "劳动仲裁")
    group = GroupProfile(client_status=ClientStatus.PROSPECT, group_id="g8", case_type="劳动仲裁")
    snd = Snd()
    lead.dispatch(store, group, _hist("我想委托你们，电话17721275495"), snd,
                  settings=settings)
    text = snd.direct[0][1]
    # 客服手上只认强/弱两档（业务决策 2026-08）——P 码留在内部给评分与督办用
    assert text.startswith("【强意愿】")
    assert "1 小时内联系" in text and "优先依据" in text and "已留电话 +40" in text

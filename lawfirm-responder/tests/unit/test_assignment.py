"""派单引擎：粘性 → 负载均衡（在办最少/轮转）→ 名册为空回落旧链路。

**没有专长这一层**（2026-08-12 律所方：「客服不分什么专长不专长」）——
下面几条用例刻意给不同案件类型，验证的正是「案件类型完全不影响派给谁」。
"""

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


def add_lawyer(store, userid, name="", on_duty=True, active=True):
    store.upsert_lawyer(userid, {
        "name": name or userid,
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


def test_assigns_to_roster_and_syncs_group_owner(tmp_path):
    store, settings = make(tmp_path)
    add_lawyer(store, "wei", "魏")
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
    add_lawyer(store, "a")
    add_lawyer(store, "b")
    store.upsert_lead("busy1", {"intent": "hot"})
    store.assign_lead("busy1", "a")
    group = GroupProfile(client_status=ClientStatus.PROSPECT, group_id="g2", case_type="劳动仲裁")
    lead.dispatch(store, group, _hist(), Snd(), settings=settings)
    assert store.get_lead("g2")["assigned_userid"] == "b"


def test_case_type_does_not_steer_assignment(tmp_path):
    """取消专长后的核心约束：案件类型不参与挑人，两条线索按轮转分给两个人。

    以前这里靠「案件类型 ⊇ 专长」定人，填错专长就静默派错人。现在只剩
    「谁在办的最少谁先接、平局谁最久没接谁先接」，案由再不同也一样轮流。
    """
    store, settings = make(tmp_path)
    add_lawyer(store, "a")
    add_lawyer(store, "b")
    # 两个不同的手机号：同号会走「跨渠道认人」沿用同一位律师，那是另一条规则
    lead.dispatch(
        store, GroupProfile(client_status=ClientStatus.PROSPECT, group_id="g3a",
                            case_type="劳动仲裁"),
        _hist("我想委托你们，电话17721275495"), Snd(), settings=settings,
    )
    lead.dispatch(
        store, GroupProfile(client_status=ClientStatus.PROSPECT, group_id="g3b",
                            case_type="刑事辩护"),
        _hist("我想委托你们，电话13900001234"), Snd(), settings=settings,
    )
    got = {store.get_lead("g3a")["assigned_userid"],
           store.get_lead("g3b")["assigned_userid"]}
    assert got == {"a", "b"}


def test_off_duty_lawyer_not_assigned(tmp_path):
    store, settings = make(tmp_path)
    add_lawyer(store, "resting", on_duty=False)
    add_lawyer(store, "working")
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
    add_lawyer(store, "a")
    add_lawyer(store, "b")
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
    add_lawyer(store, "gone")
    group = GroupProfile(client_status=ClientStatus.PROSPECT, group_id="g7", case_type="劳动仲裁")
    lead.dispatch(store, group, _hist(), Snd(), settings=settings)
    assert store.get_lead("g7")["assigned_userid"] == "gone"
    add_lawyer(store, "gone", active=False)
    add_lawyer(store, "next")
    snd = Snd()
    lead.dispatch(
        store, store.get_group("g7"),
        _hist("怎么还没人联系我？电话17721275495"), snd, settings=settings, force=True,
    )
    assert store.get_lead("g7")["assigned_userid"] == "next"


def test_notification_carries_priority_and_factors(tmp_path):
    """交接单首行是层级+时限，随后是评分依据——可解释的排序才会被执行。"""
    store, settings = make(tmp_path)
    add_lawyer(store, "wei", "魏")
    group = GroupProfile(client_status=ClientStatus.PROSPECT, group_id="g8", case_type="劳动仲裁")
    snd = Snd()
    lead.dispatch(store, group, _hist("我想委托你们，电话17721275495"), snd,
                  settings=settings)
    text = snd.direct[0][1]
    # 客服手上只认强/弱两档（业务决策 2026-08）——P 码留在内部给评分与督办用
    assert text.startswith("【强意愿】")
    assert "1 小时内联系" in text and "优先依据" in text and "已留电话 +40" in text

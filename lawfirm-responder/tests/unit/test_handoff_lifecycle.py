"""转接之后：会话归属、名单反向同步、以及「读不到」与「读到空」的区别。

2026-08-12 体检的三条，共同点是**两个系统的状态悄悄错开了，而两边各自都显示正常**：

1. 转给律师之后没人露面 → 系统把企微那边的归属要回给 AI，但**本地那条
   「已经转给张律师」的记录谁也不清**。而它正是转接的六个前提之一
   （「本会话未转过」），于是这个客户后面再怎么说「我要委托」「让律师联系我」，
   系统都会以「已经转过了」为由**永远拒绝再转真人**。
2. 律所在「团队」页删掉一个人，企微那边的接待人名单纹丝不动——
   一个刚离职的人照样能看到并接走所里新进来的咨询。
3. 接待人接口读失败时返回空列表，调用方读成「一个接待人都没有」：
   最该转的那单静默转不成，自检页还红着报一个假故障。而律所修那个假故障的动作
   （去企微网页端手工点接待人）会夺走管理权、打断消息推送，整套 AI 当场哑掉。
"""

import json
from datetime import datetime, timedelta

import pytest

from responder.config import Settings
from responder.models import ClientStatus, GroupProfile, IncomingMessage
from responder.service import Pipeline
from responder.store.db import Store

OPEN_KFID = "wk-life"
EXT = "wmLifeCustomer"
GID = f"kf:{OPEN_KFID}:{EXT}"


class Kf:
    def __init__(self, servicers=("wei",), list_raises=False):
        self.servicers = list(servicers)
        self.list_raises = list_raises
        self.sent: list[str] = []
        self.transfers: list[str] = []
        self.robot_calls = 0
        self.deleted: list[str] = []

    def available(self):
        return True

    def account_list(self):
        return [{"open_kfid": OPEN_KFID, "name": "松沪律所咨询"}]

    def servicer_raw(self, kfid):
        if self.list_raises:
            return {"error": "kf servicer/list error: timeout"}
        return {"servicer_list": [{"userid": u} for u in self.servicers]}

    def servicer_list(self, kfid):
        from responder.gateway.wecom_kf import KfUnavailable

        if self.list_raises:
            raise KfUnavailable("timeout")
        return list(self.servicers)

    def servicer_add(self, kfid, userids):
        self.servicers = sorted(set(self.servicers) | set(userids))
        return {"errcode": 0}

    def servicer_del(self, kfid, userids):
        self.deleted += list(userids)
        self.servicers = [u for u in self.servicers if u not in userids]
        return {"errcode": 0}

    def send_text(self, kfid, ext, text):
        self.sent.append(text)
        return True

    def transfer(self, kfid, ext, userid):
        self.transfers.append(userid)
        return True

    def service_state(self, kfid, ext):
        return 3  # 人工接待中

    def to_robot(self, kfid, ext):
        self.robot_calls += 1
        return True


class Snd:
    def send_direct_text(self, userid, text):
        return True


def make(tmp_path, kf=None, **over):
    db = str(tmp_path / "l.db")
    store = Store(db)
    cfg = dict(
        mode="live", db_path=db, wecom_kf_secret="s", split_messages=False,
        split_delay_seconds=0, llm_answer_enabled=False, llm_refine_enabled=False,
        lead_brief_enabled=False,
    )
    cfg.update(over)
    settings = Settings(**cfg)
    kf = kf or Kf()
    store.upsert_lawyer("wei", {"name": "魏", "role": "lawyer",
                                "on_duty": True, "active": True})
    return store, kf, Pipeline(store, sender=Snd(), settings=settings, kf_client=kf)


def kf_group(**over) -> GroupProfile:
    fields = dict(
        client_status=ClientStatus.PROSPECT, group_id=GID,
        kf_open_kfid=OPEN_KFID, kf_external_userid=EXT, case_type="劳动仲裁",
    )
    fields.update(over)
    return GroupProfile(**fields)


def msg(text, mid="m1") -> IncomingMessage:
    return IncomingMessage(
        msg_id=mid, group_id=GID, sender_id=EXT, content=text,
        msg_type="text", created_at=datetime.now(), sender_is_staff=False,
    )


def lead_row():
    return {"priority": "P0", "assigned_userid": "wei", "intent": "hot",
            "signals": json.dumps(["engage"])}


# ------------------------------------------------ ① 彻底放弃等人之后要能再转
def test_a_customer_can_be_transferred_again_after_nobody_showed_up(tmp_path):
    """**本组最贵的一条。** 转过一次而律师没露面，不能让这个客户从此再也转不了人。"""
    store, kf, p = make(tmp_path, handoff_reclaim_seconds=1800)
    store.upsert_group(kf_group())

    # 转出去，律师一直没打开企微
    assert p._maybe_handoff(store.get_group(GID), lead_row(), urgent=False) is True
    assert kf.transfers == ["wei"]
    with store._conn() as conn:
        long_ago = (datetime.now() - timedelta(hours=2)).isoformat()
        conn.execute("UPDATE groups SET handoff_at=? WHERE group_id=?", (long_ago, GID))

    # 客户又来了，而且这次说得更明确
    d = p.handle(msg("我要委托你们，麻烦让律师给我打电话", mid="m2"))
    assert "handoff:reclaimed" in d.reasons
    assert store.get_group(GID).handoff_userid == "", (
        "彻底放弃等人之后必须把这条记录清掉，否则「已经转过了」会永远挡住下一次转接"
    )

    # 现在才是关键：还能不能再转一次
    kf.transfers.clear()
    assert p._maybe_handoff(store.get_group(GID), lead_row(), urgent=False) is True
    assert kf.transfers == ["wei"]


def test_a_fresh_handoff_is_kept_while_the_ai_accompanies(tmp_path):
    """转完 AI 接着陪，但**转接记录不清**——客户已经在律师的工作台里，
    他随时能开口接手；AI 只是在他开口之前不让客户对着空屋子说话。"""
    store, _, p = make(tmp_path)
    store.upsert_group(kf_group())
    p._maybe_handoff(store.get_group(GID), lead_row(), urgent=False)

    d = p.handle(msg("在吗", mid="m2"))

    assert store.get_group(GID).handoff_userid == "wei"
    assert "handoff:accompanying" in d.reasons
    assert d.should_speak is True, "转接后客户来话，AI 得接着回，不能晾着"


def test_the_ai_keeps_screening_after_the_handoff(tmp_path):
    """律所方 2026-08-13：「转人工后如果人工没有及时的回复，AI 也应该先试着
    陪客户聊，去问案件详情，然后等人工真正发消息了再闭嘴转接。」

    所以转接**不是筛查的终点**：真人开口之前，AI 接着把还缺的那几件问出来。
    等律师接手时，案情比转接那一刻更全——这才是转接早一点的意义。
    """
    store, kf, p = make(tmp_path)
    store.upsert_group(kf_group())
    p._maybe_handoff(store.get_group(GID), lead_row(), urgent=False)
    n = len(kf.sent)

    d = p.handle(msg("对方是家建筑公司，合同我手上有", mid="m2"))

    assert d.should_speak is True, "真人还没开口，AI 不能停"
    assert len(kf.sent) > n, "客户补充了案情，AI 得接着往下问"
    assert "handoff:accompanying" in d.reasons


def test_minutes_of_accompanying_do_not_clear_it_either(tmp_path):
    """陪了几分钟也不清状态——离「彻底放弃等这个人」还远。"""
    store, _, p = make(tmp_path, handoff_reclaim_seconds=1800)
    store.upsert_group(kf_group())
    p._maybe_handoff(store.get_group(GID), lead_row(), urgent=False)
    with store._conn() as conn:
        recent = (datetime.now() - timedelta(minutes=5)).isoformat()
        conn.execute("UPDATE groups SET handoff_at=? WHERE group_id=?", (recent, GID))

    d = p.handle(msg("那我需要准备什么材料", mid="m2"))

    assert "handoff:accompanying" in d.reasons
    assert store.get_group(GID).handoff_userid == "wei", "还没到放弃的时候"


# ------------------------------------------------ ② 离职的人要从企微名单里下来
def test_a_departed_lawyer_is_removed_from_the_wecom_servicer_list(tmp_path):
    """在「团队」页删掉人，所有人都以为处理完了——而他照样能接走新客资。"""
    from responder import kfroster

    store, kf, _ = make(tmp_path, kf=Kf(servicers=("wei", "gone")))

    result = kfroster.sync(store, kf, {OPEN_KFID})

    assert kf.deleted == ["gone"]
    assert kf.servicers == ["wei"]
    assert result["accounts"][0]["removed"] == ["gone"]
    assert result["ok"] is True


def test_a_stale_servicer_that_cannot_be_removed_is_reported(tmp_path):
    """删不掉也要说出来——静默的「删了但其实没删」比不删更糟。"""
    from responder import kfroster

    kf = Kf(servicers=("wei", "gone"))
    kf.servicer_del = lambda kfid, userids: {"error": "60011 no privilege"}
    store, _, _ = make(tmp_path, kf=kf)

    result = kfroster.sync(store, kf, {OPEN_KFID})

    assert result["ok"] is False
    assert result["accounts"][0]["stale"] == ["gone"]


# ------------------------------------------------ ③ 读不到 ≠ 读到了空
def test_a_flaky_read_is_not_reported_as_an_empty_roster(tmp_path):
    """报一个假的「谁都没加上」，律所会跑去企微后台手工点——那会让 AI 当场哑掉。"""
    from responder import kfroster

    store, kf, _ = make(tmp_path, kf=Kf(list_raises=True))

    result = kfroster.sync(store, kf, {OPEN_KFID})

    acc = result["accounts"][0]
    assert acc["unknown"] is True
    assert acc["failed"] == [], "读不到的时候，一个人的名字都不该被点出来"
    assert result["ok"] is False, "也不能假装成功——下一轮要重试"


def test_a_flaky_read_blocks_the_transfer_with_an_honest_reason(tmp_path):
    """转接前校验接待人时读失败：不转是对的，但理由要说准。"""
    store, kf, p = make(tmp_path, kf=Kf(list_raises=True))
    store.upsert_group(kf_group())

    assert p._maybe_handoff(store.get_group(GID), lead_row(), urgent=False) is False
    note = store.get_note(f"handoff_skip:{GID}")
    assert "查不到" in note, f"理由该是「没读上来」而不是「他不在名单里」：{note}"
    assert not kf.sent, "没转成就不能先跟客户说「转给律师了」"


@pytest.mark.parametrize("state", [0, 2, 4])
def test_sessions_nobody_is_handling_are_claimed_back(tmp_path, state):
    """0 未处理 / 2 待接入 / 4 已结束——这三种状态下客户发什么都石沉大海。"""
    from responder.worker import Worker

    kf = Kf()
    kf.service_state = lambda a, b: state
    store, _, p = make(tmp_path, kf=kf)
    store.upsert_group(kf_group())
    w = Worker(p, store, sender=Snd(), kf_client=kf)

    w._ensure_robot_state(GID, OPEN_KFID, EXT)

    assert kf.robot_calls == 1


def test_a_freshly_transferred_session_is_not_yanked_back(tmp_path):
    """已转接的会话不碰归属——律师打开工作台时那通对话得还在他名下。
    要收回只有一条路：`_release_stale_handoff` 先清状态，之后才轮得到这里。"""
    from responder.worker import Worker

    store, kf, p = make(tmp_path)
    store.upsert_group(kf_group())
    p._maybe_handoff(store.get_group(GID), lead_row(), urgent=False)
    w = Worker(p, store, sender=Snd(), kf_client=kf)

    w._ensure_robot_state(GID, OPEN_KFID, EXT)

    assert kf.robot_calls == 0

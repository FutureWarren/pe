from datetime import datetime, timedelta

from responder.config import Settings
from responder.models import (
    Action,
    Category,
    ClientStatus,
    Decision,
    GroupProfile,
    IncomingMessage,
    Reminder,
)
from responder.notify import escalation
from responder.store.db import Store

GROUP = GroupProfile(
    group_id="g1", name="测试群", client_status=ClientStatus.SIGNED,
    case_type="刑事辩护", lawyer_name="王", lawyer_userid="wang", backup_userid="li",
)


def _decision(urgent: bool) -> Decision:
    return Decision(
        msg_id="m1", group_id="g1", action=Action.HANDOFF,
        category=Category.URGENT if urgent else Category.CASE_STATUS, urgent=urgent,
    )


def _msg() -> IncomingMessage:
    return IncomingMessage(msg_id="m1", group_id="g1", sender_id="u1", content="人被拘留了")


def test_build_reminder_contains_context():
    r = escalation.build_reminder(_msg(), _decision(True), GROUP, "AI 安抚回复")
    assert r.urgent and "加急" in r.summary
    assert "人被拘留了" in r.summary and "AI 安抚回复" in r.summary
    assert r.to_userid == "wang"


class _Snd:
    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    def send_direct_text(self, userid, text):
        self.sent.append((userid, text))
        return self.ok


def test_escalate_only_overdue_urgent(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.upsert_group(GROUP)
    settings = Settings(escalation_seconds=600)
    snd = _Snd()

    rid_urgent = store.save_reminder(
        Reminder(msg_id="m1", group_id="g1", to_userid="wang", urgent=True, summary="s")
    )
    store.save_reminder(
        Reminder(msg_id="m2", group_id="g1", to_userid="wang", urgent=False, summary="s")
    )

    # 未超时 → 不升级
    assert escalation.escalate_overdue(store, snd, settings=settings) == []
    # 超时 → 仅紧急项升级
    later = datetime.now() + timedelta(seconds=700)
    escalated = escalation.escalate_overdue(store, snd, settings=settings, now=later)
    assert escalated == [rid_urgent]
    statuses = {r["id"]: r["status"] for r in store.pending_reminders()}
    assert statuses[rid_urgent] == "escalated"
    assert snd.sent and snd.sent[0][0] == GROUP.backup_userid


def test_escalation_not_marked_when_send_fails(tmp_path):
    """没送到就标「已升级」是假状态：第二责任人没收到，系统还不再重试。"""
    store = Store(str(tmp_path / "t2.db"))
    store.upsert_group(GROUP)
    settings = Settings(escalation_seconds=600)
    rid = store.save_reminder(
        Reminder(msg_id="m1", group_id="g1", to_userid="wang", urgent=True, summary="s")
    )
    later = datetime.now() + timedelta(seconds=700)
    dead = _Snd(ok=False)
    assert escalation.escalate_overdue(store, dead, settings=settings, now=later) == []
    assert {r["id"]: r["status"] for r in store.pending_reminders()}[rid] == "pending"
    # 下一轮恢复后应当补上
    live = _Snd()
    assert escalation.escalate_overdue(store, live, settings=settings, now=later) == [rid]


def test_escalation_falls_back_to_global_target(tmp_path):
    """没配第二责任人时升给全局兜底人——升级链不该断在「没人可升」上。"""
    from responder.models import GroupProfile

    store = Store(str(tmp_path / "t3.db"))
    store.upsert_group(GroupProfile(group_id="g9", lawyer_userid="wang"))
    settings = Settings(escalation_seconds=600, default_notify_userid="reception")
    store.save_reminder(
        Reminder(msg_id="m9", group_id="g9", to_userid="wang", urgent=True, summary="s")
    )
    snd = _Snd()
    later = datetime.now() + timedelta(seconds=700)
    assert escalation.escalate_overdue(store, snd, settings=settings, now=later)
    assert snd.sent[0][0] == "reception"


# ------------------------------------------------------- P1 也要有人管
# 律所方问到点子上：只按 P0 转接，会不会把 P1 丢掉？
# 转接不丢——P1 照样派单、照样推交接单，只是不占用律师的即时注意力。
# 但**督办**一度真的只扫 P0：单子推出去之后律师不跟，就再没有任何机制
# 会提起它。而 P1 是「有意愿但还没留电话」，恰恰最需要有人推一把。
def _overdue_env(tmp_path, priority, **over):
    from datetime import datetime, timedelta

    from responder.config import Settings
    from responder.models import ClientStatus, GroupProfile
    from responder.service import Pipeline
    from responder.store.db import Store
    from responder.worker import Worker

    db = str(tmp_path / f"sla-{priority}.db")
    store = Store(db)
    cfg = dict(mode="live", db_path=db, lead_brief_enabled=True, lead_sla_enabled=True,
               default_notify_userid="wei")
    cfg.update(over)
    settings = Settings(**cfg)

    class Snd:
        def __init__(self):
            self.direct = []

        def send_direct_text(self, userid, text):
            self.direct.append((userid, text))
            return True

    snd = Snd()
    store.upsert_group(GroupProfile(group_id="kf:wk:c", name="客户 A",
                                    client_status=ClientStatus.PROSPECT))
    store.upsert_lead("kf:wk:c", {"intent": "warm", "priority": priority,
                                  "summary": "咨询欠薪", "contact": "13712345678"})
    store.assign_lead("kf:wk:c", "wei")
    old = (datetime.now() - timedelta(days=3)).isoformat()
    with store._conn() as conn:
        conn.execute("UPDATE leads SET assigned_at=? WHERE group_id=?", (old, "kf:wk:c"))
    worker = Worker(Pipeline(store, snd, settings), store, snd)
    worker._sweep_lead_sla(datetime.now())
    return snd


def test_p1_leads_get_chased_too(tmp_path):
    snd = _overdue_env(tmp_path, "P1")
    assert snd.direct, "P1 派出去没人跟的话，得有人提起它"
    assert "有意愿线索" in snd.direct[0][1]


def test_p1_sla_can_be_switched_off(tmp_path):
    snd = _overdue_env(tmp_path, "P1", lead_p1_sla_seconds=0)
    assert not snd.direct


def test_long_sla_is_phrased_in_hours(tmp_path):
    """「已超 1440 分钟」没人算得过来。"""
    snd = _overdue_env(tmp_path, "P1")
    assert "24 小时" in snd.direct[0][1]


def test_p0_still_uses_its_own_shorter_clock(tmp_path):
    snd = _overdue_env(tmp_path, "P0")
    assert "强意愿线索" in snd.direct[0][1] and "60 分钟" in snd.direct[0][1]

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

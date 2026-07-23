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


def test_escalate_only_overdue_urgent(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.upsert_group(GROUP)
    settings = Settings(escalation_seconds=600)

    rid_urgent = store.save_reminder(
        Reminder(msg_id="m1", group_id="g1", to_userid="wang", urgent=True, summary="s")
    )
    store.save_reminder(
        Reminder(msg_id="m2", group_id="g1", to_userid="wang", urgent=False, summary="s")
    )

    # 未超时 → 不升级
    assert escalation.escalate_overdue(store, None, settings=settings) == []
    # 超时 → 仅紧急项升级
    later = datetime.now() + timedelta(seconds=700)
    escalated = escalation.escalate_overdue(store, None, settings=settings, now=later)
    assert escalated == [rid_urgent]
    statuses = {r["id"]: r["status"] for r in store.pending_reminders()}
    assert statuses[rid_urgent] == "escalated"

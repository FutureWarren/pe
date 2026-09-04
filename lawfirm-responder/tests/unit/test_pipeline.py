from responder.config import Settings
from responder.models import ClientStatus, GroupProfile, IncomingMessage
from responder.service import Pipeline
from responder.store.db import Store


def make_pipeline(tmp_path) -> Pipeline:
    store = Store(str(tmp_path / "test.db"))
    store.upsert_group(
        GroupProfile(
            group_id="g1", name="测试群", client_status=ClientStatus.SIGNED,
            case_type="刑事辩护", lawyer_name="王", lawyer_userid="wang",
            backup_userid="li",
        )
    )
    settings = Settings(mode="shadow", db_path=str(tmp_path / "test.db"))
    return Pipeline(store, sender=None, settings=settings)


def _msg(content: str, msg_id: str = "m1", staff: bool = False) -> IncomingMessage:
    return IncomingMessage(
        msg_id=msg_id, group_id="g1", sender_id="u1", content=content, sender_is_staff=staff
    )


def test_handoff_creates_reply_and_reminder(tmp_path):
    p = make_pipeline(tmp_path)
    decision = p.handle(_msg("我的案子到哪一步了？"))
    assert decision.action.value == "handoff"
    assert len(p.store.list_replies("g1")) == 1
    todo = p.store.pending_reminders()
    assert len(todo) == 1
    assert "我的案子到哪一步了" in todo[0]["summary"]


def test_silence_logged_no_reply(tmp_path):
    p = make_pipeline(tmp_path)
    decision = p.handle(_msg("谢谢王律师"))
    assert decision.action.value == "silence"
    assert p.store.list_decisions("g1")  # 沉默也入日志
    assert p.store.list_replies("g1") == []
    assert p.store.pending_reminders() == []


def test_urgent_reminder_flagged(tmp_path):
    p = make_pipeline(tmp_path)
    p.handle(_msg("我老公被拘留了怎么办"))
    todo = p.store.pending_reminders()
    assert todo and todo[0]["urgent"] == 1
    assert "加急" in todo[0]["summary"]


def test_staff_message_updates_takeover(tmp_path):
    p = make_pipeline(tmp_path)
    p.handle(_msg("我来回复一下这个问题", msg_id="s1", staff=True))
    # 律师刚发言过 → 客户新消息被接管门拦下（不发言，但判断照记）
    decision = p.handle(_msg("我的案子到哪一步了？", msg_id="m2"))
    assert not decision.should_speak
    assert "gate:human-takeover" in decision.reasons


def test_shadow_mode_reply_is_draft(tmp_path):
    p = make_pipeline(tmp_path)
    p.handle(_msg("我的案子到哪一步了？"))
    reply = p.store.list_replies("g1")[0]
    assert reply["mode"] == "shadow"

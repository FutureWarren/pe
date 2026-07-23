from datetime import datetime

from responder.config import Settings
from responder.engine.decision import decide, wait_seconds
from responder.models import Action, GroupProfile, IncomingMessage

SETTINGS = Settings(
    wait_seconds_day=150, wait_seconds_night=60,
    night_start_hour=21, night_end_hour=8, takeover_seconds=1800,
)
DAY = datetime(2026, 7, 23, 14, 0)
NIGHT = datetime(2026, 7, 23, 23, 0)
GROUP = GroupProfile(group_id="g1", lawyer_name="王", lawyer_userid="wang")


def _msg(content: str, staff: bool = False) -> IncomingMessage:
    return IncomingMessage(
        msg_id="m1", group_id="g1", sender_id="u1", content=content, sender_is_staff=staff
    )


def test_wait_seconds_day_vs_night():
    assert wait_seconds(DAY, SETTINGS) == 150
    assert wait_seconds(NIGHT, SETTINGS) == 60


def test_silence_never_speaks():
    d = decide(_msg("早上好"), GROUP, settings=SETTINGS, now=DAY)
    assert d.action == Action.SILENCE and not d.should_speak


def test_waits_before_speaking():
    d = decide(_msg("取保候审需要什么条件？"), GROUP,
               seconds_unanswered=10, settings=SETTINGS, now=DAY)
    assert d.action == Action.ANSWER and not d.should_speak


def test_speaks_after_wait():
    d = decide(_msg("取保候审需要什么条件？"), GROUP,
               seconds_unanswered=200, settings=SETTINGS, now=DAY)
    assert d.should_speak


def test_night_shorter_wait():
    d = decide(_msg("取保候审需要什么条件？"), GROUP,
               seconds_unanswered=90, settings=SETTINGS, now=NIGHT)
    assert d.should_speak


def test_urgent_bypasses_wait():
    d = decide(_msg("我老公被拘留了怎么办"), GROUP,
               seconds_unanswered=0, settings=SETTINGS, now=DAY)
    assert d.urgent and d.should_speak


def test_human_takeover_silences_ai():
    d = decide(_msg("我的案子到哪一步了？"), GROUP,
               seconds_since_last_staff_reply=60, seconds_unanswered=999,
               settings=SETTINGS, now=DAY)
    assert d.action == Action.HANDOFF and not d.should_speak
    assert "gate:human-takeover" in d.reasons


def test_takeover_expires():
    d = decide(_msg("我的案子到哪一步了？"), GROUP,
               seconds_since_last_staff_reply=3600, seconds_unanswered=999,
               settings=SETTINGS, now=DAY)
    assert d.should_speak


def test_ai_disabled_group():
    group = GroupProfile(group_id="g1", ai_enabled=False)
    d = decide(_msg("我的案子到哪一步了？"), group,
               seconds_unanswered=999, settings=SETTINGS, now=DAY)
    assert not d.should_speak and "gate:ai-disabled" in d.reasons

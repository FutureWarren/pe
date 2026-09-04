"""紧急路径的四处误伤（2026-08-12 体检「值得做」）。

四条各不相同，但都属于同一类：**回复本身在拆自己的台**——
话说得像在管事，下一句就否掉了它。
"""

from datetime import datetime

import pytest

from responder.config import Settings
from responder.engine import rules
from responder.models import (
    Action,
    Category,
    ClientStatus,
    GroupProfile,
    IncomingMessage,
)
from responder.reply import templates
from responder.service import Pipeline
from responder.store.db import Store

OPEN_KFID = "wk-urg"
EXT = "wmUrgent"
GID = f"kf:{OPEN_KFID}:{EXT}"


class Kf:
    def __init__(self):
        self.sent: list[str] = []

    def available(self):
        return True

    def servicer_list(self, kfid):
        return ["wei"]

    def send_text(self, kfid, ext, text):
        self.sent.append(text)
        return True

    def transfer(self, kfid, ext, userid):
        return True


class Snd:
    def send_direct_text(self, userid, text):
        return True


def make(tmp_path, **over):
    db = str(tmp_path / "u.db")
    store = Store(db)
    cfg = dict(
        mode="live", db_path=db, wecom_kf_secret="s", split_messages=False,
        split_delay_seconds=0, llm_answer_enabled=False, llm_refine_enabled=False,
        lead_brief_enabled=False,
    )
    cfg.update(over)
    kf = Kf()
    return store, kf, Pipeline(store, sender=Snd(), settings=Settings(**cfg), kf_client=kf)


def kf_group() -> GroupProfile:
    return GroupProfile(
        client_status=ClientStatus.PROSPECT, group_id=GID,
        kf_open_kfid=OPEN_KFID, kf_external_userid=EXT,
    )


def msg(text, mid="m1") -> IncomingMessage:
    return IncomingMessage(
        msg_id=mid, group_id=GID, sender_id=EXT, content=text,
        msg_type="text", created_at=datetime.now(), sender_is_staff=False,
    )


# ------------------------------------------- ① 「去劳动局投诉」不是投诉律所
@pytest.mark.parametrize("text", [
    "我要去劳动局投诉他们",
    "我准备向劳动监察大队投诉公司",
    "打算投诉这家公司，能行吗",
    "我要举报他们违规用工",
])
def test_complaining_about_the_other_side_is_not_an_emergency(text):
    """劳动争议是本所主业，这是客户描述自己诉求最常见的说法之一。

    误判成「客户在投诉律所」的后果有两层：换回一句「您先别慌，我们很重视」——
    把一个来问怎么维权的人当成正在投诉我们的人，当面证明没听懂，
    而这本是最该展示专业度的一句；以及这批假紧急件被拍成 P0，
    把真正的刑拘、开庭在即挤在同一档排队。
    """
    _, _, urgent, reasons = rules.classify(text, is_one_on_one=True)
    assert not urgent, f"{text} → {reasons}"


@pytest.mark.parametrize("text", [
    "我要投诉你们律所",
    "再不回复我就投诉你们",
    "我要举报贵所",
])
def test_complaining_about_us_is_still_an_emergency(text):
    """反向不放松：真冲着我们来的照旧当场加急。"""
    _, _, urgent, _ = rules.classify(text, is_one_on_one=True)
    assert urgent, text


# ------------------------------------------- ② 紧急回复的下一步要自洽
def test_urgent_reply_does_not_say_the_lawyer_will_call_when_free(tmp_path):
    """「已经加急通知律师了……律师**一有空**就给您回电话」——后半句当场否掉前半句。

    刑拘只有 37 天，家属此刻正在比谁反应最快。
    """
    store, kf, p = make(tmp_path)
    store.upsert_group(kf_group())

    p.handle(msg("我弟弟昨天被刑事拘留了"))

    assert kf.sent
    body = kf.sent[0]
    assert "加急" in body
    assert "一有空" not in body, f"跟「加急」自相矛盾：{body}"


def test_urgent_next_step_asks_for_what_the_lawyer_needs(tmp_path):
    """让一个正慌着的人有具体的事可做，本身就是最有效的安抚。"""
    text = templates.urgent_next_step(kf_group(), seed="s")
    assert any(w in text for w in ("看守所", "关在哪", "地点"))
    assert "一有空" not in text


def test_urgent_wording_has_enough_variants():
    """客户第三条又说「我快撑不住了」，收到一字不差的同一句，本身就是问题。"""
    seen = {templates.handoff_urgent(kf_group(), seed=f"s{i}") for i in range(20)}
    assert len(seen) >= 3


# ------------------------------------------- ③ 模型挂了也不能复读同一句
def test_the_degraded_answer_echoes_what_was_asked():
    """模型超时/限流/密钥过期时走这条路，而它是静默的。

    客户连问两句，两条回复开头一字不差——「免费法律咨询」这个卖点当场归零，
    AI 退回成一个复读的转达员。
    """
    g = kf_group()
    a = templates.answer_without_llm(g, question="仲裁一般要多久出结果", seed="a")
    b = templates.answer_without_llm(g, question="那我该准备什么材料", seed="b")
    assert "仲裁" in a and "材料" in b, "答不了也得让他知道这句话被听见了"
    assert a.split("，")[0] != b.split("，")[0] or a != b


def test_degradation_is_counted(tmp_path):
    """连着几天走降级而没人发现，是这条路最贵的地方。"""
    store, _, p = make(tmp_path, llm_answer_enabled=False)
    store.upsert_group(kf_group())

    p.handle(msg("公司拖欠我三个月工资，仲裁一般要多久"))

    assert store.counters().get("llm_degraded", {}).get("n", 0) >= 1


# ------------------------------------------- ④ 企微说没送到就不能当已送到
def test_a_send_failure_event_marks_the_reply_undelivered(tmp_path):
    """不处理的后果全是假象：库里标着「已发送」、控制台显示「AI 已回复」、
    追问逻辑认定「已经答过了」不再补发——而客户那头一个字都没收到。"""
    from responder.worker import Worker

    store, kf, p = make(tmp_path)
    store.upsert_group(kf_group())
    store.save_reply("r1", GID, "您好，这里是上海松沪律师事务所。", "live", True,
                     category="greeting", parts=1)
    assert store.has_greeting(GID) is True
    w = Worker(p, store, sender=Snd(), kf_client=kf)

    w._handle_kf_message({
        "msgtype": "event", "open_kfid": OPEN_KFID, "external_userid": EXT,
        "event": {"event_type": "msg_send_fail", "fail_msgid": "r1"},
    })

    assert store.has_greeting(GID) is False, (
        "标成未送达之后，「已经答过了吗」的判据要跟着变回「还没答过」，"
        "补发/挽留/追问才重新可用"
    )
    assert store.get_note(f"undelivered:{GID}")
    assert store.counters().get("kf_send_failed", {}).get("n", 0) == 1


def test_an_unknown_event_is_still_just_recorded(tmp_path):
    """认不出的事件照旧只留证据，不做动作——这条没被上面那步改坏。"""
    from responder.worker import Worker

    store, kf, p = make(tmp_path)
    w = Worker(p, store, sender=Snd(), kf_client=kf)

    w._handle_kf_message({
        "msgtype": "event", "open_kfid": OPEN_KFID, "external_userid": EXT,
        "event": {"event_type": "something_new"},
    })

    assert store.get_note("kf_unknown_event") == "something_new"


def test_urgent_still_reaches_a_human(tmp_path):
    """收窄投诉规则不能顺手把真紧急件的转人工也弄没了。"""
    _, cat, urgent, _ = rules.classify("我老公被刑拘了怎么办", is_one_on_one=True)
    assert urgent and cat == Category.URGENT
    assert rules.classify("我老公被刑拘了怎么办", is_one_on_one=True)[0] == Action.HANDOFF

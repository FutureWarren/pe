"""话术松绑（律所方 2026-08-13）：进线承接由模型看上下文生成，模板只兜底。

真机第四轮的原话：「AI 的智能程度完全被我们预先写的话钉死了。不要设置那么多
固定话语，我们要的是灵活性。」两幕最刺眼：客户说「你这个回答完全不跟我的案子
有关」，收到的是「帮您催一下律师」；刚说完案情，被要求「把情况讲一下」。
病根相同——回复是从固定话术里**挑**出来的，谁也没看客户到底说了什么。

放开的同时四道闸一道不少：
  1. 出口闸门 guard 照过——模型说漏嘴（金额/承诺）就当场拦下，落回模板；
  2. 紧急、费用两类不放给模型：「已加急」的承诺和授权原话必须一字不差；
  3. 点头/收号/报家门这些专属答法照旧走模板——对话里最值钱的几拍不容有失；
  4. 模型不可用/示弱时模板兜底，客户永远有人应。
"""

from datetime import datetime

import pytest

from responder.config import Settings
from responder.engine import llm
from responder.models import (
    Action,
    Category,
    ClientStatus,
    Decision,
    GroupProfile,
    IncomingMessage,
)
from responder.reply import templates
from responder.reply.generator import generate

OPEN_KFID = "wk-flex"
EXT = "wmFlexCustomer"
GID = f"kf:{OPEN_KFID}:{EXT}"


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


def handoff_decision(category=Category.OTHER, reasons=None, urgent=False) -> Decision:
    return Decision(
        msg_id="m1", group_id=GID, action=Action.HANDOFF, category=category,
        urgent=urgent, reasons=list(reasons or []), should_speak=True,
    )


SETTINGS = dict(mode="live", llm_answer_enabled=True, llm_refine_enabled=False)

MODEL_REPLY = "不好意思，是我没说到点上。\n您说的拖欠工资，拖了几个月了，有没有签劳动合同？"


@pytest.fixture
def model(monkeypatch):
    """把模型层换成录音桩：记下调用，回放固定文本。"""
    calls = []

    def fake(latest, **kw):
        calls.append((latest, kw))
        return MODEL_REPLY

    monkeypatch.setattr(llm, "generate_intake_body", fake)
    return calls


# ---------------------------------------------------------------- 模型接管承接
def test_the_model_reply_reaches_the_customer(model):
    d = handoff_decision(reasons=["kf:substance"])
    r = generate(msg("你这个回答完全不跟我的案子有关"), d, kf_group(),
                 settings=Settings(**SETTINGS))
    assert r is not None and r.passed
    assert r.text == MODEL_REPLY
    assert "intake:llm" in d.reasons
    assert model, "模型应当被调到"


def test_the_model_sees_the_conversation_so_far(model):
    """不看历史就会重蹈覆辙：追问客户刚说过的事。摘录必须进 user 消息。"""
    history = [
        {"sender_is_staff": 0, "content": "公司拖欠我三个月工资"},
        {"sender_is_staff": 1, "content": "您说的我记下了"},
    ]
    generate(msg("那我现在该怎么办"), handoff_decision(), kf_group(),
             history=history, settings=Settings(**SETTINGS))
    _, kw = model[0]
    assert "拖欠我三个月工资" in kw["history_text"]
    assert "律所同事" in kw["history_text"]


def test_the_model_knows_the_session_was_handed_off(model):
    """转接后 AI 接着陪（应转尽转）。模型不知道已转，就会再许一次
    「我帮您转给律师」——那句话暗示上一次没转成。"""
    generate(msg("好的麻烦了"), handoff_decision(), kf_group(handoff_userid="wei"),
             settings=Settings(**SETTINGS))
    assert model[0][1]["handed_off"] is True

    model.clear()
    generate(msg("好的麻烦了"), handoff_decision(), kf_group(),
             settings=Settings(**SETTINGS))
    assert model[0][1]["handed_off"] is False


def test_case_status_complaints_go_to_the_model_too(model):
    """真机那一幕的病根：催单/不满被 CASE 类模板接走，答非所问。也要放开。"""
    d = handoff_decision(category=Category.CASE_STATUS)
    r = generate(msg("我的事你们到底看了没有"), d, kf_group(),
                 settings=Settings(**SETTINGS))
    assert r.text == MODEL_REPLY


# ---------------------------------------------------------------- 闸门一道不少
def test_a_leaky_model_reply_is_blocked_and_the_template_takes_over(monkeypatch):
    """模型说漏嘴（提了金额）→ 出口闸门拦下 → 落回模板，客户照样有人应。
    **绝不能**把漏嘴的那句发出去，也不能让客户等一个空回复。"""
    monkeypatch.setattr(llm, "generate_intake_body",
                        lambda latest, **kw: "我们代理费一般一万块，您看合适吗")
    d = handoff_decision(reasons=["kf:intake"])
    r = generate(msg("你们怎么收费的"), d, kf_group(), settings=Settings(**SETTINGS))
    assert r is not None and r.passed
    assert "一万" not in r.text
    assert "intake:llm-blocked" in d.reasons
    assert "intake:llm" not in d.reasons


def test_urgent_messages_never_go_to_the_model(model):
    """「已加急」是承诺，必须一字不差——紧急话术不交给模型即兴。"""
    d = handoff_decision(category=Category.URGENT, urgent=True)
    r = generate(msg("我家人被拘留了"), d, kf_group(), settings=Settings(**SETTINGS))
    assert r is not None
    assert model == [], "紧急类不该调模型"


def test_fee_messages_never_go_to_the_model(model):
    """费用是合规红线：授权原话之外一个字不能多，模板层专责。"""
    d = handoff_decision(category=Category.FEE)
    r = generate(msg("律师费怎么算"), d, kf_group(), settings=Settings(**SETTINGS))
    assert r is not None
    assert model == []


def test_the_precise_beats_stay_with_templates(model):
    """点头、收号这两拍有专属答法，比模型的即兴更值钱，照旧模板。"""
    d = handoff_decision(reasons=["contact-left"])
    r = generate(msg("13800001111"), d, kf_group(), settings=Settings(**SETTINGS))
    assert r is not None
    assert model == [], "刚收到号码该走「收到」专属话术"

    d2 = handoff_decision(reasons=["affirm:office"])
    r2 = generate(msg("好的"), d2, kf_group(), settings=Settings(**SETTINGS))
    assert r2 is not None
    assert model == []


def test_group_chats_are_untouched(model):
    """群聊里承办律师在场，承接话术维持既有口径，不放给模型。"""
    g = GroupProfile(group_id="g-1", client_status=ClientStatus.SIGNED,
                     lawyer_name="魏")
    r = generate(msg("我的案子怎么样了"), handoff_decision(category=Category.CASE_STATUS),
                 g, settings=Settings(**SETTINGS))
    assert r is not None
    assert model == []


def test_a_pending_office_invite_keeps_the_template_pairing(model):
    """带邀约收口的回复是成对话术（正文 + 邀约），由 _close 统一拼装——
    模型正文接一段模板邀约会话风分裂，这类整体走模板。"""
    d = handoff_decision()
    generate(msg("那你们能帮我处理吗"), d, kf_group(),
             office_invite=True, settings=Settings(**SETTINGS))
    assert model == []


# ---------------------------------------------------------------- 模板兜底
def test_no_model_falls_back_to_the_intake_template(monkeypatch):
    monkeypatch.setattr(llm, "generate_intake_body", lambda latest, **kw: None)
    d = handoff_decision(reasons=["kf:intake"])
    r = generate(msg("公司拖欠我三个月工资"), d, kf_group(),
                 settings=Settings(**SETTINGS))
    assert r is not None and r.passed
    assert r.text == templates.intake_probe(kf_group(), seed="m1",
                                            settings=Settings(**SETTINGS))
    assert "intake:fallback-no-llm" in d.reasons


def test_switching_the_llm_off_restores_the_old_behavior(model):
    """llm_answer_enabled=False 时一次模型也不调，也不记「降级」——那是关，不是坏。"""
    d = handoff_decision(reasons=["kf:intake"])
    r = generate(msg("公司拖欠我三个月工资"), d, kf_group(),
                 settings=Settings(mode="live", llm_answer_enabled=False))
    assert r is not None and r.passed
    assert model == []
    assert "intake:fallback-no-llm" not in d.reasons


def test_model_output_still_gets_sanitized(monkeypatch):
    """模型带出 markdown/表情这类「一看就是机器人」的形态要被净化层收拾掉。"""
    monkeypatch.setattr(
        llm, "generate_intake_body",
        lambda latest, **kw: "**收到！** 您说的情况我记下了，请问是什么时候发生的？",
    )
    d = handoff_decision()
    r = generate(msg("公司欠我工资"), d, kf_group(), settings=Settings(**SETTINGS))
    assert r is not None
    assert "**" not in r.text

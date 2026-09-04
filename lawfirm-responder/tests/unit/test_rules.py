from responder.engine.rules import classify
from responder.models import Action, Category


def test_urgent_detention():
    action, category, urgent, _ = classify("我老公被拘留了，怎么办啊？")
    assert action == Action.HANDOFF and category == Category.URGENT and urgent


def test_urgent_bypasses_general_topic():
    # 传唤同时命中通用话题，但紧急层优先
    action, category, urgent, _ = classify("刚收到传唤通知，让我明天过去")
    assert action == Action.HANDOFF and urgent


def test_fee_never_answer():
    action, category, _, _ = classify("这种案子律师费大概多少？")
    assert action == Action.HANDOFF and category == Category.FEE


def test_case_specific():
    action, category, _, _ = classify("我的案子现在到哪一步了？")
    assert action == Action.HANDOFF and category == Category.CASE_STATUS


def test_self_case_ref_never_answer():
    # 自指本案即使是通用话题也走承接
    action, category, _, _ = classify("我想问下我的案子，判几年有说法了吗？")
    assert action == Action.HANDOFF


def test_general_law_question():
    action, category, _, _ = classify("取保候审需要什么条件？")
    assert action == Action.ANSWER and category == Category.GENERAL_LAW


def test_chitchat_silence():
    for text in ["早上好", "谢谢王律师", "[微笑]", "收到", "今天天气真不错"]:
        action, _, _, _ = classify(text)
        assert action == Action.SILENCE, text


def test_at_mention_silence():
    action, _, _, _ = classify("@老张 你昨天说的那个事办了吗")
    assert action == Action.SILENCE


def test_non_text_silence():
    action, _, _, _ = classify("", msg_type="image")
    assert action == Action.SILENCE


def test_contact_ping():
    action, category, _, _ = classify("在吗？")
    assert action == Action.HANDOFF and category == Category.CONTACT

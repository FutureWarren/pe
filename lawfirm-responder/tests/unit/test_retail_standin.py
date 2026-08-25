"""AI 补位：客服来不及回时，AI 顶上还是等人。

这一组守的是酷基时代方案里最容易做坏的那个判断。产品设计的原话：
「成交之后客服来不及回复用户，AI 直接代替客服进行回复。」

对，但**不是所有消息都该由 AI 顶上**——按「说错的代价」分三档：
说错也就再查一次的（物流、保修）零等待立刻答；说错要赔钱的（退款、尾款）
绝不代答；说错会点着火的（投诉）只安抚 + 立刻叫人。
"""

from datetime import datetime, timedelta

import pytest

from responder.retail import standin
from responder.retail.intents import Handling, detect, handling_of


# ------------------------------------------------------ ① 真人在场一律让位
def test_a_human_who_just_spoke_silences_the_ai():
    """**本组最重要的一条。** 两个人抢答，比慢一点糟得多。

    判据与律所侧同一个开关（takeover_seconds），行为一致——
    运维只需要理解一套规则，而不是每个行业一套。
    """
    now = datetime(2026, 8, 25, 15, 0, 0)
    d = standin.decide(
        "我那台什么时候能到", after_sale=True,
        staff_replied_at=now - timedelta(minutes=2), now=now,
    )
    assert d.speak is False
    assert d.escalate is False, "真人在场时连回执都不该发——发了就是插话"
    assert "让位" in d.reason


def test_the_ai_steps_back_in_once_the_human_goes_quiet():
    """销售回了一句就不管了，客户第二天又来问——不能让窗口死掉。"""
    now = datetime(2026, 8, 25, 15, 0, 0)
    d = standin.decide(
        "发货了吗", after_sale=True,
        staff_replied_at=now - timedelta(hours=3), now=now,
        takeover_seconds=1800,
    )
    assert d.speak is True


# ------------------------------------------------------ ② 三档分流
@pytest.mark.parametrize("text,intent_key", [
    ("我的订单发货了吗", "order_status"),
    ("到哪了", "order_status"),
    ("什么时候能去拿", "pickup"),
    ("发票开了吗", "invoice"),
    ("屏幕摔碎了保修吗", "warranty"),
    ("怎么把旧手机的微信记录导过来", "data_migration"),
    ("修好了吗", "repair_status"),
])
def test_information_questions_are_answered_instantly(text, intent_key):
    """说错也就再查一次的，零等待。

    为什么不「等三分钟看销售回不回」：销售回这类问题的动作也是去查一下
    再告诉客户，AI 查得更快、还不会忘。那三分钟纯是白等。
    """
    d = standin.decide(text, after_sale=True)
    assert d.speak is True, f"{text} 应当由 AI 直接答"
    assert d.kind == intent_key
    assert d.wait_seconds == 0


@pytest.mark.parametrize("text", [
    "这个能退吗，我不想要了",
    "旧机的钱什么时候到账",
    "我这台旧的能抵多少",
    "能不能再便宜点",
])
def test_anything_about_money_is_never_answered_by_the_ai(text):
    """**AI 说一个数字，在客户眼里就是门店的承诺。**

    事后说「那是机器人说的」只会让事情更糟。所以涉及钱与承诺的，
    哪怕客户要等一小时也要等真人——但要先给一句回执，别让他对着静默。
    """
    d = standin.decide(text, after_sale=True)
    assert d.speak is False
    assert d.escalate is True
    assert standin.receipt_line(d), "不代答不等于不出声"
    assert not any(c.isdigit() for c in standin.receipt_line(d)), \
        "回执里绝不能出现任何数字"


def test_complaints_get_soothed_and_escalated_never_explained():
    """投诉永远不让 AI 处理——它的任何解释都可能被当成门店的正式答复。"""
    d = standin.decide("我要投诉你们，拖了这么久", after_sale=True)
    assert d.speak is False
    assert d.escalate is True
    assert "店长" in standin.receipt_line(d)


def test_an_unrecognised_message_is_handed_over_not_improvised():
    """**认不出就别答。**

    反过来（认不出就让模型自由发挥）在律所尚可（最坏是答得泛），
    在零售是灾难：模型会自信地编出一个价格、一个库存、一个到货时间。
    """
    d = standin.decide("那个啥，你们那个东西怎么弄来着")
    assert d.speak is False
    assert d.escalate is True
    assert handling_of("那个啥，你们那个东西怎么弄来着") is Handling.HUMAN


# ------------------------------------------------------ ③ 售前售后同句异义
def test_the_same_sentence_means_different_things_before_and_after_the_sale():
    """「什么时候到」——售前问的是到货，售后问的是快递。答反了比不答更糟。"""
    assert detect("什么时候到", after_sale=True).key == "order_status"
    assert detect("我买的那个什么时候到").key == "order_status", \
        "提到「我买的」就该按售后理解，不用外部传参也能认出来"


# ------------------------------------------------------ ④ 别让销售重复一遍
def test_the_salesperson_is_told_what_the_ai_already_said():
    """零售比律所多一个坑：销售看不到 AI 说过什么，就会再说一遍。

    客户收到两遍一样的回复，观感是「这家店乱糟糟的」。
    """
    d = standin.decide("发货了吗", after_sale=True)
    note = standin.notice_for_staff(d, "已经发出来了，走的顺丰，昨晚到兰州了。")
    assert "AI 已代回" in note
    assert "顺丰" in note
    assert "自动让开" in note


def test_no_staff_notice_when_the_ai_stayed_quiet():
    d = standin.decide("能退吗", after_sale=True)
    assert standin.notice_for_staff(d, "") == ""

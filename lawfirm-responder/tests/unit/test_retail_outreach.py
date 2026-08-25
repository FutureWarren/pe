"""主动触达的红线：不许拿客服通道做营销。

《微信客服功能服务条款》3.3.3：「不得通过客服账号发送与客服咨询无关的，
如频繁营销、骚扰、侮辱等信息。」

零售最容易起的念头就是「48 小时内能发 5 条，正好用来推新品推活动」。
**这条路是封的**——那 5 条是技术上限，不是许可范围。
而违规的代价不是少发一条，是**客服账号被限制，整条通道一起没**，
包括那些正经的售后答复。
"""

import pytest

from responder.retail.outreach import check_outbound


# ------------------------------------------------------------ ① 主动触达
@pytest.mark.parametrize("text", [
    "限时抢购！Mate 70 Pro 直降 500，最后三天",
    "新品上市，欢迎来店里体验",
    "专属福利：老客户回馈，仅剩 5 个名额",
    "周年庆大促，扫码领券",
])
def test_marketing_pushes_are_blocked(text):
    v = check_outbound(text, replying_to_customer=False)
    assert v.allowed is False
    assert "3.3.3" in v.reason or "营销" in v.reason


@pytest.mark.parametrize("text", [
    "您的订单已发货，走的顺丰，预计明天到",
    "您那台已经到店了，可以来取了",
    "您送修的机器修好了，随时能来拿",
    "您这单的发票已开好",
])
def test_service_notices_about_their_own_order_are_fine(text):
    """服务性通知不是营销：客户会关心，而且与他自己那笔交易直接相关。"""
    assert check_outbound(text, replying_to_customer=False).allowed is True


def test_an_ambiguous_proactive_message_is_refused():
    """**灰区一律按不允许处理——赌错的代价是整条通道。**"""
    v = check_outbound("在吗，最近怎么样", replying_to_customer=False)
    assert v.allowed is False
    assert "灰区" in v.reason


# ------------------------------------------------------------ ② 答客户时夹带
def test_a_normal_reply_passes():
    v = check_outbound("已经发出来了，走的顺丰，昨晚到兰州了。",
                       replying_to_customer=True)
    assert v.allowed is True


def test_promo_smuggled_into_a_legitimate_reply_is_caught():
    """**最常见的一种越界。**

    「您的货明天到，另外我们店庆最后三天欢迎来抢购」——
    前半句完全合规，后半句让整条消息变成营销。
    """
    v = check_outbound(
        "您的货明天就到了。另外我们店庆最后三天，欢迎来抢购！",
        replying_to_customer=True,
    )
    assert v.allowed is False
    assert "夹带" in v.reason


def test_empty_content_is_refused():
    assert check_outbound("", replying_to_customer=True).allowed is False

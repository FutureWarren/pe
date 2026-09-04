"""会话归属：AI 什么时候还有发言权。

这一组守的是 2026-08-25 查证出来的一条企微硬约束，它推翻了一个想当然的设计：

> `kf/send_msg` 只在会话状态 0（未处理）和 1（智能助手接待）可用；
> 状态 2/3/4 一律返回 95018。而状态流转是单向的——**3 不能回 1**。

也就是说「转给销售，但销售没来之前 AI 接着陪」在企微上**做不到**：
转过去那一刻 AI 就失去了发言权，它以为自己在陪聊，实际每一句都发不出去，
客户那头一个字都收不到。**日志正常、判断正常、客户什么也没收到**——
正是这套代码库最怕的那种失败。

所以补位的正确做法是「先别转」：通知销售和转移归属拆成两件事。
"""

import pytest

from responder.retail import session_state as ss
from responder.retail import standin
from responder.retail.session_state import KfState


# ------------------------------------------------------------ ① 硬约束本身
@pytest.mark.parametrize("state,ok", [
    (KfState.UNHANDLED, True),
    (KfState.ROBOT, True),
    (KfState.POOL, False),
    (KfState.HUMAN, False),
    (KfState.ENDED, False),
])
def test_only_unhandled_and_robot_can_send(state, ok):
    assert ss.can_send(state) is ok


def test_an_unknown_state_is_assumed_sendable():
    """查不到状态时假定**能发**。

    反过来（查不到就不发）会让 AI 在通道抖动时集体失声，
    那比偶尔踩一次 95018 糟得多——95018 至少会被记下来，静默失声不会。
    """
    assert ss.can_send(None) is True


def test_a_session_given_to_a_human_can_never_come_back_to_the_ai():
    """**这是整个补位设计的支点。**

    企微只允许 3→3 和 3→4。想当然地以为「转过去还能要回来」，
    会让整套「转人工后 AI 继续陪聊」变成一个发不出去的空动作。
    """
    assert ss.can_transition(KfState.HUMAN, KfState.ROBOT) is False
    assert ss.can_transition(KfState.HUMAN, KfState.ENDED) is True
    assert ss.can_transition(KfState.ROBOT, KfState.HUMAN) is True
    assert ss.can_transition(KfState.UNHANDLED, KfState.ROBOT) is True


def test_an_ended_session_cannot_be_moved_at_all():
    for to in (KfState.ROBOT, KfState.HUMAN, KfState.POOL):
        assert ss.can_transition(KfState.ENDED, to) is False


# ------------------------------------------------------------ ② 计划
def test_an_answerable_question_never_gives_away_the_ai_voice():
    """**能自己答的绝不转。**

    转了就永久失去发言权，而下一句客户很可能又问一个 AI 本来能答的问题。
    """
    d = standin.decide("我那台发货了吗", after_sale=True)
    p = ss.plan_for(should_speak=d.speak, escalate=d.escalate)
    assert p.speak is True
    assert p.transfer_to_human is False
    assert p.keeps_ai_voice is True


def test_handing_over_always_speaks_first():
    """**先把话说完再转。**

    回执必须在 transfer 之前发出去——转完就发不了了，客户会对着静默。
    与律所侧那条教训同源：「过渡话术没发出去就绝不转接」。
    """
    d = standin.decide("这个能退吗", after_sale=True)
    p = ss.plan_for(should_speak=d.speak, escalate=d.escalate)
    assert p.transfer_to_human is True
    assert p.speak is True, "转之前必须留一句回执"
    assert p.notify_staff is True
    assert p.keeps_ai_voice is False


def test_staying_quiet_for_a_human_does_not_touch_ownership():
    """真人正在场时不说话，也不该顺手改会话归属。"""
    p = ss.plan_for(should_speak=False, escalate=False)
    assert p.speak is False
    assert p.transfer_to_human is False
    assert p.notify_staff is False


def test_notifying_and_transferring_are_separate_things():
    """通知是即时可逆的，转移是不可逆的。混成一件事，就会为了让销售
    看见一条消息而永久交出 AI 的发言权。"""
    answerable = ss.plan_for(should_speak=True, escalate=False)
    assert answerable.notify_staff is False and answerable.transfer_to_human is False
    needs_human = ss.plan_for(should_speak=False, escalate=True)
    assert needs_human.notify_staff is True and needs_human.transfer_to_human is True

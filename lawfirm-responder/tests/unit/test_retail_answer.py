"""模型档的出口闸门。**这一组比生成本身重要得多。**

生成将来会换模型、换 prompt、换供应商；闸门贴在出口上，无论上游怎么变都拦得住。
它拦的六类不是风格问题，每一类都对应一次真实的赔钱或客诉：

    数字   → 说错一个参数，客户拿着截图来店里，错的是门店不是模型
    时间   → 「明天就到」是投诉的第一来源
    承诺   → 「保证没问题」，出了事这句话会被截图
    库存   → 库存只能从数据源出
    价钱   → 铁律，一个数字都不许由模型产生
    代言   → 经销商没有替品牌方表态的资格
"""

import pytest

from responder.retail import answer, replier
from responder.retail.intents import Handling, detect


# ------------------------------------------------------------ ① 放行的样子
@pytest.mark.parametrize("text", [
    "鸿蒙用着挺顺的，跟安卓最大的差别是多设备流转，家里有华为平板会方便很多。"
    "您平时主要用来做什么？",
    "拍照挺好的，尤其夜景。您是经常拍人还是拍风景？我按您的用法说得具体点。",
    "先重启试试，还不行就长按电源键强制重启。要是还开不了机，拿到店里我们上机看。",
    "这个得工程师上手看一眼才能定，我不敢替他下结论。",
])
def test_a_good_answer_passes(text):
    ok, why = answer.gate(text)
    assert ok, f"正常回答被拦了：{why}"


# ------------------------------------------------------------ ② 六类都要拦
@pytest.mark.parametrize("text,expect", [
    ("这款电池是5000毫安的", "数字"),
    ("屏幕刷新率120赫兹", "数字"),
    ("明天就能到", "时间"),
    ("这两天就发出去", "时间"),
    ("三天内肯定发货", "时间"),
    ("保证没问题", "承诺"),
    ("肯定能修好", "承诺"),
    ("包您满意", "承诺"),
    ("现在有货的", "库存"),
    ("这款缺货了", "库存"),
    ("大概两千块钱", "价钱"),
    ("这个多少钱来着", "价钱"),
    ("华为官方承诺三年保修", "代言"),
])
def test_the_six_kinds_are_all_stopped(text, expect):
    ok, why = answer.gate(text)
    assert not ok, f"「{text}」应该被拦（{expect}），却放行了"
    assert why, "拦下必须说清楚是哪一条——否则没人查得出为什么 AI 老是转人工"


def test_an_empty_answer_is_not_an_answer():
    assert answer.gate("")[0] is False
    assert answer.gate("   ")[0] is False


# ------------------------------------------------------------ ③ 整段丢弃
def test_a_blocked_reply_is_dropped_whole_not_edited():
    """**拦下即整段丢弃，不做删改。**

    删掉一个数字剩下的句子往往变成病句，而一句病句比一句套话更像故障。
    """
    bad = "这台电池5000毫安，日常用一天没问题"
    ok, _ = answer.gate(bad)
    assert not ok
    # 闸门只回答「行不行」，不返回「改过的版本」——没有那个出口就不会有人去用它
    assert not hasattr(answer, "sanitize")


# ------------------------------------------------------------ ④ 没模型时的行为
def test_without_a_model_it_hands_over_instead_of_guessing(monkeypatch):
    """没配 key 时**不是静默失败**：转人工，且理由记清楚。

    「AI 怎么老是转人工」这个问题，答案可能是没配 key、可能是模型示弱、
    也可能是被闸门拦下——三种的修法完全不同，所以理由必须分得开。
    """
    monkeypatch.setattr("responder.engine.llm.resolve", lambda *_a, **_k: None)
    body, why = answer.generate("鸿蒙好用吗")
    assert body == ""
    assert "没有可用的模型" in why


def test_a_model_that_says_it_cannot_answer_is_believed(monkeypatch):
    """示弱出口不许移除——模型自己说答不了，比它硬答一句强得多。"""
    monkeypatch.setattr("responder.engine.llm.resolve",
                        lambda *_a, **_k: type("P", (), {"name": "deepseek", "model": "x"})())
    monkeypatch.setattr("responder.engine.llm._chat_deepseek",
                        lambda *a, **k: f"这个我说不好 {answer.NEED_HUMAN}")
    body, why = answer.generate("鸿蒙好用吗")
    assert body == "" and "示弱" in why


def test_a_model_that_invents_a_number_is_stopped_at_the_door(monkeypatch):
    monkeypatch.setattr("responder.engine.llm.resolve",
                        lambda *_a, **_k: type("P", (), {"name": "deepseek", "model": "x"})())
    monkeypatch.setattr("responder.engine.llm._chat_deepseek",
                        lambda *a, **k: "这款是5000毫安电池，用一天没问题")
    body, why = answer.generate("续航怎么样")
    assert body == ""
    assert "闸门" in why


def test_the_replier_turns_a_blocked_answer_into_a_handover(monkeypatch):
    """闸门拦下之后，客户收到的是一句回执，不是沉默，也不是半截话。"""
    monkeypatch.setattr("responder.engine.llm.resolve",
                        lambda *_a, **_k: type("P", (), {"name": "deepseek", "model": "x"})())
    monkeypatch.setattr("responder.engine.llm._chat_deepseek",
                        lambda *a, **k: "明天就到了")
    out = replier.handle("鸿蒙好用吗", after_sale=True)
    assert out.escalate and out.reply and out.audit_failed
    assert "5000" not in out.reply


# ------------------------------------------------------------ ⑤ 提示词里的硬规矩
@pytest.mark.parametrize("must", ["数字", "到货时间", "保证", "有货没货", "[[NEED_HUMAN]]"])
def test_the_system_prompt_states_the_hard_rules(must):
    """闸门是兜底，prompt 是第一道。两道都要有——只靠闸门会让模型不停撞墙，
    只靠 prompt 则挡不住模型偶尔的自信。"""
    assert must in answer.SYSTEM


def test_the_model_tier_is_wired_end_to_end():
    """分对了档还不够，得真的走到模型那一支去。"""
    assert detect("鸿蒙好用吗").handling is Handling.MODEL
    assert detect("充电很慢正常吗").handling is Handling.MODEL

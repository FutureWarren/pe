"""发送类错误码要能被翻成一句能照着做的话。

2026-08-25 查证腾讯官方文档时发现的缺口：`ERR_HINTS` 覆盖了一堆配置类错误
（48007 没托管、60030 不在可见范围…），却**一个发送类错误码都没有**。
于是「AI 明明判断要回复，客户却什么也没收到」在控制台上只是一句
「发送失败」——而这四个码各自要做的事完全不同：

- 95018 会话已交给人工 → 要改流程，不是重试能解决的
- 95031 客户 48 小时没开口 → 只能等，做什么都没用
- 95002 超出 48 小时窗口 → 同上
- 95001 超出条数限额 → 是我们自己发多了，调分条数

**分不清就修不了。** 这正是这套代码库反复强调的那件事：
静默失败最贵，因为没人会去查一个「看起来正常」的系统。
"""

import pytest

from responder.gateway import wecom_kf


@pytest.mark.parametrize("code,must_contain", [
    (95018, "人工"),
    (95031, "48 小时"),
    (95002, "48 小时"),
    (95001, "5 条"),
])
def test_send_error_codes_explain_what_to_do(code, must_contain):
    hint = wecom_kf.err_hint({"errcode": code, "errmsg": "x"})
    assert hint, f"{code} 没有对应提示，控制台上只会显示一句「发送失败」"
    assert must_contain in hint


def test_95018_names_the_real_constraint():
    """**这一条是最要紧的。**

    腾讯官方文档两处明确：kf/send_msg 只在会话状态 0 和 1 可用；
    且状态 3 只能转 3 或 4，**无法转回 1**。
    合起来意味着「转人工之后 AI 继续陪聊」在企微上走不通——
    提示语必须把这件事说清楚，否则下一个人还会再设计一遍同样的东西。
    """
    hint = wecom_kf.err_hint({"errcode": 95018})
    assert "无法再转回" in hint or "无法再转回 1" in hint
    assert "别那么早转" in hint


def test_an_unknown_code_stays_silent_rather_than_guessing():
    """认不出的码返回空串——编一句「大概是权限问题」会让人去修错的东西。"""
    assert wecom_kf.err_hint({"errcode": 999999}) == ""
    assert wecom_kf.err_hint({}) == ""


def test_the_hint_survives_a_stringified_payload():
    """有些路径拿到的是 str(dict)，不是 dict 本身。"""
    assert "人工" in wecom_kf.err_hint("{'errcode': 95018, 'errmsg': 'x'}")


# ---------------------------------------------------------- msg_send_fail 回调
@pytest.mark.parametrize("ft,must_contain", [
    (4, "48 小时"),
    (6, "5 条"),
    (10, "拒收"),
    (11, "登录"),
])
def test_async_send_failures_are_explained(ft, must_contain):
    """**接口返回成功不代表送达。**

    企微是异步告知发送失败的。只看 send_msg 的返回值，会让库里标着
    「已发送」而客户一个字没收到——日志正常、控制台正常、客户那头是空的。
    """
    hint = wecom_kf.send_fail_hint(ft)
    assert hint and must_contain in hint


def test_fail_type_11_is_called_out_as_the_deceptive_one():
    """实战最常见的一条，而且症状具有欺骗性：

    企业没有任何成员登录过企业微信 App 时，接口一路返回成功，
    客户却什么都收不到。提示语必须把这个反差写出来，
    否则排查的人会一直盯着代码看。
    """
    hint = wecom_kf.send_fail_hint(11)
    assert "登录" in hint and ("成功" in hint or "欺骗" in hint)


def test_an_unknown_fail_type_stays_silent():
    assert wecom_kf.send_fail_hint(999) == ""
    assert wecom_kf.send_fail_hint(None) == ""

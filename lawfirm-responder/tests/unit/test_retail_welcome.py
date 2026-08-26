"""成交后加微信那一刻：把客户接进 AI 能说话的通道。

酷机时代的主要客流是线下：**客户到店买完手机，成交之后才加微信**，
后续售后全走这个微信。一个月几万客户，绝大多数是这么来的。

而他加的是销售的企业微信（外部联系人）——这条通道上服务端不能代发任意消息。
**唯一的例外是新客户欢迎语**：服务端全自动、内容任意、不需要任何人点确认，
而它的触发时机恰好就是「客户刚把销售加上」的那一秒。

所以这一组守的是整套方案的支点：那一枪必须打响，且每个客户只有一次机会。
"""

import pytest

from responder.retail import welcome

KF = "https://work.weixin.qq.com/kfid/kfc123456"


# ------------------------------------------------------------ ① 内容
def test_the_welcome_answers_the_three_things_he_actually_cares_about():
    """客户刚花了几千块，手里是台新机器。他关心的按顺序是：
    机器没问题吧 / 出问题找谁 / 怎么用。欢迎语就答这三件。"""
    p = welcome.build(
        customer_name="张先生", store_name="酷机时代城关店",
        item="Mate 70 Pro 12+512 雅川青", kf_link=KF,
    )
    assert p.ok
    assert "张先生" in p.text
    assert "Mate 70 Pro" in p.text
    assert "保修" in p.text
    assert "酷机时代城关店" in p.text


def test_it_contains_no_filler():
    """「感谢您选择我们，我们将竭诚为您服务」等于没说，还占位置。"""
    p = welcome.build(store_name="城关店", item="Mate 70", kf_link=KF)
    for filler in ("竭诚", "感谢您选择", "尊敬的"):
        assert filler not in p.text


def test_a_missing_name_does_not_leave_a_ragged_opening():
    p = welcome.build(store_name="城关店", item="Mate 70", kf_link=KF)
    assert not p.text.startswith(" ")
    assert "您好" in p.text


def test_a_missing_item_still_produces_a_usable_message():
    """门店没登记机型时也要能发——这条路径每人只有一次机会，不能因为
    缺一个字段就整条放弃。"""
    p = welcome.build(customer_name="李女士", store_name="七里河店", kf_link=KF)
    assert p.ok
    assert "李女士" in p.text
    assert "保修" in p.text


# ------------------------------------------------------------ ② 引流
def test_the_kf_entry_is_framed_as_a_person_not_a_robot():
    """**客户要的是「有人管我」，不是「你们给我配了个机器人」。**

    所以说成「售后专属客服，晚上和周末也有人在」，
    不说「智能客服」「机器人」。
    """
    p = welcome.build(store_name="城关店", item="Mate 70", kf_link=KF)
    assert p.with_kf_entry is True
    assert KF in p.text
    assert "有人在" in p.text
    for bad in ("机器人", "智能客服", "AI"):
        assert bad not in p.text


def test_no_link_means_no_dangling_invitation():
    """**宁可不引流，也不能发一句「点这里」却没有链接。**"""
    p = welcome.build(customer_name="王先生", store_name="城关店", item="Mate 70")
    assert p.ok, "欢迎语本身照发"
    assert p.with_kf_entry is False
    assert "点一下" not in p.text
    assert "http" not in p.text


# ------------------------------------------------------------ ③ 失败要说得清
@pytest.mark.parametrize("code,must_contain", [
    (41051, "已经发过"),
    (41096, "别的应用"),
    (40096, "20 秒"),
])
def test_every_failure_says_what_to_do_now(code, must_contain):
    """这一枪每个客户只有一次机会，打不响必须当场知道原因。"""
    hint = welcome.err_hint(code)
    assert hint and must_contain in hint


def test_an_unknown_code_stays_silent():
    assert welcome.err_hint(999999) == ""
    assert welcome.err_hint(None) == ""


def test_the_quietest_failure_of_all_is_spelled_out():
    """**本路径最容易踩、且最安静的一个坑。**

    管理后台给成员配了欢迎语，企微就不再返回 welcome_code。
    现象是「什么都没发生」——不报错、不告警、日志里也看不出异常，
    而每一个新客户都在悄悄流失掉。所以这句提示必须直接给出修复动作。
    """
    msg = welcome.why_no_code()
    assert "管理后台" in msg
    assert "欢迎语" in msg
    assert "关掉" in msg or "关闭" in msg

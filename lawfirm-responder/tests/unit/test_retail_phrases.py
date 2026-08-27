"""话术库：门店自己维护的那几段固定答案。

零售侧的回答分三种来源，这一组管第三种：

    价格 / 库存 / 订单  →  只能从数据源出，一个数字都不许编
    退款 / 尾款 / 谈价  →  一律不代答，叫人
    保修 / 激活 / 门店  →  写一次长期有效，就是这里

它单独做成一个可外挂的文件，是因为这些话**全是对外承诺**：
「进水算不算保修」「贴膜送不送」全国政策一样、各家做法不同，
照抄官方条款会当着客户的面跟门店的实际做法打架。这句话该由门店自己定。
"""

from responder.retail import replier
from responder.retail.phrases import DEFAULTS, Phrases, template


def write(tmp_path, body: str, name: str = "话术.csv"):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


# ------------------------------------------------------------ ① 缺口要看得见
def test_the_unfilled_ones_are_reported_not_hidden():
    """**缺话术的意图会安静地退化成转人工，而后台一切正常。**

    客户那头的表现是「问什么都说帮您问一下同事」，然后他就不问了。
    """
    keys = {k for k, _ in Phrases().gaps()}
    assert keys == {"store_info", "authenticity", "payment"}


def test_those_three_are_left_empty_on_purpose():
    """空不是遗漏。门店地址、正品口径、支付方式——每一条都只有门店说了才算数。"""
    for k in ("store_info", "authenticity", "payment"):
        assert DEFAULTS[k] == ""


def test_the_health_line_says_what_to_do():
    text = Phrases().health()
    assert "还没有话术" in text and "转人工" in text


def test_filling_one_in_closes_the_gap(tmp_path):
    p = Phrases(write(tmp_path, "意图,话术\n门店位置与营业时间,城关店在张掖路 88 号，10:00-21:00\n"))
    assert "张掖路" in p.get("store_info")
    assert "store_info" not in {k for k, _ in p.gaps()}


# ------------------------------------------------------------ ② 表怎么写都认
def test_the_chinese_name_works_as_the_key(tmp_path):
    p = Phrases(write(tmp_path, "意图,话术\n保修与三包,我们门店的口径是这样的\n"))
    assert p.get("warranty") == "我们门店的口径是这样的"


def test_the_english_key_works_too(tmp_path):
    p = Phrases(write(tmp_path, "意图,话术\nwarranty,英文键也行\n"))
    assert p.get("warranty") == "英文键也行"


def test_a_file_without_a_header_still_loads(tmp_path):
    """门店直接从别处粘两列过来，不该因为少一行表头就整张表作废。"""
    p = Phrases(write(tmp_path, "保修与三包,没有表头也认\n"))
    assert p.get("warranty") == "没有表头也认"


def test_an_unrecognised_row_is_surfaced_not_swallowed(tmp_path):
    """**认不出来的行不静默丢掉。**

    门店把「保修与三包」写成「三包」，那一整条话术就作废了，
    而没有任何地方会说明为什么——他只会觉得「填了没用」。
    """
    p = Phrases(write(tmp_path, "意图,话术\n三包,写岔了的一行\n"))
    p.get("warranty")
    assert "三包" in p.unknown
    assert "认不出来" in p.health()


def test_a_missing_file_falls_back_to_defaults_instead_of_going_mute(tmp_path):
    """路径填错不该让 AI 从此一句话都不会说——退回出厂默认，比全哑好。"""
    p = Phrases(str(tmp_path / "不存在.csv"))
    assert p.get("warranty") == DEFAULTS["warranty"]


def test_editing_the_file_takes_effect_without_a_restart(tmp_path):
    """活动天天变。改一条要立刻生效，不能等发版。"""
    import os
    import time
    path = write(tmp_path, "意图,话术\n活动与优惠,本周三期免息\n")
    p = Phrases(path)
    assert "三期" in p.get("promo")
    time.sleep(0.01)
    write(tmp_path, "意图,话术\n活动与优惠,活动已经结束了\n")
    os.utime(path, (time.time() + 1, time.time() + 1))
    assert "结束" in p.get("promo")


# ------------------------------------------------------------ ③ 只有一份话术
def test_there_is_exactly_one_source_of_truth_for_the_scripts():
    """**两份话术早晚会说不一样的话**，而客户只会看到其中一份。

    `replier` 里曾经另有一份 `_AUTO`，现在它就是 `phrases.DEFAULTS`。
    """
    assert replier._AUTO is DEFAULTS


def test_the_pipeline_actually_uses_the_store_s_own_words(tmp_path):
    """外挂的话术要真的走到客户那句回复里，不是只存在于配置文件中。"""
    from responder.retail.pipeline import Inbound, RetailPipeline
    from responder.store.db import Store

    sent: list[str] = []

    class S:
        def send_text(self, _u, t):
            sent.append(t)
            return True

    p = RetailPipeline(
        Store(str(tmp_path / "t.db")), mode="live", sender=S(),
        phrases=Phrases(write(tmp_path, "意图,话术\n保修与三包,本店保修口径以小票为准\n")),
    )
    p.handle(Inbound("mp", "oA", "保修多久啊", "m1"))
    assert sent and "小票" in sent[0]


# ------------------------------------------------------------ ④ 模板
def test_the_template_lists_every_auto_intent(tmp_path):
    """给门店的那份空表要**把该填的都列出来**——让他数着填，别靠猜。"""
    p = template(tmp_path / "模板.csv")
    body = p.read_text(encoding="utf-8-sig")
    for zh in ("保修与三包", "门店位置与营业时间", "正品行货质疑", "支付方式与开票方式"):
        assert zh in body

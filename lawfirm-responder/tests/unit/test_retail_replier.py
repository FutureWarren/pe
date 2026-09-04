"""端到端：一条客户消息进来，出去的是什么。

这一组把五关串起来验：补位判断 → 意图分档 → 取数 → 成文 → 出口审计。
最该盯的是**失败路径**——查不到、过期、审计拦下时，系统必须优雅地退回真人，
而不是硬答一句。零售里「硬答」的代价是钱。
"""

from datetime import datetime, timedelta

from responder.retail import catalog as cat
from responder.retail import orders as odr
from responder.retail import replier

NOW = datetime(2026, 8, 25, 15, 0, 0)
CUST = "kf:kuji:cust001"


def a_catalog(**kw):
    base = dict(model="Mate 70 Pro", spec="12+512", color="雅川青", price=6499,
                stock={"城关店": 3}, promo="24 期免息", updated_at=NOW.isoformat())
    base.update(kw)
    return cat.Catalog([cat.Sku(**base)])


def a_book(state=odr.OrderState.SHIPPED, **kw):
    base = dict(order_no="20260820001", customer_key=CUST, state=state,
                items="Mate 70 Pro 12+512 雅川青 ×1", carrier="顺丰",
                tracking_no="SF1234567890", last_node="昨晚已到兰州中转站",
                eta="预计今天下午送到", store="城关店", placed_at="2026-08-20")
    base.update(kw)
    return odr.OrderBook([odr.Order(**base)])


# ------------------------------------------------------------ ① 售后代答成功
def test_a_logistics_question_is_answered_in_full_without_any_human():
    """客户问「到哪了」，他要的是**什么时候能拿到**，不是一个状态码。

    「运输中」三个字等于没说，他还得再问一句。一次答完和分三次答，
    在客户那里是两种店。
    """
    out = replier.handle("我那台到哪了", customer_key=CUST, book=a_book(),
                         after_sale=True, now=NOW)
    assert out.reply
    assert out.escalate is False
    assert "顺丰" in out.reply and "兰州" in out.reply and "今天下午" in out.reply
    assert out.intent == "order_status"


def test_the_salesperson_gets_told_what_the_ai_said():
    out = replier.handle("发货了吗", customer_key=CUST, book=a_book(),
                         after_sale=True, now=NOW)
    assert "AI 已代回" in out.staff_note


def test_a_pickup_question_tells_them_where_and_what_to_bring():
    out = replier.handle("能去取了吗", customer_key=CUST,
                         book=a_book(state=odr.OrderState.ARRIVED),
                         after_sale=True, now=NOW)
    assert "城关店" in out.reply and "身份证" in out.reply


# ------------------------------------------------------------ ② 失败路径
def test_an_order_we_cannot_find_goes_to_a_human_not_to_a_guess():
    """**查不到就是查不到。** 编一个「明天到」，客户明天不会收到货。"""
    out = replier.handle("我的订单到哪了", customer_key="kf:kuji:someone-else",
                         book=a_book(), after_sale=True, now=NOW)
    assert out.escalate is True
    assert out.reply, "但仍要给一句回执——别让付过钱的客户对着静默"
    assert not any(c.isdigit() for c in out.reply)


def test_another_customers_order_number_finds_nothing():
    """归属校验是硬的：报别人的单号查不到。这不是不便，是必须的。"""
    out = replier.handle("单号 99999999 到哪了", customer_key=CUST,
                         book=a_book(), after_sale=True, now=NOW)
    assert out.escalate is True


def test_a_stale_price_sheet_never_quotes():
    """昨天的价今天报出去，门店要么亏钱要么客诉。"""
    old = (NOW - timedelta(hours=30)).isoformat()
    out = replier.handle("Mate 70 Pro 多少钱", catalog=a_catalog(updated_at=old),
                         now=NOW)
    assert out.escalate is True
    assert "6499" not in out.reply


def test_no_catalog_configured_means_no_price_is_ever_quoted():
    """还没接价格表就上线时，AI 绝不能自己编一个价出来。"""
    out = replier.handle("Mate 70 Pro 多少钱", catalog=None, now=NOW)
    assert out.escalate is True
    assert not any(c.isdigit() for c in out.reply)


# ------------------------------------------------------------ ③ 钱与投诉
def test_money_questions_get_a_receipt_and_a_human_never_a_number():
    out = replier.handle("旧机的钱什么时候到账", customer_key=CUST,
                         book=a_book(), after_sale=True, now=NOW)
    assert out.escalate is True
    assert not any(c.isdigit() for c in out.reply)
    assert "同事" in out.reply


def test_a_complaint_is_soothed_and_handed_to_the_manager():
    out = replier.handle("拖了这么久，我要投诉", customer_key=CUST, after_sale=True,
                         now=NOW)
    assert out.escalate is True
    assert "店长" in out.reply


# ------------------------------------------------------------ ④ 真人在场
def test_the_ai_says_nothing_at_all_while_a_human_is_active():
    """真人在场时连回执都不发——发了就是插话。"""
    out = replier.handle("发货了吗", customer_key=CUST, book=a_book(),
                         after_sale=True, now=NOW,
                         staff_replied_at=NOW - timedelta(minutes=3))
    assert out.reply == ""
    assert out.escalate is False
    assert out.staff_note == ""


# ------------------------------------------------------------ ⑤ 售前
def test_a_price_question_quotes_only_from_the_catalog():
    out = replier.handle("Mate 70 Pro 12+512 多少钱", catalog=a_catalog(), now=NOW)
    assert "6499" in out.reply
    assert out.escalate is False


def test_an_ambiguous_model_asks_back_with_no_price_at_all():
    """反问的那一句里**一个价都不能有**——还没确定他问的是哪台。"""
    c = cat.Catalog([
        cat.Sku(model="Mate 70", price=5499, updated_at=NOW.isoformat()),
        cat.Sku(model="Mate 70 Pro", price=6499, updated_at=NOW.isoformat()),
    ])
    out = replier.handle("Mate 70 多少钱", catalog=c, now=NOW)
    assert out.reply and out.escalate is False
    assert "5499" not in out.reply and "6499" not in out.reply
    assert "哪一款" in out.reply


def test_a_policy_question_needs_no_data_at_all():
    out = replier.handle("怎么把旧手机的微信记录导过来", after_sale=True, now=NOW)
    assert "手机克隆" in out.reply
    assert out.escalate is False


def test_warranty_explains_the_policy_but_refuses_to_judge_this_device():
    """**不许判定「你这台算不算人为损坏」**——那要工程师验机。

    AI 判了就是替门店做了承诺。
    """
    out = replier.handle("屏幕摔碎了保修吗", after_sale=True, now=NOW)
    assert out.reply
    assert "工程师" in out.reply


# ------------------------------------------------------------ ⑥ 隐私
def test_a_phone_number_is_masked_before_it_ever_goes_out():
    """客户自己知道自己的号，回显只会在截图流传时变成泄露。"""
    assert odr.mask_phone("您留的是 17721275495") == "您留的是 177****5495"


# ------------------------------------------------------------ ⑦ 试问抓到的两处
def test_a_model_with_no_price_does_not_answer_with_stock_instead():
    """**客户问的是多少钱，不能拿「有现货」去应付他。**

    库存表里写「面议」的行，价格是空的。答非所问比说「我帮您问一下」
    更像敷衍，而且他还得再问一遍。这一处是跑 check_catalog 试问时抓到的。
    """
    c = cat.Catalog([cat.Sku("Mate 70 RS", "16+1T", "玄黑", None,
                             {"城关店": 1}, "", NOW.isoformat())])
    out = replier.handle("Mate 70 RS 什么价", catalog=c, now=NOW)
    assert out.escalate is True
    assert "有现货" not in out.reply


def test_a_bare_stock_number_never_leaks_the_placeholder_store_name():
    """库存列只写了个数字时，内部记成「默认」门店。

    **那个占位符绝不能出现在客户眼前**——「默认 有现货」既像故障，
    又没回答「哪家店有」。
    """
    c = cat.Catalog([cat.Sku("Mate 70 Pro", "12+512", "雅川青", 6499,
                             {"默认": 2}, "", NOW.isoformat())])
    out = replier.handle("Mate 70 Pro 12+512 多少钱", catalog=c, now=NOW)
    assert "默认" not in out.reply
    assert "有现货" in out.reply
    assert "6499" in out.reply

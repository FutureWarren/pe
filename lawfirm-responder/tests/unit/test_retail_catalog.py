"""价格与库存：这套系统里唯一允许产生数字的地方。

铁律一句话：**价格、库存、优惠、尾款，一个数字都不许模型生成。**
律所那边的同构规矩是「手机号必须正则提取，绝不经模型」——打不通电话
可以再打一次，**报错一个价是要按报价履约或者赔付的**。

所以这一组把三道机制逐一钉死：查不到不答、过期不答、出口逐个数字核对。
"""

from datetime import datetime, timedelta

import pytest

from responder.retail import catalog as cat
from responder.retail.intents import ALL, Handling
from responder.retail.standin import INSTANT, NEVER_STANDIN

NOW = datetime(2026, 8, 25, 15, 0, 0)


def sku(**kw):
    base = dict(model="Mate 70 Pro", spec="12+512", color="雅川青",
                price=6499, stock={"城关店": 3}, updated_at=NOW.isoformat())
    base.update(kw)
    return cat.Sku(**base)


# ------------------------------------------------------------ ① 查不到不答
def test_a_model_we_do_not_carry_returns_nothing_not_the_closest_match():
    """**绝不返回「最接近的一条」——最接近的那条也是错的价。**"""
    c = cat.Catalog([sku()])
    q = c.lookup("小米 15 多少钱", now=NOW)
    assert q.empty
    assert q.ok is False
    assert q.sku is None


def test_several_specs_of_one_model_ask_back_instead_of_guessing():
    """同一型号多个配置，客户没说要哪个 → 反问，别挑一个报。"""
    c = cat.Catalog([sku(spec="12+256", price=5999), sku(spec="12+512", price=6499)])
    q = c.lookup("Mate 70 Pro 多少钱", now=NOW)
    assert q.ambiguous
    assert q.ok is False


def test_a_model_family_is_ambiguous_even_on_an_exact_name_match():
    """**这条是真机上最容易赔钱的一幕。**

    客户打「Mate 70 多少钱」，库里同时有 Mate 70、Mate 70 Pro、Mate 70 Pro+。
    纯子串匹配会命中「Mate 70」那一条，然后自信地把标准版的价报出去——
    而他八成想问的是 Pro，价差好几千。他拿着聊天记录来店里，门店只有两个
    选择：认这个价（亏钱），或者不认（客诉）。

    所以命中的型号只要是别的型号的前缀，就一律反问。
    """
    c = cat.Catalog([
        sku(model="Mate 70", price=5499),
        sku(model="Mate 70 Pro", price=6499),
        sku(model="Mate 70 Pro+", price=8499),
    ])
    q = c.lookup("Mate 70 多少钱", now=NOW)
    assert q.ambiguous, "型号族没区分开就报价，是这套系统最贵的一种错"
    assert q.ok is False


def test_naming_the_full_model_is_not_dragged_into_ambiguity():
    """客户把话说全了就别再反问——「Mate 70 Pro+」没有下级型号了。"""
    c = cat.Catalog([
        sku(model="Mate 70", price=5499),
        sku(model="Mate 70 Pro+", price=8499),
    ])
    q = c.lookup("Mate 70 Pro+ 多少钱", now=NOW)
    assert q.ok
    assert q.sku.price == 8499


def test_the_customer_naming_a_spec_narrows_it_down():
    c = cat.Catalog([sku(spec="12+256", price=5999), sku(spec="12+512", price=6499)])
    q = c.lookup("Mate 70 Pro 12+512 什么价", now=NOW)
    assert q.ok
    assert q.sku.price == 6499


# ------------------------------------------------------------ ② 过期不报价
def test_a_stale_price_sheet_refuses_to_quote():
    """昨天的价今天报出去，门店要么认（亏钱）要么不认（客诉）。

    两个都比「我帮您问一下店里」贵得多。所以超时一律降级转人工。
    """
    old = (NOW - timedelta(hours=30)).isoformat()
    c = cat.Catalog([sku(updated_at=old)], max_age_hours=24)
    q = c.lookup("Mate 70 Pro 多少钱", now=NOW)
    assert q.matched, "能查到这一行"
    assert q.stale is True, "但太旧了，不许拿去报价"
    assert q.ok is False


def test_a_row_with_no_timestamp_is_treated_as_stale():
    """**没写更新时间 = 无法证明是新的 = 不许报。**

    这是刻意的保守：门店导出时漏了这一列的概率不低，
    而「默认当成新鲜的」意味着一张三个月前的表会被当成今天的价报出去。
    """
    c = cat.Catalog([sku(updated_at="")])
    q = c.lookup("Mate 70 Pro 多少钱", now=NOW)
    assert q.stale is True


def test_the_oldest_row_decides_when_several_match():
    """取最旧而不是最新：宁可保守，少报一次远好过报错一次。"""
    c = cat.Catalog([
        sku(spec="12+256", updated_at=NOW.isoformat()),
        sku(spec="12+512", updated_at=(NOW - timedelta(hours=40)).isoformat()),
    ], max_age_hours=24)
    q = c.lookup("Mate 70 Pro 多少钱", now=NOW)
    assert q.stale is True


# ------------------------------------------------------------ ③ 出口审计
def test_a_price_the_model_invented_is_caught_at_the_exit():
    """**最后一道，也是最重要的一道。**

    前面两道将来任何一次改动都可能出漏子，而这一道贴在出口上：
    凡是回复里出现了没查过的金额，一律拦下。
    """
    c = cat.Catalog([sku()])
    q = c.lookup("Mate 70 Pro 多少钱", now=NOW)
    good = cat.audit("Mate 70 Pro 12+512 雅川青，现价 6499 元", q)
    assert good.passed

    bad = cat.audit("这款现在 5888 元，很划算", q)
    assert bad.passed is False
    assert "5888" in bad.offending


def test_the_audit_lets_model_names_and_specs_through():
    """只拦金额，不拦型号里的 70、配置里的 512、分期的 24 期——

    那些也拦下来的话，AI 一句完整的话都说不出来。
    """
    c = cat.Catalog([sku()])
    q = c.lookup("Mate 70 Pro 多少钱", now=NOW)
    a = cat.audit("Mate 70 Pro 12+512 雅川青，现价 6499 元，可以做 24 期免息", q)
    assert a.passed, f"误伤：{a.offending}"


def test_quoting_with_no_lookup_at_all_is_refused():
    """没查过就报价 = 纯属编造，必须拦。"""
    a = cat.audit("这款 6499 元", None)
    assert a.passed is False


def test_quote_line_never_computes_anything():
    """**不做「每月多少钱」这类计算。**

    分期利息、手续费、贴息各家不同，算出来一旦有偏差，就是我们自己造的错。
    要报月供，让 catalog 里直接给这个数。
    """
    import re
    s = sku(promo="24 期免息")
    line = cat.quote_line(s)
    assert "6499" in line and "城关店" in line
    # 出现在这句话里的每一串数字，都必须能在这条 SKU 的原始字段里找到出处
    source = " ".join([s.model, s.spec, s.color, s.promo, str(s.price),
                       *map(str, s.stock.values())])
    for n in re.findall(r"\d+", line):
        assert n in source, f"凭空多出来的数字 {n}：{line}"


def test_out_of_stock_offers_to_order_rather_than_claiming_availability():
    line = cat.quote_line(sku(stock={"城关店": 0}))
    assert "预订" in line
    assert "现货" not in line


# ------------------------------------------------------------ ④ 结构性守卫
def test_money_intents_are_never_in_the_instant_lane():
    """**防回归。** 「旧机的钱什么时候到账」里含着「什么时候到」，
    真机上会被查物流那条先吃掉，于是一个钱的问题被判成信息类由 AI 代答——
    这是这套系统最不能犯的错，本条守着它不再发生。
    """
    assert NEVER_STANDIN & INSTANT == set(), \
        "同一个意图不能既「绝不代答」又「零等待立刻答」"


def test_every_human_intent_stays_out_of_the_instant_lane():
    human = {i.key for i in ALL if i.handling is Handling.HUMAN}
    assert human & INSTANT == set(), \
        f"这些必须真人的意图被放进了自动答的快车道：{human & INSTANT}"


@pytest.mark.parametrize("text,expect", [
    ("旧机的钱什么时候到账", "tradein_balance"),
    ("抵扣款到账了吗", "tradein_balance"),
    ("我那台什么时候到", "order_status"),
])
def test_money_questions_win_over_information_patterns(text, expect):
    from responder.retail.intents import detect
    assert detect(text, after_sale=True).key == expect

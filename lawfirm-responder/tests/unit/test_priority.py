"""优先级评分：docs/lead-routing.md 的规则表逐条可验，评分必须可解释。"""

from responder.config import Settings
from responder.engine import priority


def _hist(*texts, staff_flags=None):
    staff_flags = staff_flags or [False] * len(texts)
    return [
        {"content": t, "sender_is_staff": s, "created_at": "2026-07-29T10:00:00"}
        for t, s in zip(texts, staff_flags)
    ]


S = Settings(db_path=":memory:")


def keys(factors):
    return {f["key"] for f in factors}


def test_contact_plus_engage_is_p0():
    score, tier, factors = priority.evaluate(
        _hist("我想委托你们处理", "电话17721275495"), settings=S
    )
    assert tier == priority.P0 and score >= 60
    assert {"contact", "engage"} <= keys(factors)


def test_contact_alone_is_p1():
    score, tier, _ = priority.evaluate(_hist("17721275495"), settings=S)
    assert (score, tier) == (40, priority.P1)


def test_fee_alone_is_p2():
    score, tier, _ = priority.evaluate(_hist("请问你们怎么收费"), settings=S)
    assert (score, tier) == (15, priority.P2)


def test_urgent_forces_p0_regardless_of_score():
    """紧急不排队：这是合规护栏的延伸，不允许被排序算法延后。"""
    score, tier, factors = priority.evaluate(_hist("你好"), urgent=True, settings=S)
    assert tier == priority.P0 and score < 60
    assert "urgent" in keys(factors)


def test_amount_wan_detected_but_phone_not_treated_as_amount():
    """「4万多」计金额分；手机号 11 位纯数字绝不能被当成金额。"""
    _, _, with_amount = priority.evaluate(_hist("拖欠了我4万多工资"), settings=S)
    assert "amount" in keys(with_amount)
    _, _, phone_only = priority.evaluate(_hist("13912345678"), settings=S)
    assert "amount" not in keys(phone_only)


def test_big_amount_scores_higher_than_small():
    s_big, _, _ = priority.evaluate(_hist("合同金额150万，怎么收费"), settings=S)
    s_small, _, _ = priority.evaluate(_hist("合同金额3万，怎么收费"), settings=S)
    assert s_big == s_small + 5  # 10 vs 5


def test_deadline_pressure_counts():
    _, _, factors = priority.evaluate(_hist("下周就开庭了还能换律师吗"), settings=S)
    assert "deadline" in keys(factors)


def test_depth_bonus_only_counts_client_messages():
    texts = ["在吗", "我想问劳动仲裁", "被辞退了", "没有赔偿", "拖了三个月", "怎么办"]
    _, _, factors = priority.evaluate(_hist(*texts), settings=S)
    assert "depth" in keys(factors)
    # 同样条数但一半是客服发言 → 不计
    flags = [False, True, False, True, False, True]
    _, _, f2 = priority.evaluate(_hist(*texts, staff_flags=flags), settings=S)
    assert "depth" not in keys(f2)


def test_staff_messages_never_contribute_signals():
    """客服自己说「留个电话吧」不能把客户算成强意愿。"""
    score, tier, _ = priority.evaluate(
        _hist("方便留个电话吗，微信同号也行", staff_flags=[True]), settings=S
    )
    assert (score, tier) == (0, priority.P2)


def test_score_capped_at_100():
    score, _, _ = priority.evaluate(
        _hist("我想委托你们，约时间面谈，怎么收费，电话17721275495微信同号，"
              "涉及150万，下周开庭"),
        urgent=True, settings=S,
    )
    assert score == 100


def test_factors_line_human_readable():
    _, _, factors = priority.evaluate(_hist("17721275495"), settings=S)
    assert priority.factors_line(factors) == "已留电话 +40"

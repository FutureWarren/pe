"""筛查门槛：案情说清楚了才交给人工（律所方 2026-08-13）。

原话是一句反问：「我们不能在客户都没有描述清楚案情的情况下就转接给人工啊，
那人工还是得再问一轮。」——这句话点出的是前一版的真实代价：客户随口一句
「公司拖欠工资」就被转过去，律师打开工作台看到的还是那一句，该问的一件没问。
律所方的观感准确：**有 AI 和没 AI 完全没区别。**

所以这一组守的是四件事：
  1. 一句话不算说清楚，四件事够了三件才算（门槛可配）；
  2. **客户自己要人时门槛一律不生效**——他在伸手，别让他先答题；
  3. 问不动的客户不会被永远扣在 AI 手里（问满上限照转，单上写明缺什么）；
  4. AI 自己的追问不能把格子点亮——不然一轮问话就能凑满，门槛当场归零。
"""

import pytest

from responder.config import Settings
from responder.engine import priority, screening, signals


def cust(*lines: str) -> list[dict]:
    return [{"content": t, "sender_is_staff": 0} for t in lines]


# ---------------------------------------------------------------- ① 四件事
@pytest.mark.parametrize("text,slot", [
    ("拖了三个月了", "when"),
    ("上个月刚辞的我", "when"),
    ("已经申请劳动仲裁了", "when"),          # 阶段也回答「走到哪一步」
    ("对方是一家建筑公司", "who"),
    ("我老板一直躲着", "who"),
    ("前夫不同意离婚", "who"),
    ("劳动合同在我手里", "evidence"),
    ("聊天记录和转账记录都有", "evidence"),
    ("医院的诊断证明有", "evidence"),
    ("我想把工资要回来", "want"),
    ("孩子抚养权想争取", "want"),
])
def test_each_slot_is_recognised(text, slot):
    assert slot in screening.scan(cust(text)).filled, text


def test_a_single_sentence_is_not_a_clear_case():
    """这一句信息量不小，但律师接手后仍然得从头问：对方是谁？有没有材料？
    想要什么结果？——这正是律所方说的「人工还是得再问一轮」。"""
    p = screening.scan(cust("公司拖欠我三个月工资，还把我辞退了"))
    assert not screening.ready(p, min_slots=3)
    assert p.missing, "还缺的要能列出来，AI 下一句才知道问什么"


def test_three_of_four_is_clear_enough():
    """定 3 不定 4：有些案子天然缺一件，凑齐反而变成查户口。"""
    p = screening.scan(cust(
        "公司拖欠我三个月工资",           # when
        "对方是家餐饮公司",               # who
        "劳动合同和工资条我都有",         # evidence
    ))
    assert screening.ready(p, min_slots=3)
    assert p.score == "3/4"


def test_the_missing_pieces_are_named_in_chinese():
    """这几句会原样喂给模型当「还缺」，也会印在交接单上给律师看。"""
    p = screening.scan(cust("已经拖了三个月了"))       # 只答了「什么时候」
    assert "手上有哪些材料" in p.missing_zh
    assert "对方是谁（公司还是个人）" in p.missing_zh
    # 说了「公司」就等于答了「对方是谁」——这一格不该再问第二遍
    assert "who" in screening.scan(cust("公司拖欠我三个月工资")).filled


# ---------------------------------------------------------------- ② 别把自己问的算进去
def test_our_own_questions_do_not_fill_the_slots():
    """**这条写反了门槛就当场归零。** AI 的追问里天然带着所有关键词
    （「有合同吗」「对方是公司还是个人」），拿它去匹配，一轮问话就能把
    四格全点亮——于是每个客户第一句之后立刻「筛查达标」，等于没有门槛。"""
    convo = [
        {"content": "公司拖欠我工资", "sender_is_staff": 0},
        {"content": "这事什么时候开始的？对方是公司还是个人？"
                    "合同、聊天记录、转账记录有吗？您最想要什么结果？",
         "sender_is_staff": 1},
    ]
    p = screening.scan(convo)
    assert not screening.ready(p, min_slots=3), "AI 自己问的不算客户说的"


# ---------------------------------------------------------------- ③ 不把人扣住
def test_a_customer_who_stops_answering_still_gets_through():
    """客户可能话少、可能在开车语音打字。为了凑满一格把一个热客户
    扣在 AI 手里，比少问一件贵得多——问满上限就放行，单上写明缺什么。"""
    p = screening.scan(cust("公司拖欠我三个月工资"), rounds=4, max_rounds=4)
    assert p.exhausted is True
    assert screening.ready(p, min_slots=3)
    assert "还缺" in screening.summary_line(p)


def test_a_case_that_is_actually_clear_is_not_marked_exhausted():
    p = screening.scan(
        cust("上个月公司辞退我", "对方是家餐饮店", "合同我有"),
        rounds=4, max_rounds=4,
    )
    assert p.exhausted is False
    assert screening.summary_line(p).startswith("筛查完成度：3/4")


def test_all_four_reads_cleanly_on_the_card():
    p = screening.scan(cust(
        "上个月公司把我辞了", "对方是家餐饮店", "劳动合同我有", "我想要赔偿",
    ))
    assert screening.summary_line(p) == "筛查完成度：4/4（四件都问到了）"


# ---------------------------------------------------------------- ④ 与转接清单接线
def test_the_checklist_carries_the_screened_signal():
    """`screened` 必须同时在转接清单和热信号里——两边不一致的后果很隐蔽：
    清单认得它，但那条消息被当成「冷消息」走另一条分支，转接晚一轮。"""
    assert "screened" in {k for k, _ in priority.WANTS_HUMAN}
    assert "screened" in signals.HOT_SIGNALS


def test_wanting_a_human_does_not_wait_for_screening():
    """**门槛只管信息型转接。** 客户自己开口要人（想委托、要律师联系他、
    留了电话、问收费…）一律立刻转——让一个已经在伸手的人先答完四道题，
    是本末倒置，也正是应转尽转（2026-08-12）当初要废掉的东西。"""
    for hit in ("engage", "want-contact", "contact", "meeting", "fee", "wechat"):
        assert priority.wants_human([hit], urgent=False), hit


def test_the_threshold_is_configurable():
    """门槛属判断阈值，须律所方确认后再动（见 CLAUDE.md 合规护栏）。"""
    assert Settings().screening_min_slots == 3
    assert Settings().screening_max_rounds == 4
    p = screening.scan(cust("已经拖了三个月了"))   # 只有 when
    assert screening.ready(p, min_slots=1)
    assert not screening.ready(p, min_slots=2)

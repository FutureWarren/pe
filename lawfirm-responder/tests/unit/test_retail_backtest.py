"""零售意图分档回测：**词表是封闭枚举，必然漏，而漏掉的那次永远是静默的。**

这份语料（`tests/data/retail_backtest.jsonl`）是拿真实客户会说的话写的，
每条带着「判错了会怎样」的理由。第一次跑出来的基线是 **57% 认不出**——
而认不出一律回「收到，我叫同事来看一下」，也就是每两句话就有一句是这句套话。

跟 `scripts/backtest_retail.py` 共用同一份语料：脚本给人看细节，这里守住底线。
"""

import json
from pathlib import Path

from responder.retail.intents import Handling, detect

DATA = Path(__file__).resolve().parents[2] / "tests" / "data" / "retail_backtest.jsonl"
CHAT_KEYS = {"chitchat"}


def _tier(text: str) -> str:
    i = detect(text, after_sale=True)
    if i is None:
        return "认不出"
    if i.key in CHAT_KEYS:
        return "chat"
    return i.handling.value


def _rows() -> list[dict]:
    return [json.loads(ln) for ln in DATA.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_the_corpus_is_real_and_annotated():
    """每条都要写清楚判错了会怎样——**没有理由的语料一年后没人敢改。**"""
    rows = _rows()
    assert len(rows) >= 80
    assert all(r["text"] and r["why"] for r in rows)
    assert {r["expect"] for r in rows} <= {"human", "lookup", "auto", "model", "chat"}


def test_nothing_that_needs_a_person_is_answered_by_the_ai():
    """**这一档一条都不许错。** 错的是钱和承诺，不是体验。

    「我的旧手机你们什么时候给钱」曾经落进查物流那一条——一个钱的问题
    被判成信息类，AI 就去代答了。那是这套系统最不能犯的错。
    """
    wrong = [(r["text"], _tier(r["text"]), r["why"])
             for r in _rows() if r["expect"] == "human" and _tier(r["text"]) != "human"]
    assert not wrong, "必须真人的被判成了别的：\n" + "\n".join(
        f"  · {t} → {got}｜{why}" for t, got, why in wrong)


def test_the_overall_accuracy_holds():
    rows = _rows()
    hit = sum(1 for r in rows if _tier(r["text"]) == r["expect"])
    pct = hit * 100 / len(rows)
    misses = [(r["text"], r["expect"], _tier(r["text"])) for r in rows
              if _tier(r["text"]) != r["expect"]]
    assert pct >= 95, (f"分档准确率跌到 {pct:.0f}%（基线 100%）：\n" + "\n".join(
        f"  · {t}：应为 {w}，实为 {g}" for t, w, g in misses))


def test_a_greeting_never_gets_the_handover_line():
    """**对着一句「在吗」回「我叫同事来看一下」是最糟的答法。**

    他还没问任何事，就先被推给了另一个人。而这恰恰是对话的第一句：
    第一句答得像机器人，后面答得再好也没人看了。
    """
    for t in ("在吗", "你好", "老板", "有人吗"):
        assert _tier(t) == "chat", t


def test_an_acknowledgement_ends_the_conversation():
    """「谢谢」之后还继续说话是打扰，而且白吃一条 5 条额度里的份额。"""
    from responder.retail import replier

    for t in ("谢谢", "好的", "嗯", "哦哦好的", "辛苦了"):
        out = replier.handle(t, after_sale=True)
        assert not out.reply and not out.escalate, f"「{t}」不该有任何回复：{out.reply}"


def test_a_customer_asking_for_a_person_gets_one_immediately():
    """他已经开口要人了。再收到一句 AI 的答复，无论多准都是冒犯。"""
    from responder.retail import replier

    for t in ("转人工", "我要找个真人说", "你是机器人吧", "能不能让人给我打个电话"):
        i = detect(t, after_sale=True)
        assert i is not None and i.handling is Handling.HUMAN, t
        out = replier.handle(t, after_sale=True)
        assert out.escalate and "同事" in out.reply, t

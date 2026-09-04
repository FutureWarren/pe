"""销售待办的送达：整条链路上最后一处「系统自认为做完了，那头什么都没发生」。

在这之前，转人工的结果是一条落在运维小记里的记录——**查得到，但没有人会去查**。
那等于没有待办：客户问了一句 AI 答不了的话，后台一切正常，而那边没有任何人知道。
"""

from datetime import datetime, timedelta

import pytest

from responder.retail.notify import TodoNotifier
from responder.retail.pipeline import Inbound, RetailPipeline
from responder.store.db import Store

NOW = datetime(2026, 8, 29, 15, 0, 0)


class FakeBot:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[tuple[str, str]] = []

    def send_robot_text(self, webhook: str, text: str) -> bool:
        self.sent.append((webhook, text))
        return self.ok


class Silent:
    def send_text(self, *_a, **_k) -> bool:
        return True


@pytest.fixture()
def store(tmp_path):
    return Store(str(tmp_path / "t.db"))


def build(store, bot=None, *, webhook="k1", base="https://kf.example.com"):
    n = TodoNotifier(webhook, sender=bot, base_url=base)
    return RetailPipeline(store, mode="live", sender=Silent(), notifier=n), n


def msg(text, n=1, at=NOW, **kw):
    return Inbound("mp", "oCUST", text, f"m{n}", at=at, **kw)


# ------------------------------------------------------------ ① 出口
def test_no_webhook_means_the_todo_has_no_way_out(store):
    """**留空不是「静默关闭」，是一个要被数出来的状态。**

    「推送一直失败」和「根本没配」在客户那头长得一样，在后台也一样安静。
    """
    p, _ = build(store, None, webhook="")
    p.handle(msg("我要退货"), now=NOW)
    assert int((store.counters().get("retail_todo_unrouted") or {}).get("n", 0)) == 1


def test_a_handover_rings_the_sales_group(store):
    bot = FakeBot()
    p, _ = build(store, bot)
    p.handle(msg("我要退货"), now=NOW)
    assert len(bot.sent) == 1
    assert int((store.counters().get("retail_todo_pushed") or {}).get("n", 0)) == 1


def test_the_message_carries_the_customer_s_own_words_and_a_link(store):
    """只推一句「客户要退货」，他还得去别处找这个人是谁、之前说了什么——

    而公众号那头没有客服工作台，他其实**无处可找**。
    """
    bot = FakeBot()
    p, _ = build(store, bot)
    p.handle(msg("我要退货"), now=NOW)
    text = bot.sent[0][1]
    assert "我要退货" in text
    assert "https://kf.example.com/g/mp:oCUST" in text


def test_without_a_public_url_it_says_where_to_look(store):
    """没配公网地址时要说清楚去哪看，别让人对着一条没有下文的提醒发愣。"""
    bot = FakeBot()
    p, _ = build(store, bot, base="")
    p.handle(msg("我要退货"), now=NOW)
    assert "控制台" in bot.sent[0][1]


# ------------------------------------------------------------ ② 只响一次
def test_one_handover_rings_once_even_if_the_customer_keeps_talking(store):
    """**响到第三次他就把这个群折叠了，而那正是最要紧的时候。**

    补充的话照样落库（记录要全），只是不再响铃。
    """
    bot = FakeBot()
    p, _ = build(store, bot)
    p.handle(msg("我要退货", 1, NOW), now=NOW)
    for i, t in enumerate(["买了才三天", "还没拆封", "你们管不管"], start=2):
        at = NOW + timedelta(seconds=i * 10)
        p.handle(msg(t, i, at), now=at)
    assert len(bot.sent) == 1
    todo = store.get_note("retail_todo:mp:oCUST")
    assert len([ln for ln in todo.split("\n") if ln.strip()]) == 4


def test_a_new_handover_later_rings_again(store):
    """静默是几分钟，不是这通对话的余生。"""
    bot = FakeBot()
    p, _ = build(store, bot)
    p.handle(msg("我要退货", 1, NOW), now=NOW)
    later = NOW + timedelta(minutes=10)
    p.handle(msg("我旧机的钱什么时候到账", 2, later), now=later)
    assert len(bot.sent) == 2


# ------------------------------------------------------------ ③ 最贵的那条
def test_a_reply_the_customer_never_got_always_rings(store):
    """**客户一个字没收到**——这一条比任何转人工都急，只有人能救。"""
    bot = FakeBot()
    p, _ = build(store, bot)
    p.handle(msg("保修多久啊", at=NOW - timedelta(hours=49)), now=NOW)
    assert bot.sent and "没能回复" in bot.sent[0][1]


# ------------------------------------------------------------ ④ 不能拖垮主链路
def test_a_failed_push_is_recorded_not_swallowed(store):
    bot = FakeBot(ok=False)
    p, _ = build(store, bot)
    p.handle(msg("我要退货"), now=NOW)
    assert store.get_note("retail_todo_undelivered:mp:oCUST")
    assert int((store.counters().get("retail_todo_push_failed") or {}).get("n", 0)) == 1


def test_an_exploding_bot_never_reaches_the_customer_path(store):
    """这一步是锦上添花——它失败时客户其实已经收到回执了，绝不能反过来搞砸那个。"""
    class Boom:
        def send_robot_text(self, *_a, **_k):
            raise RuntimeError("企微挂了")

    sent: list[str] = []

    class S:
        def send_text(self, _u, t):
            sent.append(t)
            return True

    p = RetailPipeline(
        store, mode="live", sender=S(),
        notifier=TodoNotifier("k1", sender=Boom(), base_url="https://x"),
    )
    out = p.handle(msg("我要退货"), now=NOW)
    assert out.delivered and sent, "推送失败把给客户的回执也带走了"


# ------------------------------------------------------------ ⑤ 文案
def test_the_note_is_short_enough_to_be_read_in_a_group():
    """群里的长消息没人读完。"""
    n = TodoNotifier("k", sender=FakeBot(), base_url="https://kf.example.com")
    text = n.compose(group_id="mp:oA", said="我" * 200, why="客户问的是「退货退款换货」",
                     now=NOW)
    assert len(text) < 220

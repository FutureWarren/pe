"""零售链路的装配点：一条真实消息进来，一条真实回复出去。

在 `retail/pipeline.py` 出现之前，`responder/retail/` 是一个**没有人调用的库**——
每一块都单独测得过，合起来一条客户消息也处理不了。这一组守的正是
「接上线之后才会出现」的那几件事，它们在单元测试里看不见，在真机上很贵：

  · 微信会重推同一条消息（客户一句话被回三遍，还各吃一条额度）
  · 48 小时 / 5 条的额度（算错不是少发一条，是号被标记）
  · 转人工的回执连回三句（既烧额度，读起来也正是「这是个机器人」）
  · 客户发来语音（有内容，我们读不了——沉默和乱猜都是错的）
"""

from datetime import datetime, timedelta

import pytest

from responder.retail.pipeline import RECEIPT_QUIET_SECONDS, Inbound, RetailPipeline
from responder.retail.sources import Sources
from responder.store.db import Store

NOW = datetime(2026, 8, 27, 15, 0, 0)


class FakeSender:
    """记下发出去的每一条。`ok=False` 模拟接口失败。"""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[tuple[str, str]] = []

    def send_text(self, user_key: str, text: str) -> bool:
        self.sent.append((user_key, text))
        return self.ok


@pytest.fixture()
def store(tmp_path):
    return Store(str(tmp_path / "t.db"))


def pipe(store, **kw) -> RetailPipeline:
    kw.setdefault("mode", "live")
    kw.setdefault("sender", FakeSender())
    return RetailPipeline(store, **kw)


def msg(text: str, *, n: int = 1, **kw) -> Inbound:
    return Inbound(channel="mp", user_key="oCUST", text=text,
                   msg_id=kw.pop("msg_id", f"m{n}"), **kw)


def catalog_at(tmp_path, price: int = 6499) -> Sources:
    when = NOW.strftime("%Y-%m-%d %H:%M")
    p = tmp_path / "c.csv"
    p.write_text(
        "型号,配置,颜色,价格,库存,更新时间\n"
        f"Mate 70 Pro,12+256,曜金黑,{price},城关店:3,{when}\n",
        encoding="utf-8",
    )
    return Sources(str(p))


# ------------------------------------------------------------ ① 重推去重
def test_the_same_callback_twice_is_answered_once(store):
    """微信在没收到及时响应时会重推同一条（MsgId 相同）。

    不去重的话客户一句话被回三遍，而那三遍还各吃掉一条 5 条额度里的份额。
    """
    p = pipe(store)
    first = p.handle(msg("保修多久啊"), now=NOW)
    again = p.handle(msg("保修多久啊"), now=NOW)
    assert first.delivered
    assert not again.reply and "重复" in again.reason
    assert len(p.sender.sent) == 1


# ------------------------------------------------------------ ② 真人在场
def test_a_human_speaking_puts_the_ai_to_sleep(store):
    """**两个人抢答比慢一点糟得多。** 与律所侧 gate:handed-off 同源。"""
    p = pipe(store)
    p.handle(msg("我在的，您说", n=1, is_staff=True), now=NOW)
    out = p.handle(msg("保修多久啊", n=2), now=NOW + timedelta(seconds=30))
    assert not out.reply
    assert p.sender.sent == []


def test_a_human_in_the_room_needs_no_todo_line(store):
    """真人正看着屏幕，再往待办上记一行「客户又说了什么」是多余的。

    这一条看着琐碎，但待办的价值全在**每一行都值得看**——
    掺进十条他早就看见的话，他就不再看它了。
    """
    p = pipe(store)
    p.handle(msg("我要退货", n=1, at=NOW), now=NOW)
    at2 = NOW + timedelta(seconds=20)
    p.handle(msg("我来处理", n=2, at=at2, is_staff=True), now=at2)
    at3 = NOW + timedelta(seconds=40)
    p.handle(msg("那太好了，谢谢", n=3, at=at3), now=at3)
    todo = [ln for ln in store.get_note("retail_todo:mp:oCUST").split("\n") if ln.strip()]
    assert len(todo) == 1 and "退货" in todo[0]


def test_the_ai_comes_back_after_the_takeover_window(store):
    p = pipe(store, takeover_seconds=600)
    p.handle(msg("我在的", n=1, is_staff=True), now=NOW)
    out = p.handle(msg("保修多久啊", n=2), now=NOW + timedelta(seconds=900))
    assert out.delivered


# ------------------------------------------------------------ ③ 关注事件
def test_a_follow_event_is_not_answered_by_us(store):
    """公众号后台自己配了关注回复（酷机时代那个号还挂在云盛 ERP 上）。

    我们再发一条，客户连着看到两句招呼——那正是「一看就知道是机器人」
    最典型的样子。**一个窗口只该有一个人在说话。**
    """
    p = pipe(store)
    out = p.handle(msg("", from_event=True), now=NOW)
    assert not out.reply
    assert p.sender.sent == []


# ------------------------------------------------------------ ④ 读不了的消息
def test_a_voice_message_is_never_met_with_silence(store):
    """客户发语音是**有内容**的，只是我们读不了。

    沉默正是这套系统存在的理由要消灭的东西——他不会想「机器人读不了语音」，
    他只会觉得没人管。乱猜更糟：对着一条没听过的语音答话，
    是这类系统出丑最快的方式。
    """
    p = pipe(store)
    out = p.handle(msg("", media="voice"), now=NOW)
    assert out.escalated
    assert "语音" in out.reply and "同事" in out.reply
    assert store.get_note("retail_todo:mp:oCUST")


def test_four_photos_in_a_row_get_one_reply_not_four(store):
    """连发四张照片只该收到一句。四句一模一样的话会吃掉 5 条额度里的 4 条。"""
    p = pipe(store)
    for i in range(4):
        p.handle(msg("", n=i, media="image"), now=NOW)
    assert len(p.sender.sent) == 1


# ------------------------------------------------------------ ⑤ 回执去重
def test_one_handover_gets_one_receipt(store):
    """客户被转人工后接着说三句，旧写法回三句「我叫同事来看一下」。"""
    p = pipe(store)
    for i, t in enumerate(["我要退货", "买了才三天", "在吗"]):
        p.handle(msg(t, n=i), now=NOW + timedelta(seconds=i))
    assert len(p.sender.sent) == 1
    assert int((store.counters().get("retail_deferred_to_human") or {}).get("n", 0)) >= 1


def test_the_staff_still_get_told_every_time(store):
    """压住的只是**给客户的回执**，销售那边一次都不能漏。"""
    p = pipe(store)
    p.handle(msg("我要退货", n=1), now=NOW)
    p.handle(msg("而且屏幕还有划痕", n=2), now=NOW + timedelta(seconds=5))
    assert "划痕" in store.get_note("retail_todo:mp:oCUST") or \
           int((store.counters().get("retail_escalated") or {}).get("n", 0)) >= 2


def test_a_normal_answer_does_not_muzzle_the_next_receipt(store):
    """**这是去重最容易写错的地方。**

    按「最近发过任何一条」去重的话，AI 两分钟前正常答过一个保修问题，
    就会压住现在这句回执——于是一个要退货的客户彻底收不到回音。
    去重只看回执那一档。
    """
    p = pipe(store)
    p.handle(msg("保修多久啊", n=1), now=NOW)
    out = p.handle(msg("我要退货", n=2), now=NOW + timedelta(seconds=60))
    assert out.reply, "正常回答把后面的转人工回执压掉了"
    assert len(p.sender.sent) == 2


def test_after_a_handover_the_ai_stops_talking_for_a_few_minutes(store):
    """**刚说完「我叫同事来看一下」，AI 就不该再自己接话。**

    演示时抓到的：客户说「我要退货」（转人工），十五秒后补一句
    「买了才三天，还没拆封」——那是同一件事的下半句。可意图识别是无状态的，
    它按字面把这句判成了另一类，于是 AI 答了一段跟退货毫无关系的话。
    再多的词表也修不掉这个：客户的补充说的是什么，本来就要看上一句才知道。
    """
    p = pipe(store)
    p.handle(msg("我要退货", n=1, at=NOW), now=NOW)
    later = NOW + timedelta(seconds=15)
    out = p.handle(msg("买了才三天，还没拆封", n=2, at=later), now=later)
    assert not out.reply
    assert len(p.sender.sent) == 1
    assert "让给真人" in out.reason
    # 给销售的那一行**只搬原话，不下判断**：这时意图标签是不可信的，
    # 印一个错的上去比不印更容易把人带偏。
    assert "正品" not in store.get_note("retail_todo:mp:oCUST")


def test_the_supplement_still_reaches_the_sales_desk(store):
    """让给真人**不等于把这句话丢掉**——他补充的正是最该被人看到的内容。"""
    p = pipe(store)
    p.handle(msg("我要退货", n=1, at=NOW), now=NOW)
    later = NOW + timedelta(seconds=15)
    p.handle(msg("买了才三天，还没拆封", n=2, at=later), now=later)
    assert "还没拆封" in store.get_note("retail_todo:mp:oCUST")


def test_the_ai_speaks_again_once_the_window_passes(store):
    """静默是几分钟，不是这通对话的余生。"""
    p = pipe(store)
    p.handle(msg("我要退货", n=1, at=NOW), now=NOW)
    later = NOW + timedelta(seconds=RECEIPT_QUIET_SECONDS + 10)
    out = p.handle(msg("保修多久啊", n=2, at=later), now=later)
    assert out.delivered


def test_the_quiet_window_is_three_minutes(store):
    """要压住的是「连发三四句补充」那个爆发，它就发生在一两分钟里。

    开到十分钟的那一版，演示时把客户五分钟后问的另一件事也一起吞了。
    """
    assert RECEIPT_QUIET_SECONDS == 180


def test_a_different_question_later_still_gets_an_answer(store):
    """静默窗口过去之后，下一次转人工照样要回执——**不能让他觉得没人管。**"""
    p = pipe(store)
    p.handle(msg("我要退货", n=1, at=NOW), now=NOW)
    later = NOW + timedelta(seconds=RECEIPT_QUIET_SECONDS + 1)
    out = p.handle(msg("我旧机的钱什么时候到账", n=2, at=later), now=later)
    assert out.reply and len(p.sender.sent) == 2


def test_every_handover_leaves_its_own_line_for_the_sales(store):
    """**追加，不是覆盖。**

    覆盖写过一版，演示时当场露馅：客户连问了四件事，计数器记着 4 次转人工，
    而销售那边只剩最后一件。待办看起来是有的，没人会想到去数它够不够。
    """
    p = pipe(store)
    for i, t in enumerate(["我要退货", "我旧机的钱什么时候到账"]):
        at = NOW + timedelta(seconds=i * 10)
        p.handle(msg(t, n=i, at=at), now=at)
    todo = store.get_note("retail_todo:mp:oCUST")
    assert len(todo.strip().split("\n")) == 2
    assert "退货" in todo and "旧机" in todo


# ------------------------------------------------------------ ⑥ 额度
def test_a_reply_that_comes_more_than_48_hours_late_is_not_sent(store):
    """**窗口要拿真实的此刻去比，不是拿消息自己的时间。**

    队列积压、进程重启、微信补推——消息晚很久才轮到处理是常态。
    早先把「消息时间」和「处理时刻」混成一个，`now - msg.at` 恒等于零，
    这道闸就永远不会响，直到接口开始报 45015 才发现。
    """
    p = pipe(store)
    out = p.handle(msg("保修多久啊", at=NOW - timedelta(hours=49)), now=NOW)
    assert out.mode == "blocked"
    assert p.sender.sent == []


def test_the_quota_is_shared_with_whatever_else_uses_this_account(store):
    """5 条额度是**这个公众号的**，不是我们的。

    酷机时代那个号同时挂在云盛 ERP 上（模板消息在那边发）。
    别人先发满了，我们就一条也发不出去——这不是假设，是这个号的现状。
    """
    p = pipe(store)
    for i in range(5):
        store.save_reply(msg_id=f"other{i}", group_id="mp:oCUST", text="别处发的",
                         mode="live", passed=True, created_at=NOW)
    out = p.handle(msg("保修多久啊", at=NOW), now=NOW)
    assert out.mode == "blocked"


def test_running_out_of_budget_is_never_silent(store):
    """**这是最贵的一种失败**：库里一切正常，客户一个字没收到。

    所以它既要落回复行（mode='blocked'，控制台看得见），也要变成销售那边的
    一条待办——这时候只有人能救这一句。
    """
    p = pipe(store)
    p.handle(msg("保修多久啊", at=NOW - timedelta(hours=49)), now=NOW)
    assert store.get_note("retail_blocked:mp:oCUST")
    assert "没能回复" in store.get_note("retail_todo:mp:oCUST")
    modes = {r["mode"] for r in store.list_replies(group_id="mp:oCUST")}
    assert "blocked" in modes


def test_answering_each_question_once_never_trips_the_cap(store):
    """客户连问五句、我们答五句是**正常服务**，不该被自己的记账挡住。

    额度按「客户最后一次开口」起算：他每说一句，窗口和条数一起重置。
    按「第一条消息」起算的写法会在第六句上突然哑掉，而那时对话正热。
    """
    p = pipe(store)
    for i in range(6):
        p.handle(msg("保修多久啊", n=i, at=NOW + timedelta(minutes=i)),
                 now=NOW + timedelta(minutes=i))
    assert len(p.sender.sent) == 6


# ------------------------------------------------------------ ⑦ 模式
def test_shadow_mode_runs_everything_and_sends_nothing(store):
    """上线第一周要的就是这个：**先看它会说什么，再决定让不让它说。**"""
    p = pipe(store, mode="shadow")
    out = p.handle(msg("保修多久啊"), now=NOW)
    assert out.mode == "shadow" and out.reply
    assert p.sender.sent == []
    assert store.list_replies(group_id="mp:oCUST")


def test_a_failed_send_is_recorded_as_failed_not_as_sent(store):
    """接口失败一定是客户没收到。库里标着「已发送」而对面是空的，
    比失败本身贵得多——所有后续判断都会以为他已经被安抚过了。"""
    p = pipe(store, sender=FakeSender(ok=False))
    out = p.handle(msg("保修多久啊"), now=NOW)
    assert out.mode == "failed" and not out.delivered
    assert store.get_note("retail_send_failed:mp:oCUST")


def test_an_exploding_sender_does_not_take_the_pipeline_down(store):
    class Boom:
        def send_text(self, *_a, **_k):
            raise RuntimeError("网络断了")

    out = pipe(store, sender=Boom()).handle(msg("保修多久啊"), now=NOW)
    assert out.mode == "failed"


# ------------------------------------------------------------ ⑧ 铁律：数据
def test_without_a_catalog_a_price_question_goes_to_a_human(store):
    """**查不到就是查不到。** 绝不返回「最接近的那一条」——它也是错的价。"""
    out = pipe(store).handle(msg("Mate 70 Pro 多少钱"), now=NOW)
    assert out.escalated and not any(c.isdigit() for c in out.reply)


def test_with_a_fresh_catalog_the_price_is_quoted(store, tmp_path):
    out = pipe(store, sources=catalog_at(tmp_path)).handle(
        msg("Mate 70 Pro 多少钱"), now=NOW)
    assert "6499" in out.reply and not out.escalated


def test_order_questions_go_to_a_human_while_the_erp_is_not_connected(store, tmp_path):
    """订单在云盛 ERP 里，第一期我们没有那份数据。**没有数据就不要装作有。**"""
    out = pipe(store, sources=catalog_at(tmp_path)).handle(
        msg("我那台什么时候能到啊"), now=NOW)
    assert out.escalated


# ------------------------------------------------------------ ⑨ 会话隔离
def test_two_customers_do_not_share_a_budget_or_a_receipt(store):
    p = pipe(store)
    p.handle(Inbound("mp", "oA", "我要退货", "a1"), now=NOW)
    p.handle(Inbound("mp", "oB", "我要退货", "b1"), now=NOW)
    assert len(p.sender.sent) == 2
    assert {u for u, _ in p.sender.sent} == {"oA", "oB"}

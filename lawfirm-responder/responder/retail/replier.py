"""把意图、数据、补位判断接成一条链路：进来一句话，出去一条回复（或一次叫人）。

这一层是零售包的装配点。刻意做得薄——真正的判断都在
`intents` / `catalog` / `orders` / `standin` 里，各自可以单独测。

## 一条回复要过的五关

    1. 补位判断（standin）   —— 现在轮不轮得到 AI 说话
    2. 意图分档（intents）   —— 这句话属于哪一类、能不能自动
    3. 取数（catalog/orders）—— 需要数字的，只从数据源取
    4. 组织语言              —— 模板兜底；接了模型时由模型润色
    5. **出口审计（audit）** —— 金额逐个核对，凡不在白名单一律拦下

第 5 关是整条链路的底线。前四关将来任何一次改动都可能出漏子，
而它贴在出口上：只要回复里出现了没查过的金额，就退回转人工。
宁可少答一句，不可错报一个价。
"""

from dataclasses import dataclass
from datetime import datetime

from responder.retail import catalog as cat
from responder.retail import orders as odr
from responder.retail import standin
from responder.retail.intents import Handling
from responder.retail.phrases import DEFAULTS, Phrases


@dataclass
class Outcome:
    """一次处理的结果。"""

    reply: str = ""            # 要发给客户的话（空 = 不发）
    escalate: bool = False     # 要不要叫真人
    staff_note: str = ""       # 给销售看的提示（AI 代回了什么 / 为什么叫他）
    reason: str = ""           # 判断日志
    intent: str = ""
    audit_failed: bool = False # 被出口审计拦下过（控制台要能看到）


def handle(
    text: str,
    *,
    customer_key: str = "",
    catalog: cat.Catalog | None = None,
    book: odr.OrderBook | None = None,
    after_sale: bool = False,
    staff_replied_at: datetime | None = None,
    now: datetime | None = None,
    takeover_seconds: int = 1800,
    store_hint: str = "",
    phrases: Phrases | None = None,
) -> Outcome:
    """处理一条客户消息。"""
    now = now or datetime.now()
    say = phrases.get if phrases is not None else _auto_body

    d = standin.decide(
        text, after_sale=after_sale, staff_replied_at=staff_replied_at,
        now=now, takeover_seconds=takeover_seconds,
    )

    # 真人在场 → 彻底闭嘴，连回执都不发（发了就是插话）
    if not d.speak and not d.escalate:
        return Outcome(reason=d.reason, intent=d.kind)

    # 不代答但要叫人 → 发一句回执，绝不含数字与承诺
    if not d.speak:
        return Outcome(
            reply=standin.receipt_line(d), escalate=True,
            staff_note=_why_staff(d), reason=d.reason, intent=d.kind,
        )

    intent = d.intent
    assert intent is not None  # speak=True 时 standin 一定给了 intent

    # ---- 取数与成文 -------------------------------------------------------
    quote: cat.Quote | None = None
    allowed: set[str] = set()

    if intent.handling is Handling.LOOKUP:
        body, quote, allowed, failed = _lookup_body(
            intent.key, text, customer_key, catalog, book, now, store_hint, say,
        )
        if failed:
            # 查不到 → 不猜，转人工。这是铁律的第一道。
            return Outcome(
                reply=standin.receipt_line(
                    standin.Decision(False, escalate=True, intent=intent)
                ),
                escalate=True, intent=intent.key,
                staff_note=f"客户问「{intent.zh}」，系统里没查到对应数据，需要你确认。",
                reason=f"{d.reason} → 但取数失败，转人工",
            )
    else:
        body = say(intent.key)

    if not body:
        return Outcome(
            escalate=True, intent=intent.key, reason=f"{d.reason} → 没有可用话术，转人工",
            staff_note=f"客户问「{intent.zh}」，暂无对应话术。",
        )

    body = odr.mask_phone(body)

    # ---- 出口审计：最后一道 ------------------------------------------------
    a = cat.audit(body, quote, extra_allowed=allowed)
    if not a.passed:
        return Outcome(
            reply="", escalate=True, intent=intent.key, audit_failed=True,
            staff_note=f"⚠️ AI 拟的回复里出现了未经查询的金额（{'、'.join(a.offending)}），"
                       f"已拦下没发出去，请你手动回复。",
            reason=f"{d.reason} → 出口审计拦下：{a.reason}",
        )

    return Outcome(
        reply=body, escalate=False, intent=intent.key,
        staff_note=standin.notice_for_staff(d, body), reason=d.reason,
    )


def _why_staff(d: standin.Decision) -> str:
    zh = d.intent.zh if d.intent else "未识别的问题"
    return f"客户问的是「{zh}」，这类不让 AI 代答，需要你来回。"


def _lookup_body(
    key: str, text: str, customer_key: str,
    catalog: cat.Catalog | None, book: odr.OrderBook | None,
    now: datetime, store_hint: str, say=None,
) -> tuple[str, cat.Quote | None, set[str], bool]:
    """需要查数据才能答的几类。返回 (正文, quote, 额外白名单, 是否失败)。"""

    if key in ("price", "stock", "accessory"):
        if catalog is None:
            return "", None, set(), True
        q = catalog.lookup(text, now=now)
        if q.empty:
            return "", q, set(), True
        if q.ambiguous:
            # 命中多条 → 反问，别猜。这一句不含任何金额，安全。
            names = "、".join(s.title for s in q.matched[:4])
            return (f"您说的是哪一款？我这边看到有 {names}，"
                    f"您说一下配置和颜色，我给您准确的价。"), q, set(), False
        if q.stale:
            # 数据过期 → 不许报价。昨天的价今天报出去，
            # 门店要么认（亏钱）要么不认（客诉），两个都比多问一句贵。
            return "", q, set(), True
        # **客户问价，而这一行没有价（表里写的是「面议」之类）→ 转人工。**
        # 不能拿「有现货」去应付一个问价的人：他问的是多少钱，
        # 答非所问比说「我帮您问一下」更像敷衍，而且他还得再问一遍。
        if key == "price" and q.sku.price is None:
            return "", q, set(), True
        return cat.quote_line(q.sku), q, set(), False

    if key in ("order_status", "pickup", "invoice"):
        if book is None or not customer_key:
            return "", None, set(), True
        o = book.lookup(customer_key, text)
        if o is None:
            return "", None, set(), True
        if key == "invoice":
            state = o.invoice or "还没开"
            body = f"您这单的发票{state}。需要改抬头或者补开的话我叫同事处理。"
            return body, None, o.allowed_numbers(), False
        return o.human_status() + "。", None, o.allowed_numbers(), False

    if key == "repair_status":
        if book is None or not customer_key:
            return "", None, set(), True
        t = book.ticket_for(customer_key, text)
        if t is None:
            return "", None, set(), True
        return t.human_status() + "。", None, {t.ticket_no}, False

    if key == "book_visit":
        where = store_hint or "离您最近的门店"
        return (f"好的，我记下了。我这就跟{where}的同事说一声，"
                f"让他给您留着，您到店报手机号就行。"), None, set(), False

    if key == "store_info":
        say = say or _auto_body
        return say("store_info"), None, set(), not say("store_info")

    return "", None, set(), True


# 出厂默认话术的唯一真相来源是 `phrases.DEFAULTS`。
# 这里只留一个取值函数，供没有传 `phrases` 的调用方（测试、老代码）兜底。
# **不要在这个文件里再放一份话术**：两份话术早晚会说不一样的话，
# 而客户只会看到其中一份，没有人知道另一份也在。
_AUTO = DEFAULTS


def _auto_body(key: str) -> str:
    return DEFAULTS.get(key, "")

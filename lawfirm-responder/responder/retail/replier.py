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
) -> Outcome:
    """处理一条客户消息。"""
    now = now or datetime.now()

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
            intent.key, text, customer_key, catalog, book, now, store_hint,
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
        body = _auto_body(intent.key)

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
    now: datetime, store_hint: str,
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
        return _auto_body("store_info"), None, set(), bool(not _auto_body("store_info"))

    return "", None, set(), True


# 固定话术：写一次长期有效的那一类。
# 真实部署时这些应当放进知识库由门店自己维护（改一条立刻生效，不用找技术），
# 这里的默认值只是让链路在零配置下也能跑起来、也能被测试。
_AUTO: dict[str, str] = {
    "warranty": "主机是一年保修，电池和充电器半年，屏幕、进水、摔碰这些属于人为，"
                "不在保修范围里，但可以走付费维修。您那台具体算不算，"
                "得工程师上手看一眼才能定，我不敢替他下结论。",
    "activate": "开机之后按提示选语言、插卡、连 WiFi，然后登录或新建华为账号就行。"
                "实名是运营商那边做的，营业厅或者官方 App 都能办。"
                "要是卡在哪一步，您截个图发我。",
    "data_migration": "用「手机克隆」最省事：新旧机都装这个 App，"
                      "旧机选「发送」、新机选「接收」，扫个码就开始传，"
                      "通讯录、照片、微信记录都能带过去。半小时左右。"
                      "您要是不方便弄，拿到店里我们帮您导，不收费。",
    "installment": "我们支持花呗、信用卡分期和银行分期，常见的是 12 期和 24 期，"
                   "活动期内有免息名额。具体您这台能做几期免息、每期多少，"
                   "得按下单时的活动算，我叫同事给您报准数。",
    "promo": "当期活动我这边随时在更新，您说一下想要哪款，我把对应的优惠给您列清楚。",
    "compare": "这两款主要差在影像和屏幕上，日常用差别不大，"
               "拍照多、经常拍夜景的建议上高配那款。您平时主要用来做什么？"
               "我按您的用法给您说得具体点。",
    "store_info": "",  # 由 Settings.stores 注入，见 docs/retail-kuji.md
}


def _auto_body(key: str) -> str:
    return _AUTO.get(key, "")

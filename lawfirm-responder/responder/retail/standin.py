"""AI 补位：客服来不及回，AI 顶上——还是老实等人。

这是酷机时代这套方案里**最值钱的一个判断**，也是最容易做坏的一个。

## 场景

客户成交之后在企业微信里发来一句「我那台什么时候能到」。销售正在店里
接待别的客户、或者干脆已经下班了。这条消息就躺在那儿。

律所那套系统里已经有一半答案了：`handoff:accompanying`——转给真人之后，
只要真人还没开口，AI 就接着陪、接着答；真人一开口，AI 立刻闭嘴。
**同一个机制，零售直接复用。** 这里要补的是零售独有的另一半：

> **不是所有消息都该由 AI 顶上。**

## 三条线，按「说错的代价」划

| 代价 | 例子 | 怎么办 |
|---|---|---|
| 说错也就再查一次 | 查物流、问保修、问营业时间、教激活 | **零等待**，AI 立刻答 |
| 说错要赔钱 / 算承诺 | 退款、尾款、抵扣、能不能换 | **绝不代答**，AI 只回执，等真人 |
| 说错会点着火 | 投诉、骂人、要说法 | **立刻叫人**，AI 只安抚一句 |

为什么信息类要「零等待」而不是「等三分钟看销售回不回」：
销售回这类问题的动作也是去查一下再告诉客户，AI 查得更快、还不会忘。
让客户干等三分钟，换来的是同一个答案——那三分钟纯是白等。

而涉及钱的哪怕等一小时也要等真人。**AI 说一个数字，在客户眼里
就是门店的承诺**，事后说「那是机器人说的」只会让事情更糟。

## 为什么要给「已答过什么」留痕

零售比律所多一个坑：AI 补位答完之后，销售过一会儿打开企微，
**看不到 AI 说过什么**，就会把同样的话再说一遍。客户收到两遍一样的回复，
观感是「这家店乱糟糟的」。所以 `Standin.notice_for_staff()` 生成一句
给销售看的提示，附在会话里——他一眼知道 AI 已经替他说到哪儿了。
"""

from dataclasses import dataclass
from datetime import datetime

from responder.retail.intents import Handling, Intent, detect


@dataclass(frozen=True)
class Decision:
    """补不补位，以及为什么。"""

    speak: bool                 # AI 现在要不要开口
    wait_seconds: int = 0       # 开口前先等多久（给真人让路）
    escalate: bool = False      # 要不要立刻叫真人
    reason: str = ""            # 写进判断日志，控制台可查
    intent: Intent | None = None
    urgent: bool = False        # 要响铃，不能混在普通待办里排队

    @property
    def kind(self) -> str:
        return self.intent.key if self.intent else "unknown"


# 涉及钱与承诺的意图：AI 一律不代答，只回执 + 叫人。
# 这张表**宁可长不可短**——多放一类进来最多是多转一次人工，
# 漏放一类出去就是一次赔付或客诉。
NEVER_STANDIN = {
    "arriving",          # 人已经在路上了——这一条要响铃，见下
    "refund_return",     # 退货退款
    "tradein_balance",   # 旧机尾款、抵扣款
    "tradein_quote",     # 旧机估价（要验机）
    "buy_now",           # 谈价、下单
    "complaint",         # 投诉
    "delivery",          # 异地邮寄：付款与运损责任要人定
    "secondhand",        # 二手/官换/样机：一机一况
    "carrier_plan",      # 运营商合约：条款与违约金
}

# 说错也只是再查一次的信息类：零等待，AI 立刻答。
INSTANT = {
    "order_status", "pickup", "invoice", "warranty",
    "activate", "data_migration", "store_info", "promo",
    "installment", "compare", "accessory", "repair_status",
    "authenticity", "payment", "new_launch",
}


def decide(
    text: str,
    *,
    after_sale: bool = False,
    staff_replied_at: datetime | None = None,
    now: datetime | None = None,
    takeover_seconds: int = 1800,
    grace_seconds: int = 0,
) -> Decision:
    """这条消息，AI 该不该替客服回。

    `staff_replied_at`：真人最近一次在这个会话里说话的时间。
      **真人正在场就一律让位**——这一条优先于下面所有判断，
      因为两个人抢答比慢一点糟得多。判据与律所侧同一个开关
      （`takeover_seconds`），行为保持一致，运维只需要理解一套规则。

    `grace_seconds`：AI 开口前给真人留的缓冲。信息类是 0（等也是白等），
      需要斟酌的类别可以配得大一些，让销售有机会先接。
    """
    now = now or datetime.now()

    # ① 真人在场 → 闭嘴。与 engine/decision.py 的 gate:handed-off 同源。
    if staff_replied_at is not None:
        idle = (now - staff_replied_at).total_seconds()
        if idle < takeover_seconds:
            return Decision(False, reason=f"standin:真人 {int(idle)} 秒前刚说过话，让位")

    intent = detect(text, after_sale=after_sale)

    # ② 认不出来 → 不答。零售里「不认识就别答」是硬规矩：
    #    让模型自由发挥，它会自信地编出一个价格、一个库存、一个到货时间。
    if intent is None:
        return Decision(
            False, escalate=True,
            reason="standin:这句话没认出属于哪一类，交给真人",
        )

    # ③ 涉及钱与承诺 → 只回执，不代答
    if intent.key in NEVER_STANDIN or intent.handling is Handling.HUMAN:
        why = ("——人已经在路上，立刻响铃" if intent.urgent
               else "涉及金额或承诺，只叫人不代答")
        return Decision(
            False, escalate=True, intent=intent, urgent=intent.urgent,
            reason=f"standin:{intent.zh} {why}",
        )

    # ④ 信息类 → 零等待，立刻答
    if intent.key in INSTANT:
        return Decision(
            True, wait_seconds=grace_seconds, intent=intent,
            reason=f"standin:{intent.zh} 属信息类，AI 直接答（真人未在场）",
        )

    # ⑤ 剩下的按意图自己的档位走，且给真人留缓冲
    if intent.can_auto:
        return Decision(
            True, wait_seconds=max(grace_seconds, 30), intent=intent,
            reason=f"standin:{intent.zh} 可自动答，先等 30 秒给真人机会",
        )
    return Decision(
        False, escalate=True, intent=intent,
        reason=f"standin:{intent.zh} 不在自动范围内",
    )


def notice_for_staff(decision: Decision, reply_text: str) -> str:
    """给销售看的一句话：AI 刚替你说了什么。

    没有这一句，销售打开企微看不到 AI 说过什么，就会把同样的话再说一遍——
    客户收到两遍一样的回复，观感是「这家店乱糟糟的」。
    这是零售比律所多出来的一个坑：律所是律师接手后自己往下聊，
    零售是销售随时插进来，重复的概率高得多。
    """
    if not decision.speak:
        return ""
    head = f"🤖 AI 已代回（{decision.intent.zh if decision.intent else '常规'}）"
    body = reply_text.strip().replace("\n", " ")
    if len(body) > 60:
        body = body[:60] + "…"
    return f"{head}：{body}\n你接着回就行，AI 会自动让开。"


def receipt_line(decision: Decision) -> str:
    """不代答时，给客户的那句回执。

    **不代答不等于不出声。** 客户发了消息一个字没收到，和收到一句
    「我马上叫同事」，是完全不同的体验——尤其他已经付过钱了。
    这句话刻意不含任何数字、不含任何承诺，只说「有人在」。
    """
    if decision.speak:
        return ""
    key = decision.intent.key if decision.intent else ""
    if key == "complaint":
        return "您先别急，我把情况记下来了，这就叫店长过来跟您说。"
    if key in ("refund_return", "tradein_balance", "tradein_quote"):
        return "这个涉及具体金额，我叫负责的同事来跟您说，免得我说岔了。"
    if key == "arriving":
        # 人在路上，这句话必须让他知道「到了有人接」——他最怕的就是白跑一趟
        return "好嘞，我这就通知店里的同事，您到了直接说找我们就行，有人接您。"
    if key == "buy_now":
        return "好的，我这就叫同事过来跟您对接。"
    return "收到，我叫同事来看一下，稍等一会儿。"

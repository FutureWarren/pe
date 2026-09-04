"""订单与售后工单：售后代答的数据来源。

和 `catalog` 一样的规矩：**查不到就转人工，绝不编。**
差别在于订单是「一个人一条」的隐私数据，所以多两道约束：

1. **必须先认人。** 订单只能按「这个客户自己的」查——按订单号查也要校验
   这个订单是不是他的。否则客户 A 报个单号就能看到客户 B 的收货信息，
   那是数据事故，不是功能。
2. **手机号要脱敏。** 回复里不回显完整手机号和详细地址——客户自己知道，
   写出来只会在截图流传时变成泄露。

## 为什么状态要说成「到哪了」而不是「运输中」

客户问「我那台到哪了」，他要的是**什么时候能拿到**，不是一个状态码。
「运输中」这三个字等于没说，他还得再问一句。「昨晚到兰州了，今天派送，
一般下午能到」——这才叫答完了。所以 `Order.human_status()` 刻意把
状态 + 最新节点 + 预计时间揉成一句人话。

一次答完和分三次答，在客户那里是两种店。
"""

import re
from dataclasses import dataclass
from enum import Enum


class OrderState(str, Enum):
    PAID = "paid"            # 已付款，等发货/等备货
    PREPARING = "preparing"  # 备货中
    SHIPPED = "shipped"      # 已发货，在路上
    ARRIVED = "arrived"      # 到店可自提 / 已派送到本地
    DONE = "done"            # 已签收 / 已提货
    REFUNDING = "refunding"  # 退款中
    CANCELLED = "cancelled"


class TicketState(str, Enum):
    RECEIVED = "received"    # 已收机
    DIAGNOSING = "diagnosing"  # 检测中
    REPAIRING = "repairing"  # 维修中
    WAIT_PART = "wait_part"  # 等配件
    DONE = "done"            # 修好可取
    RETURNED = "returned"    # 已返还客户


@dataclass(frozen=True)
class Order:
    order_no: str
    customer_key: str          # 与会话绑定的客户标识，用来校验归属
    state: OrderState
    items: str = ""            # 「Mate 70 Pro 12+512 雅川青 ×1」
    store: str = ""            # 自提门店
    carrier: str = ""          # 顺丰
    tracking_no: str = ""
    last_node: str = ""        # 「已到达兰州中转站」
    eta: str = ""              # 「预计今天下午」
    placed_at: str = ""
    invoice: str = ""          # 发票状态：已开 / 未开 / 已寄出

    def human_status(self) -> str:
        """把状态说成人话——一次答完，不让客户再问第二句。"""
        head = {
            OrderState.PAID: "款已经收到了，正在给您备货",
            OrderState.PREPARING: "正在备货",
            OrderState.SHIPPED: "已经发出来了",
            OrderState.ARRIVED: "已经到了",
            OrderState.DONE: "显示已经签收了",
            OrderState.REFUNDING: "退款正在处理中",
            OrderState.CANCELLED: "这单已经取消了",
        }[self.state]
        bits = [head]
        if self.state is OrderState.SHIPPED:
            if self.carrier:
                bits.append(f"走的{self.carrier}")
            if self.last_node:
                bits.append(self.last_node)
        if self.state is OrderState.ARRIVED and self.store:
            bits.append(f"在{self.store}，带上身份证就能取")
        if self.eta and self.state in (OrderState.SHIPPED, OrderState.PREPARING):
            bits.append(self.eta)
        return "，".join(bits)

    def allowed_numbers(self) -> set[str]:
        """这条订单授权可以出现在回复里的数字（快递单号等）。"""
        out = {self.order_no, self.tracking_no}
        return {x for x in out if x}


@dataclass(frozen=True)
class Ticket:
    """售后维修工单。"""

    ticket_no: str
    customer_key: str
    state: TicketState
    device: str = ""
    issue: str = ""
    store: str = ""
    eta: str = ""
    note: str = ""

    def human_status(self) -> str:
        head = {
            TicketState.RECEIVED: "机器已经收到了，排队检测",
            TicketState.DIAGNOSING: "工程师正在检测",
            TicketState.REPAIRING: "正在维修",
            TicketState.WAIT_PART: "在等配件到货",
            TicketState.DONE: "已经修好了，可以来取了",
            TicketState.RETURNED: "已经返还给您了",
        }[self.state]
        bits = [head]
        if self.eta and self.state is not TicketState.DONE:
            bits.append(self.eta)
        if self.state is TicketState.DONE and self.store:
            bits.append(f"在{self.store}")
        return "，".join(bits)


# 订单号：连续 8 位以上数字或字母数字混排。刻意写宽——
# 认不出订单号只是多问一句，认错了就查到别人的单
_ORDER_NO = re.compile(r"(?<![0-9A-Za-z])([0-9]{8,24}|[A-Z]{2,4}[0-9]{6,20})(?![0-9A-Za-z])")


def find_order_no(text: str) -> str:
    m = _ORDER_NO.search((text or "").replace(" ", "").replace("-", ""))
    return m.group(1) if m else ""


class OrderBook:
    """订单与工单的读取口。第一期同样建议 CSV 日更或直接对接进销存。"""

    def __init__(self, orders: list[Order], tickets: list[Ticket] | None = None) -> None:
        self.orders = orders
        self.tickets = tickets or []

    def for_customer(self, customer_key: str) -> list[Order]:
        """这个客户自己的订单，最近的排前面。"""
        mine = [o for o in self.orders if o.customer_key == customer_key]
        return sorted(mine, key=lambda o: o.placed_at or "", reverse=True)

    def lookup(self, customer_key: str, text: str = "") -> Order | None:
        """查这个客户问的那张单。

        **归属校验是硬的**：即使客户报了订单号，也只在他自己的单里找。
        报别人的单号查不到——这不是不便，这是必须的。
        """
        mine = self.for_customer(customer_key)
        if not mine:
            return None
        if no := find_order_no(text):
            for o in mine:
                if o.order_no == no or o.tracking_no == no:
                    return o
            return None  # 报了单号但不是他的 → 查不到，不要退回「最近一单」
        # 没报单号：只有唯一一单在进行中时才敢认，多单并行就得问清楚
        live = [o for o in mine
                if o.state not in (OrderState.DONE, OrderState.CANCELLED)]
        if len(live) == 1:
            return live[0]
        if len(mine) == 1:
            return mine[0]
        return None

    def ticket_for(self, customer_key: str, text: str = "") -> Ticket | None:
        mine = [t for t in self.tickets if t.customer_key == customer_key]
        if not mine:
            return None
        if no := find_order_no(text):
            return next((t for t in mine if t.ticket_no == no), None)
        live = [t for t in mine if t.state is not TicketState.RETURNED]
        return live[0] if len(live) == 1 else (mine[0] if len(mine) == 1 else None)


def mask_phone(text: str) -> str:
    """回复里不回显完整手机号。客户自己知道自己的号，写出来只会在截图里泄露。"""
    return re.sub(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)", r"\1****\2", text or "")

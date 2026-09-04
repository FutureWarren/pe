"""会话归属与「AI 还能不能说话」的关系——这一条决定了补位设计成不成立。

## 企微给的硬约束（2026-08-25 查证，腾讯官方文档）

微信客服会话有五个归属状态：

    0 未处理 / 1 智能助手接待 / 2 排队待接入 / 3 人工接待 / 4 已结束

**`kf/send_msg` 只在状态 0 和 1 能调用。** 状态 2、3、4 调用一律返回
`95018`。而状态流转是**单向**的：

    0 → 1 / 2 / 3      1 → 2 / 3      2 → 3      3 → 3 / 4      4 → 不可变更

也就是说：**一旦把会话转给人工（3），就再也不能用 API 拉回智能助手（1）。**
只能等会话结束（4），客户重新发消息回到 0，才有机会再转回 1。

## 这条约束推翻了一个想当然的设计

「转给销售，但销售没来之前 AI 接着陪」——听起来天经地义，**在企微上做不到**。
转过去的那一刻 AI 就失去了发言权；它以为自己在陪聊，实际上每一句
`send_msg` 都返回 95018，客户那头一个字都收不到。
而这正是这套代码库最怕的那种失败：**日志正常、判断正常、客户什么也没收到。**

## 所以补位的正确做法是：先别转

把「通知销售」和「转移会话归属」拆开——这两件事本来就不是一件事：

- **通知**（推一张客户情况单到销售的企微）→ 立刻做，不影响会话归属；
- **转移归属**（`service_state` → 3）→ **等到真的该由人接手时才做**。

于是三档意图天然对应三种会话动作：

| 意图档位 | 会话状态 | 谁在说话 |
|---|---|---|
| 信息类（物流/保修/激活） | **保持 1** | AI 直接答完，销售完全不用出现 |
| 要斟酌但能自动 | **保持 1** | AI 答，同时推单让销售知道 |
| 钱 / 投诉 / 谈价 | **转 3** | AI 说完最后一句回执就闭嘴，交给人 |

**「绝不代答」的那几类，恰好就是该转 3 的那几类**——这不是巧合：
两者判据是同一个，即「这句话该不该由真人负责」。

## 代价与取舍

保持状态 1 意味着这通会话**不出现在销售的「微信客服」工作台里**。
销售看到的是我们推过去的那张客户情况单（含深链）。这是有意的取舍：

- 让它进工作台 = AI 当场失去发言权，而多数售后问题 AI 本可以当场答完；
- 不进工作台 = AI 答完就完了，销售连看都不用看——**这才是省人力的地方**。

真正需要人的那几类照样进工作台，一次都不会漏。
"""

from dataclasses import dataclass
from enum import IntEnum


class KfState(IntEnum):
    """微信客服会话归属状态（与 `gateway/wecom_kf.py` 的常量同源）。"""

    UNHANDLED = 0
    ROBOT = 1
    POOL = 2
    HUMAN = 3
    ENDED = 4


# `kf/send_msg` 允许的状态。其余一律 95018。
SENDABLE = frozenset({KfState.UNHANDLED, KfState.ROBOT})

# 允许的状态迁移（企微单向约束）。**3 不能回 1，这是整个设计的支点。**
ALLOWED_TRANSITIONS: dict[KfState, frozenset[KfState]] = {
    KfState.UNHANDLED: frozenset({KfState.ROBOT, KfState.POOL, KfState.HUMAN}),
    KfState.ROBOT: frozenset({KfState.POOL, KfState.HUMAN}),
    KfState.POOL: frozenset({KfState.HUMAN}),
    KfState.HUMAN: frozenset({KfState.HUMAN, KfState.ENDED}),
    KfState.ENDED: frozenset(),
}


def can_send(state: int | None) -> bool:
    """这个状态下 AI 还能不能发消息。

    `None`（查不到状态）按**能发**处理：查状态失败时假定不能发，
    会让 AI 在通道抖动时集体失声，那比偶尔踩一次 95018 糟得多——
    95018 至少会被记下来，静默失声不会。
    """
    if state is None:
        return True
    return state in SENDABLE


def can_transition(frm: int | None, to: int) -> bool:
    """这次状态迁移企微允不允许。**不允许的迁移不要发出去**——

    发出去只会拿回一个错误码，然后我们基于「已经转过去了」继续往下走，
    而实际上一动没动。
    """
    if frm is None:
        return True  # 不知道当前状态，交给企微去判，失败按失败处理
    try:
        return KfState(to) in ALLOWED_TRANSITIONS.get(KfState(frm), frozenset())
    except ValueError:
        return False


@dataclass(frozen=True)
class Plan:
    """这一轮除了「说什么」之外，还要对会话归属做什么。"""

    speak: bool
    transfer_to_human: bool     # 要不要把归属转给销售（不可逆！）
    notify_staff: bool          # 要不要推一张客户情况单
    reason: str = ""

    @property
    def keeps_ai_voice(self) -> bool:
        """这一轮之后 AI 还有没有发言权。"""
        return not self.transfer_to_human


def plan_for(
    *, should_speak: bool, escalate: bool, state: int | None = None
) -> Plan:
    """把补位结论翻译成会话动作。

    三条规则：

    1. **要转人工的，先把话说完再转。** 回执（「我叫同事来跟您说」）必须在
       `transfer` 之前发出去——转完就发不了了，客户会对着静默。
       这与律所侧那条教训同源：「过渡话术没发出去就绝不转接」。
    2. **能自己答的，绝不转。** 转了就永久失去发言权，而下一句客户可能
       又问一个 AI 本来能答的问题。
    3. **通知与转移分开。** 推单是即时的、可逆的、不影响归属；
       转移是不可逆的，只在真的该由人接手时做。
    """
    if escalate:
        return Plan(
            speak=should_speak or True,   # 转之前一定要留一句回执
            transfer_to_human=True, notify_staff=True,
            reason="该由人接手：先发回执，再转归属（转完就发不出去了）",
        )
    if should_speak:
        return Plan(
            speak=True, transfer_to_human=False, notify_staff=False,
            reason="AI 自己答得了，保持智能助手接待，不动归属",
        )
    return Plan(
        speak=False, transfer_to_human=False, notify_staff=False,
        reason="真人在场或无需回应，不说也不动归属",
    )

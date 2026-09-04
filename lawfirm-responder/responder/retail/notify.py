"""把销售待办真的送到人眼前。

在这个文件出现之前，转人工的结果是一条落在运维小记里的记录——**查得到，
但没有人会去查**。那等于没有待办：客户问了一句 AI 答不了的话，系统一切正常，
而那边没有任何人知道。这是整条链路上最后一处「系统自认为做完了，
那头什么都没发生」。

## 为什么是企微群机器人

- **不需要任何审批**。建一个群、加一个机器人、复制 webhook——五分钟的事。
  相比之下自建应用要管理员配可见范围，而华为管控下的企微能不动就不动。
- **不需要 access_token**，也就不跟任何别的系统抢凭据。
- **销售本来就在企微里。** 送到他每天都开着的那个窗口，比送到一个要登录的
  后台强得多。

## 三条克制

1. **一次「叫人」只响一次铃。** 客户连发三句补充时，落库要落三条（记录要全），
   但**不能响三次**——响到第三次他就把这个群折叠了，而那正是最要紧的时候。
   由调用方通过 `push=False` 控制（见 `pipeline._defer`）。
2. **推送里必须带一条能看到全部上下文的链接。** 只推一句「客户要退货」，
   他还得去别处找这个人是谁、之前说了什么；而公众号那头没有客服工作台，
   他其实无处可找。链接指向我们自己的会话页。
3. **推不出去不能把主链路带走。** 这一步是锦上添花——它失败时客户其实已经
   收到回执了。失败落痕 + 计数，不抛出。
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# 企微群机器人的限流是 20 条/分钟。按酷机时代的量（几万/月 ≈ 每分钟一两条）
# 远够用，但活动日会有尖峰——所以失败一定要落痕，别让它安静地丢。
RATE_LIMIT_PER_MINUTE = 20


class TodoNotifier:
    """企微群机器人。`sender` 只需要一个 `send_robot_text(webhook, text) -> bool`。"""

    def __init__(self, webhook: str = "", *, sender=None, base_url: str = "") -> None:
        self.webhook = webhook
        self.sender = sender
        self.base_url = (base_url or "").rstrip("/")

    def available(self) -> bool:
        return bool(self.webhook and self.sender is not None)

    def push(self, *, group_id: str, said: str, why: str,
             now: datetime | None = None) -> bool:
        """推一条待办。**没配 webhook 返回 False**，调用方据此落痕。"""
        if not self.available():
            return False
        text = self.compose(group_id=group_id, said=said, why=why, now=now)
        try:
            return bool(self.sender.send_robot_text(self.webhook, text))
        except Exception:                              # noqa: BLE001
            logger.exception("待办推送失败: %s", group_id)
            return False

    def compose(self, *, group_id: str, said: str, why: str,
                now: datetime | None = None) -> str:
        """成文。**短**——群里的长消息没人读完。"""
        now = now or datetime.now()
        said = " ".join((said or "").split())[:60]
        lines = [f"🔔 有客户要人接一下（{now:%H:%M}）"]
        if said:
            lines.append(f"客户说：{said}")
        if why:
            lines.append(why)
        if self.base_url:
            lines.append(f"看完整对话：{self.base_url}/g/{group_id}")
        else:
            # 没配公网地址时说清楚去哪看，别让人对着一条没有下文的提醒发愣。
            lines.append("（未配 RESPONDER_PUBLIC_BASE_URL，去控制台「会话」页找）")
        return "\n".join(lines)

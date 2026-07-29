"""智能机器人回调报文解析。

与自建应用回调是两套报文结构（字段名、嵌套层级都不同），因此单独成模块。
关键字段 `WebhookUrl`：企微在每条回调里下发一个**该会话专用**的发送地址，
用它回复就不必让员工在每个群里再手工配一个群机器人 webhook——这是「客户零操作、
员工零配置」闭环的最后一块。企微若未下发（版本差异），字段为空，发送侧自动回落
到群档案里手工配置的 `robot_webhook`。

报文结构参考企微文档「智能机器人 - 接收消息」：
    <xml><From><UserId/><Name/><Alias/></From><MsgType/><ChatType/><ChatId/>
         <WebhookUrl/><MsgId/><Text><Content/></Text></xml>
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass

from responder.gateway import mention
from responder.models import IncomingMessage

CHAT_SINGLE = "single"
EVENT_ADD_TO_CHAT = "add_to_chat"


@dataclass
class BotEnvelope:
    """一条机器人消息 + 建档/发送所需的会话元信息。"""

    msg: IncomingMessage | None = None
    chat_id: str = ""
    chat_type: str = ""
    sender_id: str = ""
    sender_name: str = ""
    webhook_url: str = ""
    event_type: str = ""

    @property
    def is_single(self) -> bool:
        return self.chat_type == CHAT_SINGLE

    @property
    def group_id(self) -> str:
        """单聊没有 ChatId，用发送者标识兜底，保证会话档案稳定唯一。"""
        return self.chat_id or (f"bot:{self.sender_id}" if self.sender_id else "")


def parse(xml: ET.Element, *, fallback_msg_id: str) -> BotEnvelope | None:
    """解密后的 XML → BotEnvelope；无法处理的报文返回 None。"""
    msg_type = xml.findtext("MsgType") or ""
    sender = (
        xml.findtext("From/UserId")
        or xml.findtext("From/Alias")
        or xml.findtext("FromUserName")
        or ""
    )
    env = BotEnvelope(
        chat_id=xml.findtext("ChatId") or "",
        chat_type=xml.findtext("ChatType") or "",
        sender_id=sender,
        sender_name=xml.findtext("From/Name") or "",
        webhook_url=xml.findtext("WebhookUrl") or "",
        event_type=xml.findtext("Event/EventType") or "",
    )
    if msg_type == "event":
        return env  # 入群等事件：只建档，不进判断
    if msg_type != "text":
        return None
    if not env.group_id:
        return None

    # 正文里的 @机器人 前缀要剥掉：留着会命中「@点名 → 沉默」规则（那条规则是为
    # 「客户点名律师」设计的），也会污染模型上下文。
    raw = xml.findtext("Text/Content") or xml.findtext("Content") or ""
    content, _ = mention.strip_mentions(raw)
    env.msg = IncomingMessage(
        msg_id=xml.findtext("MsgId") or fallback_msg_id,
        group_id=env.group_id,
        sender_id=sender,
        content=content,
        msg_type="text",
        # 群里机器人只收得到被 @ 的消息，单聊本身就是直接对话——两种情况都是
        # 客户直接冲着助手来的，故恒为 True（不依赖企微是否保留 @ 前缀）。
        mentioned_bot=True,
    )
    return env

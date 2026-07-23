"""模块边界契约：消息、群档案、判断结果、回复、提醒。"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class ClientStatus(str, Enum):
    SIGNED = "signed"  # 已成交
    PROSPECT = "prospect"  # 未成交


class Action(str, Enum):
    """三分类判断结果。"""

    ANSWER = "answer"  # ① 直接回答（通用法律知识）
    HANDOFF = "handoff"  # ② 安抚 + 承接 + 通知人工（案件特定 / 报价 / 紧急）
    SILENCE = "silence"  # ③ 保持沉默


class Category(str, Enum):
    """问题类型，决定话术与提醒分级。"""

    GENERAL_LAW = "general_law"  # 通用法律知识
    CASE_STATUS = "case_status"  # 案件进展等案件特定问题
    FEE = "fee"  # 报价 / 费用试探（AI 绝不报价）
    URGENT = "urgent"  # 紧急情形：拘留/传唤/开庭临近/情绪/投诉
    CONTACT = "contact"  # 找人 / 催回复
    CHITCHAT = "chitchat"  # 闲聊、表情、客户互聊、非问题
    OTHER = "other"


class GroupProfile(BaseModel):
    """每个客户群绑定的案件上下文，由人工维护，粗粒度即可。"""

    group_id: str
    name: str = ""
    client_status: ClientStatus = ClientStatus.SIGNED
    case_type: str = ""  # 如「刑事辩护」「离婚纠纷」
    case_stage: str = ""  # 如「侦查阶段」「已立案」
    lawyer_name: str = ""  # 承办律师姓名（话术中点名用）
    lawyer_userid: str = ""  # 企微 userid，提醒推送用
    backup_userid: str = ""  # 第二责任人，升级提醒用
    ai_enabled: bool = True  # 控制台可按群开关 AI


class IncomingMessage(BaseModel):
    msg_id: str
    group_id: str
    sender_id: str
    sender_is_staff: bool = False  # 律师/客服发言 → 触发接管
    content: str = ""
    msg_type: str = "text"
    created_at: datetime = Field(default_factory=datetime.now)


class Decision(BaseModel):
    """判断引擎输出。沉默也要记录，便于复盘误判。"""

    msg_id: str
    group_id: str
    action: Action
    category: Category
    urgent: bool = False
    should_speak: bool = False  # 综合等待时长/接管/开关后的最终发言判定
    reasons: list[str] = Field(default_factory=list)


class Reply(BaseModel):
    msg_id: str
    group_id: str
    text: str
    mode: str = "shadow"  # shadow: 草稿待人工采用; live: 已自动发出
    compliance_passed: bool = True


class Reminder(BaseModel):
    """企微单聊推送给承办律师的提醒。"""

    msg_id: str
    group_id: str
    to_userid: str
    urgent: bool = False
    summary: str = ""

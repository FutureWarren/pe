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
    GREETING = "greeting"  # 一对一客服场景的开场/意图不明 → 引导客户说明情况
    OTHER = "other"


# 零售会话的标记（见 responder/retail/）。**两条产品线共用一个进程，
# 边界必须显式。** 律所侧的定时事务（线索简报、客户记忆、静默挽留）是按
# 「安静下来的会话」扫的，**不看渠道**——不挡住的话，一个在公众号里问保修
# 的人会变成一张推给律师的交接单，甚至收到一句律所的挽留话术。
RETAIL_CASE_TYPE = "retail"


class GroupProfile(BaseModel):
    """每个客户群绑定的案件上下文，由人工维护，粗粒度即可。"""

    group_id: str
    name: str = ""
    client_status: ClientStatus = ClientStatus.SIGNED
    case_type: str = ""  # 如「刑事辩护」「离婚纠纷」
    case_stage: str = ""  # 如「侦查阶段」「已立案」
    lawyer_name: str = ""  # 承办律师姓名（话术中点名用）
    # **数据归属**：这通对话是谁的。律师个人令牌能看到哪些会话，判据就是它
    # （`Store.own_group_ids`）。只有分案引擎真的把单派给某人、或人工指定承办律师时
    # 才写；**建档时一律留空**。
    lawyer_userid: str = ""
    # **提醒接收人**：还没派给具体律师时，那张交接单先推给谁。
    #
    # 2026-08-12 体检发现的最严重一条：这两件事本来共用 `lawyer_userid` 一个字段。
    # 微信客服会话建档时把企微返回的**第一位接待人**填了进去，于是名册里排第一的
    # 那位普通律师，个人链接一点开就能看到全所每一通咨询原文、每一条 AI 回复，
    # 还能把任意客户的会话「转给我」。护栏文档上写着「律师只看自己名下数据」，
    # 所以没有人会去查。
    #
    # 「该通知谁」和「这是谁的数据」是两个问题，必须两个字段。
    notify_userid: str = ""
    backup_userid: str = ""  # 第二责任人，升级提醒用
    ai_enabled: bool = True  # 控制台可按群开关 AI
    robot_webhook: str = ""  # 群机器人 webhook（key 或完整 URL），人工配置，长期有效
    # 智能机器人回调随每条消息下发的会话专用发送地址：有效期短（分钟级），每次回调刷新。
    # 有它就不必让员工手工配 robot_webhook——群聊通道「员工零配置」靠这一对字段。
    bot_webhook: str = ""
    bot_webhook_at: datetime | None = None
    # 微信客服会话（一对一）：两者齐备时回复走客服通道，优先于机器人/群聊
    kf_open_kfid: str = ""  # 客服账号 ID
    kf_external_userid: str = ""  # 客户的外部联系人 ID
    # 抖音企业号私信会话：对方的 open_id。与微信客服同属「一对一」，
    # 但发送侧受平台配额限制（24 小时窗口 / 6 条），见 gateway/douyin.py
    douyin_open_id: str = ""
    # 外部渠道会话（美团/小红书/…）：由 RPA 之类的自动化工具搬运收发。
    # 判断层与微信客服完全同构，差异只在「怎么把字发出去」——外部渠道没有
    # 推送接口，回复排进发件箱等那头来取（见 gateway/channel.py）。
    ext_channel: str = ""  # 渠道标识，如 meituan / xhs
    ext_user_id: str = ""  # 对方在该渠道的标识
    # 会话已转人工接待：接手的律师 userid 与转接时刻（见 docs/kf-handoff.md）
    handoff_userid: str = ""
    handoff_at: datetime | None = None
    # 客户跨会话记忆（见 responder/memory.py）：老客户回访时注入模型上下文。
    # 只由已入库的事实拼装，不让模型自由发挥——记错一件客户没说过的事，
    # 比不记得更伤人。
    memory: str = ""
    memory_at: datetime | None = None

    @property
    def is_retail(self) -> bool:
        return self.case_type == RETAIL_CASE_TYPE

    @property
    def is_douyin(self) -> bool:
        return bool(self.douyin_open_id)

    @property
    def is_external(self) -> bool:
        """外部渠道会话（由 RPA 等自动化工具搬运收发）。"""
        return bool(self.ext_channel and self.ext_user_id)

    @property
    def is_kf(self) -> bool:
        """一对一客服会话。与群聊的关键差异：AI 是第一响应人而非补位者，
        故不等待、不对开场白沉默（见 engine/decision.py）。

        抖音私信、以及 RPA 搬运的外部渠道，在判断层与微信客服完全同构
        （都是客户主动找上门的一对一窗口），故一并归入 is_kf——差异只在
        「怎么把字发出去」，那在发送层处理。这条同构是整套多渠道方案的地基：
        **每多一个渠道只该多一只「手」，判断、评分、合规、派单一行都不该动。**
        """
        return (
            bool(self.kf_open_kfid and self.kf_external_userid)
            or self.is_douyin
            or self.is_external
        )

    @property
    def reminder_userid(self) -> str:
        """这通对话的提醒该推给谁（不含全局兜底——那要读配置，模型层不碰配置）。

        承办律师优先：真派出去之后，交接单和督办都该走他，而不是建档时那个
        临时接收人。两个都空时由调用方回落 `settings.default_notify_userid`。
        """
        return self.lawyer_userid or self.notify_userid


class IncomingMessage(BaseModel):
    msg_id: str
    group_id: str
    sender_id: str
    sender_is_staff: bool = False  # 律师/客服发言 → 触发接管
    content: str = ""
    msg_type: str = "text"
    # 群里被 @ 点名：客户是冲着助手来的，不应再走「给律师留时间」的补位等待
    mentioned_bot: bool = False
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
    summary: str = ""  # 推送给律师的完整文本（需自带上下文）
    # 结构化字段：控制台按信息层级展示，不必去解析 summary
    question: str = ""
    ai_reply: str = ""

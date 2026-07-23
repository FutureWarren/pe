"""判断引擎编排：三层判断。

1. 要不要响应：群 AI 开关、人工是否已接管、补位等待时长是否已到
2. 能不能直接答：规则分类（rules.classify），可选 LLM 复核边界样本
3. 答什么怎么答：交给 reply.generator（按客户状态与问题类型选话术）
"""

from datetime import datetime

from responder.config import Settings, get_settings
from responder.engine import rules
from responder.models import Action, Decision, GroupProfile, IncomingMessage


def _is_night(now: datetime, settings: Settings) -> bool:
    h = now.hour
    start, end = settings.night_start_hour, settings.night_end_hour
    return h >= start or h < end if start > end else start <= h < end


def wait_seconds(now: datetime, settings: Settings | None = None) -> int:
    """当前时段的 AI 补位等待时长。"""
    settings = settings or get_settings()
    return settings.wait_seconds_night if _is_night(now, settings) else settings.wait_seconds_day


def decide(
    msg: IncomingMessage,
    group: GroupProfile,
    *,
    seconds_since_last_staff_reply: float | None = None,
    seconds_unanswered: float = 0.0,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> Decision:
    """对一条群消息给出三分类判断与最终发言判定。

    seconds_since_last_staff_reply: 律师/客服最近一次群内发言距今秒数（None = 从未）。
    seconds_unanswered: 该消息已等待人工回复的秒数（调度器传入；0 表示刚收到）。
    """
    settings = settings or get_settings()
    now = now or datetime.now()

    action, category, urgent, reasons = rules.classify(msg.content, msg.msg_type)
    decision = Decision(
        msg_id=msg.msg_id,
        group_id=msg.group_id,
        action=action,
        category=category,
        urgent=urgent,
        reasons=reasons,
    )

    if action == Action.SILENCE:
        return decision

    # ---- 要不要响应（人工优先原则）
    if not group.ai_enabled:
        decision.reasons.append("gate:ai-disabled")
        return decision
    if msg.sender_is_staff:
        decision.reasons.append("gate:staff-message")
        return decision
    if (
        seconds_since_last_staff_reply is not None
        and seconds_since_last_staff_reply < settings.takeover_seconds
    ):
        decision.reasons.append("gate:human-takeover")
        return decision

    # 紧急情形不等待：立即一句安抚 + 强提醒
    if urgent:
        decision.should_speak = True
        decision.reasons.append("gate:urgent-bypass-wait")
        return decision

    required = wait_seconds(now, settings)
    if seconds_unanswered < required:
        decision.reasons.append(f"gate:waiting({int(seconds_unanswered)}s/{required}s)")
        return decision

    decision.should_speak = True
    return decision

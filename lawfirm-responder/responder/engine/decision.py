"""判断引擎编排：三层判断。

1. 要不要响应：群 AI 开关、人工是否已接管、补位等待时长是否已到
2. 能不能直接答：规则分类（rules.classify），可选 LLM 复核边界样本
3. 答什么怎么答：交给 reply.generator（按客户状态与问题类型选话术）
"""

from datetime import datetime

from responder.config import Settings, get_settings
from responder.engine import rules, signals
from responder.models import Action, Category, Decision, GroupProfile, IncomingMessage


def _is_night(now: datetime, settings: Settings) -> bool:
    h = now.hour
    start, end = settings.night_start_hour, settings.night_end_hour
    return h >= start or h < end if start > end else start <= h < end


def wait_seconds(
    now: datetime, settings: Settings | None = None, group: GroupProfile | None = None
) -> int:
    """当前时段的 AI 补位等待时长。

    群聊里 AI 是补位者，要留时间给律师先答；一对一客服会话里 AI 就是第一响应人
    （客户主动点进来咨询，无人值守），等待没有意义，默认 0 秒即时响应。
    """
    settings = settings or get_settings()
    if group is not None and group.is_kf:
        return settings.kf_wait_seconds
    return settings.wait_seconds_night if _is_night(now, settings) else settings.wait_seconds_day


def decide(
    msg: IncomingMessage,
    group: GroupProfile,
    *,
    seconds_since_last_staff_reply: float | None = None,
    seconds_unanswered: float = 0.0,
    settings: Settings | None = None,
    now: datetime | None = None,
    classification: tuple | None = None,
) -> Decision:
    """对一条群消息给出三分类判断与最终发言判定。

    seconds_since_last_staff_reply: 律师/客服最近一次群内发言距今秒数（None = 从未）。
    seconds_unanswered: 该消息已等待人工回复的秒数（调度器传入；0 表示刚收到）。
    classification: 可选的外部分类结果 (action, category, urgent, reasons)——
        供管道在规则分类后经 LLM 复核修正再进门控（见 service.Pipeline）。
    """
    settings = settings or get_settings()
    now = now or datetime.now()

    action, category, urgent, reasons = classification or rules.classify(
        msg.content, msg.msg_type
    )
    # 一对一客服：意图不明的开场（「你好」「我想咨询一下」）在群里该沉默，
    # 在客服窗口沉默 = 把客户晾着。转为引导型开场白，同时服务于首轮筛查。
    # 「谢谢/好的」这类收尾应答（chitchat-fastpath）仍保持沉默，不刷屏。
    if (
        action == Action.SILENCE
        and "default-silence" in reasons
        and group.is_kf
        and msg.msg_type == "text"
        and msg.content.strip()
    ):
        action, category = Action.ANSWER, Category.GREETING
        # 刚留下联系方式的客户不能再被问「您是什么情况」——换收下并转交的话术
        reasons = reasons + [
            "kf:contact-ack" if signals.extract_contact(msg.content) else "kf:greeting-opener"
        ]

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

    required = wait_seconds(now, settings, group)
    if seconds_unanswered < required:
        decision.reasons.append(f"gate:waiting({int(seconds_unanswered)}s/{required}s)")
        return decision

    decision.should_speak = True
    return decision

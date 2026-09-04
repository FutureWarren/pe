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


def _no_wait(msg: IncomingMessage, group: GroupProfile) -> bool:
    """客服会话、或群里被 @ 点名——客户是直接冲着助手来的，等待没有意义。"""
    return group.is_kf or msg.mentioned_bot


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
    # 「@助手 在吗」——被叫的人就是助手本人，回「我帮您叫律师」很荒谬，直接应答
    if msg.mentioned_bot and rules.is_presence_check(msg.content):
        action, category = Action.ANSWER, Category.GREETING
        reasons = reasons + ["bot:presence-answer"]

    # 客服会话里的语音/图片/文件：沉默 = 把客户晾着（他不知道这里其实是要打字的）。
    # 转承接：一句「收到，稍后回复」+ 提醒人工去客服后台看内容。
    # 客户连发多张图不会刷屏——追问去重策略（second_touch/静默升级）按类别兜着。
    if action == Action.SILENCE and group.is_kf and msg.msg_type != "text":
        action, category = Action.HANDOFF, Category.OTHER
        reasons = reasons + ["kf:non-text-handoff"]

    # 被 @ 点名同样不能沉默：客户明确在叫助手，不吭声比答错更伤
    if (
        action == Action.SILENCE
        and "default-silence" in reasons
        and (group.is_kf or msg.mentioned_bot)
        and msg.msg_type == "text"
        and msg.content.strip()
    ):
        if signals.extract_contact(msg.content):
            # 刚留下联系方式的客户不能再被问「您是什么情况」——换收下并转交的话术
            action, category = Action.ANSWER, Category.GREETING
            reasons = reasons + ["kf:contact-ack"]
        elif rules.has_substance(msg.content):
            # 客户已经把事情说出来了，再回「麻烦您把情况讲一下」就是没在听。
            # 真机测试里最刺眼的一条：客户说「公司拖欠我三个月工资，还把我辞退了」，
            # 换回一句「比如『准备离婚，孩子抚养权想争取』」——他刚说完，我们还在要他说。
            # 交给承接路径，由管道的 _maybe_intake 转成追问。
            action, category = Action.HANDOFF, Category.OTHER
            reasons = reasons + ["kf:substance"]
        else:
            action, category = Action.ANSWER, Category.GREETING
            reasons = reasons + ["kf:greeting-opener"]

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
    # 会话已转人工接待：**转接不再让 AI 闭嘴**（2026-08-12 律所方拍板「应转尽转」，
    # 这是它的另一半）。转接的含义是「让这个客户出现在人工的工作台里」——
    # 客户一说出实质情况就转，人工那边随时看得到、随时能插进来；
    # 而真人开口**之前**，AI 接着陪、接着把案情问全，两头都不耽误。
    # 旧版转完静默 90 秒，真机里客户连发「你好」「人呢」全程无声——
    # 在「应转尽转」下几乎每个客户都会经历那 90 秒，代价不可接受。
    if group.handoff_userid and group.handoff_at is not None:
        waited = (now - group.handoff_at).total_seconds()
        # 接手的人**正在**说话（接管窗口内说过话），AI 才让位。
        # 判据必须是「最近」而不是「说过」：后者一旦成立就永远成立，
        # 律师接手说了一句、第二天客户再来，AI 依然一个字不回。
        being_handled = (
            seconds_since_last_staff_reply is not None
            and seconds_since_last_staff_reply < settings.takeover_seconds
        )
        if being_handled:
            decision.reasons.append(f"gate:handed-off({group.handoff_userid})")
            return decision
        # 没人在跟 → AI 继续。reclaimed 到点后由 service._release_stale_handoff
        # 清掉转接记录，客户之后再表达强意愿还能重新转。
        decision.reasons.append(
            "handoff:reclaimed" if waited >= settings.handoff_reclaim_seconds
            else "handoff:accompanying"
        )

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

    required = 0 if _no_wait(msg, group) else wait_seconds(now, settings, group)
    if seconds_unanswered < required:
        decision.reasons.append(f"gate:waiting({int(seconds_unanswered)}s/{required}s)")
        return decision

    decision.should_speak = True
    return decision

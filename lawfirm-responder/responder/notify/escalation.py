"""人工提醒分级与升级链路。

普通承接 → 企微普通推送承办律师；
紧急情形 → 强提醒（标题加急）+ 未处理超时升级第二责任人。
提醒内容：客户问题原文 + AI 已回复内容 + 建议回复要点。
"""

import logging
from datetime import datetime, timedelta

from responder import retry
from responder.config import Settings, get_settings
from responder.gateway.sender import WeComSender
from responder.models import Category, Decision, GroupProfile, IncomingMessage, Reminder
from responder.store.db import Store

_HINTS = {
    Category.CASE_STATUS: "建议回复要点：告知当前阶段与下一步动作，给出预期时间。",
    Category.FEE: "建议回复要点：费用请律师直接与客户确认，AI 未提及任何金额。",
    Category.URGENT: "建议回复要点：请尽快直接联系客户，安抚并说明处理安排。",
    Category.CONTACT: "建议回复要点：客户在催回复，请尽快在群内响应。",
}

logger = logging.getLogger(__name__)

_MSG_TYPE_ZH = {"voice": "语音", "image": "图片", "video": "视频", "file": "文件"}


def _question_text(msg: IncomingMessage) -> str:
    """提醒里的「客户问题」。语音/图片没有文字原文，要说清让人去哪看，
    而不是留一个空字段让律师猜。"""
    if msg.content:
        return msg.content
    zh = _MSG_TYPE_ZH.get(msg.msg_type, "非文字")
    return f"（客户发来{zh}消息，请在企微·微信客服后台查看原文）"


def build_reminder(
    msg: IncomingMessage,
    decision: Decision,
    group: GroupProfile,
    ai_reply: str | None,
    settings: Settings | None = None,
) -> Reminder:
    settings = settings or get_settings()
    prefix = "【加急】" if decision.urgent else ""
    where = "微信客服会话" if group.is_kf else "客户群"
    channel_hint = "客服会话中" if group.is_kf else "群内"
    lines = [
        f"{prefix}{where}「{group.name or group.group_id}」有待跟进消息",
        f"客户问题：{_question_text(msg)}",
    ]
    if ai_reply:
        lines.append(f"AI 已回复：{ai_reply}")
    hint = _HINTS.get(decision.category, f"建议回复要点：请尽快在{channel_hint}跟进。")
    lines.append(hint.replace("群内", channel_hint))
    return Reminder(
        msg_id=msg.msg_id,
        group_id=msg.group_id,
        question=_question_text(msg),
        ai_reply=ai_reply or "",
        # 群档案未配律师企微号时回落到全局兜底接收人——话术已经向客户承诺
        # 「已通知律师」，提醒必须真的送得出去。
        to_userid=group.reminder_userid or settings.default_notify_userid,
        urgent=decision.urgent,
        summary="\n".join(lines),
    )


def dispatch(reminder: Reminder, store: Store, sender: WeComSender | None) -> int:
    """入库提醒队列并（live 模式下）推送企微单聊。返回 reminder_id。"""
    rid = store.save_reminder(reminder)
    if sender and reminder.to_userid:
        if sender.send_direct_text(reminder.to_userid, reminder.summary):
            store.set_reminder_status(rid, "sent")
    return rid


def escalate_overdue(
    store: Store,
    sender: WeComSender | None,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> list[int]:
    """紧急提醒超时未处理 → 升级第二责任人。由定时任务调用。

    只有真的送出去才算升级：无人可升或发送失败时保持原状态等下一轮，
    否则「已升级」这个状态是假的——第二责任人根本没收到，而系统不再重试。
    """
    settings = settings or get_settings()
    now = now or datetime.now()
    escalated: list[int] = []
    cutoff = now - timedelta(seconds=settings.escalation_seconds)
    for r in store.overdue_urgent_reminders(cutoff):
        group = store.get_group(r["group_id"])
        # 没配第二责任人就升给全局兜底人——升级链断在「没人可升」上最没道理
        backup = (group.backup_userid if group else "") or settings.default_notify_userid
        if not (backup and sender):
            continue
        ident = str(r["id"])
        # **有限次 + 退避。** 原来失败就 `continue`，下一轮（10 秒后）再来一次，
        # 永不停止。一位律师离职被移出应用可见范围、或备用接收人写错一个字母，
        # 这里就会每 10 秒重试上百次——而这个循环占用的正是处理所有客户消息的
        # 那唯一一条线程，同时把企微打到限流，之后交接单、督办、战报、告警
        # 全部一起静默失效。**系统亲手拆掉了「出事有人知道」这条链。**
        if not retry.should_try(store, "escalation", ident, now=now):
            if retry.exhausted(store, "escalation", ident):
                store.set_reminder_status(r["id"], "escalation-failed")
                retry.give_up(
                    store, "escalation", ident,
                    f"加急提醒升级不出去（收件人 {backup}）。"
                    f"多半是这个人不在自建应用的可见范围里，或者账号已停用——"
                    f"去企微后台确认，然后在「状态」页看这条小记是否消失。",
                )
            continue
        if not sender.send_direct_text(backup, f"【升级提醒】\n{r['summary']}"):
            n = retry.record_failure(store, "escalation", ident)
            logger.warning("escalation send failed (%s 次): reminder=%s", n, r["id"])
            continue
        retry.succeeded(store, "escalation", ident)
        store.set_reminder_status(r["id"], "escalated")
        escalated.append(r["id"])
    return escalated

"""人工提醒分级与升级链路。

普通承接 → 企微普通推送承办律师；
紧急情形 → 强提醒（标题加急）+ 未处理超时升级第二责任人。
提醒内容：客户问题原文 + AI 已回复内容 + 建议回复要点。
"""

from datetime import datetime

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
        f"客户问题：{msg.content}",
    ]
    if ai_reply:
        lines.append(f"AI 已回复：{ai_reply}")
    hint = _HINTS.get(decision.category, f"建议回复要点：请尽快在{channel_hint}跟进。")
    lines.append(hint.replace("群内", channel_hint))
    return Reminder(
        msg_id=msg.msg_id,
        group_id=msg.group_id,
        # 群档案未配律师企微号时回落到全局兜底接收人——话术已经向客户承诺
        # 「已通知律师」，提醒必须真的送得出去。
        to_userid=group.lawyer_userid or settings.default_notify_userid,
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
    """紧急提醒超时未处理 → 升级第二责任人。由定时任务调用。"""
    settings = settings or get_settings()
    now = now or datetime.now()
    escalated: list[int] = []
    for r in store.pending_reminders():
        if not r["urgent"] or r["status"] == "escalated":
            continue
        age = (now - datetime.fromisoformat(r["created_at"])).total_seconds()
        if age < settings.escalation_seconds:
            continue
        group = store.get_group(r["group_id"])
        backup = group.backup_userid if group else ""
        if backup and sender:
            sender.send_direct_text(backup, f"【升级提醒】\n{r['summary']}")
        store.set_reminder_status(r["id"], "escalated")
        escalated.append(r["id"])
    return escalated

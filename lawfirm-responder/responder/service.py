"""消息处理主管道：入库 → 判断 → 生成 → 合规 → 发言/草稿 → 提醒。"""

import logging
from datetime import datetime

from responder.config import Settings, get_settings
from responder.engine.decision import decide
from responder.gateway.sender import WeComSender
from responder.models import Action, Decision, GroupProfile, IncomingMessage
from responder.notify import escalation
from responder.reply.generator import generate
from responder.store.db import Store

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, store: Store, sender: WeComSender | None = None,
                 settings: Settings | None = None):
        self.store = store
        self.settings = settings or get_settings()
        # 影子模式不需要发送通道
        self.sender = sender if self.settings.mode == "live" else None

    def handle(self, msg: IncomingMessage, *, seconds_unanswered: float = 0.0) -> Decision:
        self.store.save_message(msg)

        group = self.store.get_group(msg.group_id) or GroupProfile(group_id=msg.group_id)

        last_staff = self.store.last_staff_reply_at(msg.group_id)
        since_staff = (
            (datetime.now() - last_staff).total_seconds() if last_staff else None
        )
        # 律师自己的发言只用于更新接管状态，不进判断
        if msg.sender_is_staff:
            return Decision(
                msg_id=msg.msg_id, group_id=msg.group_id,
                action=Action.SILENCE, category="chitchat",
                reasons=["staff-message"],
            )

        decision = decide(
            msg, group,
            seconds_since_last_staff_reply=since_staff,
            seconds_unanswered=seconds_unanswered,
            settings=self.settings,
        )
        self.store.save_decision(decision)

        if decision.action == Action.SILENCE:
            return decision

        result = generate(msg, decision, group)
        reply_text = result.text if result else None
        if result:
            mode = "live" if (self.settings.mode == "live" and decision.should_speak) else "shadow"
            self.store.save_reply(msg.msg_id, msg.group_id, result.text, mode, result.passed)
            if mode == "live" and self.sender:
                self.sender.send_group_text(msg.group_id, result.text)

        # 承接类一律触发人工提醒；直接回答类也提醒律师补充
        reminder = escalation.build_reminder(msg, decision, group, reply_text)
        escalation.dispatch(reminder, self.store, self.sender)

        return decision

"""消息处理主管道：入库 → 分类（规则 + 可选 LLM 复核）→ 门控 → 生成 → 合规 → 发言/草稿 → 提醒。

追问处理（同一群、同一问题类别、接管时间窗内）：
  第 1 次 → 正常话术；第 2 次 → 二次安抚（不复读）；第 3 次起 → 群内静默 + 升级提醒。
"""

import logging
import time
from datetime import datetime

from responder.config import Settings, get_settings
from responder.engine import llm, rules
from responder.engine.decision import decide
from responder.gateway.sender import WeComSender
from responder.models import Action, Decision, GroupProfile, IncomingMessage
from responder.notify import escalation
from responder.reply import sanitize, templates
from responder.reply.generator import generate
from responder.store.db import Store

logger = logging.getLogger(__name__)

# LLM 复核采信阈值：低于此置信度维持规则结果（宁沉默不抢答）
REFINE_CONFIDENCE = 0.7


class Pipeline:
    def __init__(self, store: Store, sender: WeComSender | None = None,
                 settings: Settings | None = None):
        self.store = store
        self.settings = settings or get_settings()
        # 影子模式不需要发送通道
        self.sender = sender if self.settings.mode == "live" else None

    # ------------------------------------------------------------ 分类
    def _classify(self, msg: IncomingMessage, group: GroupProfile, history: list[dict]) -> tuple:
        """规则分类 + 边界样本 LLM 复核。

        只复核规则判「default-silence」的样本（漏答方向）；高优先级规则命中不交模型改判。
        """
        action, category, urgent, reasons = rules.classify(msg.content, msg.msg_type)
        if (
            action == Action.SILENCE
            and "default-silence" in reasons
            and len(msg.content.strip()) >= 6  # 过短消息不值得进模型
            and self.settings.llm_refine_enabled
            and llm.llm_available()
        ):
            refined = llm.refine(
                msg.content,
                history_text=_history_text(history),
                case_type=group.case_type,
                timeout=self.settings.llm_timeout_seconds,
                settings=self.settings,
            )
            if (
                refined
                and refined.action != Action.SILENCE
                and refined.confidence >= REFINE_CONFIDENCE
            ):
                action, category = refined.action, refined.category
                urgent = refined.category.value == "urgent"
                reasons = reasons + [f"llm-refine({refined.confidence:.2f}):{refined.reason}"]
        return action, category, urgent, reasons

    # ------------------------------------------------------------ 主流程
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

        history = self.store.recent_messages(msg.group_id, self.settings.history_window)

        decision = decide(
            msg, group,
            seconds_since_last_staff_reply=since_staff,
            seconds_unanswered=seconds_unanswered,
            settings=self.settings,
            classification=self._classify(msg, group, history),
        )

        if decision.action == Action.SILENCE:
            self.store.save_decision(decision)
            return decision

        result = generate(
            msg, decision, group, history=history, settings=self.settings,
            include_cta=not self._recent_cta(msg.group_id),
        )
        reply_text = None
        if result:
            mode = "live" if (self.settings.mode == "live" and decision.should_speak) else "shadow"
            final_text = self._apply_followup_policy(msg, decision, group, result.text, mode)
            if final_text is None:
                # 第 3 次追问起：群内静默，原话术仅留档草稿，提醒已升级
                final_text, mode = result.text, "shadow"
            self.store.save_reply(
                msg.msg_id, msg.group_id, final_text, mode,
                result.passed, category=decision.category.value,
            )
            if mode == "live" and self.sender:
                self._send_group(group, msg.group_id, final_text)
            reply_text = final_text

        # 判断日志在去重/门控修饰后入库，控制台看到的即最终裁决
        self.store.save_decision(decision)

        # 承接类一律触发人工提醒；直接回答类也提醒律师补充
        reminder = escalation.build_reminder(msg, decision, group, reply_text)
        escalation.dispatch(reminder, self.store, self.sender)

        return decision

    def _recent_cta(self, group_id: str) -> bool:
        """接管时间窗内该群是否已发过带面谈引导/收尾语的回复——有则本次不再带（防套路感）。"""
        for r in self.store.list_replies(group_id, limit=6):
            if r["mode"] != "live":
                continue
            age = (datetime.now() - datetime.fromisoformat(r["created_at"])).total_seconds()
            if age >= self.settings.takeover_seconds:
                break
            if any(m in r["text"] for m in templates.CTA_MARKERS):
                return True
        return False

    # ------------------------------------------------------------ 发送
    def _send_group(self, group: GroupProfile, group_id: str, text: str) -> None:
        """分条发送：多句内容拆成多条消息，条间隔模拟打字（见 docs/voice-guide.md）。"""
        chunks = (
            sanitize.split_messages(text, self.settings.split_max_parts)
            if self.settings.split_messages
            else [text]
        )
        for i, chunk in enumerate(chunks):
            if i and self.settings.split_delay_seconds > 0:
                time.sleep(self.settings.split_delay_seconds)
            if group.robot_webhook:
                self.sender.send_robot_text(group.robot_webhook, chunk)
            else:
                self.sender.send_group_text(group_id, chunk)

    # ------------------------------------------------------------ 追问策略
    def _apply_followup_policy(
        self, msg: IncomingMessage, decision: Decision, group: GroupProfile,
        text: str, mode: str,
    ) -> str | None:
        """返回实际要发的文本；None 表示本次群内静默（仅升级提醒）。

        判据：接管时间窗内，同一群同一问题类别已实际发出过几条回复。
        """
        if mode != "live":
            return text
        n = self.store.count_recent_live(
            msg.group_id, decision.category.value, self.settings.takeover_seconds
        )
        if n == 0:
            return text
        if n == 1:
            decision.reasons.append("followup:second-touch")
            return templates.second_touch(group, urgent=decision.urgent)
        decision.urgent = True
        decision.reasons.append("followup:suppressed-escalated")
        return None


def _history_text(history: list[dict]) -> str:
    from responder.reply import prompts

    return prompts.format_history(history)

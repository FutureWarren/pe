"""消息处理主管道：入库 → 分类（规则 + 可选 LLM 复核）→ 门控 → 生成 → 合规 → 发言/草稿 → 提醒。

追问处理（同一群、同一问题类别、接管时间窗内）：
  第 1 次 → 正常话术；第 2 次 → 二次安抚（不复读）；第 3 次起 → 群内静默 + 升级提醒。
"""

import logging
import time
from datetime import datetime, timedelta

from responder import lead
from responder.config import Settings, get_settings
from responder.engine import llm, rules, signals
from responder.engine.decision import decide, wait_seconds
from responder.gateway.sender import WeComSender
from responder.models import Action, Category, Decision, GroupProfile, IncomingMessage
from responder.notify import escalation
from responder.reply import sanitize, templates
from responder.reply.generator import generate
from responder.store.db import Store

logger = logging.getLogger(__name__)

# LLM 复核采信阈值：低于此置信度维持规则结果（宁沉默不抢答）
REFINE_CONFIDENCE = 0.7


class Pipeline:
    def __init__(self, store: Store, sender: WeComSender | None = None,
                 settings: Settings | None = None, kf_client=None):
        self.store = store
        self.settings = settings or get_settings()
        self._sender = sender
        self._kf_client = kf_client

    # 发送通道按模式实时门控：影子模式一律不出声。
    # 用属性而非构造期固化，是为了支持控制台运行时切换模式（无需重启服务）。
    @property
    def sender(self) -> WeComSender | None:
        return self._sender if self.settings.mode == "live" else None

    @property
    def kf_client(self):
        """「发」受模式门控；「收」用 worker 持有的原始 client，不受此限。"""
        return self._kf_client if self.settings.mode == "live" else None

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
            # 已留联系方式/要约见的消息意图已经明确，不必再问模型「这是不是法律问题」
            and signals.detect(msg.content)[0] != signals.HOT
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
        # 律师自己的发言只用于更新接管状态，不进判断（沉默同样入判断日志）
        if msg.sender_is_staff:
            decision = Decision(
                msg_id=msg.msg_id, group_id=msg.group_id,
                action=Action.SILENCE, category="chitchat",
                reasons=["staff-message"],
            )
            self.store.save_decision(decision)
            return decision

        # 上下文一次查到底：分类、生成、线索简报共用同一份，按各自窗口切片
        window = max(self.settings.history_window, self.settings.lead_history_window)
        convo = self.store.recent_messages(msg.group_id, window)
        history = convo[-self.settings.history_window :]

        decision = decide(
            msg, group,
            seconds_since_last_staff_reply=since_staff,
            seconds_unanswered=seconds_unanswered,
            settings=self.settings,
            classification=self._classify(msg, group, history),
        )

        if decision.action == Action.SILENCE:
            self.store.save_decision(decision)
            # 沉默不等于不值钱：群里客户单发一句「我电话138…你们联系我」按规则
            # 判沉默（AI 不必接话），但那是最强的转化信号，必须进线索通道。
            # 早退前不做这一步，这类线索就永远只躺在聊天记录里。
            self._maybe_dispatch_lead(msg, decision, group, convo)
            return decision

        # 补位等待未到点：登记到点复评任务，由后台工作线程届时重跑本判断。
        # 没有这一步，live 模式下非紧急消息会永远停在「等待中」不被发出。
        if not decision.should_speak and any(
            r.startswith("gate:waiting") for r in decision.reasons
        ):
            required = wait_seconds(datetime.now(), self.settings, group)
            self.store.add_pending_check(
                msg.msg_id, msg.group_id,
                msg.created_at + timedelta(seconds=required + 1),
            )

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
            sent = True
            if mode == "live" and self._can_send(group):
                sent = self._send_group(group, msg.group_id, final_text)
                if not sent:
                    # 发送失败还按 live 记账，追问策略会误判「已经答过了」，
                    # 客户从此拿不到首答。记为 failed，并让人工兜底。
                    mode = "failed"
                    decision.reasons.append("send:failed")
            self.store.save_reply(
                msg.msg_id, msg.group_id, final_text, mode,
                result.passed, category=decision.category.value,
            )
            # 只有确实发出去的才算「AI 已回复」——否则提醒会让律师
            # 误以为客户已被安抚，实际上那边一片安静
            reply_text = final_text if sent else None

        # 判断日志在去重/门控修饰后入库，控制台看到的即最终裁决
        self.store.save_decision(decision)

        # 转化信号：客户留了联系方式/表达面谈意愿 → 立刻整理交接单推给接待人。
        # 首响只是止损，真正的业务价值在这一步。
        lead_pushed = self._maybe_dispatch_lead(msg, decision, group, convo)

        # 承接类一律触发人工提醒；直接回答类也提醒律师补充。
        # 同一条消息只提醒一次（到点复评会二次经过这里，不重复打扰律师）。
        # 开场问候不惊动律师——客户只是说了句「你好」。
        # 客服会话的**文字**消息已由线索简报统一承载（一次咨询 = 一条交接单），
        # 不再逐条推提醒；语音/图片没有文字可归纳、不进简报，必须逐条提醒兜底，
        # 否则客户发的语音就没人知道了。
        # 本条消息刚触发过交接单推送的也不再提醒——同一件事不给律师发两条 DM。
        # 紧急消息例外：即使交接单已经推过，也要落一条 urgent 提醒入库——
        # 600 秒未处理自动升级第二责任人这条链，扫的是 reminders 表。
        # 不落库，客服通道（主进线通道）就整体没有紧急升级兜底。
        silent_urgent = decision.urgent and (lead_pushed or group.is_kf)
        if (
            decision.category != Category.GREETING
            and not self.store.has_reminder(msg.msg_id)
            and (
                silent_urgent
                or (
                    not lead_pushed
                    and not (
                        group.is_kf
                        and self.settings.lead_brief_enabled
                        and msg.msg_type == "text"
                    )
                )
            )
        ):
            reminder = escalation.build_reminder(
                msg, decision, group, reply_text, self.settings
            )
            # silent_urgent：只入库供升级扫描，不再推 DM（简报已经推过一条了）
            escalation.dispatch(
                reminder, self.store, None if silent_urgent else self.sender
            )

        return decision

    def _maybe_dispatch_lead(
        self, msg: IncomingMessage, decision: Decision, group: GroupProfile,
        convo: list[dict],
    ) -> bool:
        """转化信号或紧急情形 → 生成/更新线索简报。返回**本轮是否真的推送了**。

        返回值供逐条提醒去重：只有交接单实际发出（而非仅入库/被节流）时才算，
        否则群通道的费用咨询等仍需要逐条提醒兜底。
        紧急消息在客服会话里也走这条路（force 推送）：律师收到的是一张
        含背景与联系方式的交接单，而不是一句孤零零的「客户说了什么」。
        """
        if not self.settings.lead_brief_enabled:
            return False
        urgent_kf = group.is_kf and decision.urgent
        if not urgent_kf and signals.detect(msg.content)[0] == signals.COLD:
            return False
        try:
            # 用门控后的 sender：影子模式只入库不外发（与律师提醒口径一致）
            row = lead.dispatch(
                self.store, group, convo, self.sender,
                settings=self.settings, force=urgent_kf, urgent=decision.urgent,
            )
            return bool(row and row.get("_notified_now"))
        except Exception:
            logger.exception("lead dispatch failed: %s", msg.group_id)
            return False

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
    def _is_kf(self, group: GroupProfile) -> bool:
        return bool(group.kf_open_kfid and group.kf_external_userid)

    def _can_send(self, group: GroupProfile) -> bool:
        return bool(self.kf_client) if self._is_kf(group) else bool(self.sender)

    def _reply_webhook(self, group: GroupProfile) -> str:
        """群聊回复地址。

        优先用智能机器人回调随消息下发的会话 webhook——回复由被 @ 的机器人本人发出，
        身份一致，且员工不必手工配置。但它只在分钟级窗口内有效，过期（例如补位等待
        到点才发言、或线索简报隔了很久才补发）就回落到人工配置的群机器人 webhook。
        """
        fresh = (
            group.bot_webhook
            and group.bot_webhook_at
            and (datetime.now() - group.bot_webhook_at).total_seconds()
            < self.settings.bot_webhook_ttl_seconds
        )
        return group.bot_webhook if fresh else group.robot_webhook

    def _send_group(self, group: GroupProfile, group_id: str, text: str) -> bool:
        """分条发送：多句内容拆成多条消息，条间隔模拟打字（见 docs/voice-guide.md）。

        通道优先级：微信客服会话 → 机器人 webhook（回调下发的 > 人工配置的）→ 应用群聊。
        返回是否全部发送成功——失败要能被上层看见，不能静默吞掉。
        """
        webhook = self._reply_webhook(group)
        chunks = (
            sanitize.split_messages(text, self.settings.split_max_parts)
            if self.settings.split_messages
            else [text]
        )
        ok = True
        for i, chunk in enumerate(chunks):
            if i and self.settings.split_delay_seconds > 0:
                time.sleep(self.settings.split_delay_seconds)
            try:
                if self._is_kf(group):
                    r = self.kf_client.send_text(
                        group.kf_open_kfid, group.kf_external_userid, chunk
                    )
                elif webhook:
                    r = self.sender.send_robot_text(webhook, chunk)
                else:
                    r = self.sender.send_group_text(group_id, chunk)
            except Exception:
                logger.exception("send failed: %s", group_id)
                r = False
            # 通道实现返回 None 视为成功（旧签名兼容），只有显式 False 才算失败
            if r is False:
                ok = False
                break  # 首条就发不出去，后续几条只会继续失败并拖慢链路
        return ok

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
            # 二次安抚是在 guard 之后替换文本的，必须自己再过一次出口闸门——
            # 合规护栏「所有 AI 生成文本都要过 guard」不接受任何绕行
            from responder.compliance.guard import guard

            checked = guard(
                templates.second_touch(group, urgent=decision.urgent),
                Action.HANDOFF, templates.safe_fallback(group),
            )
            return checked.text
        decision.urgent = True
        decision.reasons.append("followup:suppressed-escalated")
        return None


def _history_text(history: list[dict]) -> str:
    from responder.reply import prompts

    return prompts.format_history(history)

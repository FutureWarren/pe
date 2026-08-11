"""消息处理主管道：入库 → 分类（规则 + 可选 LLM 复核）→ 门控 → 生成 → 合规 → 发言/草稿 → 提醒。

追问处理（同一群、同一问题类别、接管时间窗内）：
  第 1 次 → 正常话术；第 2 次 → 二次安抚（不复读）；第 3 次起 → 群内静默 + 升级提醒。
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta

from responder import lead, memory
from responder.config import Settings, get_settings
from responder.engine import llm, priority, rules, signals
from responder.engine.decision import decide, wait_seconds
from responder.gateway.sender import WeComSender
from responder.models import (
    Action,
    Category,
    ClientStatus,
    Decision,
    GroupProfile,
    IncomingMessage,
)
from responder.notify import escalation
from responder.reply import sanitize, templates
from responder.reply.generator import generate
from responder.store.db import Store

logger = logging.getLogger(__name__)

# LLM 复核采信阈值：低于此置信度维持规则结果（宁沉默不抢答）
REFINE_CONFIDENCE = 0.7


class Pipeline:
    def __init__(self, store: Store, sender: WeComSender | None = None,
                 settings: Settings | None = None, kf_client=None,
                 douyin_client=None):
        self.store = store
        self.settings = settings or get_settings()
        self._sender = sender
        self._kf_client = kf_client
        self._douyin_client = douyin_client

    # 发送通道按模式实时门控：影子模式一律不出声。
    # 用属性而非构造期固化，是为了支持控制台运行时切换模式（无需重启服务）。
    @property
    def sender(self) -> WeComSender | None:
        return self._sender if self.settings.mode == "live" else None

    @property
    def kf_client(self):
        """「发」受模式门控；「收」用 worker 持有的原始 client，不受此限。"""
        return self._kf_client if self.settings.mode == "live" else None

    @property
    def douyin_client(self):
        """同 kf_client：影子模式只入库草稿，绝不对客户发言。"""
        return self._douyin_client if self.settings.mode == "live" else None

    # ------------------------------------------------------------ 分类
    def _classify(self, msg: IncomingMessage, group: GroupProfile, history: list[dict]) -> tuple:
        """规则分类 + 边界样本 LLM 复核。

        只复核规则判「default-silence」的样本（漏答方向）；高优先级规则命中不交模型改判。
        """
        action, category, urgent, reasons = rules.classify(
            msg.content, msg.msg_type, is_one_on_one=group.is_kf,
            in_consultation=self._in_consultation(group, history),
        )
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
                is_one_on_one=group.is_kf,
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

    def _in_consultation(self, group: GroupProfile, history: list[dict]) -> bool:
        """这通对话已经进入咨询状态了吗。

        进入之后，判据从「命中话题词」放宽为「这是不是一个问句」——
        因为**追问几乎从不重复话题词**：客户开头说了「交通事故」，接着问
        「那我该准备什么材料」「如果对方不赔怎么办」，一个法律词都没有，
        于是全部落进默认沉默。而这些正是筛查最需要的对话：
        客户每答一句，案情就清楚一分，交给客服的那张单就厚一分。

        三个前提：
          1. **一对一进线窗口**——群聊里承办律师在场，AI 是补位不是主答；
          2. **未成交客户**——已委托的客户由律师全程跟，AI 不该替他答本案；
          3. **客户确实把事说出来过**——只打了个招呼就放宽，等于对着
             一句「在吗」大谈法律，那不是热情，是没听懂。
        """
        if not (group.is_kf and group.client_status == ClientStatus.PROSPECT):
            return False
        if self._being_handled(group):
            return False  # 人还在跟，AI 不作答

        return any(
            rules.has_substance(m.get("content", ""))
            for m in history if not m.get("sender_is_staff")
        )

    def _being_handled(self, group: GroupProfile) -> bool:
        """这通对话此刻是不是真的有人在跟。

        判据是「律师**最近**说过话」，不是「转接过」。转接过是个永久状态，
        拿它当判据会让 AI 在律师早就离开之后仍然一言不发——
        真机里客户第二天回来，对着一个死掉的窗口发「你好」。
        """
        if not group.handoff_userid:
            return False
        last = self.store.last_staff_reply_at(group.group_id)
        if last is None:
            return False
        return (datetime.now() - last).total_seconds() < self.settings.takeover_seconds

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
            # 客服/律师在这通对话里说了一句话 = 他接手了。就地记成正式转接。
            # 律所方要的「在企业微信里回一个字就接管」就是这条：不用开控制台、
            # 不用点按钮——他本来就要打字，那句话本身就是接管动作。
            # 只对一对一进线窗口生效：群聊里律师发言是常态，不该把群标记成已转接。
            if group.is_kf and not group.handoff_userid and msg.sender_id:
                self.store.set_handoff(msg.group_id, msg.sender_id)
                logger.info("takeover by reply: %s → %s", msg.group_id, msg.sender_id)
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
        self._avoid_repeat_greeting(decision, msg)

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

        want_contact = self._should_ask_contact(group, decision, convo)
        want_office = (not want_contact) and self._should_invite_office(
            group, decision, convo
        )
        # 承接类回复本身是死胡同（「我帮您问下律师，请您稍等」说完客户只能干等）。
        # 完整邀约还不到时候的，至少轻推一句，别让对话停在没有下一步的地方。
        # 只对一对一窗口做：群聊里承办律师本人在场，「请您稍等」的下一步就是
        # 律师自己会在群里回话，再让 AI 追着要电话既多余又越界。
        want_next_step = (
            not want_contact
            and self.settings.handoff_next_step
            and decision.action == Action.HANDOFF
            and group.is_kf
            and group.client_status == ClientStatus.PROSPECT
            and not signals.scan(convo)[1]
            and not self._recent_marker(group.group_id, templates.ASK_CONTACT_MARKERS)
        )
        want_next_step = want_next_step and not want_office
        self._maybe_intake(msg, decision, group, convo)
        result = generate(
            msg, decision, group, history=history, settings=self.settings,
            include_cta=not self._recent_cta(msg.group_id),
            ask_contact=want_contact, next_step=want_next_step,
            office_invite=want_office,
            knowledge_text=self._recall(msg, decision),
            memory_text=self._customer_memory(group, convo),
        )
        reply_text = None
        if result:
            result.text = self._with_intro(result.text, decision, group)
            mode = "live" if (self.settings.mode == "live" and decision.should_speak) else "shadow"
            final_text = self._apply_followup_policy(
                msg, decision, group, result.text, mode, ask_contact=want_contact,
            )
            if final_text is None:
                # 第 3 次追问起：群内静默，原话术仅留档草稿，提醒已升级
                final_text, mode = result.text, "shadow"
            sent, parts = True, 1
            if mode == "live" and self._can_send(group):
                sent, parts = self._send_group(group, msg.group_id, final_text)
                if not sent:
                    # 发送失败还按 live 记账，追问策略会误判「已经答过了」，
                    # 客户从此拿不到首答。记为 failed，并让人工兜底。
                    mode = "failed"
                    decision.reasons.append("send:failed")
            self.store.save_reply(
                msg.msg_id, msg.group_id, final_text, mode,
                result.passed, category=decision.category.value, parts=parts,
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

    def _avoid_repeat_greeting(self, decision: Decision, msg: IncomingMessage) -> None:
        """一通对话只允许一次开场白，第二次改走承接。就地修改 decision。

        客户扫码进来时已被问候过一次，接着他把自己的事打出来——这类陈述句
        （「公司拖欠我三个月工资，还把我辞退了」）没有问号，规则判不出是不是
        法律问题，模型复核又可能超时/未配置，就会退回开场白，等于当着客户的面
        问他刚说完的事。宁可回一句「收到，我转给律师」：是废话，但不冒犯。

        「收下联系方式」那套话术走的也是 GREETING，但它是应答不是自我介绍，不受此限。
        """
        if decision.category != Category.GREETING or decision.action == Action.SILENCE:
            return
        if "kf:contact-ack" in decision.reasons:
            return
        if not self.store.has_greeting(decision.group_id):
            return
        # 光打了个招呼、没说事的（回访客户最常见的第一句）：改走「再次问候」。
        # 降级成承接在这里是句废话——「我帮您转给律师」，转什么？他什么都没说。
        if rules.is_bare_greeting(msg.content):
            decision.reasons.append("greeting:again")
            return
        decision.action = Action.HANDOFF
        decision.category = Category.OTHER
        decision.reasons.append("greeting:already-sent")

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
        # 冷消息不在这里出单，但**不是被丢掉**：等这通对话安静下来，
        # `worker._sweep_idle_leads` 会补一张完整的单并推给客服
        # （notify_all_leads，业务决策 2026-08）。
        # 当场推的问题是客户才说了一句「在吗」——那张单上什么也没有，
        # 三十秒后他讲完案情还得再推一张。
        if not urgent_kf and signals.detect(msg.content)[0] == signals.COLD:
            # 这一条本身不出单，但**分数要照算**。客户是边聊边变强的：
            # 留了电话（40 分）之后接着问「赔多少」「地址在哪」，
            # 每一句都在加分——旧写法在这里直接 return，于是那些后续加分
            # 一次都没被记进去，客服看到的永远是他刚留电话那一刻的样子。
            # summarize=False：不烧模型，纯规则重算，成本接近零。
            # 走 dispatch 而不是直接重算：这样「弱 → 强」那一跳能当场推给客服，
            # 而那一跳恰恰最可能发生在这类看似平淡的追问里。
            if self.store.get_lead(msg.group_id) is not None:
                try:
                    row = lead.dispatch(
                        self.store, group, convo, self.sender,
                        settings=self.settings, summarize=False,
                    )
                    # 重算出来的分数**也要能触发转接**。原来这里算完就 return，
                    # 于是「客户在一句看似平淡的追问里跨过 P0 门槛」这条路上，
                    # 转接永远不会发生——而那恰恰是最常见的一条路：
                    # 硬信号（受伤/要联系方式/留电话）往往落在冷消息里。
                    if row:
                        self._maybe_handoff(group, row, urgent=False)
                        if row.get("_handoff_skip"):
                            decision.reasons.append(row["_handoff_skip"])
                    return bool(row and row.get("_notified_now"))
                except Exception:
                    logger.exception("lead rescore failed: %s", msg.group_id)
            return False
        try:
            # 用门控后的 sender：影子模式只入库不外发（与律师提醒口径一致）
            row = lead.dispatch(
                self.store, group, convo, self.sender,
                settings=self.settings, force=urgent_kf, urgent=decision.urgent,
            )
            if row:
                self._maybe_handoff(group, row, urgent=decision.urgent)
                if row.get("_handoff_skip"):
                    decision.reasons.append(row["_handoff_skip"])
            return bool(row and row.get("_notified_now"))
        except Exception:
            logger.exception("lead dispatch failed: %s", msg.group_id)
            return False

    def _maybe_handoff(self, group: GroupProfile, lead_row: dict, *, urgent: bool) -> bool:
        """强意愿线索 → 把会话直接转给分到的律师（见 docs/kf-handoff.md）。

        为什么值得做：现在的链路最后一步是「律师打电话给客户」，那是最脆的一环——
        陌生号码接通率本来就低，何况客户往往正在上班、开庭，或正因为被辞退而
        不敢接陌生电话。转接把这一步整个删掉：律师在原窗口接着聊，上下文全在。

        六个前提缺一不可，少一个就回落到现有链路（交接单已经推过了，不会有人掉队）：
          1. 开关打开；
          2. 微信客服会话——抖音侧接待走 AI即用，没有对等能力；
          3. 本会话尚未转接过（转两次没有意义，且会把 SLA 计时打乱）；
          4. 够格：P0 或紧急。别放宽到 P1/P2——一周 416 人进私信，
             全转过去律师什么也别干了；
          5. 已经派给了具体律师（分案引擎的结论，转接只是兑现它）；
          6. 那位律师确实是这个客服账号的接待人——不是的话企微直接拒，
             白白让客户等一场空。
        """
        s = self.settings
        client = self.kf_client  # 受模式门控：影子模式绝不真的转

        def _skip(reason: str) -> bool:
            """记下**为什么没转**。

            这六道前提原来是六个静默的 return：转不成就悄悄回落原链路，
            控制台里什么都看不出来，律所方只能问「那怎么会没有自动转接呢」，
            而我只能一条条猜。判断日志本来就是为这种时刻存在的——
            把原因写进去，下次一眼可查（控制台「为什么没回复」也读这一条）。
            """
            decision_reason = f"handoff:skip({reason})"
            logger.info("handoff skipped: %s — %s", group.group_id, reason)
            self.store.set_note(f"handoff_skip:{group.group_id}", reason)
            lead_row["_handoff_skip"] = decision_reason
            return False

        if not s.handoff_enabled:
            return _skip("转接开关关着")
        if client is None:
            return _skip("影子模式或微信客服通道未配置，不真的转")
        if not group.is_kf or group.is_douyin:
            return _skip("不是微信客服会话（抖音/群聊没有对等能力）")
        if group.handoff_userid:
            return _skip(f"这通对话已经转给过 {group.handoff_userid}，不重复转")
        # 清单制：客户做出「想找真人」的动作才转（业务决策 2026-08-09，
        # 见 priority.WANTS_HUMAN）。不看分数——分数是排队用的，不是开关。
        try:
            hits = json.loads(lead_row.get("signals") or "[]")
        except (ValueError, TypeError):
            hits = []
        # 冷消息那条重算路径拿不到本轮的 urgent，但线索上记着——别丢了它
        is_urgent = urgent or lead_row.get("urgency") == "high"
        trigger = priority.wants_human(hits, urgent=is_urgent)
        if not trigger:
            return _skip("客户还没做出「想找真人」的动作（只是问问题 / 问收费 / 打招呼）")
        target = lead_row.get("assigned_userid") or ""
        if not target:
            return _skip("这条线索还没派给具体律师（名册为空或都不在班？）")
        try:
            servicers = set(client.servicer_list(group.kf_open_kfid))
        except Exception:
            logger.exception("handoff servicer check failed: %s", group.group_id)
            return _skip("查不到该客服账号的接待人名单")
        if target not in servicers:
            return _skip(
                f"{target} 不在客服账号「{group.kf_open_kfid}」的接待人名单里"
                "（去控制台「状态」页点「把名册律师加为接待人」）"
            )

        # 先跟客户说一句再转，否则律师还没看到的这段时间里客户对着静默。
        # 不点名（业务决策 2026-08，见 CLAUDE.md）：这里更不能点——客户读到名字
        # 就会等那个人，万一律师临时改派或没接手，等的就是一个不会出现的人。
        text = templates.handing_over(seed=group.group_id)
        from responder.compliance.guard import guard

        checked = guard(text, Action.HANDOFF, templates.safe_fallback(group))
        sent, _parts = self._send_group(group, group.group_id, checked.text)
        if not sent:
            # 这句话没送到，就绝不能转。转了之后 `gate:handed-off` 让 AI 闭嘴，
            # 而客户那头**从头到尾一个字都没收到**——他会以为没人在，然后走掉。
            # 宁可不转：交接单已经推给律师了，他还能打电话。
            return _skip("过渡话术没发出去（通道异常），不敢转——转了客户会对着空窗口")

        if not client.transfer(group.kf_open_kfid, group.kf_external_userid, target):
            # 转不过去就当没转：交接单已经推给律师了，他还能打电话，客户不会掉队
            logger.warning("handoff transfer failed, 回落原链路: %s", group.group_id)
            return _skip("企微拒绝了转接请求（接口返回失败）")
        self.store.set_handoff(group.group_id, target)
        self.store.save_reply(
            f"handoff-{group.group_id}-{int(time.time())}", group.group_id,
            checked.text, "live", checked.passed, category="handoff",
        )
        logger.info("handoff: %s → %s（%s）", group.group_id, target, trigger)
        self.store.set_note(f"handoff_skip:{group.group_id}", f"已转给 {target}：{trigger}")
        return True

    def _recall(self, msg: IncomingMessage, decision: Decision) -> str:
        """检索本所既定口径，注入这一轮的回答（见 responder/memory.py）。

        只在**直接回答**时检索：承接类走确定性模板，不进模型，注了也没人读。
        只用 approved 条目——草稿是机器提炼或刚导入的，没经人审的话术不能对客户生效
        （CLAUDE.md 合规护栏）。

        检索不到就返回空串，整段不出现在 prompt 里。塞一条不相关的知识
        比不塞更糟：模型会努力把它用上，于是答非所问，而且答得理直气壮。
        """
        if decision.action != Action.ANSWER or not self.settings.knowledge_enabled:
            return ""
        try:
            entries = self.store.list_knowledge(status="approved")
            hits = memory.search(entries, msg.content, limit=self.settings.knowledge_top_k)
            if not hits:
                return ""
            # 记下哪几条真被用到：用不上的条目该被清掉，而不是越攒越多
            self.store.bump_knowledge_hits([h["id"] for h in hits])
            decision.reasons.append(f"kb:{','.join(str(h['id']) for h in hits)}")
            return memory.format_for_prompt(hits)
        except Exception:
            logger.exception("knowledge recall failed: %s", msg.group_id)
            return ""  # 知识库出问题不能拖垮回复——没有它照样能答

    def _customer_memory(self, group: GroupProfile, convo: list[dict]) -> str:
        """回访客户才注入上次的情况（见 responder/memory.py）。

        **只在回访时给**：同一通对话里完整历史本来就在上下文里，再塞一遍
        既占地方又可能让模型把「上次」和「刚才」搞混。
        判据是本次会话的消息条数——刚开口一两句就说明这是新的一轮。

        不给已委托客户：他的事律师全程在跟，AI 复述一段旧摘要只会显得多余。
        """
        if not (self.settings.knowledge_enabled and group.is_kf and group.memory):
            return ""
        if group.client_status != ClientStatus.PROSPECT:
            return ""
        gap = self.settings.lead_session_gap_seconds
        recent = [
            m for m in convo
            if not m.get("sender_is_staff") and m.get("msg_type") != "event"
        ]
        # 本次会话已经聊开了（超过两句）就不必再提上次——上下文里有的是内容
        if len(recent) > 2:
            return ""
        # 「上一条客户发言」必须**排除刚进来的这一条**。
        # 原来查的是 last_customer_message_at()，而 handle() 一进门就把当前消息
        # 存了库——于是间隔恒等于零，永远判成「还在同一通对话里」，
        # 客户记忆这一整层从上线起一次都没注入过。功能在、数据在、就是不生效，
        # 而且没有任何迹象：日志里看不出，控制台里也看不出。
        if len(recent) < 2:
            return ""  # 这是他有史以来第一句话，没有「上次」可言
        try:
            prev = datetime.fromisoformat(recent[-2]["created_at"])
        except (KeyError, ValueError, TypeError):
            return ""
        if (datetime.now() - prev).total_seconds() < gap:
            return ""  # 还在同一通对话里
        return memory.format_customer_memory(group.memory)

    def _with_intro(self, text: str, decision: Decision, group: GroupProfile) -> str:
        """本通对话的第一条回复补一句自报家门。

        开场白话术自带律所全称，但客户一进来就直接说事时走的是承接/追问路径，
        那句就丢了——对面不知道在跟谁说话。补一行，不改正文。
        """
        if not group.is_kf or decision.category == Category.GREETING:
            return text  # 开场白话术里本来就有全称，别报两遍
        if decision.urgent:
            return text  # 人正急着（拘留/开庭），先安抚，自我介绍往后放
        if self.store.has_greeting(group.group_id):
            return text  # 本会话已经开过口，不再报第二遍全称
        return templates.intro_line(self.settings) + "\n" + text

    def _maybe_intake(
        self, msg: IncomingMessage, decision: Decision, group: GroupProfile,
        convo: list[dict],
    ) -> None:
        """客户第一次把自己的事说出来 → 改走追问，别回泛泛承接。就地修改 decision。

        真机测试的原话：「显得非常的笨」。客户说「我遇到劳务仲裁的问题，拖欠工资」，
        AI 回「看到您消息了，这个我帮您转给律师确认下」——他刚把事情交出来，
        换回一句套话。这一刻是整通对话里信息量最大的一刻，接住它的方式是追问。

        只在一对一进线窗口做：群聊里承办律师本人在场，AI 追着问情况是越界。
        只做一次：问第二遍就成了查户口。
        已经留了联系方式的不问：那时候该做的是把人交出去，不是继续采集。
        """
        if not group.is_kf:
            return
        # 2026-08-10：判断层放开之后，「公司拖欠我三个月工资」这类**陈述句**
        # 从承接改判成了直接作答，于是这条追问再也不触发——客户第一次把事
        # 交出来，换回的是一段泛泛的法律框架。
        #
        # 分界线是**他在陈述还是在提问**：
        #   陈述（「公司拖欠我三个月工资」）→ 接着问，这时候我们几乎什么都不知道，
        #     一个好问题的信息量远大于一段好答案；
        #   提问（「醉驾一般判多久」）→ 正面回答，追问他「这事什么时候开始的」
        #     等于答非所问。
        probing_a_statement = False
        if decision.action == Action.ANSWER:
            if rules.QUESTION_MARK.search(msg.content):
                return
            probing_a_statement = True
        elif decision.action != Action.HANDOFF:
            return
        elif decision.category not in (Category.OTHER, Category.CASE_STATUS):
            return
        if group.client_status != ClientStatus.PROSPECT or group.handoff_userid:
            return
        # 已经留了联系方式的不再追问要电话——追问话术里带着「留个手机号」，
        # 对一个刚给过号码的人再问一次，就是在证明没人在听
        if signals.scan(convo)[1]:
            return
        # 太短的消息没有可追问的内容（「嗯」「好的」），泛泛追问反而更像机器人
        if len(_norm(msg.content)) < 6:
            return
        if self._recent_marker(group.group_id, templates.INTAKE_MARKERS, limit=20):
            return
        # 追问里那句「留个手机号」受同一条业务规则约束：聊够了才开口。
        # 客户刚说第一句就被问号码，像推销；刚问过又问，像催单。两种都要让。
        spoken = sum(
            1 for m in convo
            if not m.get("sender_is_staff") and m.get("msg_type") != "event"
            and (m.get("content") or "").strip()
        )
        threshold = self.settings.ask_contact_after_messages
        quiet = (
            threshold <= 0  # 0 = 整条「主动要电话」的收口动作被关掉了
            or spoken < threshold
            or self._recent_marker(group.group_id, templates.ASK_CONTACT_MARKERS)
        )
        decision.reasons.append("kf:intake-quiet" if quiet else "kf:intake")
        if probing_a_statement:
            # 追问是**承接**，不是作答：话术走 templates.intake_probe，
            # 而生成层是按 action 分派的。不改这一下，理由挂上去也没人读。
            decision.action = Action.HANDOFF
            decision.category = Category.OTHER

    def _is_repeat_message(self, msg: IncomingMessage) -> bool:
        """这条消息客户刚刚发过一模一样的（标点/空格差异不算）。

        真的把同一句话再发一遍，就是在催了——那时候「我又催了一下」才对题。
        比对的是客户自己的历史消息，不是类别：类别相同的两个不同问题不算重复。
        """
        cur = _norm(msg.content)
        if not cur:
            return False
        # 本条消息此刻已经入库，也在这份历史里——所以「重复」的判据是出现 ≥2 次
        seen = 0
        for m in self.store.recent_messages(msg.group_id, limit=8):
            if m.get("sender_is_staff") or m.get("msg_type") == "event":
                continue
            if _norm(m.get("content", "")) == cur:
                seen += 1
        return seen >= 2

    def _recent_cta(self, group_id: str) -> bool:
        """接管时间窗内该群是否已发过带面谈引导/收尾语的回复——有则本次不再带（防套路感）。"""
        return self._recent_marker(group_id, templates.CTA_MARKERS)

    def _recent_marker(self, group_id: str, markers: tuple[str, ...], limit: int = 6) -> bool:
        """接管时间窗内最近几条实发回复里是否出现过某类话术标记。"""
        for r in self.store.list_replies(group_id, limit=limit):
            if r["mode"] != "live":
                continue
            age = (datetime.now() - datetime.fromisoformat(r["created_at"])).total_seconds()
            if age >= self.settings.takeover_seconds:
                break
            if any(m in r["text"] for m in markers):
                return True
        return False

    def _should_ask_contact(
        self, group: GroupProfile, decision: Decision, convo: list[dict]
    ) -> bool:
        """这一轮该不该开口要电话 + 邀约到所面谈。

        首轮筛查的收口动作：线上只能给一般性框架，真要把事办了必须落到
        「谁跟进、怎么找到他」。抖音后台数据摆在那儿——90 个开口的人里只有 50 个留资，
        不主动开口要，剩下那四成聊完就走了。

        六个前提缺一不可：
          1. 一对一进线窗口（客服/抖音私信）——群聊里承办律师本人在场，
             AI 再追着要电话既多余又越界；何况新咨询本来就全从一对一进来；
          2. 未成交客户（已委托的客户电话我们本来就有，再问一遍很怪）；
          3. 聊够了（默认第 2 条客户发言）——太早像推销，太晚人已经走了；
          4. 通篇还没出现过联系方式（客户自己留了就不必再要）；
          5. 不是开场白那一轮（刚打上照面就问电话，人只会退出去）；
          6. 接管时间窗内没做过完整邀约（同一通对话邀第二遍就成了催单）。

        注意第 6 条比的是**邀约**标记而不是「手机号」：承接回复里的轻推
        （「留个手机号也行」）不该挡住这一步——完整邀约多出的是所址和面谈邀请，
        是新信息，属于正常升级。

        但**紧挨着的上一条**刚问过电话就得让一让（第 7 条）：升级是隔一轮再进一步，
        连着两条都在要号码，读起来就是催单，不是引导。
        """
        threshold = self.settings.ask_contact_after_messages
        if threshold <= 0 or not group.is_kf:
            return False
        if group.client_status != ClientStatus.PROSPECT:
            return False
        if decision.category == Category.GREETING:
            return False
        # 事件占位（进线事件）不是发言，不计入「聊了几句」
        spoken = [
            m for m in convo
            if not m.get("sender_is_staff")
            and m.get("msg_type") != "event"
            and (m.get("content") or "").strip()
        ]
        if len(spoken) < threshold:
            return False
        # **得先听懂他的事，再开口要电话。** 律所方实测原话：「AI 应该在了解完
        # 基础信息之后，再去引导客户留联系方式」。客户第二句话刚问完一个法律
        # 问题就被要号码，那不像接待，像推销——而推销的第一反应是关掉窗口。
        # 判据是「他把事说出来过」，不是「他说够两句了」：两句寒暄也能凑够数。
        if not any(
            rules.has_substance(m.get("content", ""))
            for m in spoken
        ):
            return False
        if signals.scan(convo)[1]:
            return False
        # 同一个接管窗口里只要一次号码。问第二遍就是催单，
        # 而催单是客户判断「对面是不是机器人」最强的一条线索。
        return not self._recent_marker(group.group_id, templates.ASK_CONTACT_MARKERS)

    def _should_invite_office(
        self, group: GroupProfile, decision: Decision, convo: list[dict]
    ) -> bool:
        """该不该邀他来所里当面聊。

        **和要电话分开、并且晚一步。** 原来这两件事挤在同一条消息里
        （「留个手机号吧……当面聊也可以，地址是××路 88 号平高广场 11 楼」），
        律所方实测的原话是「这一长串的说话方式，让客户一看就会觉得这是不是 AI」。
        真人不会在刚听完一句话之后，把电话和地址一口气报出来。

        所以拆成两拍：先只要电话；号码还是没留、而他仍在聊，才补一句所址。
        客户自己表达了想见面的（`meeting` 信号）也直接给。
        """
        if not group.is_kf or group.client_status != ClientStatus.PROSPECT:
            return False
        if decision.category == Category.GREETING:
            return False
        if signals.scan(convo)[1]:
            return False  # 号码已经有了，该打电话而不是请人跑一趟
        if self._recent_marker(group.group_id, templates.OFFICE_INVITE_MARKERS):
            return False  # 邀第二遍就成了催
        hits = signals.scan(convo)[2]
        if "meeting" in hits:
            return True  # 他自己说想来，那就别绕
        # 否则：得是「已经问过号码但他没给」的那一步
        return self._recent_marker(group.group_id, templates.ASK_CONTACT_MARKERS)

    # ------------------------------------------------------------ 发送
    def _is_kf(self, group: GroupProfile) -> bool:
        return bool(group.kf_open_kfid and group.kf_external_userid)

    def _can_send(self, group: GroupProfile) -> bool:
        if group.is_douyin:
            return bool(self.douyin_client)
        return bool(self.kf_client) if self._is_kf(group) else bool(self.sender)

    def _douyin_budget(self, group_id: str) -> int:
        """抖音本轮还能发几条平台消息。0 = 一条都不能发。

        两条平台硬限制合成一个数（见 gateway/douyin.py 顶部注释）：
          ① 客户最后一次发言起 24 小时内才允许回复，超时接口直接拒；
          ② 该窗口内、客户下次开口之前，最多 6 条。

        算准这个数是这条通道能不能长期活着的关键：超发不是「多发了一条」，
        是接口报错 + 应用被平台标记。宁可少说一句，也不要把通道打死。
        """
        s = self.settings
        last = self.store.last_customer_message_at(group_id)
        if last is None:
            return 0  # 客户没开过口 → 平台不允许我们主动发起
        if (datetime.now() - last).total_seconds() > s.douyin_reply_window_seconds:
            return 0
        used = self.store.sent_parts_since(group_id, last)
        return max(0, s.douyin_max_parts_per_window - used)

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

    def _send_group(
        self, group: GroupProfile, group_id: str, text: str
    ) -> tuple[bool, int]:
        """分条发送：多句内容拆成多条消息，条间隔模拟打字（见 docs/voice-guide.md）。

        通道优先级：抖音私信 → 微信客服会话 → 机器人 webhook（回调下发的 > 人工配置的）
        → 应用群聊。

        返回 (是否全部发送成功, 实际发出的条数)。条数要往上传：抖音按条限额，
        记不准就算不准剩余配额（见 _douyin_budget）。
        """
        webhook = self._reply_webhook(group)
        max_parts = self.settings.split_max_parts
        if group.is_external:
            # 外部渠道（RPA 搬运）没有推送接口：话排进发件箱，等那头来取。
            # **不在这里等那头**——它可能正卡在一个更新弹窗上，而这个函数
            # 跑在消息处理链里，一等就把整条链堵死。
            chunks = (
                sanitize.split_messages(text, max_parts)
                if self.settings.split_messages
                else [text]
            )
            n = self.store.queue_outbound(
                group_id, chunks, channel=group.ext_channel,
            )
            return n > 0, n
        if group.is_douyin:
            # 抖音先按配额收敛，再按话术拆条——顺序反了会拆出发不出去的尾巴
            budget = self._douyin_budget(group_id)
            if budget <= 0:
                logger.warning("douyin quota exhausted, skip send: %s", group_id)
                return False, 0
            max_parts = min(max_parts, self.settings.douyin_split_max_parts, budget)
        chunks = (
            sanitize.split_messages(text, max_parts)
            if self.settings.split_messages
            else [text]
        )
        ok, sent = True, 0
        for i, chunk in enumerate(chunks):
            if i and self.settings.split_delay_seconds > 0:
                time.sleep(self.settings.split_delay_seconds)
            try:
                if group.is_douyin:
                    r = self.douyin_client.send_text(group.douyin_open_id, chunk)
                elif self._is_kf(group):
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
            sent += 1
        return ok, sent

    # ------------------------------------------------------------ 追问策略
    def _apply_followup_policy(
        self, msg: IncomingMessage, decision: Decision, group: GroupProfile,
        text: str, mode: str, *, ask_contact: bool = False,
    ) -> str | None:
        """返回实际要发的文本；None 表示本次群内静默（仅升级提醒）。

        判据：接管时间窗内，同一群同一问题类别已实际发出过几条回复。
        """
        if mode != "live":
            return text
        # 问候不适用追问去重：打招呼不是「又问了一遍同一个问题」。
        # 套上去会变成对一句「你好」回「抱歉让您久等了，我刚又催了一下」——
        # 催什么？他还没问呢。
        if decision.category == Category.GREETING:
            return text
        n = self.store.count_recent_live(
            msg.group_id, decision.category.value, self.settings.takeover_seconds
        )
        if n == 0:
            return text
        # 同类别 ≠ 同一个问题。客户接连问了两个费用问题，第二个被当成「又问了一遍」，
        # 于是回「抱歉让您久等了，我刚又催了一下」——他没在等，他在问。
        # 真机测试实测到的答非所问，比复读更伤客户。
        # 「又问了一遍」只有两种：他在催（催回复/在吗），或者他真的把同一句话再发了一次。
        if not (
            rules.is_chasing(msg.content, decision.category)
            or self._is_repeat_message(msg)
        ):
            decision.reasons.append("followup:new-question-same-category")
            return text
        if n == 1:
            decision.reasons.append("followup:second-touch")
            # 二次安抚整段替换原文，generate() 拼好的索要联系方式也一并被丢掉了，
            # 得在这儿补回来——客户第二次追问同一件事，正是最该把他接到电话上的时候。
            body = templates.second_touch(group, urgent=decision.urgent)
            if ask_contact:
                body += "\n" + templates.ask_contact(
                    group, seed=msg.msg_id, settings=self.settings
                )
            # 二次安抚是在 guard 之后替换文本的，必须自己再过一次出口闸门——
            # 合规护栏「所有 AI 生成文本都要过 guard」不接受任何绕行
            from responder.compliance.guard import guard

            checked = guard(body, Action.HANDOFF, templates.safe_fallback(group))
            return checked.text
        # **不要在这里改 decision.urgent。** 这里的「紧急」只是想让升级提醒
        # 走加急通道，可 decision 随后要交给线索评分——urgent 会加 25 分、
        # 把 urgency 拍成 high、并让优先级直接置 P0，于是客户只是把同一句话
        # 说了三遍，客服就收到一张【强意愿】·紧急·1 小时内联系 的假单，
        # 还顺带解锁了会话转接。用一个独立标记，别污染判断本身。
        #
        # **一对一进线窗口不静默。** 静默这条规矩是为群聊写的：那里承办律师
        # 在场，AI 说到第三遍就成了刷屏。而进线窗口里没有别人——
        # 客户问了三遍还是这句话，说明他**越来越急**，这时候闭嘴是最坏的回应，
        # 他下一步就是关掉窗口走人。改成换一句说法 + 给一个此刻能做的事。
        if group.is_kf and group.client_status == ClientStatus.PROSPECT:
            decision.reasons.append("followup:third-touch-kf")
            from responder.compliance.guard import guard

            body = templates.third_touch(group, seed=msg.msg_id, settings=self.settings)
            return guard(body, Action.HANDOFF, templates.safe_fallback(group)).text
        decision.reasons.append("followup:suppressed-escalated")
        return None


_PUNCT = re.compile(r"[\s，。！？、,.!?~～…]+")


def _norm(text: str) -> str:
    return _PUNCT.sub("", (text or "").strip())


def _history_text(history: list[dict]) -> str:
    from responder.reply import prompts

    return prompts.format_history(history)

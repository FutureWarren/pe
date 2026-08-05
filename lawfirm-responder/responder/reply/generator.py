"""回复生成：模板为骨架，Claude 生成一般性法律框架为可选增强。

链路：LLM 正文（可选）→ sanitize 形态净化 → templates 结构拼装 → compliance.guard 出口闸门。
所有输出（含影子模式草稿）一律经过闸门；LLM 任一环节失败即确定性降级，绝不空转。
"""

from datetime import datetime

from responder.compliance.guard import GuardResult, guard
from responder.config import Settings, get_settings
from responder.engine import llm
from responder.models import (
    Action,
    Category,
    ClientStatus,
    Decision,
    GroupProfile,
    IncomingMessage,
)
from responder.reply import prompts, sanitize, templates

_STATUS_LABEL = {
    ClientStatus.SIGNED: "已委托客户",
    ClientStatus.PROSPECT: "咨询客户（尚未委托）",
}


def _llm_answer_body(
    msg: IncomingMessage,
    group: GroupProfile,
    history: list[dict],
    settings: Settings,
    now: datetime,
) -> str | None:
    """取模型生成的正文并净化；不可用/失败/示弱/净化判废 → None。"""
    if not settings.llm_answer_enabled:
        return None
    body = llm.generate_answer_body(
        msg.content,
        case_type=group.case_type,
        client_status_label=_STATUS_LABEL[group.client_status],
        case_stage=group.case_stage,
        history_text=prompts.format_history(history),
        is_night=now.hour >= 22 or now.hour < 6,
        is_one_on_one=group.is_kf,
        max_tokens=settings.llm_max_tokens_answer,
        timeout=settings.llm_timeout_seconds,
        settings=settings,
    )
    if body is None or sanitize.is_unusable(body):
        return None
    cleaned = sanitize.sanitize(body, max_chars=settings.answer_max_chars)
    return cleaned or None


def generate(
    msg: IncomingMessage,
    decision: Decision,
    group: GroupProfile,
    *,
    history: list[dict] | None = None,
    require_disclaimer: bool | None = None,
    include_cta: bool = True,
    ask_contact: bool = False,
    next_step: bool = False,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> GuardResult | None:
    """按判断结果生成回复文本并过合规闸门。SILENCE 返回 None。

    require_disclaimer 缺省时读取运行配置（当前业务决策默认关闭）。
    ask_contact=True 时在正文后追加「留个电话 / 欢迎到所面谈」的收口话术，
    并顶掉泛泛的面谈引导（两者同时出现会变成两段意思重复的收尾）。
    由管道判定时机（见 service.Pipeline._should_ask_contact）。
    """
    if decision.action == Action.SILENCE:
        return None

    settings = settings or get_settings()
    now = now or datetime.now()
    history = history or []
    if require_disclaimer is None:
        require_disclaimer = settings.disclaimer_required

    fallback = templates.safe_fallback(group)

    def _close(text: str) -> str:
        """收口：把下一步引导接在正文之后（整体再过闸门）。

        两档，互斥：ask_contact 是完整邀约（留电话 + 到所面谈），
        next_step 是轻推一句。都不带的回复等于死胡同——客户看完只能干等，
        这是转化上最贵的沉默（业务决策 2026-08）。
        """
        if ask_contact:
            decision.reasons.append("cta:ask-contact")
            return text + "\n" + templates.ask_contact(
                group, seed=msg.msg_id, settings=settings
            )
        if next_step:
            decision.reasons.append("cta:next-step")
            return text + "\n" + templates.next_step(group, seed=msg.msg_id)
        return text

    # 客服开场引导：确定性话术，不含法律实质内容，不进模型。
    # 开场白不接索要电话——刚打上照面就问电话，客户只会退出去。
    if decision.category == Category.GREETING:
        if "greeting:again" in decision.reasons:
            # 老客户回来打招呼：接住并问近况，不把律所全称再报一遍
            text = templates.greeting_again(group, seed=msg.msg_id)
        else:
            text = templates.greeting_opener(
                group, seed=msg.msg_id,
                contact_left="kf:contact-ack" in decision.reasons,
            )
        return guard(text, Action.ANSWER, fallback)

    if decision.action == Action.HANDOFF:
        # 两类消息有专属答法，且都自带下一步，不再套 _close（会变成问两遍电话）
        if "identity-question" in decision.reasons:
            return guard(
                templates.who_we_are(group, seed=msg.msg_id), Action.HANDOFF, fallback
            )
        if "kf:intake" in decision.reasons or "kf:intake-quiet" in decision.reasons:
            # 追问本身就是下一步，不再套 _close（问完三句再问电话就成了查户口）
            return guard(
                templates.intake_probe(
                    group, seed=msg.msg_id, settings=settings,
                    ask_phone="kf:intake-quiet" not in decision.reasons,
                ),
                Action.HANDOFF, fallback,
            )
        if "want-lawyer-contact" in decision.reasons:
            return guard(
                templates.exchange_contact(group, seed=msg.msg_id, settings=settings),
                Action.HANDOFF, fallback,
            )
        text = _close(templates.build_handoff(decision.category, group, seed=msg.msg_id))
        return guard(text, Action.HANDOFF, fallback)

    # ANSWER：优先 Claude 生成一般性框架；失败/示弱时确定性降级为承接式回答
    body = _llm_answer_body(msg, group, history, settings, now)
    if body:
        text = templates.answer_scaffold(
            group, body,
            include_disclaimer=require_disclaimer,
            opening=templates.answer_opening(msg.content, now),
            include_cta=include_cta and not (ask_contact or next_step),
            seed=msg.msg_id,
        )
    else:
        decision.reasons.append("answer:fallback-no-llm")
        text = templates.answer_without_llm(
            group,
            include_disclaimer=require_disclaimer,
            include_cta=include_cta and not (ask_contact or next_step),
        )
    return guard(
        _close(text), Action.ANSWER, fallback, require_disclaimer=require_disclaimer
    )

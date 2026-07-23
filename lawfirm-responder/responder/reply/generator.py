"""回复生成：模板为骨架，Claude 生成一般性法律框架为可选增强。

链路：LLM 正文（可选）→ sanitize 形态净化 → templates 结构拼装 → compliance.guard 出口闸门。
所有输出（含影子模式草稿）一律经过闸门；LLM 任一环节失败即确定性降级，绝不空转。
"""

from datetime import datetime

from responder.compliance.guard import GuardResult, guard
from responder.config import Settings, get_settings
from responder.engine import llm
from responder.models import Action, ClientStatus, Decision, GroupProfile, IncomingMessage
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
    settings: Settings | None = None,
    now: datetime | None = None,
) -> GuardResult | None:
    """按判断结果生成回复文本并过合规闸门。SILENCE 返回 None。

    require_disclaimer 缺省时读取运行配置（当前业务决策默认关闭）。
    """
    if decision.action == Action.SILENCE:
        return None

    settings = settings or get_settings()
    now = now or datetime.now()
    history = history or []
    if require_disclaimer is None:
        require_disclaimer = settings.disclaimer_required

    fallback = templates.safe_fallback(group)

    if decision.action == Action.HANDOFF:
        text = templates.build_handoff(decision.category, group, seed=msg.msg_id)
        return guard(text, Action.HANDOFF, fallback)

    # ANSWER：优先 Claude 生成一般性框架；失败/示弱时确定性降级为承接式回答
    body = _llm_answer_body(msg, group, history, settings, now)
    if body:
        text = templates.answer_scaffold(
            group, body,
            include_disclaimer=require_disclaimer,
            opening=templates.answer_opening(msg.content, now),
            include_cta=include_cta,
            seed=msg.msg_id,
        )
    else:
        decision.reasons.append("answer:fallback-no-llm")
        text = templates.answer_without_llm(
            group, include_disclaimer=require_disclaimer, include_cta=include_cta
        )
    return guard(text, Action.ANSWER, fallback, require_disclaimer=require_disclaimer)

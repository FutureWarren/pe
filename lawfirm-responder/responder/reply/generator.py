"""回复生成：模板为主，Claude 生成一般性法律框架为可选增强。

所有输出（含影子模式草稿）一律经过 compliance.guard 出口闸门。
"""

import os

from responder.compliance.guard import GuardResult, guard
from responder.models import Action, Decision, GroupProfile, IncomingMessage
from responder.reply import templates

_ANSWER_SYSTEM = """你是律所企业微信客户群的智能助理，为客户的通用法律知识问题提供一般性说明。
硬性要求：
- 只给一般性法律框架：法条依据、一般区间、关键影响因素；绝不针对提问者的具体案件下结论
- 不承诺结果、不预测判决、不提及任何费用金额、不评价对方当事人或司法机关、不催促任何法律行为
- 白话、克制，2–4 句，群聊场景不写小作文
- 只输出正文本身，不要问候语、不要收尾、不要免责声明（结构由系统统一拼装）"""


def _llm_answer_body(question: str, case_type: str) -> str | None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic

        client = anthropic.Anthropic()
        response = client.messages.create(
            model=os.environ.get("RESPONDER_CLAUDE_MODEL", "claude-opus-4-8"),
            max_tokens=600,
            system=_ANSWER_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": f"客户群案件类型：{case_type or '未标注'}\n客户问题：{question}",
                }
            ],
        )
        if response.stop_reason == "refusal":
            return None
        return next((b.text for b in response.content if b.type == "text"), None)
    except Exception:
        return None


def generate(msg: IncomingMessage, decision: Decision, group: GroupProfile) -> GuardResult | None:
    """按判断结果生成回复文本并过合规闸门。SILENCE 返回 None。"""
    if decision.action == Action.SILENCE:
        return None

    fallback = templates.safe_fallback(group)

    if decision.action == Action.HANDOFF:
        text = templates.build_handoff(decision.category, group)
        return guard(text, Action.HANDOFF, fallback)

    # ANSWER：优先 Claude 生成一般性框架，未配置/失败时确定性降级为承接式回答
    body = _llm_answer_body(msg.content, group.case_type)
    if body:
        text = templates.answer_scaffold(group, body)
    else:
        text = templates.answer_without_llm(group)
    return guard(text, Action.ANSWER, fallback)

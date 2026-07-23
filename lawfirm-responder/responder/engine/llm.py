"""可选的 Claude 复核层：仅用于规则引擎判为沉默/通用的边界样本二次确认。

- 未配置 ANTHROPIC_API_KEY 时完全跳过（纯规则路径可独立运行与验证）。
- 高优先级规则命中（紧急/报价/案件特定）不交给模型改判——合规层级硬编码优先。
- 模型输出走结构化 JSON，解析失败或 API 异常时回退规则结果。
"""

import json
import os

from responder.models import Action, Category

_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["answer", "handoff", "silence"]},
        "category": {
            "type": "string",
            "enum": [
                "general_law",
                "case_status",
                "fee",
                "urgent",
                "contact",
                "chitchat",
                "other",
            ],
        },
        "reason": {"type": "string"},
    },
    "required": ["action", "category", "reason"],
    "additionalProperties": False,
}

_SYSTEM = (
    "你是律所企业微信客户群 AI 助手的消息分类器。对一条客户群消息给出三分类：\n"
    "- answer: 通用法律知识问题，不需要本案具体信息即可给一般性法律框架\n"
    "- handoff: 需要承办律师核实的案件特定问题、任何费用/报价话题、"
    "紧急情形（拘留/传唤/开庭临近/情绪崩溃/投诉）\n"
    "- silence: 闲聊、表情、致谢、客户之间的对话、非指向律所的消息\n"
    "规则：涉及费用/报价一律 handoff；提及「我的案子」等自指一律 handoff；"
    "拿不准时倾向 silence（AI 是补位，不抢答）。"
)


def llm_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def refine(content: str, model: str) -> tuple[Action, Category, str] | None:
    """返回模型分类结果，不可用或失败时返回 None（调用方回退规则结果）。"""
    if not llm_available():
        return None
    try:
        import anthropic

        client = anthropic.Anthropic()
        response = client.messages.create(
            model=model,
            max_tokens=256,
            system=_SYSTEM,
            output_config={"format": {"type": "json_schema", "schema": _CLASSIFY_SCHEMA}},
            messages=[{"role": "user", "content": f"群消息：{content}"}],
        )
        if response.stop_reason == "refusal":
            return None
        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)
        return Action(data["action"]), Category(data["category"]), data["reason"]
    except Exception:
        return None

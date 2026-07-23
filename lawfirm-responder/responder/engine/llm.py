"""Claude 调用层：分类复核 + 回答生成。

原则：
- 未配置 ANTHROPIC_API_KEY 时所有函数快速返回 None，纯规则/模板路径可独立运行与验证
- 高优先级规则命中（紧急/报价/案件特定/自指）不交给模型改判——合规层级硬编码优先，
  模型只复核规则判「沉默」的边界样本（漏答方向），不做宽松方向的改判
- 任何 API 异常 / 拒答 / 解析失败一律返回 None，由调用方回退确定性路径
"""

import json
import logging
import os
from dataclasses import dataclass
from functools import lru_cache

from responder.models import Action, Category
from responder.reply import prompts

logger = logging.getLogger(__name__)


def llm_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


@lru_cache
def _client(timeout: float = 15.0):
    import anthropic

    # 群聊补位场景延迟预算有限：短超时 + SDK 自带 1 次重试
    return anthropic.Anthropic(timeout=timeout, max_retries=1)


def _text_of(response) -> str | None:
    if response.stop_reason == "refusal":
        return None
    return next((b.text for b in response.content if b.type == "text"), None)


# ================================================================ 分类复核
@dataclass
class Refined:
    action: Action
    category: Category
    confidence: float
    reason: str


def refine(
    content: str,
    model: str,
    *,
    history_text: str = "",
    case_type: str = "",
    timeout: float = 15.0,
) -> Refined | None:
    """对规则判「沉默」的边界样本做二次分类。失败/不可用返回 None。"""
    if not llm_available():
        return None
    try:
        response = _client(timeout).messages.create(
            model=model,
            max_tokens=300,
            system=prompts.CLASSIFY_SYSTEM,
            output_config={
                "format": {"type": "json_schema", "schema": prompts.CLASSIFY_SCHEMA}
            },
            messages=[
                {
                    "role": "user",
                    "content": prompts.classify_user_prompt(content, history_text, case_type),
                }
            ],
        )
        text = _text_of(response)
        if not text:
            return None
        data = json.loads(text)
        return Refined(
            action=Action(data["action"]),
            category=Category(data["category"]),
            confidence=float(data["confidence"]),
            reason=str(data["reason"])[:120],
        )
    except Exception:
        logger.exception("llm refine failed")
        return None


# ================================================================ 回答生成
def generate_answer_body(
    question: str,
    model: str,
    *,
    case_type: str = "",
    client_status_label: str = "已委托客户",
    case_stage: str = "",
    history_text: str = "",
    is_night: bool = False,
    max_tokens: int = 500,
    timeout: float = 15.0,
) -> str | None:
    """生成一般性法律框架正文。

    返回 None 表示：不可用 / 失败 / 模型主动示弱（NEED_LAWYER）——调用方一律转承接。
    """
    if not llm_available():
        return None
    try:
        response = _client(timeout).messages.create(
            model=model,
            max_tokens=max_tokens,
            system=prompts.ANSWER_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": prompts.answer_user_prompt(
                        question, case_type, client_status_label,
                        case_stage, history_text, is_night,
                    ),
                }
            ],
        )
        text = _text_of(response)
        if not text or prompts.NEED_LAWYER in text:
            return None
        return text
    except Exception:
        logger.exception("llm answer generation failed")
        return None

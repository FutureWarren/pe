"""LLM 调用层：分类复核 + 回答生成，多供应商（DeepSeek / Anthropic）。

供应商解析（resolve）：
- 配置 llm_provider 指定 deepseek/anthropic；auto 时谁的 key 在就用谁（deepseek 优先，成本考虑）
- 未配置任何 key 时所有函数快速返回 None，纯规则/模板路径可独立运行与验证

原则：
- 高优先级规则命中（紧急/报价/案件特定/自指）不交给模型改判——合规层级硬编码优先，
  模型只复核规则判「沉默」的边界样本（漏答方向），不做宽松方向的改判
- 任何 API 异常 / 拒答 / 解析失败一律返回 None，由调用方回退确定性路径
"""

import json
import logging
import os
from dataclasses import dataclass
from functools import lru_cache

import httpx

from responder.config import Settings, get_settings
from responder.models import Action, Category
from responder.reply import prompts

logger = logging.getLogger(__name__)

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"


@dataclass
class Provider:
    name: str  # "deepseek" | "anthropic"
    model: str


def resolve(settings: Settings | None = None) -> Provider | None:
    """按配置与环境变量解析当前可用的供应商。"""
    settings = settings or get_settings()
    ds = os.environ.get("DEEPSEEK_API_KEY")
    an = os.environ.get("ANTHROPIC_API_KEY")
    pref = settings.llm_provider
    if pref == "deepseek":
        return Provider("deepseek", settings.deepseek_model) if ds else None
    if pref == "anthropic":
        return Provider("anthropic", settings.claude_model) if an else None
    # auto：deepseek 优先（成本），退而 anthropic
    if ds:
        return Provider("deepseek", settings.deepseek_model)
    if an:
        return Provider("anthropic", settings.claude_model)
    return None


def llm_available() -> bool:
    return resolve() is not None


# ================================================================ 后端
@lru_cache
def _anthropic_client(timeout: float = 15.0):
    import anthropic

    # 群聊补位场景延迟预算有限：短超时 + SDK 自带 1 次重试
    return anthropic.Anthropic(timeout=timeout, max_retries=1)


def _chat_deepseek(
    system: str, user: str, model: str, *,
    max_tokens: int, timeout: float, json_mode: bool, temperature: float,
) -> str | None:
    payload: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    resp = httpx.post(
        DEEPSEEK_URL,
        headers={"Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}"},
        json=payload,
        timeout=timeout,
    )
    if resp.status_code != 200:
        logger.error("deepseek http %s: %s", resp.status_code, resp.text[:200])
        return None
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _chat_anthropic(
    system: str, user: str, model: str, *,
    max_tokens: int, timeout: float, json_schema: dict | None,
) -> str | None:
    kwargs: dict = {}
    if json_schema:
        kwargs["output_config"] = {"format": {"type": "json_schema", "schema": json_schema}}
    response = _anthropic_client(timeout).messages.create(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}], **kwargs,
    )
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
    *,
    history_text: str = "",
    case_type: str = "",
    timeout: float = 15.0,
    settings: Settings | None = None,
) -> Refined | None:
    """对规则判「沉默」的边界样本做二次分类。失败/不可用返回 None。"""
    provider = resolve(settings)
    if provider is None:
        return None
    user = prompts.classify_user_prompt(content, history_text, case_type)
    try:
        if provider.name == "deepseek":
            text = _chat_deepseek(
                prompts.CLASSIFY_SYSTEM + "\n" + prompts.CLASSIFY_JSON_INSTRUCTION,
                user, provider.model,
                max_tokens=300, timeout=timeout, json_mode=True, temperature=0.2,
            )
        else:
            text = _chat_anthropic(
                prompts.CLASSIFY_SYSTEM, user, provider.model,
                max_tokens=300, timeout=timeout, json_schema=prompts.CLASSIFY_SCHEMA,
            )
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
        logger.exception("llm refine failed (%s)", provider.name)
        return None


# ================================================================ 回答生成
def generate_answer_body(
    question: str,
    *,
    case_type: str = "",
    client_status_label: str = "已委托客户",
    case_stage: str = "",
    history_text: str = "",
    is_night: bool = False,
    max_tokens: int = 500,
    timeout: float = 15.0,
    settings: Settings | None = None,
) -> str | None:
    """生成一般性法律框架正文。

    返回 None 表示：不可用 / 失败 / 模型主动示弱（NEED_LAWYER）——调用方一律转承接。
    """
    provider = resolve(settings)
    if provider is None:
        return None
    user = prompts.answer_user_prompt(
        question, case_type, client_status_label, case_stage, history_text, is_night
    )
    try:
        if provider.name == "deepseek":
            text = _chat_deepseek(
                prompts.ANSWER_SYSTEM, user, provider.model,
                max_tokens=max_tokens, timeout=timeout, json_mode=False, temperature=0.7,
            )
        else:
            text = _chat_anthropic(
                prompts.ANSWER_SYSTEM, user, provider.model,
                max_tokens=max_tokens, timeout=timeout, json_schema=None,
            )
        if not text or prompts.NEED_LAWYER in text:
            return None
        return text
    except Exception:
        logger.exception("llm answer generation failed (%s)", provider.name)
        return None

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
    # auto：deepseek 优先（成本）。
    if ds:
        return Provider("deepseek", settings.deepseek_model)
    # **`auto` 不会自己滑到境外那家。**
    #
    # 客户在这里讲的是欠薪、离婚、伤情、家人有没有被拘留，而每条咨询原文都会
    # 随上下文发给模型。DeepSeek 的 key 掉了、环境里恰好还有个
    # ANTHROPIC_API_KEY，旧写法就**静默地**改走境外服务商——
    # 《个人信息保护法》上那不是换个模型，是从「向第三方提供」变成
    # 「个人信息出境」，要求完全不同，而律所对此毫不知情。
    #
    # 所以 `auto` 只在国内那家可用时成立；不可用就退回确定性话术
    # （规则 + 模板照常工作，客户仍然有人应），而不是换个处理者。
    # 真要用境外那家，把 `RESPONDER_LLM_PROVIDER=anthropic` 明确写出来——
    # 换处理者是律所的合规决定，不能是环境变量的副作用。
    if an:
        logger.warning(
            "llm_provider=auto 且只有 ANTHROPIC_API_KEY 可用：不自动启用。"
            "换处理者涉及个人信息出境，须显式配置 RESPONDER_LLM_PROVIDER=anthropic"
        )
    return None


def llm_available(settings: Settings | None = None) -> bool:
    """模型这条路现在能不能走。

    **必须能收 settings。** 「用哪家」现在是一个显式配置（见 `resolve`），
    而管道持有的是注入的那份配置；这里若一律读全局，两边就能错开——
    表现是判断层以为模型可用、真去调时却解析不出供应商，
    或者反过来白白退回确定性话术。
    """
    return resolve(settings) is not None


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


def ping(settings: Settings | None = None) -> tuple[bool, str]:
    """最小成本的连通性自检（部署后远程确认 key 有效）。返回 (是否可用, 错误摘要)。"""
    settings = settings or get_settings()
    provider = resolve(settings)
    if provider is None:
        return False, "未配置任何模型 key"
    try:
        if provider.name == "deepseek":
            out = _chat_deepseek(
                "回复 ok", "ok", provider.model,
                max_tokens=5, timeout=15, json_mode=False, temperature=0,
            )
        else:
            out = _chat_anthropic(
                "回复 ok", "ok", provider.model,
                max_tokens=5, timeout=15, json_schema=None,
            )
        return (out is not None), ("" if out is not None else "模型返回空")
    except Exception as e:  # 网络/鉴权/额度
        return False, f"{type(e).__name__}: {e}"[:200]


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
    is_one_on_one: bool = False,
    timeout: float = 15.0,
    settings: Settings | None = None,
) -> Refined | None:
    """对规则判「沉默」的边界样本做二次分类。失败/不可用返回 None。"""
    provider = resolve(settings)
    if provider is None:
        return None
    user = prompts.classify_user_prompt(content, history_text, case_type, is_one_on_one)
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


# ================================================================ 线索简报
@dataclass
class LeadBrief:
    summary: str
    case_type: str
    key_facts: list[str]
    urgency: str
    suggested_action: str
    opening_line: str


def extract_lead(
    history_text: str,
    *,
    contact: str = "",
    signals: list[str] | None = None,
    timeout: float = 20.0,
    settings: Settings | None = None,
) -> LeadBrief | None:
    """把一次咨询对话整理成交接单。不可用/失败返回 None，由调用方降级为规则摘要。"""
    provider = resolve(settings)
    if provider is None:
        return None
    user = prompts.lead_user_prompt(history_text, contact, signals or [])
    try:
        if provider.name == "deepseek":
            text = _chat_deepseek(
                prompts.LEAD_SYSTEM + "\n" + prompts.LEAD_JSON_INSTRUCTION,
                user, provider.model,
                max_tokens=600, timeout=timeout, json_mode=True, temperature=0.2,
            )
        else:
            text = _chat_anthropic(
                prompts.LEAD_SYSTEM, user, provider.model,
                max_tokens=600, timeout=timeout, json_schema=prompts.LEAD_SCHEMA,
            )
        if not text:
            return None
        d = json.loads(text)
        facts = [str(f)[:60] for f in (d.get("key_facts") or [])][:5]
        return LeadBrief(
            summary=str(d["summary"])[:120],
            case_type=str(d.get("case_type", ""))[:20],
            key_facts=facts,
            urgency=str(d.get("urgency", "low")),
            suggested_action=str(d.get("suggested_action", ""))[:120],
            opening_line=str(d.get("opening_line", ""))[:120],
        )
    except Exception:
        logger.exception("llm extract_lead failed (%s)", provider.name)
        return None


# ================================================================ 进线承接
def generate_intake_body(
    latest: str,
    *,
    case_type: str = "",
    history_text: str = "",
    handed_off: bool = False,
    is_night: bool = False,
    missing: list[str] | None = None,
    max_tokens: int = 500,
    timeout: float = 15.0,
    settings: Settings | None = None,
) -> str | None:
    """生成一对一进线的承接正文：回应客户刚说的话 + 追问还缺的关键信息。

    与 generate_answer_body 的分工：那边回答「一般法律怎么规定」，这边负责
    「接住并往下问」——不答题、不下结论，只把案情问全（话术松绑，律所方
    2026-08-13：「不要设置那么多固定话语，我们要的是灵活性」）。
    返回 None 表示：不可用 / 失败 / 模型示弱（NEED_LAWYER）——调用方回落模板。
    """
    provider = resolve(settings)
    if provider is None:
        return None
    user = prompts.intake_user_prompt(
        latest, case_type, history_text, handed_off=handed_off, is_night=is_night,
        missing=missing,
    )
    try:
        if provider.name == "deepseek":
            text = _chat_deepseek(
                prompts.INTAKE_SYSTEM, user, provider.model,
                max_tokens=max_tokens, timeout=timeout, json_mode=False, temperature=0.7,
            )
        else:
            text = _chat_anthropic(
                prompts.INTAKE_SYSTEM, user, provider.model,
                max_tokens=max_tokens, timeout=timeout, json_schema=None,
            )
        if not text or prompts.NEED_LAWYER in text:
            return None
        return text
    except Exception:
        logger.exception("llm intake generation failed (%s)", provider.name)
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
    is_one_on_one: bool = False,
    knowledge_text: str = "",
    memory_text: str = "",
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
        question, case_type, client_status_label, case_stage, history_text,
        is_night, is_one_on_one, knowledge_text=knowledge_text,
        memory_text=memory_text,
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

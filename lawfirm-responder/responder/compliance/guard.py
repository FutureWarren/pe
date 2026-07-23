"""出口闸门：所有将发出（或进入控制台草稿）的 AI 文本必须经过 guard()。

- 禁止事项命中 → 丢弃原文，回退到安全承接模板
- 直接回答路径缺免责句式 → 原样追加合伙人审定句式
"""

from dataclasses import dataclass

from responder.compliance import disclaimer, forbidden
from responder.models import Action


@dataclass
class GuardResult:
    text: str
    passed: bool  # False = 原文被拦截、已替换为安全回退
    violations: list[str]


def guard(
    text: str, action: Action, safe_fallback: str, *, require_disclaimer: bool = False
) -> GuardResult:
    violations = forbidden.check(text)
    if violations:
        # 回退文本同样过一遍闸门要求：不允许回退模板本身违规
        assert not forbidden.check(safe_fallback), "safe_fallback 违反禁止事项清单"
        return GuardResult(text=safe_fallback, passed=False, violations=violations)

    if action == Action.ANSWER and require_disclaimer:
        text = disclaimer.ensure_disclaimer(text)

    return GuardResult(text=text, passed=True, violations=[])

"""免责句式。句式模板由合伙人审定，AI 不得自行改写。

[待定] 以下为默认句式，上线前须经合伙人书面确认后替换。
"""

# 实质性法律内容回复必须原样携带的句式（直接回答路径）
DISCLAIMER = "以上为一般性法律信息，具体请以承办律师结合您案件情况的意见为准。"

# 承接路径的软性版本（可选，不强制）
HANDOFF_NOTE = "具体情况以承办律师核实后的答复为准。"


def has_disclaimer(text: str) -> bool:
    return DISCLAIMER in text


def ensure_disclaimer(text: str) -> str:
    """实质性法律内容缺免责句式时原样追加（不改写句式本身）。"""
    if has_disclaimer(text):
        return text
    return text.rstrip() + "\n" + DISCLAIMER

"""模型输出净化：把 LLM 文本收敛到微信群聊可直接发出的形态。

出口顺序：sanitize() → 结构拼装（templates）→ compliance.guard。
sanitize 只管形态（markdown/长度/口吻残留），合规语义由 guard 把关。
"""

import re

# 模型自曝 AI 身份 → 整条不可用（业务决策：不明示 AI）
_AI_SELF_REF = re.compile(
    r"(作为(一个|一名)?(AI|人工智能|智能|机器人|语言模型|大模型|助手模型|聊天机器人))"
    r"|(我是(一个|一名)?(AI|人工智能|机器人|语言模型|大模型|智能助[手理]))"
    r"|(我(只是|其实是)个?程序)"
    r"|(作为(一个|一名)?(大型)?(语言|对话|生成式)?模型)"
    r"|(我(被)?训练(出来|成)?的?)"
    r"|(我没有(实际|真实)的?(身体|情感|意识))",
    re.IGNORECASE,
)

# 模型元话语/拒答残留 → 不可用（换承接模板比发这种话强）
# 元话语一旦漏出去，客户看到的是「我无法就您的具体案件提供法律意见」——
# 一句典型的机器人腔，而且当场否定了他刚讲完的事。换承接模板比发这个强得多。
_META = re.compile(
    r"(我(不能|无法|没办法|不便)(提供|回答|评价|给出|就)"
    r"|抱歉[，,]?我(不能|无法)"
    r"|(无法|不能)(就|针对).{0,12}(提供|给出|作出)"
    r"|建议(您)?(咨询|寻求)(专业)?(律师|法律)"
    r"|as an ai)",
    re.IGNORECASE,
)

_GREETING = re.compile(r"^(您好|你好|亲爱的|亲)[，,！!。.\s]*")
_MD_HEADING = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_MD_BULLET = re.compile(r"^\s*[-*•]\s+", re.MULTILINE)
_MD_NUMBERED = re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE)
_MD_BOLD = re.compile(r"\*{1,2}([^*]+)\*{1,2}")
_MD_CODE = re.compile(r"`+")
_EMOJI = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f000-\U0001f0ff️]"
)
# 书面连接词 = AI 腔最大暴露点（见 docs/voice-guide.md），出现即剥离
# 只在**句首或紧跟标点**时剥离，且要求后面有停顿。
# 原来不限位置也不看词边界，于是把「因此次事故」切成「次事故」、
# 「另外一方还要赔」切成「一方还要赔」——发出去的是语法破碎的中文，
# 而 guard 只看语义禁止事项，破句照样 passed=True。
# 「另外一方」在交通事故和合同纠纷的答复里极常见，这不是边角案例。
_FORMAL_OPENERS = re.compile(
    r"(?m)(?:^|(?<=[。！？!?\n]))\s*(首先|其次|再次|此外|另外|综上所述|综上|"
    r"总而言之|总之|因此)[，,、]\s*"
)
_TILDE = re.compile(r"[~～]+")
_SENTENCE_END = "。！？!?；;"


def is_unusable(text: str) -> bool:
    """整条丢弃的情形：空、自曝 AI、元话语拒答。"""
    t = (text or "").strip()
    return not t or bool(_AI_SELF_REF.search(t)) or bool(_META.search(t))


def clamp(text: str, max_chars: int) -> str:
    """超长时在句末标点处截断；找不到句界则硬截。"""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    for i in range(len(cut) - 1, max(0, len(cut) - 80) - 1, -1):
        if cut[i] in _SENTENCE_END:
            return cut[: i + 1]
    return cut


def sanitize(text: str, max_chars: int = 240) -> str:
    """去 markdown / 表情 / 问候残留，压空行，收长度。调用前先用 is_unusable 判废。"""
    t = text.strip()
    t = _MD_HEADING.sub("", t)
    t = _MD_BULLET.sub("", t)
    t = _MD_NUMBERED.sub("", t)
    t = _MD_BOLD.sub(r"\1", t)
    t = _MD_CODE.sub("", t)
    t = _EMOJI.sub("", t)
    t = _FORMAL_OPENERS.sub("", t)
    t = _TILDE.sub("", t)
    t = _GREETING.sub("", t)
    # 压掉多余空行与行尾空白
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{2,}", "\n", t).strip()
    return clamp(t, max_chars)


def split_messages(text: str, max_parts: int = 3) -> list[str]:
    """按换行拆成多条微信消息（真人不发大段文字，见 docs/voice-guide.md）。

    超出 max_parts 的行并入最后一条；≤20 字的短条去掉结尾句号（更像随手打字）。
    """
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        return []
    if len(lines) > max_parts:
        lines = lines[: max_parts - 1] + ["\n".join(lines[max_parts - 1 :])]
    out = []
    for ln in lines:
        if len(ln) <= 20 and ln.endswith("。"):
            ln = ln[:-1]
        out.append(ln)
    return out

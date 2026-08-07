"""转化信号识别：判断一次咨询「值不值得人工立刻接手」。

北极星是首响时长，但首响之后真正的业务价值发生在两个瞬间：
客户留下联系方式、客户表达面谈/委托意愿。本模块只做确定性识别——
联系方式必须靠正则而非模型抽取（模型抄错一位数字，律师就打不通电话）。

意向分级供控制台排序与推送节流：
  hot  —— 留了联系方式 / 明确要约见或委托 → 立刻出简报并通知
  warm —— 问了费用 / 已描述具体案情       → 会话静默后出简报
  cold —— 其余                            → 仅归档，不打扰人工
"""

import re

# 中国大陆手机号：先剔除分隔符再匹配，兼容「177 2127 5495」「177-2127-5495」
_SEP = re.compile(r"[\s\-—－]")
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
# 座机（区号可选）：021-51697771 / 0755 12345678
_TEL = re.compile(r"(?<!\d)0\d{2,3}\d{7,8}(?!\d)")

_WECHAT_HINT = re.compile(r"(微信同号|微信是|加(我|个)?微信|vx|wx)[:：]?", re.I)

# 明确的面谈 / 委托意愿
_MEETING = re.compile(
    r"(约个?时间|什么时候方便|可以见面|想见|面谈|去(你们)?所里|来(一趟|所里)|"
    r"上门|当面(聊|谈|说)|预约)"
)
# 「我想找律师」在实测里是最常见的委托表达，却一直没被算成意向——
# 那条线索因此停在 P1，转接不触发，客户等的是一通不会来的电话。
_ENGAGE = re.compile(
    r"(想委托|要委托|请你们|你们(能|可以)?(接|代理)|签(合同|委托)|办手续|"
    r"(想|要|得|需要)(找|请)(个|位)?律师|找你们律师)"
)
# 主动索要联系方式也算强信号（客户想把沟通挪到线下）
_WANT_CONTACT = re.compile(r"(电话(联系|多少|号码)|打(我|个)?电话|留(个)?(电话|联系方式)|怎么联系)")
_FEE = re.compile(r"(收费|费用|多少钱|价格|报价|律师费)")

HOT, WARM, COLD = "hot", "warm", "cold"


def extract_contact(text: str) -> str:
    """从文本中提取联系方式，取第一个命中；无则空串。"""
    flat = _SEP.sub("", text or "")
    m = _PHONE.search(flat) or _TEL.search(flat)
    return m.group(0) if m else ""


def detect(text: str) -> tuple[str, list[str]]:
    """返回 (意向等级, 命中的信号名)。纯规则，可测、可解释。"""
    text = text or ""
    hits: list[str] = []
    contact = extract_contact(text)
    if contact:
        hits.append("contact")
    if _WECHAT_HINT.search(text):
        hits.append("wechat")
    if _MEETING.search(text):
        hits.append("meeting")
    if _ENGAGE.search(text):
        hits.append("engage")
    if _WANT_CONTACT.search(text):
        hits.append("want-contact")
    if _FEE.search(text):
        hits.append("fee")

    if {"contact", "meeting", "engage", "want-contact", "wechat"} & set(hits):
        return HOT, hits
    if "fee" in hits:
        return WARM, hits
    return COLD, hits


def rank(*levels: str) -> str:
    """取一组意向中的最高级别。"""
    for level in (HOT, WARM, COLD):
        if level in levels:
            return level
    return COLD


def scan(history: list[dict]) -> tuple[str, str, list[str]]:
    """扫一段对话的全部客户发言，返回 (意向, 联系方式, 信号)。

    纯规则、零成本——先用它决定「要不要花钱调模型」，而不是反过来。
    """
    contact, hits, levels = "", set(), []
    for m in history:
        if m.get("sender_is_staff"):
            continue
        level, names = detect(m.get("content", ""))
        levels.append(level)
        hits.update(names)
        # 取**最后**一个号码，不是第一个。客户后来改了口
        # （「刚才那个打不通，用这个 139…」，或换了个人的号），
        # 我们却把最早那个抄在交接单上——律师打的是一个作废的号码，
        # 而系统看起来一切正常。以他最后给的为准。
        contact = extract_contact(m.get("content", "")) or contact
    return rank(*levels), contact, sorted(hits)

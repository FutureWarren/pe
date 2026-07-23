"""确定性话术模板：按响应模式 × 客户状态 × 问题类型组装。

结构遵循方案 3.2：
  ① 直接回答：共情开场（按情绪/时段自适应）→ 一般性法律框架 → 自然收尾
  ② 安抚承接：确认收到 + 需律师核实 → 给出预期 →（未成交群）面谈引导
每类承接话术有多个变体，按 msg_id 稳定选取——同一条消息永远得到同一话术（可测），
不同消息之间有变化（不像机器人复读）。追问场景另有「二次安抚」话术，不复读原话。

群上下文（案件类型、承办律师姓名）注入话术，避免「通用机器人感」。
模板文本改动必须人工审核后合并（见 CLAUDE.md）。
"""

import re
import zlib
from datetime import datetime

from responder.compliance.disclaimer import DISCLAIMER
from responder.models import Category, ClientStatus, GroupProfile


def _lawyer(group: GroupProfile) -> str:
    return f"{group.lawyer_name}律师" if group.lawyer_name else "承办律师"


def _case(group: GroupProfile) -> str:
    return f"您{group.case_type}案件" if group.case_type else "您的案件"


def _pick(variants: list[str], seed: str) -> str:
    """按 seed 稳定选取变体：同一 seed 永远同一条。"""
    return variants[zlib.crc32(seed.encode()) % len(variants)]


# ---------------------------------------------------------------- ② 承接类
# 语感依据见 docs/voice-guide.md：先接住，再给预期；短句；不打公文腔。
def handoff_case_status(group: GroupProfile, seed: str = "") -> str:
    L, C = _lawyer(group), _case(group)
    variants = [
        f"收到，{C}的最新进展我帮您问下{L}哈，一有消息马上在群里说。",
        f"看到您消息了。具体进展得{L}那边确认，我已经跟{L}说了，回头就答复您。",
        f"收到您的消息，这个我帮您催一下{L}，他核实了就在群里回您。",
    ]
    return _pick(variants, seed)


def handoff_fee(group: GroupProfile, seed: str = "") -> str:
    L = _lawyer(group)
    if group.client_status == ClientStatus.PROSPECT:
        variants = [
            f"收到。费用这块跟案子具体情况关系很大，得{L}了解情况后才能给您准数，我已经转达了。\n"
            f"方便的话可以约个时间当面聊，把您的情况一次说清楚。",
            f"这个得{L}结合您的情况来说才准确，我让他尽快跟您联系哈。"
            f"要是方便，也欢迎约个时间来所里坐坐，当面把情况理一理。",
        ]
    else:
        variants = [
            f"收到，费用的事由{L}跟您直接确认，我已经提醒他了，稍等哈。",
            f"看到您消息了。这块{L}会跟您直接沟通，我这边也催着，请您稍等。",
        ]
    return _pick(variants, seed)


def handoff_urgent(group: GroupProfile, seed: str = "") -> str:
    L = _lawyer(group)
    variants = [
        f"看到您的消息了，您先别急。这个情况比较要紧，我已经第一时间加急联系{L}了，"
        f"会尽快跟您联系。",
        f"收到，您先别慌，这个情况我们很重视。已经加急通知{L}了，尽快给您答复。",
    ]
    return _pick(variants, seed)


def handoff_contact(group: GroupProfile, seed: str = "") -> str:
    L = _lawyer(group)
    variants = [
        f"看到您消息了，{L}这会儿应该在忙。我已经提醒他了，忙完会第一时间回您。",
        f"在的。{L}可能暂时腾不出手，我已经跟他说了，看到就回您哈。",
        f"收到，我这就帮您叫一下{L}，他看到会尽快回复您。",
    ]
    return _pick(variants, seed)


def handoff_generic(group: GroupProfile, seed: str = "") -> str:
    L = _lawyer(group)
    variants = [
        f"收到您的消息，这个得让{L}来说比较准确，我已经转达了，请您稍等。",
        f"看到您消息了，这个我帮您转给{L}确认下，有回复马上告诉您。",
    ]
    return _pick(variants, seed)


def safe_fallback(group: GroupProfile) -> str:
    """合规拦截后的兜底回复：只承接，不含任何实质内容。固定文本，不走变体。"""
    L = _lawyer(group)
    return f"收到您的消息，这个得让{L}来说比较准确，我已经转达了，请您稍等。"


def second_touch(group: GroupProfile, urgent: bool = False) -> str:
    """客户追问同一件事时的二次安抚：不复读，升级姿态。"""
    L = _lawyer(group)
    if urgent:
        return f"实在抱歉让您久等了，我刚又加急催了{L}，也跟所里其他同事说了，一定尽快回您。"
    return f"抱歉让您久等了，我刚又跟{L}那边催了一下，一有回复马上在群里说。"


HANDOFF_BY_CATEGORY = {
    Category.CASE_STATUS: handoff_case_status,
    Category.FEE: handoff_fee,
    Category.URGENT: handoff_urgent,
    Category.CONTACT: handoff_contact,
}


def build_handoff(category: Category, group: GroupProfile, seed: str = "") -> str:
    return HANDOFF_BY_CATEGORY.get(category, handoff_generic)(group, seed)


# ---------------------------------------------------------------- ① 直接回答
_ANXIOUS = re.compile(r"(害怕|好怕|慌|担心|着急|焦虑|睡不着|紧张|心里没底|绝望|难受)")


def answer_opening(question: str, now: datetime | None = None) -> str:
    """按情绪与时段选择共情开场（确定性）。常规情况不加开场——直接说事最像真人。"""
    now = now or datetime.now()
    late = now.hour >= 22 or now.hour < 6
    anxious = bool(_ANXIOUS.search(question))
    if late and anxious:
        return "这么晚还没休息，能感觉到您心里不踏实，先别太担心。"
    if anxious:
        return "理解您心里着急，先别慌，我给您说说一般的情况。"
    if late:
        return "这么晚还在为这事操心，辛苦了。"
    return ""


def answer_scaffold(
    group: GroupProfile,
    body: str,
    include_disclaimer: bool = False,
    opening: str | None = None,
) -> str:
    """将（模型生成或人工维护的）一般性法律框架装入合规结构。

    body 只应包含：法条依据 + 一般区间 + 影响因素，不针对本案下结论。
    未成交群（销售顾问定位）自然收尾带一句面谈引导，做 first screening 后的转化。
    """
    parts = [opening or "", body.strip()]
    if include_disclaimer:
        parts.append(DISCLAIMER)
    if group.client_status == ClientStatus.PROSPECT:
        parts.append("每个人情况不太一样，方便的话可以约个时间，跟律师细聊下您的情况。")
    else:
        parts.append(f"具体到您这边，等{_lawyer(group)}看到再给您细说。")
    return "\n".join(p for p in parts if p)


def answer_without_llm(group: GroupProfile, include_disclaimer: bool = False) -> str:
    """未接入模型时直接回答路径的确定性降级：不编造法律内容，转为承接。"""
    text = (
        f"收到您的咨询。这个为了给您说准确，我已转达{_lawyer(group)}，"
        f"他看到会在群里给您解答。"
    )
    if group.client_status == ClientStatus.PROSPECT:
        text += "方便的话也可以约个时间，跟律师细聊下您的情况。"
    if include_disclaimer:
        text += "\n" + DISCLAIMER
    return text

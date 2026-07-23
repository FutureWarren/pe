"""确定性话术模板：按响应模式 × 客户状态 × 问题类型组装。

结构遵循方案 3.2：
  ① 直接回答：共情 → 一般性法律框架 → 免责句式 → 自然收尾（3–6 句）
  ② 安抚承接：确认收到 + 需律师核实 → 给出预期 → （可选）一般性说明补充获得感
群上下文（案件类型、承办律师姓名）注入话术，避免「通用机器人感」。

模板文本改动必须人工审核后合并（见 CLAUDE.md）。
"""

from responder.compliance.disclaimer import DISCLAIMER, HANDOFF_NOTE
from responder.models import Category, ClientStatus, GroupProfile


def _lawyer(group: GroupProfile) -> str:
    return f"{group.lawyer_name}律师" if group.lawyer_name else "承办律师"


def _case(group: GroupProfile) -> str:
    return f"您{group.case_type}案件" if group.case_type else "您的案件"


# ---------------------------------------------------------------- ② 承接类
def handoff_case_status(group: GroupProfile) -> str:
    return (
        f"您好，消息已收到。{_case(group)}的具体进展需要{_lawyer(group)}核实后回复您，"
        f"我已经提醒{_lawyer(group)}，看到后会尽快在群里答复您。\n"
        f"{HANDOFF_NOTE}"
    )


def handoff_fee(group: GroupProfile) -> str:
    if group.client_status == ClientStatus.PROSPECT:
        return (
            f"您好，收到您的咨询。费用需要{_lawyer(group)}结合案件的具体情况来说明，"
            f"我已提醒{_lawyer(group)}尽快与您沟通。"
            f"如方便的话，也欢迎您预约到所里面谈，律师可以当面帮您把情况理一理。"
        )
    return (
        f"您好，消息已收到。费用相关的问题由{_lawyer(group)}和您直接确认，"
        f"我已提醒{_lawyer(group)}，看到后会尽快回复您。"
    )


def handoff_urgent(group: GroupProfile) -> str:
    return (
        f"您好，看到您的消息了，请您先别着急。"
        f"这个情况比较重要，我已第一时间加急提醒{_lawyer(group)}，会尽快联系您。"
    )


def handoff_contact(group: GroupProfile) -> str:
    return (
        f"您好，消息已收到。{_lawyer(group)}可能暂时在忙，我已经提醒，"
        f"看到后会尽快回复您，请您稍候。"
    )


def handoff_generic(group: GroupProfile) -> str:
    return (
        f"您好，消息已收到。这个问题需要{_lawyer(group)}确认后答复您，"
        f"我已提醒{_lawyer(group)}尽快回复。{HANDOFF_NOTE}"
    )


def safe_fallback(group: GroupProfile) -> str:
    """合规拦截后的兜底回复：只承接，不含任何实质内容。"""
    return handoff_generic(group)


HANDOFF_BY_CATEGORY = {
    Category.CASE_STATUS: handoff_case_status,
    Category.FEE: handoff_fee,
    Category.URGENT: handoff_urgent,
    Category.CONTACT: handoff_contact,
}


def build_handoff(category: Category, group: GroupProfile) -> str:
    return HANDOFF_BY_CATEGORY.get(category, handoff_generic)(group)


# ---------------------------------------------------------------- ① 直接回答
def answer_scaffold(group: GroupProfile, body: str) -> str:
    """将（模型生成或人工维护的）一般性法律框架装入合规结构。

    body 只应包含：法条依据 + 一般区间 + 影响因素，不针对本案下结论。
    """
    return (
        f"您好，理解您想先了解一下相关规定。\n"
        f"{body.strip()}\n"
        f"{DISCLAIMER}\n"
        f"{_lawyer(group)}看到后会结合您的具体情况再为您补充。"
    )


def answer_without_llm(group: GroupProfile) -> str:
    """未接入模型时直接回答路径的确定性降级：不编造法律内容，转为承接。"""
    return (
        f"您好，收到您的咨询。为了给您准确的说明，这个问题我已转达{_lawyer(group)}，"
        f"看到后会在群里给您解答。\n"
        f"{DISCLAIMER}"
    )

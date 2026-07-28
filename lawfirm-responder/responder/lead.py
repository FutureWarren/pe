"""线索简报：把一次线上咨询整理成「可以直接照着打电话」的交接单。

产品定位：AI 只做首轮筛查，真正的价值在筛查之后——律师需要的不是聊天记录，
而是一张知道「谁、什么事、多急、电话多少、第一句说什么」的单子。

三条设计原则：
1. 联系方式只由正则提取（engine/signals.py），绝不交给模型——抄错一位就打不通。
2. 模型只做归纳，且被 prompt 约束为「只复述对话中出现过的信息」；模型不可用时
   降级为规则摘要（取客户原话），宁可简陋也不能编造。
3. 一个会话一条线索、反复更新而非追加；仅在意向升级时才二次通知，避免刷屏。
"""

import json
import logging

from responder.config import Settings, get_settings
from responder.engine import llm, signals
from responder.models import GroupProfile
from responder.reply import prompts
from responder.store.db import Store

logger = logging.getLogger(__name__)

_URGENCY_ZH = {"high": "紧急", "medium": "较急", "low": "一般"}
_INTENT_ZH = {"hot": "高意向", "warm": "有意向", "cold": "一般咨询"}


def _fallback_summary(history: list[dict]) -> str:
    """模型不可用时的确定性摘要：取客户最长的一句原话。"""
    said = [m["content"].strip() for m in history if not m.get("sender_is_staff")]
    said = [s for s in said if s]
    return (max(said, key=len)[:80]) if said else "（无文字内容）"


def build_and_store(
    store: Store,
    group: GroupProfile,
    history: list[dict],
    *,
    settings: Settings | None = None,
) -> dict | None:
    """生成/更新线索并入库，返回线索记录。历史为空则不生成。"""
    settings = settings or get_settings()
    if not history:
        return None

    # 联系方式与意向：扫全量客户发言（客户可能在任意一轮留电话）
    contact, hits, levels = "", set(), []
    for m in history:
        if m.get("sender_is_staff"):
            continue
        level, names = signals.detect(m["content"])
        levels.append(level)
        hits.update(names)
        contact = contact or signals.extract_contact(m["content"])
    intent = signals.rank(*levels)

    brief = llm.extract_lead(
        prompts.format_history(history, max_chars_each=200),
        contact=contact,
        signals=sorted(hits),
        timeout=settings.llm_timeout_seconds + 5,
        settings=settings,
    )
    if brief is None:
        fields = {
            "summary": _fallback_summary(history),
            "case_type": group.case_type,
            "key_facts": json.dumps([], ensure_ascii=False),
            "urgency": "high" if intent == signals.HOT else "low",
            "suggested_action": "请查看完整对话后跟进（AI 摘要不可用）",
            "opening_line": "",
        }
    else:
        fields = {
            "summary": brief.summary,
            "case_type": brief.case_type or group.case_type,
            "key_facts": json.dumps(brief.key_facts, ensure_ascii=False),
            "urgency": brief.urgency,
            "suggested_action": brief.suggested_action,
            "opening_line": brief.opening_line,
        }
    fields.update(
        intent=intent, contact=contact, signals=json.dumps(sorted(hits), ensure_ascii=False)
    )
    store.upsert_lead(group.group_id, fields)
    return store.get_lead(group.group_id)


def format_notification(lead: dict, group: GroupProfile) -> str:
    """推给律师的交接单文本（企微单聊）。"""
    facts = []
    try:
        facts = json.loads(lead.get("key_facts") or "[]")
    except (ValueError, TypeError):
        pass
    head = "【高意向线索】" if lead["intent"] == "hot" else "【新咨询线索】"
    lines = [
        f"{head}{lead.get('case_type') or '类型待确认'} · "
        f"{_URGENCY_ZH.get(lead.get('urgency'), '一般')}",
        "",
        f"客户诉求：{lead.get('summary') or '（未归纳）'}",
    ]
    if facts:
        lines.append("关键信息：")
        lines += [f"  · {f}" for f in facts]
    lines.append(f"联系方式：{lead.get('contact') or '客户未留，需在会话中继续沟通'}")
    if lead.get("suggested_action"):
        lines += ["", f"建议动作：{lead['suggested_action']}"]
    if lead.get("opening_line"):
        lines.append(f"开场参考：「{lead['opening_line']}」")
    lines += ["", f"完整对话见控制台 · 会话「{group.name or group.group_id}」"]
    return "\n".join(lines)


def should_notify(previous: dict | None, current: dict) -> bool:
    """仅在「首次达到可跟进意向」或「意向升级」时通知，避免同一客户反复打扰。"""
    if current["intent"] == signals.COLD:
        return False
    if previous is None or not previous.get("notified_at"):
        return True
    order = {signals.COLD: 0, signals.WARM: 1, signals.HOT: 2}
    return order[current["intent"]] > order.get(previous.get("intent"), 0)


def dispatch(
    store: Store, group: GroupProfile, history: list[dict], sender, *,
    settings: Settings | None = None,
) -> dict | None:
    """生成线索 →（按需）推送接待人。sender 为 None（影子模式）时只入库。"""
    settings = settings or get_settings()
    previous = store.get_lead(group.group_id)
    lead = build_and_store(store, group, history, settings=settings)
    if lead is None:
        return None
    if not should_notify(previous, lead):
        return lead
    to = group.lawyer_userid or settings.default_notify_userid
    if sender and to:
        if sender.send_direct_text(to, format_notification(lead, group)):
            store.mark_lead_notified(group.group_id)
            logger.info("lead notified: %s → %s", group.group_id, to)
    return lead

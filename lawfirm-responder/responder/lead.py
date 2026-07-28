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
from datetime import datetime

from responder.config import Settings, get_settings
from responder.engine import llm, signals
from responder.models import GroupProfile
from responder.reply import prompts
from responder.store.db import Store

logger = logging.getLogger(__name__)

_URGENCY_ZH = {"high": "紧急", "medium": "较急", "low": "一般"}
_INTENT_ZH = {"hot": "高意向", "warm": "有意向", "cold": "一般咨询"}


def current_session(history: list[dict], gap_seconds: int) -> list[dict]:
    """只取「这一次」咨询：从最新一条往回，遇到超过 gap 的时间空档就断开。

    同一个客户可能隔几小时又来问一件毫不相干的事；把两次混在一张交接单里，
    摘要会变成「咨询了拖欠工资、拘留、借款纠纷等多个问题」，律师无从下手。
    """
    if len(history) < 2:
        return history
    out = [history[-1]]
    for m in reversed(history[:-1]):
        try:
            newer = datetime.fromisoformat(out[0]["created_at"])
            older = datetime.fromisoformat(m["created_at"])
        except (KeyError, ValueError, TypeError):
            out.insert(0, m)  # 读不到时间就保留：宁可多带上下文，不可凭空丢失
            continue
        if (newer - older).total_seconds() > gap_seconds:
            break
        out.insert(0, m)
    return out


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
    summarize: bool = True,
    previous: dict | None = None,
    urgent: bool = False,
) -> dict | None:
    """生成/更新线索并入库，返回线索记录。历史为空则不生成。

    summarize=False 时只用规则更新意向/联系方式（零模型成本）——用于
    「本轮不会通知任何人」的情况：没人看的摘要不值得花一次模型调用。
    """
    settings = settings or get_settings()
    if not history:
        return None
    history = current_session(history, settings.lead_session_gap_seconds)

    intent, contact, hits = signals.scan(history)
    brief = (
        llm.extract_lead(
            prompts.format_history(history, max_chars_each=200),
            contact=contact,
            signals=hits,
            timeout=settings.llm_timeout_seconds + 5,
            settings=settings,
        )
        if summarize
        else None
    )
    if brief is not None:
        fields = {
            "summary": brief.summary,
            "case_type": brief.case_type or group.case_type,
            "key_facts": json.dumps(brief.key_facts, ensure_ascii=False),
            "urgency": brief.urgency,
            "suggested_action": brief.suggested_action,
            "opening_line": brief.opening_line,
        }
    elif previous:  # 不重新归纳时沿用上一版摘要，只刷新规则字段
        fields = {k: previous.get(k, "") for k in
                  ("summary", "case_type", "key_facts", "urgency",
                   "suggested_action", "opening_line")}
    else:
        fields = {
            "summary": _fallback_summary(history),
            "case_type": group.case_type,
            "key_facts": json.dumps([], ensure_ascii=False),
            "urgency": "high" if intent == signals.HOT else "low",
            "suggested_action": "请查看完整对话后跟进（AI 摘要不可用）",
            "opening_line": "",
        }
    fields.update(
        intent=intent, contact=contact, signals=json.dumps(hits, ensure_ascii=False)
    )
    # 规则引擎判定的紧急（拘留/传唤/开庭临近…）是确定性信号，优先于模型的估计
    if urgent:
        fields["urgency"] = "high"
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


_ORDER = {signals.COLD: 0, signals.WARM: 1, signals.HOT: 2}


def should_notify(previous: dict | None, intent: str) -> bool:
    """仅在「首次达到可跟进意向」或「意向升级」时通知，避免同一客户反复打扰。"""
    if intent == signals.COLD:
        return False
    if previous is None or not previous.get("notified_at"):
        return True
    return _ORDER[intent] > _ORDER.get(previous.get("intent"), 0)


def dispatch(
    store: Store, group: GroupProfile, history: list[dict], sender, *,
    settings: Settings | None = None, force: bool = False, urgent: bool = False,
) -> dict | None:
    """生成线索 →（按需）推送接待人。sender 为 None（影子模式）时只入库。

    先用零成本的规则判定「这次要不要通知」，只有要通知时才让模型归纳——
    一次咨询里客户可能说十句话，但律师只需要收到一条交接单。
    """
    settings = settings or get_settings()
    if not history:
        return None
    previous = store.get_lead(group.group_id)
    intent, _, _ = signals.scan(current_session(history, settings.lead_session_gap_seconds))
    notify = force or should_notify(previous, intent)
    lead = build_and_store(
        store, group, history, settings=settings, summarize=notify,
        previous=previous, urgent=urgent,
    )
    if lead is None or not notify:
        return lead
    to = group.lawyer_userid or settings.default_notify_userid
    if sender and to:
        if sender.send_direct_text(to, format_notification(lead, group)):
            store.mark_lead_notified(group.group_id)
            logger.info("lead notified: %s → %s", group.group_id, to)
    return lead

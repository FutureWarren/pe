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
import re
from datetime import datetime

from responder import assignment
from responder.config import Settings, get_settings
from responder.engine import llm, priority, signals
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


_NO_CONTACT_CLAIM = re.compile(r"(未|没有?|无)(提供|留下?|告知)?.{0,4}(联系方式|电话|手机)")


def _drop_contradicting_facts(facts: list[str], contact: str) -> list[str]:
    """删掉与确定性事实相矛盾的模型要点。

    正则已经从对话里抠出手机号，卡片却并排显示「未提供联系方式」——律师会
    当场不信任这张单子。事实层面能判定的，不让模型说了算。
    """
    if not contact:
        return facts
    return [f for f in facts if not _NO_CONTACT_CLAIM.search(f)]


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
            "key_facts": json.dumps(
                _drop_contradicting_facts(brief.key_facts, contact), ensure_ascii=False
            ),
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
            # 意向 ≠ 紧急：留了电话是「较急」，「紧急」这顶帽子留给拘留/传唤/开庭临近
            # （由下面的 urgent 规则命中才戴）。混为一谈会让紧急标记失去意义。
            "urgency": "medium" if intent == signals.HOT else "low",
            "suggested_action": "请电话联系客户，了解具体情况",
            "opening_line": "",
        }
    fields.update(
        intent=intent, contact=contact, signals=json.dumps(hits, ensure_ascii=False)
    )
    # 规则引擎判定的紧急（拘留/传唤/开庭临近…）是确定性信号，优先于模型的估计
    if urgent:
        fields["urgency"] = "high"
    # 优先级评分：律师排队跟进的依据（见 engine/priority.py 与 docs/lead-routing.md）
    score, tier, factors = priority.evaluate(history, urgent=urgent, settings=settings)
    fields.update(
        score=score, priority=tier, factors=json.dumps(factors, ensure_ascii=False)
    )
    store.upsert_lead(group.group_id, fields)
    return store.get_lead(group.group_id)


def format_notification(lead: dict, group: GroupProfile) -> str:
    """推给律师的交接单文本（企微单聊）。

    首行即优先级与时限预期——律师扫一眼就知道这单该排在手头哪个位置；
    「优先依据」把评分摊开，可解释的排序才会被照着执行。
    """
    facts = []
    try:
        facts = json.loads(lead.get("key_facts") or "[]")
    except (ValueError, TypeError):
        pass
    tier = lead.get("priority") or ""
    if tier:
        head = f"【{tier} {priority.TIER_ZH.get(tier, '')}】"
        sla = priority.TIER_SLA_ZH.get(tier, "")
    else:  # 旧数据未评分
        head = "【高意向线索】" if lead["intent"] == "hot" else "【新咨询线索】"
        sla = ""
    lines = [
        f"{head}{lead.get('case_type') or '类型待确认'} · "
        f"{_URGENCY_ZH.get(lead.get('urgency'), '一般')}"
        + (f" · 建议{sla}" if sla else ""),
        "",
        f"客户诉求：{lead.get('summary') or '（未归纳）'}",
    ]
    if facts:
        lines.append("关键信息：")
        lines += [f"  · {f}" for f in facts]
    lines.append(f"联系方式：{lead.get('contact') or '客户未留，需在会话中继续沟通'}")
    try:
        factors = json.loads(lead.get("factors") or "[]")
    except (ValueError, TypeError):
        factors = []
    if factors:
        lines.append(f"优先依据：{priority.factors_line(factors)}")
    if lead.get("suggested_action"):
        lines += ["", f"建议动作：{lead['suggested_action']}"]
    if lead.get("opening_line"):
        lines.append(f"开场参考：「{lead['opening_line']}」")
    lines += ["", f"完整对话见控制台 · 会话「{group.name or group.group_id}」"]
    return "\n".join(lines)


_ORDER = {signals.COLD: 0, signals.WARM: 1, signals.HOT: 2}


def _notified_within(previous: dict | None, seconds: int) -> bool:
    if not previous or not previous.get("notified_at") or seconds <= 0:
        return False
    try:
        age = (datetime.now() - datetime.fromisoformat(previous["notified_at"]))
    except (ValueError, TypeError):
        return False
    return age.total_seconds() < seconds


def should_notify(
    previous: dict | None, intent: str, *, gap_seconds: int = 0,
) -> bool:
    """仅在「首次达到可跟进意向」「意向升级」或「隔了一次咨询后再来」时通知。

    没有第三条时，隔周回头的老客户永远不会再惊动律师——notified_at 一旦写下
    就是永久的，等于把回访客户静音了。以「一次咨询」为节流粒度才对。
    """
    if intent == signals.COLD:
        return False
    if previous is None or not previous.get("notified_at"):
        return True
    if _ORDER[intent] > _ORDER.get(previous.get("intent"), 0):
        return True
    if gap_seconds:
        try:
            since = (
                datetime.now() - datetime.fromisoformat(previous["notified_at"])
            ).total_seconds()
        except (ValueError, TypeError):
            return False
        return since >= gap_seconds  # 已是另一次咨询，值得再提醒一次
    return False


def dispatch(
    store: Store, group: GroupProfile, history: list[dict], sender, *,
    settings: Settings | None = None, force: bool = False, urgent: bool = False,
    summarize: bool | None = None,
) -> dict | None:
    """生成线索 →（按需）推送接待人。sender 为 None（影子模式）时只入库。

    先用零成本的规则判定「这次要不要通知」，只有要通知时才让模型归纳——
    一次咨询里客户可能说十句话，但律师只需要收到一条交接单。

    summarize 显式传 False 可强制跳过模型归纳（批量导入用：几百条历史客资
    逐条调模型要十几分钟，请求早超时了，而平台自带的描述本就比模型转述更如实）。
    """
    settings = settings or get_settings()
    if not history:
        return None
    previous = store.get_lead(group.group_id)
    intent, _, _ = signals.scan(current_session(history, settings.lead_session_gap_seconds))
    # force（紧急）也要节流：客户连发五条急消息，律师不该连收五张几乎一样的
    # 交接单（每张还烧一次模型）。刚推过就只更新入库，升级由 reminders 那条链管。
    if force and _notified_within(previous, settings.lead_force_cooldown_seconds):
        force = False
    notify = force or should_notify(
        previous, intent, gap_seconds=settings.lead_session_gap_seconds
    )
    lead = build_and_store(
        store, group, history, settings=settings,
        summarize=notify if summarize is None else summarize,
        previous=previous, urgent=urgent,
    )
    if lead is None:
        return lead
    # 派单在通知之前：交接单要推给被派到的律师，而不是笼统的接待人。
    # 名册为空时 ensure 回落旧链路（会话承办人/全局兜底），行为与旧版完全一致。
    to, newly_assigned = assignment.ensure(store, group, lead, settings)
    # 刚接手的律师必须拿到单子：不能因为这条线索之前通知过别人就被节流掉
    notify = notify or newly_assigned
    if not notify:
        return lead
    lead = store.get_lead(group.group_id) or lead  # 取回含指派信息的最新版
    if sender and to:
        if sender.send_direct_text(to, format_notification(lead, group)):
            store.mark_lead_notified(group.group_id)
            # 瞬态标记（不入库）：告诉调用方「这一轮真的推送了」。
            # service 据此跳过同一条消息的逐条提醒——律师不该为一件事收到两条 DM。
            lead["_notified_now"] = True
            logger.info("lead notified: %s → %s", group.group_id, to)
    return lead

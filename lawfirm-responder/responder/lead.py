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
from urllib.parse import quote

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


def format_notification(
    lead: dict, group: GroupProfile, settings: Settings | None = None
) -> str:
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
        # 客服手上只认强/弱两档：这张单要回答的问题就一个——现在打还是排队打
        head = f"【{priority.bucket(tier)}】"
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
        # 模型爱写「我是XX律师」这种占位符。收件人就是被派的那位律师，
        # 名字我们是知道的——留着 XX 让人以为系统没做完，顺手换掉。
        opening = lead["opening_line"]
        # 派单时会把被派律师同步写回会话档案（assignment.ensure），所以这里是准的
        who = (group.lawyer_name or "").strip()
        opening = opening.replace("XX律师", f"{who}律师" if who else "律师")
        opening = opening.replace("XX", who or "")
        lines.append(f"开场参考：「{opening}」")
    # 末尾这一行原来是句死路：「完整对话见控制台」——律师得开浏览器、找地址、
    # 登录、翻列表、找到人，五步，所以他不会做。改成可点的深链，一步直达。
    # public_base_url 没配时退回原文案（本机开发/未定域名）。
    base = (settings or get_settings()).public_base_url.rstrip("/")
    if base:
        # 走 /g/<id> 而不是 /ui#g=<id>：企业微信会把 `#` 转义成 %23，
        # 于是服务器收到的路径是 `/ui%23g=...` → 404。真机上点一次才现形，
        # 浏览器里永远测不出来（浏览器不转义 `#`）。
        gid = quote(group.group_id, safe="")
        lines += ["", f"看完整对话：{base}/g/{gid}"]
    else:
        lines += ["", f"完整对话见控制台 · 会话「{group.name or group.group_id}」"]
    if group.is_kf and not group.handoff_userid:
        # 接管方式必须写在单子上。功能做了却没人知道，等于没做——
        # 而这句话省掉的正是「打开控制台、找到会话、点接管」那三步。
        lines.append("要接手就直接在企业微信「微信客服」里回他一句，AI 会自动让开。")
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


# 分数涨多少才值得再响一次。低于此只更新入库，不打扰人——
# 客户多说一句「沟通深入 +5」不该换来一条推送；而 15 分意味着
# 至少一个硬信号出现了（问收费 +15、要加微信 +20、想面谈 +30…）。
RENOTIFY_SCORE_DELTA = 15


def worth_renotifying(previous: dict | None, lead: dict) -> str:
    """老客户又来了，这一版值不值得再推一条？返回原因，不值得则返回空串。

    **为什么不能只看「刚推过」就闭嘴**（真机 2026-08-07）：
    老客户隔半小时回来，说的是另一件事（上午问交通事故，下午问拖欠工资）、
    还留了另一个号码。旧策略看到「距上次推送不到两小时、意向档位没变」
    就一条不推，于是客服那边永远停在上一版——案由是错的、电话是作废的、
    摘要是上午那件事。这不是「防打扰」，这是丢单。

    三种变化值得再响一次，都是**客服拿到会做出不同动作**的变化：
      1. 弱 → 强：该放下手头的事去接了；
      2. 联系方式变了或刚拿到：打的号不一样，这是最实的一种变化；
      3. 分数涨了一截：出现了新的硬信号（问收费、要加微信、想面谈…）。

    反向一律不推：分数会波动，「刚才急现在不急了」推给人只消耗信任。
    """
    if not previous or not previous.get("notified_at"):
        return ""  # 没推过就不叫「再推」，走 should_notify 的常规判断
    tier = (lead.get("priority") or "").upper()
    if tier == priority.P0 and (previous.get("priority") or "").upper() != priority.P0:
        return "upgrade"
    now_contact = (lead.get("contact") or "").strip()
    was_contact = (previous.get("notified_contact") or previous.get("contact") or "").strip()
    if now_contact and now_contact != was_contact:
        return "contact"
    was_score = previous.get("notified_score") or 0
    if not was_score:  # 老数据没有快照，退回用当时的分数
        was_score = previous.get("score") or 0
    if (lead.get("score") or 0) - was_score >= RENOTIFY_SCORE_DELTA:
        return "score"
    return ""


_RENOTIFY_PREFIX = {
    "upgrade": "【升级】这位客户刚从弱意愿变成强意愿——",
    "contact": "【新联系方式】这位客户刚留了新的号码——",
    "score": "【有新进展】这位客户又说了些要紧的——",
}


def tier_upgraded(previous: dict | None, tier: str) -> bool:
    """这一轮客户从「弱意愿」升成了「强意愿」吗。

    存在的理由（律所方 2026-08，真机测试后）：客户是**边聊边变强**的。
    先留个电话（40 分，弱），接着问赔多少、问地址、问怎么走——每一步都在加分，
    但旧的通知策略只认「意向档位（冷/温/热）」升级，而这三步全在「温」里，
    于是客服**永远只会收到那一条弱意愿提醒**，客户后来变得多热都无人知晓。

    「弱 → 强」这一跳必须再响一次：那正是客服该放下手头事去接的时刻。
    反向（强 → 弱）不通知，也不改已推过的判断——分数会波动，
    而「刚才说很急现在又不急了」这种噪音推给人只会消耗信任。
    """
    if tier != priority.P0 or not previous:
        return False
    # 之前没推送过就不叫「升级」——那只是这条线索第一次被看见，
    # 加个「升级」前缀反而让客服以为错过了什么。
    if not previous.get("notified_at"):
        return False
    return (previous.get("priority") or "").upper() != priority.P0


def should_notify(
    previous: dict | None, intent: str, *, gap_seconds: int = 0,
    include_cold: bool = False,
) -> bool:
    """在「首次达到可跟进意向」「意向升级」或「隔了一次咨询后再来」时通知。

    没有第三条时，隔周回头的老客户永远不会再惊动律师——notified_at 一旦写下
    就是永久的，等于把回访客户静音了。以「一次咨询」为节流粒度才对。

    include_cold（业务决策 2026-08，律所方：「我们有很多的客服，全部都得推给
    客服，不能躺死在对话里」）：冷线索也推。原口径把冷线索留在库里不打扰人，
    前提是人手紧张——律所侧不是这个前提，那条节流就从「保护」变成了「丢单」。
    系统只负责标好强弱，推不推由人手决定。

    注意它**只放开门槛，不放开频次**：下面三条节流照旧，
    所以一通对话仍然只推一张单，不会因为客户多说几句就连推五条。
    """
    if intent == signals.COLD and not include_cold:
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


def _any_recipient(store: Store, group: GroupProfile, settings: Settings) -> bool:
    """这条线索最终有没有人能收到。只做判断，不改任何状态。"""
    if group.lawyer_userid or settings.default_notify_userid:
        return True
    return any(x.get("userid") for x in store.list_lawyers(active_only=True))


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
        previous, intent, gap_seconds=settings.lead_session_gap_seconds,
        include_cold=settings.notify_all_leads,
    )
    # 没有任何收件人时，notified_at 永远写不下去，于是 should_notify 每条消息
    # 都返回 True → 每条客户消息都白烧一次模型归纳，而没有人会看到结果。
    # 先探一次收件人，探不到就不归纳（线索照常入库，规则字段照常刷新）。
    if notify and not _any_recipient(store, group, settings):
        summarize = False
    lead = build_and_store(
        store, group, history, settings=settings,
        summarize=notify if summarize is None else summarize,
        previous=previous, urgent=urgent,
    )
    if lead is None:
        return lead
    # 「弱 → 强」再响一次。判定必须在评分**之后**，因为分数是这一轮才算出来的。
    # 客户边聊边变强（留电话 40 → 问赔多少 +15 → 问地址…），旧策略只认冷/温/热
    # 三档意向，这些变化全在「温」里，于是客服永远只收到最早那条弱意愿提醒。
    reason = worth_renotifying(previous, lead)
    upgraded = bool(reason)
    if upgraded and not notify:
        notify = True
        # 刚才为省成本跳过了模型归纳，而这条恰恰是最该有摘要的一条：
        # 客服要凭它决定现在放下手头的事去接
        if summarize is None:
            lead = build_and_store(
                store, group, history, settings=settings, summarize=True,
                previous=previous, urgent=urgent,
            ) or lead
    # 派单在通知之前：交接单要推给被派到的律师，而不是笼统的接待人。
    # 名册为空时 ensure 回落旧链路（会话承办人/全局兜底），行为与旧版完全一致。
    to, newly_assigned = assignment.ensure(store, group, lead, settings)
    # 刚接手的律师必须拿到单子：不能因为这条线索之前通知过别人就被节流掉
    notify = notify or newly_assigned
    if not notify:
        return lead
    lead = store.get_lead(group.group_id) or lead  # 取回含指派信息的最新版
    if not to:
        # **该推却没有收件人**：名册为空、兜底接收人没配、会话档案也没有承办人。
        # 原来这里是句无声的空转——线索照样入库评分，控制台里一切正常，
        # 而那张交接单一个人也收不到，日志里连一行都没有。
        # 这正是 2026-08-06 记下的那类最贵的 bug，代码路径当时并没有真堵上。
        logger.error(
            "线索 %s 已就绪却无人可推（名册为空且未配 default_notify_userid），"
            "交接单未发出", group.group_id,
        )
        store.set_note(
            "lead_no_recipient",
            f"{datetime.now():%m-%d %H:%M} 线索 {group.group_id} 无收件人",
        )
    if sender and to:
        text = format_notification(lead, group, settings)
        if reason:
            # 第二条提醒必须一眼看出跟第一条不一样，否则客服会当成重复推送划走
            text = _RENOTIFY_PREFIX[reason] + "\n\n" + text
        if sender.send_direct_text(to, text):
            store.mark_lead_notified(group.group_id)
            # 瞬态标记（不入库）：告诉调用方「这一轮真的推送了」。
            # service 据此跳过同一条消息的逐条提醒——律师不该为一件事收到两条 DM。
            lead["_notified_now"] = True
            logger.info("lead notified: %s → %s", group.group_id, to)
    return lead

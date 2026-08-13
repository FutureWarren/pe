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

from responder.engine import rules

# 中国大陆手机号：先剔除分隔符再匹配，兼容「177 2127 5495」「177-2127-5495」
_SEP = re.compile(r"[\s\-—－]")
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
# 座机（区号可选）：021-51697771 / 0755 12345678
_TEL = re.compile(r"(?<!\d)0\d{2,3}\d{7,8}(?!\d)")

_WECHAT_HINT = re.compile(r"(微信同号|微信是|加(我|个)?微信|vx|wx)[:：]?", re.I)

# ---------------------------------------------------------------------------
# **词表只写一遍。**
# 这一层和 rules 判的是同一批说法（要联系方式、问收费、约见、想委托、问所址），
# 各写一套的代价回测里量出来了：规则层认得「留个电话给我」，信号层不认得，
# 于是 AI 承接得好好的，**转接却一次也不触发**——客户在要人，系统看不见。
# 所以凡是 rules 已经成文的，这里直接复用它的模式，只补 rules 用不上的说法。
def _rules_hit(patterns, text: str) -> bool:
    return any(p.search(text) for p in patterns)


_R_ENGAGE = [re.compile(p) for p in rules.ENGAGE_PATTERNS]
_R_MEETING = [re.compile(p) for p in rules.MEETING_PATTERNS]
_R_FEE = [re.compile(p) for p in rules.FEE_PATTERNS]
_R_WANT_CONTACT = [re.compile(p) for p in rules.WANT_LAWYER_CONTACT_PATTERNS]

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
# 客户主动要律师联系他——这是整通对话里最强的成交信号，比他留下号码还强，
# 因为那是他自己要往前走。真机漏过一整串：「现在有律师可以直接让我联系的吗」
# 「让律师直接来电」——一条都没命中，于是这单被判成弱意愿躺在队列里。
_WANT_CONTACT = re.compile(
    r"(电话(联系|多少|号码)|打(我|个)?电话|留(个)?(电话|联系方式)|怎么联系|"
    r"(律师|你们|谁).{0,8}(联系|对接|沟通|回电|来电|打给)我|"
    r"(让|叫|请|麻烦).{0,6}(律师|人).{0,6}(来电|回电|打|联系|沟通)|"
    r"有(没有)?(律师|人).{0,10}(联系|沟通|对接|说话|接)|"
    r"(直接|马上|现在|尽快).{0,6}(联系|对接|沟通)我)"
)

# 人身伤害正在发生：住院、受伤、抢救。对律所来说这是最该当场接住的一类，
# 而它此前一分不加——真机里客户反复说「朋友已经在医院了」「需要快一点」，
# 系统给出的结论是「弱意愿」。
_INJURY = re.compile(
    r"(在|进|送|住)(医院|急诊|ICU|重症)|住院|抢救|昏迷|重伤|骨折|"
    r"(伤得|伤情).{0,4}(重|严重)|命危|生命危险|做手术|开刀"
)

# 客户自己说「急」。不是所有「急」都等于法律意义上的紧急（拘留/开庭），
# 但它一定意味着**这个人现在就要有人回应他**——而那正是排队顺序要回答的问题。
_URGENT_PLEA = re.compile(
    r"(很|非常|特别|真的|都|挺)?(着)?急([了死坏]|得很)?|"
    r"(尽快|马上|立刻|赶紧|快一点|快点|等不了|来不及|拖不起)"
)
_FEE = re.compile(r"(收费|费用|多少钱|价格|报价|律师费)")

HOT, WARM, COLD = "hot", "warm", "cold"

# 「这个人已经在找真人了」的信号。必须与 `priority.WANTS_HUMAN` 保持一致——
# 转接清单认得的信号如果在这里不算 hot，那条消息就会掉进「冷消息」分支，
# 线索晚一轮才出、转接跟着晚一轮。两者的一致性由
# tests/unit/test_handoff.py::test_handoff_checklist_matches_hot_signals 守着。
HOT_SIGNALS = {"contact", "meeting", "engage", "want-contact", "wechat", "injury",
               # 律所方 2026-08-10：问所址、问收费一律叫真人。
               # 这两件事都发生在客户从「了解」转向「决定」的那一刻——
               # 他要听的是一个具体的人，而不是一段听起来很周到的话。
               "office", "fee",
               # 筛查达标（2026-08-13）：客户把案情说清楚了 → 该叫人了。
               # **detect() 刻意不发这个信号**：它是一段对话的累计状态
               # （见 engine/screening.py），不是单条消息的属性，
               # 且群聊里不存在「叫人」。只由 service 在一对一进线窗口
               # 经 `scan(extra_hits=)` 注入。
               "screened"}


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
    if _MEETING.search(text) or _rules_hit(_R_MEETING, text):
        hits.append("meeting")
    if _ENGAGE.search(text) or _rules_hit(_R_ENGAGE, text):
        hits.append("engage")
    if _WANT_CONTACT.search(text) or _rules_hit(_R_WANT_CONTACT, text):
        hits.append("want-contact")
    # 问所址/路线/几点上班 = 这个人打算上门。答案我们当场就给了
    # （rules 的 office-fact 那一层），但**人也该叫**——走到问路这一步的客户，
    # 离到所里只差一次确认。
    if rules.office_fact_hit(text):
        hits.append("office")
    if _INJURY.search(text):
        hits.append("injury")
    if _URGENT_PLEA.search(text):
        hits.append("urgent-plea")
    if _FEE.search(text) or _rules_hit(_R_FEE, text) or rules.FEE_BROAD.search(text):
        hits.append("fee")

    if HOT_SIGNALS & set(hits):
        return HOT, hits
    return COLD, hits


def rank(*levels: str) -> str:
    """取一组意向中的最高级别。"""
    for level in (HOT, WARM, COLD):
        if level in levels:
            return level
    return COLD


def scan(
    history: list[dict], extra_hits: list[str] | None = None
) -> tuple[str, str, list[str]]:
    """扫一段对话的全部客户发言，返回 (意向, 联系方式, 信号)。

    纯规则、零成本——先用它决定「要不要花钱调模型」，而不是反过来。

    `extra_hits`：**光看字面看不出来的信号**。典型是一声「好的」——
    它本身一个词都不命中，可如果我们上一句正是「来所里一趟」，
    那这声「好的」就是「客户已答应面谈」，是整条漏斗上最值钱的一个信号。
    判断层已经认出来了（`rules.classify(awaiting=)`），这里只负责别把它丢掉：
    丢了的后果是律师收到一张既没电话、也没写「他答应来了」的冷单。
    """
    contact, hits, levels = "", set(extra_hits or []), []
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
    # 意向要连 extra_hits 一起算，口径与 detect() 相同：注入的信号
    # （答应面谈的「好的」、筛查达标的 screened）恰恰是最热的那种，
    # 只进清单不进意向的话，这条线索会顶着 hot 信号被标成 cold。
    level = rank(*levels)
    if HOT_SIGNALS & hits:
        level = HOT
    return level, contact, sorted(hits)

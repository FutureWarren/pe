"""线索优先级评分：回答「同时进来十个客户，律师先给谁打电话」。

设计约束（docs/lead-routing.md 是本模块的业务定义，两者必须同步演进）：

1. **纯规则、可解释**——每一分都能指着对话说出来由。律师看到的不是一个黑盒分数，
   而是「已留手机号 +40 · 明确委托意向 +35」这样的依据清单；不信任的评分没人会照着排班。
2. **可行动性优先于热情**——留了电话的客户永远比没留电话的客户可行动：
   打得通的号码是所有跟进动作的前提，因此联系方式是权重最高的单项。
3. **紧急压倒评分**——拘留/传唤/开庭临近不参与排队，直接置顶（P0 加急）。
   这是合规护栏的延伸：紧急情形的第一响应不允许被任何排序算法延后。

分层（阈值在 config 可调，调整须人工确认——见 CLAUDE.md 判断阈值条款）：
  P0 强意愿  score ≥ 60：一小时内联系
  P1 有意愿  score ≥ 30：当天联系
  P2 一般    其余：48 小时内跟进或仅归档
"""

import re

from responder.config import Settings, get_settings
from responder.engine import signals

P0, P1, P2 = "P0", "P1", "P2"

TIER_ZH = {P0: "强意愿", P1: "有意愿", P2: "一般"}
TIER_SLA_ZH = {P0: "1 小时内联系", P1: "今天内联系", P2: "48 小时内跟进"}

# 给客服看的只有两档（业务决策 2026-08，律所方：「只用标记好是强意愿还是
# 弱意愿就好了」）。三档留在内部——评分、督办时限、看板都还要用它——
# 但推到人手上的那张单只需要回答一个问题：这单现在打还是排队打。
# 分得越细，接单的人越要停下来想「P1 和 P2 差在哪」，那一停就是摩擦。
STRONG, WEAK = "强意愿", "弱意愿"


def bucket(tier: str) -> str:
    return STRONG if tier == P0 else WEAK

# 各信号的分值与展示文案。key 与 signals.detect 的命中名对齐，
# 权重依据见 docs/lead-routing.md「强意愿的定义」一节。
_SIGNAL_POINTS: list[tuple[str, int, str]] = [
    ("contact", 40, "已留电话"),
    ("engage", 35, "明确委托意向"),
    ("meeting", 30, "想面谈/来所"),
    ("want-contact", 25, "主动要律师电话"),
    ("wechat", 20, "要加微信详聊"),
    ("fee", 15, "问到收费"),
]

_URGENT_POINTS = 25
_DEADLINE_POINTS = 10
_AMOUNT_HIGH, _AMOUNT_HIGH_POINTS = 100_000, 10
_AMOUNT_LOW, _AMOUNT_LOW_POINTS = 10_000, 5
_DEPTH_MSGS, _DEPTH_POINTS = 6, 5

# 金额：只认带单位的写法，纯数字串一概不碰（手机号/案号会误伤）。
# 「17万」「3.5万元」「50000元」「8000块」
_AMOUNT_WAN = re.compile(r"(\d+(?:\.\d+)?)\s*万")
_AMOUNT_YUAN = re.compile(r"(?<![\d.])(\d{4,9})\s*(?:元|块)")
# 时限压力：开庭/时效/截止将近——晚打一天电话可能就来不及
_DEADLINE = re.compile(
    r"(开庭|庭审|仲裁时效|诉讼时效|时效(快|要)?(到|过)|截止|最后期限|就这几天|"
    r"下(周|个?月)(就)?(开庭|仲裁)|马上.{0,4}(开庭|截止|到期))"
)


def _max_amount(texts: list[str]) -> int:
    best = 0
    for t in texts:
        for m in _AMOUNT_WAN.finditer(t):
            try:
                best = max(best, int(float(m.group(1)) * 10_000))
            except ValueError:
                continue
        for m in _AMOUNT_YUAN.finditer(t):
            best = max(best, int(m.group(1)))
    return best


def evaluate(
    history: list[dict],
    *,
    urgent: bool = False,
    settings: Settings | None = None,
) -> tuple[int, str, list[dict]]:
    """对一段会话（应为 lead.current_session 切好的单次咨询）评分。

    返回 (score, tier, factors)；factors = [{key, label, points}]，
    既入库供控制台展示，也进推送文本让律师看到「为什么是它优先」。
    """
    settings = settings or get_settings()
    client_texts = [
        m.get("content", "") for m in history if not m.get("sender_is_staff")
    ]
    _, _, hits = signals.scan(history)

    score = 0
    factors: list[dict] = []

    def add(key: str, points: int, label: str) -> None:
        nonlocal score
        score += points
        factors.append({"key": key, "label": label, "points": points})

    for key, points, label in _SIGNAL_POINTS:
        if key in hits:
            add(key, points, label)

    if urgent:
        add("urgent", _URGENT_POINTS, "紧急情形")

    joined = client_texts
    amount = _max_amount(joined)
    if amount >= _AMOUNT_HIGH:
        add("amount", _AMOUNT_HIGH_POINTS, f"涉及金额约 {amount // 10_000} 万")
    elif amount >= _AMOUNT_LOW:
        add("amount", _AMOUNT_LOW_POINTS, f"涉及金额约 {amount // 10_000} 万")

    if any(_DEADLINE.search(t) for t in joined):
        add("deadline", _DEADLINE_POINTS, "有时限压力")

    if len([t for t in client_texts if t.strip()]) >= _DEPTH_MSGS:
        add("depth", _DEPTH_POINTS, "沟通深入")

    score = min(score, 100)
    if urgent or score >= settings.priority_p0_threshold:
        tier = P0
    elif score >= settings.priority_p1_threshold:
        tier = P1
    else:
        tier = P2
    return score, tier, factors


def factors_line(factors: list[dict]) -> str:
    """推送文本里的依据行：「已留电话 +40 · 明确委托意向 +35」。"""
    return " · ".join(f"{f['label']} +{f['points']}" for f in factors)

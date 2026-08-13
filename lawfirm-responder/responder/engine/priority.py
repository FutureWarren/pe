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
（权重表 2026-08-08 补三项：主动要律师联系、有人受伤/住院、客户明确表示很急。
  依据见 _SIGNAL_POINTS 处注释——真机里一个「朋友在医院、反复说急、要律师
  直接来电、还留了号码」的客户被判成弱意愿，这是权重表漏项，不是阈值问题。）
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


# ------------------------------------------------------------------ 转不转人工
# 律所方 2026-08-09 拍板：**转人工不看分数，看清单。**
#
# 分数回答的是「十个人同时进来，先给谁打电话」——那是排队问题，需要排序。
# 「现在要不要把真人叫过来」是另一个问题，它只要回答一句话：
# 客户是不是已经在要真人了。把后者绑在「分数够不够 60」上，就会出现
# 律所方真机遇到的那一幕：客户明明说了「让律师给我打电话」，
# 系统还在算他够不够格。
#
# 当初那道门槛是怕转太多（一周 416 人进私信）。这个顾虑现在不成立了：
# 转接后 AI 全程接着陪（不清转接状态，律师随时能开口接手，
# 见 engine/decision.py）。转多了的代价只是企微里多几个待接窗口，
# 不再是客户被晾着——**代价变了，规则就该跟着变**。
#
# 顺序即展示优先级：命中多条时报最能说明问题的那一条。
WANTS_HUMAN: list[tuple[str, str]] = [
    ("engage", "客户说要委托 / 要找律师"),
    ("meeting", "客户想来所里面谈"),
    ("want-contact", "客户要律师直接联系他"),
    ("injury", "有人受伤 / 住院"),
    ("wechat", "客户要加微信详聊"),
    ("contact", "客户留了电话"),
    # 律所方 2026-08-10 拍板加入这两条。我原先把 fee 排除在外，理由是
    # 「问收费说明案子值钱，不说明客户想找人」——律所方的判断相反，
    # 而他们是对的：在律所这门生意里，问价的人已经在比较，
    # 而比较的时候听到的是不是一个真人，直接决定他去谁那儿。
    ("fee", "客户问到收费"),
    ("office", "客户在问所址 / 怎么走 / 几点上班（准备上门）"),
    # 应转尽转（2026-08-12 律所方拍板）：「但凡客户提供了他们的任何信息，
    # 或者客户体现出了任何的意愿，我们都把他们转接到人工客服。」
    # 于是清单收底一条：客户把自己的情况说出来了——哪怕一个强信号都没带。
    # 只等强信号的代价律所算过账：其他有意愿的客户全部流失，是一笔巨大的损失。
    # 转接的含义也随之变了：它是「让这个客户出现在人工的工作台里」，
    # **不是「AI 闭嘴」**——真人开口之前 AI 接着陪、接着把案情问全
    # （见 engine/decision.py 的门控），两头都不耽误。
    ("substance", "客户把自己的情况说出来了"),
]

# 仍然不在清单里：`depth`（聊得久）、`deadline`（有时限）、`amount`（金额大）、
# `urgent-plea`（自己说很急）。这些说明**案子值钱或客户着急**，
# 不说明**他在找人**——转过去客户会愣一下（他只是在讲自己的事），
# 而 AI 本可以接着把案情摸清楚。它们照常加分，照常影响跟进顺序。


def wants_human(hits: list[str], *, urgent: bool = False) -> str:
    """客户做了「想找真人」的动作吗？返回那句人话，没有则空串。

    紧急（拘留/传唤/开庭临近）压倒一切：他没开口要人，但等他开口就晚了。
    """
    if urgent:
        return "紧急情形（拘留 / 传唤 / 开庭临近）"
    hit = set(hits or [])
    for key, label in WANTS_HUMAN:
        if key in hit:
            return label
    return ""

# 各信号的分值与展示文案。key 与 signals.detect 的命中名对齐，
# 权重依据见 docs/lead-routing.md「强意愿的定义」一节。
_SIGNAL_POINTS: list[tuple[str, int, str]] = [
    ("contact", 40, "已留电话"),
    ("engage", 35, "明确委托意向"),
    ("meeting", 30, "想面谈/来所"),
    # 真机 2026-08-08：客户朋友出车祸住院，反复说「需要快一点」「我现在都很
    # 紧急了」，还主动要律师直接来电——系统给出的结论是【弱意愿】，
    # 因为这三件事一分都不加。这恰恰是律所最想当场接住的那类客户。
    #
    # 「有人受伤正在医院」不是普通的意向强弱：它意味着时间在往外流
    # （伤情鉴定、责任认定、医疗票据都有窗口），也意味着客户此刻正处在
    # 最需要有人接住的时刻。排队顺序要回答的问题就是「谁最该现在被联系」，
    # 而这类人一定排在只留了个电话的人前面。
    ("want-contact", 25, "主动要律师联系"),
    ("injury", 25, "有人受伤/住院"),
    ("wechat", 20, "要加微信详聊"),
    ("fee", 15, "问到收费"),
    # 客户自己说急。单独不足以定档（谁都会说急），但配上受伤或留电话
    # 就是那条压垮骆驼的稻草——真机里正是这一分之差把 P0 压成了 P1。
    ("urgent-plea", 10, "客户明确表示很急"),
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

"""筛查进度：客户到底把案情说清楚了没有。

律所方 2026-08-13 的原话，也是这一层存在的全部理由：
「我们不能在客户都没有描述清楚案情的情况下就转接给人工啊，那人工还是得再问一轮。」

**转接的门槛因此从「说了点什么」抬到「说清楚了」。** 前一版（应转尽转的第一稿）
把 `rules.has_substance` 直接当成转接信号——客户随口一句「公司拖欠工资」就转，
律师打开工作台看到的还是那一句，该问的一件没问，等于把 AI 这一环整个跳过。
律所方的观感是准确的：「有 AI 和没 AI 完全没区别」。

那么「说清楚」到底指什么？照律师接手时真正需要的东西定，四件：

  1. `when`     —— 什么时候开始的 / 走到哪一步了（时效与紧迫度的判断基础，
                   劳动仲裁一年、人身损害三年，这一件缺了律师第一句就得问）
  2. `who`      —— 对方是谁（公司还是个人，决定管辖与将来能不能执行到财产）
  3. `evidence` —— 手上有什么材料（**最硬的一件**：证据决定案子能不能打；
                   而且问这一句自带转化作用，客户去翻合同就是投入了成本）
  4. `want`     —— 他想要什么结果（「要回工资」和「要赔十万」是两单生意，
                   也是期望管理的起点）

判定全部走确定性规则，不进模型：这是转接的开关，必须可测、可解释、可复现。
模型那边只拿「还缺哪几件」去组织问话（见 `prompts.intake_user_prompt`），
问什么由模型即兴，**算没算够由规则说了算**。

两条刻意的宽松，都是为了不把客户扣在门外：
  · 门槛是四缺一（`screening_min_slots`，默认 3），不是四件全齐——
    有些案子天然缺一件（对方是谁在工伤里往往不必问），凑齐反而变成查户口；
  · 问满 `screening_max_rounds`（默认 4）轮仍不齐就**照转不误**，
    单子上写明缺哪几件。客户可能就是话少、可能在开车语音打字——
    为了凑满一格把一个热客户扣在 AI 手里，比少问一件贵得多。
"""

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 四件事的词表
# 每一条都照着「客户实际会怎么打字」写，不是照法律术语写。

# ① 时间线与阶段：具体时间、持续时长、或案件走到哪一步了
_WHEN = re.compile(
    r"(前年|去年|今年|上个?月|这个?月|上周|上个?星期|本周|昨天|前天|今天|早上|晚上)"
    r"|(\d{4}\s*年|\d{1,2}\s*月\s*\d{0,2}\s*[日号]?)"
    r"|([一二三四五六七八九十两半\d]+\s*(年|个?月|周|星期|天|日|小时)"
    r"(多|左右|以上|之前|以前|前|了)?)"
    # 阶段词：这些同样回答「走到哪一步了」
    r"|(报过?警|立案|受理|仲裁|起诉|开庭|判决|裁决|调解|上诉|执行|"
    r"通知书|传票|裁定|一审|二审|已经(谈|沟通|协商)过)"
)

# ② 对方是谁：主体类型 / 具体身份称谓
_WHO = re.compile(
    r"(公司|单位|厂|店|老板|雇主|用人单位|甲方|乙方|对方|被告|原告|"
    r"房东|租客|开发商|物业|中介|平台|银行|保险|医院|4S|司机|车主|"
    r"前夫|前妻|老公|老婆|丈夫|妻子|男方|女方|对方当事人|"
    r"合伙人|股东|供应商|客户|承包|包工头|同事|领导|经理)"
)

# ③ 材料与证据：律师最想先知道的一件
_EVIDENCE = re.compile(
    r"(合同|协议|聊天记录|微信记录|短信|录音|录像|监控|视频|照片|截图|"
    r"转账|流水|凭证|收据|发票|欠条|借条|工资条|考勤|打卡|social|社保|"
    r"病历|诊断|鉴定|报告|认定书|证明|证据|材料|文件|单据|字据|"
    r"离职证明|解除通知|辞退通知|判决书|裁决书)"
)

# ④ 诉求：他想要什么结果
_WANT = re.compile(
    r"(想要|我要|要回|讨回|拿回|追回|索赔|赔偿|补偿|赔|争取|争|"
    r"想争取|希望|想让|想问问能不能|能不能(拿|要|争|判)|"
    r"抚养权|探视|离婚|分割|过户|退款|退还|解除|恢复|撤销|"
    r"取保|减刑|无罪|不起诉|放出来|出来)"
)

SLOTS: dict[str, tuple[re.Pattern, str]] = {
    "when": (_WHEN, "什么时候开始的、现在走到哪一步"),
    "who": (_WHO, "对方是谁（公司还是个人）"),
    "evidence": (_EVIDENCE, "手上有哪些材料"),
    "want": (_WANT, "他最想要的结果"),
}

# 展示顺序＝追问的自然顺序：先时间线，再对方，再材料，最后诉求。
ORDER = ("when", "who", "evidence", "want")

SLOT_ZH = {k: v[1] for k, v in SLOTS.items()}


@dataclass
class Progress:
    """一通对话的筛查进度快照。"""

    filled: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    rounds: int = 0          # AI 已经问过几轮（实发的追问条数）
    exhausted: bool = False  # 问满上限还是没齐——照转，单子上写明缺什么

    @property
    def score(self) -> str:
        return f"{len(self.filled)}/{len(SLOTS)}"

    @property
    def missing_zh(self) -> list[str]:
        return [SLOT_ZH[k] for k in self.missing]

    @property
    def filled_zh(self) -> list[str]:
        return [SLOT_ZH[k] for k in self.filled]


def scan(
    convo: list[dict], *, min_slots: int = 3, max_rounds: int = 4,
    rounds: int = 0,
) -> Progress:
    """扫一段对话，算出四件事齐了几件。

    只看**客户自己说的话**：AI 问过「有合同吗」不算数，客户答了才算。
    这一点看着显然，写反了却很致命——AI 的追问里天然带着所有关键词，
    拿它去匹配，一轮问话就能把四格全点亮，门槛当场归零。
    """
    said = " ".join(
        (m.get("content") or "") for m in convo if not m.get("sender_is_staff")
    )
    filled = [k for k in ORDER if SLOTS[k][0].search(said)]
    missing = [k for k in ORDER if k not in filled]
    exhausted = rounds >= max_rounds and len(filled) < min_slots
    return Progress(filled=filled, missing=missing, rounds=rounds, exhausted=exhausted)


def ready(progress: Progress, *, min_slots: int = 3) -> bool:
    """案情算不算「说清楚了」——够门槛，或问到上限也就这样了。"""
    return len(progress.filled) >= min_slots or progress.exhausted


def summary_line(progress: Progress) -> str:
    """交接单上那一行：律师一眼看出这单的成色，以及还缺什么他得自己问。

    没有这一行，「AI 到底替我做了多少」就只能靠翻聊天记录去感觉——
    而感觉出来的结论上次是「有 AI 和没 AI 完全没区别」。
    """
    if not progress.missing:
        return f"筛查完成度：{progress.score}（四件都问到了）"
    return (
        f"筛查完成度：{progress.score}"
        f"（还缺：{'、'.join(progress.missing_zh)}）"
    )

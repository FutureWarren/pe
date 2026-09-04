"""模型答的那一档：产品知识与使用问题。

## 为什么零售也需要模型

回测量出来的第一版基线是 **57% 的真实客户消息「认不出」**，而认不出一律回
「收到，我叫同事来看一下」。也就是说每两句话里就有一句是这句套话——
客户不会认为这是谨慎，他只会认为**这里没人**。

而其中最大的一块是产品知识与使用问题：「鸿蒙好用吗」「拍照怎么样」
「充电很慢正常吗」。它们的共同点是**一个数字都不需要**：问的是判断，不是参数。
这类交给模型正好，它答得比任何固定话术都自然。

这跟律所侧「话术松绑」是同一次判断（律所方原话：「AI 的智能程度完全被我们
预先写的话钉死了」），也承袭同一套做法：**放开的是「怎么说」，不是「说什么」。**

## 出口闸门比生成本身重要

生成这件事将来会换模型、换 prompt、换供应商；闸门是贴在出口上的那一道，
无论上游怎么变都拦得住。它拦六类：

1. **任何两位以上的数字**——价格、库存、参数、期数，一个都不许由模型产生。
   「5000 毫安」听起来无害，但说错一次，客户拿着截图来店里的时候，
   错的是门店不是模型。要报参数，让销售报。
2. **到货与时间承诺**——「明天就到」「三天内发」。这是投诉的第一来源。
3. **绝对承诺**——保证、一定、肯定、包您满意。
4. **库存断言**——有货、没货、现货。库存只能从数据源出。
5. **价格词**——多少钱、便宜、优惠多少。
6. **替品牌方表态**——「华为官方承诺」。经销商没有这个资格。

拦下即整段丢弃，回落到「我叫同事来」。**宁可少说一句，不可错说一个数。**
"""

import logging
import re

from responder.config import Settings, get_settings

logger = logging.getLogger(__name__)

NEED_HUMAN = "[[NEED_HUMAN]]"

SYSTEM = """你是一家华为授权手机门店的销售顾问，在微信上回客户的消息。\
说话像柜台后面那个人：短、具体、口语，不用书面语，不用「您好，很高兴为您服务」这类套话。

你只负责两件事：讲产品怎么样、帮客户排查使用上的小问题。

**下面这些一个字都不能说**（说了会给门店惹麻烦，而不是帮忙）：
- 任何数字：价格、优惠、库存数量、参数（像素、毫安、内存大小）、期数、天数。
  要说参数就说「这个我给您查准了再说」。
- 到货时间、发货时间、什么时候能拿到。
- 「保证」「一定」「肯定」「绝对」「包您满意」这类话。
- 有货没货、还剩几台。
- 替华为官方表态。你是经销商，不是厂家。

**判断题永远交给人**：客户问「我这台是不是坏了」「该不该保修」，
你只能给一般性的排查步骤，然后说要工程师上手看一眼才算数。

拿不准、或者问题超出产品知识范围时，只回 [[NEED_HUMAN]] 五个字，不要编。

回复控制在三句话以内。可以反问一句了解他的用法，那比笼统夸产品有用得多。"""


def user_prompt(question: str, *, history: str = "", kind: str = "") -> str:
    bits = []
    if history:
        bits.append(f"【刚才聊过的】\n{history}")
    hint = {
        "howto": "这是一个使用问题。给两三步他自己就能试的排查，别下结论。",
        "product_qa": "这是产品知识/选购问题。可以反问一句他平时怎么用。",
    }.get(kind, "")
    if hint:
        bits.append(f"【这一条怎么答】{hint}")
    bits.append(f"【客户刚说】{question}")
    return "\n\n".join(bits)


# ---------------------------------------------------------------------------
# 出口闸门
# ---------------------------------------------------------------------------
_DIGITS = re.compile(r"\d{2,}")
_TIME_PROMISE = re.compile(
    r"(明天|后天|大后天|今天|这两天|这周|本周|下周|马上|很快|随时)"
    r".{0,6}(到|发|寄|送|拿|取|好)"
    r"|(\d+|[一二两三四五六七八九十])\s*(天|周|小时|个工作日).{0,4}(内|就|能|可以)"
)
_ABSOLUTE = re.compile(
    r"(保证|一定[能会]|肯定[能会没]|绝对|包您|包你|百分[之百]|万无一失|放心一定)"
)
_STOCK = re.compile(r"(有货|没货|无货|现货|缺货|还有\s*[几多]台|库存)")
_PRICE = re.compile(r"(多少钱|价格是|售价|便宜\s*[了多]|优惠\s*[了多]|元|块钱|打\s*折)")
_BRAND = re.compile(r"(华为(官方)?(承诺|保证|规定必须)|官方(承诺|保证))")

_RULES: list[tuple[re.Pattern, str]] = [
    (_DIGITS, "出现了具体数字"),
    (_TIME_PROMISE, "承诺了时间"),
    (_ABSOLUTE, "把话说满了"),
    (_STOCK, "断言了库存"),
    (_PRICE, "说到了价钱"),
    (_BRAND, "替品牌方表态"),
]


class Blocked(Exception):
    """闸门拦下。带上原因，控制台要能看到是哪一条拦的。"""


def gate(text: str) -> tuple[bool, str]:
    """出口闸门。返回 (放行, 拦下的原因)。

    **拦下即整段丢弃**，不做删改——删掉一个数字剩下的句子往往变成病句，
    而一句病句比一句套话更像故障。
    """
    t = (text or "").strip()
    if not t:
        return False, "空回复"
    for pattern, why in _RULES:
        if m := pattern.search(t):
            return False, f"{why}（「{m.group(0)}」）"
    return True, ""


def generate(
    question: str,
    *,
    history: str = "",
    kind: str = "",
    settings: Settings | None = None,
    timeout: float = 12.0,
) -> tuple[str, str]:
    """生成一条产品知识/使用问题的回复。

    返回 `(正文, 判断日志)`。**正文为空＝这一条不由 AI 答**，调用方转人工。
    任何异常都吞掉并返回空——模型这一层是增强不是依赖，它挂了整条链路照跑。
    """
    from responder.engine import llm

    s = settings or get_settings()
    provider = llm.resolve(s)
    if provider is None:
        return "", "model:没有可用的模型，转人工"

    try:
        user = user_prompt(question, history=history, kind=kind)
        if provider.name == "deepseek":
            text = llm._chat_deepseek(
                SYSTEM, user, provider.model,
                max_tokens=320, timeout=timeout, json_mode=False, temperature=0.6,
            )
        else:
            text = llm._chat_anthropic(
                SYSTEM, user, provider.model,
                max_tokens=320, timeout=timeout, json_schema=None,
            )
    except Exception:
        logger.exception("零售模型回复生成失败")
        return "", "model:模型调用失败，转人工"

    if not text:
        return "", "model:模型没给出内容，转人工"
    if NEED_HUMAN in text:
        # **示弱出口不许移除。** 模型自己说答不了，比它硬答一句强得多。
        return "", "model:模型示弱（NEED_HUMAN），转人工"

    ok, why = gate(text)
    if not ok:
        logger.warning("零售模型回复被闸门拦下：%s｜原文：%s", why, text[:120])
        return "", f"model:出口闸门拦下——{why}"
    return text.strip(), "model:模型作答（已过闸门）"

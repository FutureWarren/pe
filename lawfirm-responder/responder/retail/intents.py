"""零售意图识别：客户这句话到底在问什么。

律所那边的分类回答的是「该不该叫律师」，只有 8 个类；零售要回答的是
**「这句话能不能当场答掉」**，所以分得细得多——因为每多认出一类，
就少转一次人工，而转人工在零售是成本不是目标。

## 三档处理方式（`Handling`）

每个意图都标着它该怎么处理，这是整包最核心的一张表：

- `AUTO`   照着规矩答，不查任何系统。答案固定，写一次长期有效。
- `LOOKUP` 要先查数据（价格 / 库存 / 订单 / 工单）才能答。**查不到就不许答**——
           降级成 `HUMAN`，而不是让模型编一个看起来合理的数字。
- `HUMAN`  必须真人。要谈钱的、会出事的、带情绪的，一律不让 AI 处理。

## 售前 / 售后为什么分开标

**售后其实比售前更适合交给 AI**，这一点反直觉，但成立：

1. 售后问题标准化程度高得多——「我的货到哪了」有确定答案，
   「这台值不值得买」没有；
2. 售后是销售**最不愿意干**的活：成交后提成到手，注意力全在下一单，
   于是售后消息被晾着——这正是差评和投诉的头号来源；
3. 答错的代价更低——报错价格要赔钱，说错物流最多再查一次；
4. **售后客户是已经付过钱的人。** 晾着一个售前客户损失一单；
   晾着一个售后客户，损失的是复购、转介绍，还可能升级成投诉工单。

所以第一期上线的优先级建议是**售后优先**，见 `docs/retail-kuji.md`。
"""

import re
from dataclasses import dataclass
from enum import Enum


class Handling(str, Enum):
    """这一类意图该怎么处理。"""

    AUTO = "auto"      # 照规矩答，不查系统
    LOOKUP = "lookup"  # 查到数据才能答，查不到就转人工
    HUMAN = "human"    # 必须真人


class Stage(str, Enum):
    PRESALE = "presale"    # 售前
    AFTERSALE = "after"    # 售后（成交之后）
    BOTH = "both"


@dataclass(frozen=True)
class Intent:
    key: str
    zh: str
    stage: Stage
    handling: Handling
    pattern: re.Pattern
    needs: tuple[str, ...] = ()   # 要查哪个数据源才能答
    note: str = ""                # 为什么这么定档

    @property
    def can_auto(self) -> bool:
        return self.handling in (Handling.AUTO, Handling.LOOKUP)


def _p(s: str) -> re.Pattern:
    return re.compile(s)


# ---------------------------------------------------------------------------
# 售后（成交之后）——**排在售前前面**，因为已成交客户的问题更急、更该先认出来。
# 顺序即优先级：越靠前的越先匹配，写的时候把「更具体、更该优先处理」的放上面。
# ---------------------------------------------------------------------------
AFTERSALE: list[Intent] = [
    Intent(
        "complaint", "投诉与不满", Stage.BOTH, Handling.HUMAN,
        _p(r"(投诉|315|工商|消协|曝光|差评|骗人|坑人|欺骗|态度(很)?差|"
           r"什么破|垃圾|退钱|要个说法|告你们|不给我处理|拖了这么久)"),
        note="投诉永远不让 AI 处理。AI 只做一句安抚 + 立刻叫店长，"
             "任何解释都可能被当成门店的正式答复。",
    ),
    Intent(
        "refund_return", "退货退款换货", Stage.AFTERSALE, Handling.HUMAN,
        _p(r"(退货|退款|退了|想退|要退|换一台|换新|换货|七天|7天|无理由|"
           r"不想要了|后悔了|能不能退)"),
        note="涉及钱的往回走，且有三包法定条件要判。AI 可以讲清政策，"
             "但**不许承诺能不能退**——那是店里的决定。",
    ),
    Intent(
        "repair_status", "维修送修进度", Stage.AFTERSALE, Handling.LOOKUP,
        _p(r"(修好了(吗|没)|修得?怎么样|维修(进度|好了|完了)|送修|返厂|"
           r"什么时候能取|我那台(手机)?(修|好)|工单|保修单号)"),
        needs=("service_ticket",),
        note="客户等修最焦虑。查得到进度就直接说，查不到宁可转人工——"
             "「快了」这种话比不回还伤。",
    ),
    Intent(
        "order_status", "订单与物流", Stage.AFTERSALE, Handling.LOOKUP,
        _p(r"(订单|单号|发货(了吗|没|了没)|发了(吗|没)|物流|快递|到哪(了|儿了)|"
           r"什么时候(到|发)|几天能到|签收|派件|运单)"),
        needs=("order",),
        note="售后第一高频。这一类做好，AI 的价值当天就看得见。",
    ),
    Intent(
        "pickup", "到店自提", Stage.AFTERSALE, Handling.LOOKUP,
        _p(r"(自提|到店(取|拿|提)|取货|提货|去(取|拿)|"
           r"(能|可以|什么时候|何时).{0,3}(取|拿)|"
           r"到货(了吗|没|了没)|留(的|那台)机|备好了(吗|没))"),
        needs=("order", "store"),
        note="到店取货 = 又一次进店机会，答得快直接影响到店率。",
    ),
    Intent(
        "invoice", "发票", Stage.AFTERSALE, Handling.LOOKUP,
        _p(r"(发票|开票|专票|普票|税号|抬头|报销)"),
        needs=("order",),
        note="企业客户尤其在意。信息类问题，全自动最合适。",
    ),
    Intent(
        "warranty", "保修与三包", Stage.AFTERSALE, Handling.AUTO,
        _p(r"(保修|质保|三包|保多久|过保|保修期|延保|碎屏险|意外保|"
           r"人为损坏|进水|摔了.{0,6}(保|修|赔))"),
        note="政策类，答案固定。**但不许判定「你这台算不算人为损坏」**——"
             "那要工程师验机，AI 判了就是替门店做了承诺。",
    ),
    Intent(
        "activate", "激活与实名", Stage.AFTERSALE, Handling.AUTO,
        _p(r"(激活|实名|开机|首次(开机|设置)|华为账号|云服务|"
           r"卡(装|插)|双卡|eSIM|开卡)"),
        note="纯操作指导，AI 答得比人还耐心。",
    ),
    Intent(
        "data_migration", "数据迁移", Stage.AFTERSALE, Handling.AUTO,
        _p(r"(换机|数据.{0,2}(迁移|转移|导|传)|通讯录.{0,4}(导|传|转|弄|搬)|"
           r"照片.{0,4}(导|传|转|弄|搬)|微信(聊天)?记录.{0,4}(导|传|迁|转|弄|搬|怎么)|"
           r"手机克隆|一碰传|旧机数据|(旧|老)(机|手机).{0,6}(数据|东西|内容).{0,4}(导|传|转|弄|搬))"),
        note="高频且完全标准化，是自动化性价比最高的一类。",
    ),
    Intent(
        "tradein_balance", "以旧换新尾款与旧机", Stage.AFTERSALE, Handling.HUMAN,
        _p(r"(旧机.{0,3}(款|钱|到账|多少)|抵扣.{0,3}(款|到账|多少)|尾款|补差价|"
           r"回收.{0,3}(款|价|到账)|折价.{0,3}(款|多少))"),
        note="钱的事 + 验机结论，一律真人。AI 说个数字就可能被当成承诺。",
    ),
    Intent(
        "installment", "分期与还款", Stage.BOTH, Handling.AUTO,
        _p(r"(分期|免息|花呗|信用卡|白条|每月还|还款(日|多少)|利息|手续费)"),
        note="政策与规则可以答，**具体某个人的还款金额不许算**——"
             "那是金融机构的账，算错了是纠纷。",
    ),
    Intent(
        "accessory", "配件与贴膜", Stage.BOTH, Handling.LOOKUP,
        _p(r"(贴膜|钢化膜|手机壳|保护壳|充电器|数据线|耳机|"
           r"充电头|移动电源|配件|原装)"),
        needs=("catalog",),
        note="低价高频，还是进店由头。能自动答就自动答。",
    ),
]

# ---------------------------------------------------------------------------
# 售前
# ---------------------------------------------------------------------------
PRESALE: list[Intent] = [
    Intent(
        "buy_now", "要下单 / 谈价格", Stage.PRESALE, Handling.HUMAN,
        _p(r"(下单|订一台|要一台|买了|给我留|能不能便宜|便宜点|优惠点|"
           r"少点|最低多少|抹个零|再送点|谈谈价|老板价|团购|批发|"
           r"公司(采购|统一买)|要(十|几十|\d{2,})台)"),
        note="谈价和成交是销售的活，也是他的提成。AI 碰这块只会坏事。",
    ),
    Intent(
        "tradein_quote", "以旧换新估价", Stage.PRESALE, Handling.HUMAN,
        _p(r"(以旧换新|旧(机|手机).{0,6}(抵|换|折|值|回收)|"
           r"我这台.{0,8}(能抵|能换|值多少|回收)|换购)"),
        note="必须验机才能定价。AI 只能给区间并引导到店，"
             "给准数就是替门店报价。",
    ),
    Intent(
        "price", "问价格", Stage.PRESALE, Handling.LOOKUP,
        _p(r"(多少钱|什么价|价格|报价|售价|几个钱|贵吗|"
           r"(现在|now).{0,4}多少)"),
        needs=("catalog",),
        note="最高频。**价格只能从 catalog 直出**，查不到宁可转人工。",
    ),
    Intent(
        "stock", "查库存与配色", Stage.PRESALE, Handling.LOOKUP,
        _p(r"(有货(吗|没)|有没有货|现货|还有(吗|没)|缺货|"
           r"(什么|哪些)(颜色|配色)|多大(内存|存储)|\d+\+\d+|"
           r"到货|补货|预订|预约)"),
        needs=("catalog", "store"),
        note="查不到库存时的正确动作是「我帮您问一下店里」+ 转人工，"
             "不是猜一个「应该有」。",
    ),
    Intent(
        "store_info", "门店位置与营业时间", Stage.BOTH, Handling.AUTO,
        _p(r"(门店|店在哪|地址|怎么走|导航|位置|几点(开|关|上班|下班)|"
           r"营业(时间|到几点)|周末(开|营业)|停车|哪个店|附近|最近的店)"),
        needs=("store",),
        note="纯信息，全自动。**答得好直接带来到店率。**",
    ),
    Intent(
        "compare", "机型对比与选型", Stage.PRESALE, Handling.AUTO,
        _p(r"(和|跟|与).{0,12}(比|区别|差别|哪个好|选哪|哪款)|"
           r"(推荐|建议).{0,6}(哪|什么)(款|型号|机)|"
           r"(值不值得|要不要).{0,4}买|(适合|够用)吗"),
        note="参数是公开的，AI 讲得比人清楚。**但不许贬低任何品牌**——"
             "经销商对外口径有品牌方规范。",
    ),
    Intent(
        "promo", "活动与优惠", Stage.BOTH, Handling.AUTO,
        _p(r"(活动|优惠|促销|补贴|国补|以旧换新补贴|礼包|赠品|送什么|"
           r"节(日|假).{0,4}(活动|优惠)|双11|618|开学季)"),
        note="活动规则天天变，所以放进知识库由店里自己维护，"
             "改一条立刻生效，不用找技术。",
    ),
    Intent(
        "book_visit", "预约到店", Stage.PRESALE, Handling.LOOKUP,
        _p(r"(预约|约个时间|下午过去|明天(去|来)|待会(去|过来)|"
           r"现在过去|留一台|帮我留)"),
        needs=("store",),
        note="强意向信号。AI 接住时间和门店，然后**立刻叫销售**。",
    ),
]

ALL: list[Intent] = AFTERSALE + PRESALE
BY_KEY: dict[str, Intent] = {i.key: i for i in ALL}

# 「已经成交过」的信号：客户提到订单、发票、保修、我买的那台……
# 用来判断这通对话该按售后还是售前理解——同一句「什么时候到」，
# 售前问的是到货，售后问的是快递。
_BOUGHT = re.compile(
    r"(我(买|订|下)的|上(周|个月|次)买|前(几)?天(买|订)|已经(买|付|下单)|"
    r"订单|单号|发票|保修|我那台|刚提的|提回来)"
)


def looks_after_sale(text: str) -> bool:
    """这句话是不是站在「已经买过了」的立场说的。"""
    return bool(_BOUGHT.search(text or ""))


def detect(text: str, *, after_sale: bool = False) -> Intent | None:
    """认出这句话属于哪一类。认不出返回 None（交给通用路径或转人工）。

    `after_sale`：这通对话已知处于售后阶段（客户提过订单号、或系统里查得到
    他的成交记录）。它只调整**匹配顺序**，不改变结论——售后优先扫售后表，
    因为同一句「什么时候到」，售前问的是到货、售后问的是快递，
    答反了比不答更糟。
    """
    t = (text or "").strip()
    if not t:
        return None
    tables = ([AFTERSALE, PRESALE] if (after_sale or looks_after_sale(t))
              else [PRESALE, AFTERSALE])
    ordered = [i for tb in tables for i in tb]
    # **必须真人的那些先扫。** 这不是优化，是安全约束：
    # 「旧机的钱什么时候到账」里含着「什么时候到」，会被查物流那条先吃掉，
    # 于是一个**钱的问题**被判成信息类，AI 就去代答了——这正是这套系统
    # 最不能犯的错。靠「把危险的写在列表前面」来防，只能防到下一次有人
    # 往列表中间插一行为止；改成按档位分两遍扫，顺序怎么改都不会出这个错。
    for intent in ordered:
        if intent.handling is Handling.HUMAN and intent.pattern.search(t):
            return intent
    for intent in ordered:
        if intent.handling is not Handling.HUMAN and intent.pattern.search(t):
            return intent
    return None


def handling_of(text: str, *, after_sale: bool = False) -> Handling:
    """认不出来的一律按 HUMAN 处理——**不认识就别答**。

    这条默认值是整包最重要的一行。反过来（认不出就让模型自由发挥）
    在律所场景尚可（最坏是答得泛），在零售是灾难：模型会自信地
    编出一个价格、一个库存、一个「明天就能到」。
    """
    intent = detect(text, after_sale=after_sale)
    return intent.handling if intent else Handling.HUMAN

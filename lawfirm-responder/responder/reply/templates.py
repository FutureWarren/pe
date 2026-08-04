"""确定性话术模板：按响应模式 × 客户状态 × 问题类型组装。

结构遵循方案 3.2：
  ① 直接回答：共情开场（按情绪/时段自适应）→ 一般性法律框架 → 自然收尾
  ② 安抚承接：确认收到 + 需律师核实 → 给出预期 →（未成交群）面谈引导
每类承接话术有多个变体，按 msg_id 稳定选取——同一条消息永远得到同一话术（可测），
不同消息之间有变化（不像机器人复读）。追问场景另有「二次安抚」话术，不复读原话。

群上下文（案件类型、承办律师姓名）注入话术，避免「通用机器人感」。
模板文本改动必须人工审核后合并（见 CLAUDE.md）。
"""

import re
import zlib
from datetime import datetime

from responder.compliance.disclaimer import DISCLAIMER
from responder.config import Settings, get_settings
from responder.models import Category, ClientStatus, GroupProfile


def _lawyer(group: GroupProfile) -> str:
    """对客户话术里怎么称呼「那位律师」。

    只有已委托客户才点名。业务决策 2026-08：新咨询一律不说具体是谁——

      1. **可能说错人**：谁接这单是分案引擎按专长与负载算出来的
         （见 docs/lead-routing.md），而客服/私信会话建档时填的 lawyer_name
         只是一个配置默认值（`kf_default_lawyer_name`）。AI 说「魏律师会给您
         回电话」、实际派给了别人，客户等的就是个不会来的电话。
      2. **不该提前承诺人**：咨询阶段还没有「承办律师」这回事。

    群聊相反：那里的 lawyer_name 是人工维护的真名，律师本人也在群里，
    点名才显得「这是我的案子、有人在管」，不能一刀切抹掉。
    """
    if group.is_kf:
        return "律师"
    return f"{group.lawyer_name}律师" if group.lawyer_name else "承办律师"


def _case(group: GroupProfile) -> str:
    return f"您{group.case_type}案件" if group.case_type else "您的案件"


def _in_group(group: GroupProfile) -> str:
    """答复发生在哪儿。

    群聊里说「在群里回您」是自然的；一对一客服窗口（微信客服/抖音私信）根本没有群，
    对着私聊窗口说「我在群里回您」，客户会以为要被拉进某个群，或者干脆看不懂。
    客服会话已经是主进线通道，这个词必须跟着渠道走。
    """
    return "" if group.is_kf else "在群里"


def _pick(variants: list[str], seed: str) -> str:
    """按 seed 稳定选取变体：同一 seed 永远同一条。"""
    return variants[zlib.crc32(seed.encode()) % len(variants)]


# ---------------------------------------------------------------- ② 承接类
# 语感依据见 docs/voice-guide.md：先接住，再给预期；短句；不打公文腔。
def handoff_case_status(group: GroupProfile, seed: str = "") -> str:
    L, C, G = _lawyer(group), _case(group), _in_group(group)
    variants = [
        f"收到，{C}的最新进展我帮您问下{L}哈，一有消息马上{G}回您。",
        f"看到您消息了。具体进展得{L}那边确认，我已经跟{L}说了，回头就答复您。",
        f"收到您的消息，这个我帮您催一下{L}，他核实了就{G}回您。",
    ]
    return _pick(variants, seed)


def handoff_fee(group: GroupProfile, seed: str = "") -> str:
    L = _lawyer(group)
    if group.client_status == ClientStatus.PROSPECT:
        variants = [
            f"收到。费用这块跟案子具体情况关系很大，得{L}了解情况后才能给您准数，我已经转达了。\n"
            f"方便的话可以约个时间当面聊，把您的情况一次说清楚。",
            f"这个得{L}结合您的情况来说才准确，我让他尽快跟您联系哈。"
            f"要是方便，也欢迎约个时间来所里坐坐，当面把情况理一理。",
        ]
    else:
        variants = [
            f"收到，费用的事由{L}跟您直接确认，我已经提醒他了，稍等哈。",
            f"看到您消息了。这块{L}会跟您直接沟通，我这边也催着，请您稍等。",
        ]
    return _pick(variants, seed)


def handoff_urgent(group: GroupProfile, seed: str = "") -> str:
    L = _lawyer(group)
    variants = [
        f"看到您的消息了，您先别急。这个情况比较要紧，我已经第一时间加急联系{L}了，"
        f"会尽快跟您联系。",
        f"收到，您先别慌，这个情况我们很重视。已经加急通知{L}了，尽快给您答复。",
    ]
    return _pick(variants, seed)


def handoff_contact(group: GroupProfile, seed: str = "") -> str:
    L = _lawyer(group)
    variants = [
        f"看到您消息了，{L}这会儿应该在忙。我已经提醒他了，忙完会第一时间回您。",
        f"在的。{L}可能暂时腾不出手，我已经跟他说了，看到就回您哈。",
        f"收到，我这就帮您叫一下{L}，他看到会尽快回复您。",
    ]
    return _pick(variants, seed)


def handoff_generic(group: GroupProfile, seed: str = "") -> str:
    L = _lawyer(group)
    variants = [
        f"收到您的消息，这个得让{L}来说比较准确，我已经转达了，请您稍等。",
        f"看到您消息了，这个我帮您转给{L}确认下，有回复马上告诉您。",
    ]
    return _pick(variants, seed)


def greeting_opener(group: GroupProfile, seed: str = "", contact_left: bool = False) -> str:
    """一对一客服的开场引导：接住客户，并请他说明情况（首轮筛查的第一步）。

    这是整条管道上最贵的一句话——抖音后台数据：416 人进私信只有 90 人开口，
    78% 的人看完第一句就走了。设计据此定三条：

    1. **先自报家门**（「上海松沪律师事务所」）：正式，让人知道这不是野鸡账号；
    2. **给例句**：客户照着说就行。让一个正焦虑的人「组织语言描述法律问题」
       门槛太高，给个具体例子是抬开口率最有效的杠杆；
    3. **明说不用讲究**：消除「说不好会不会被笑话」的顾虑。

    正式但不端着：用「您」、机构全称、完整句；不用「亲」「哈喽」，
    也不用「兹收悉」「特此告知」这类公文腔——书面公文和真人说话是两回事。

    客户刚留下联系方式时必须换一套话术——此时再问「您是什么情况」既冒犯又像机器人。
    不含任何法律实质内容，因此走确定性模板即可，无需模型。
    """
    if contact_left:
        L = _lawyer(group)
        variants = [
            f"好的，您的联系方式我已经记下，稍后由{L}与您电话联系。",
            f"收到，电话我记下来了。我先把您的情况整理给{L}，他会尽快与您联系。",
            f"好的，联系方式收到。我这边转给{L}，请您留意来电。",
        ]
        return _pick(variants, seed)
    variants = [
        "您好，这里是上海松沪律师事务所。\n"
        "您把遇到的情况说两句就行，不用特意组织语言——"
        "比如「公司欠了我三个月工资，还把我辞退了」这样，我先帮您理一理。",
        "您好，上海松沪律师事务所，我在的。\n"
        "您遇到的是什么事？简单说个大概就可以，"
        "比如「对方借钱不还，有转账记录」，我帮您看看该怎么处理。",
        "您好，这里是上海松沪律师事务所。\n"
        "麻烦您把情况大致讲一下，怎么想的就怎么说，"
        "比如「准备离婚，孩子抚养权想争取」，我先了解清楚再给您建议。",
    ]
    return _pick(variants, seed)


def greeting_again(group: GroupProfile, seed: str = "") -> str:
    """回访客户的再次问候：短，不重新自我介绍。

    这条补的是一个真实踩过的坑：老客户隔几天回来说一句「你好」，
    因为「一通对话只许一次开场白」而被降级成承接，于是 AI 回了
    「我帮您转给律师确认下」——转什么？他什么都还没说。

    对已经认识我们的人，正确的反应是接住并问一句近况，
    而不是把律所全称再报一遍（他上次就听过了），也不是答非所问。
    """
    variants = [
        "您好，我在的。\n是上次那件事有新情况，还是有别的想问？",
        "您好，看到您消息了。\n您这边是有新的进展，还是别的事想咨询？",
        "在的，您说。\n是接着上次那事，还是有别的想问的？",
    ]
    return _pick(variants, seed)


def handing_over(lawyer_name: str = "", seed: str = "") -> str:
    """把会话转给律师之前，先跟客户说一句。

    直接转会留下一段尴尬的静默（律师那边还没看到）。先说一句，客户才知道
    发生了什么、在等什么。

    **默认不点名**（业务决策 2026-08，见 CLAUDE.md 对客称呼一条）：客户读到
    名字就会等那个人，而转接后律师可能改派、可能没接手，等的就成了一个不会
    出现的人。`lawyer_name` 保留给「人已确定坐在对面」的场景（如群聊）。
    末句给一个确定的下一步：客户知道现在该干什么，就不会中途走掉。
    """
    who = f"{lawyer_name}律师" if lawyer_name else "我们的律师"
    variants = [
        f"您的情况我整理好，转给{who}了，稍等一下就在这个对话里回您。"
        "您要是还有材料（合同、聊天记录、工资条），可以先发过来。",
        f"我把您说的情况转给{who}了，看到会直接在这儿回您，您稍等。"
        "这会儿手边有相关材料的话，先发过来会更快。",
    ]
    return _pick(variants, seed)


def who_we_are(group: GroupProfile, seed: str = "") -> str:
    """客户问「你是人还是机器」时的回答。

    此前这句话落进默认沉默，客户什么也收不到——问这句的人本来就在怀疑，
    没人应更坐实了怀疑，是最差的处理。

    答什么：不明示 AI 身份（业务决策 2026-07），也不谎称自己是真人。
    两样都不必要——客户问这句真正想知道的是「我说的话有没有人当回事、
    专业意见到底谁给」。所以回答的是这个，然后立刻把话头交回给他，
    别让对话停在一个关于我们自己的话题上。
    """
    variants = [
        "我是所里负责接待咨询的，先帮您把情况理清楚；"
        "具体的法律意见由律师给您，我会把您说的完整转过去。\n您接着说，我在。",
        "我这边做的是接待，负责先了解情况、安排对接的律师，"
        "专业判断还是律师来下。\n您继续说就行，我记着呢。",
    ]
    return _pick(variants, seed)


def exchange_contact(
    group: GroupProfile, seed: str = "", settings: Settings | None = None
) -> str:
    """客户主动要律师电话时——反过来留下他的号码。

    不直接给号码有两个实在的理由：那是律师的个人信息；而且客户一旦自己拨过去，
    谁接的、聊了什么、跟没跟进，所里全不知道。

    更要紧的是这一刻的性质：**客户主动要电话，是整通对话里最强的成交信号**，
    比他被动留号码还强——那是他自己要往前走。这时候回一句「我帮您转达」
    是把伸出来的手放下了。正确的动作是当场换：留下他的号，律师主动打过去。
    """
    settings = settings or get_settings()
    L = _lawyer(group)
    addr = settings.office_address
    where = f"{settings.office_name}（{addr}）" if addr else settings.office_name
    variants = [
        f"这样更快：您把手机号发我，我马上让{L}给您打过来，"
        "省得您等接通、又要从头说一遍。\n"
        f"想直接过来也行，我们在{where}，来之前跟我说一声，我给您安排。",
        f"您留个手机号吧，我这就转给{L}，让他直接联系您——"
        "他手上的号码看到就回，比您一个个打过来省事。\n"
        f"或者您得空过来当面聊也行，地址是{where}。",
    ]
    return _pick(variants, seed)


def safe_fallback(group: GroupProfile) -> str:
    """合规拦截后的兜底回复：只承接，不含任何实质内容。固定文本，不走变体。"""
    L = _lawyer(group)
    return f"收到您的消息，这个得让{L}来说比较准确，我已经转达了，请您稍等。"


def second_touch(group: GroupProfile, urgent: bool = False) -> str:
    """客户追问同一件事时的二次安抚：不复读，升级姿态。"""
    L, G = _lawyer(group), _in_group(group)
    if urgent:
        return f"实在抱歉让您久等了，我刚又加急催了{L}，也跟所里其他同事说了，一定尽快回您。"
    return f"抱歉让您久等了，我刚又跟{L}那边催了一下，一有回复马上{G}告诉您。"


HANDOFF_BY_CATEGORY = {
    Category.CASE_STATUS: handoff_case_status,
    Category.FEE: handoff_fee,
    Category.URGENT: handoff_urgent,
    Category.CONTACT: handoff_contact,
}


def build_handoff(category: Category, group: GroupProfile, seed: str = "") -> str:
    return HANDOFF_BY_CATEGORY.get(category, handoff_generic)(group, seed)


# ---------------------------------------------------------------- ① 直接回答
_ANXIOUS = re.compile(r"(害怕|好怕|慌|担心|着急|焦虑|睡不着|紧张|心里没底|绝望|难受)")


def answer_opening(question: str, now: datetime | None = None) -> str:
    """按情绪与时段选择共情开场（确定性）。常规情况不加开场——直接说事最像真人。"""
    now = now or datetime.now()
    late = now.hour >= 22 or now.hour < 6
    anxious = bool(_ANXIOUS.search(question))
    if late and anxious:
        return "这么晚还没休息，能感觉到您心里不踏实，先别太担心。"
    if anxious:
        return "理解您心里着急，先别慌，我给您说说一般的情况。"
    if late:
        return "这么晚还在为这事操心，辛苦了。"
    return ""


# 收尾语（含面谈引导 CTA）。真人不会每条都带收尾——由管道按频率控制 include_cta，
# 时间窗内已带过就不再重复（防套路感）。CTA_MARKERS 供管道识别近期是否已用过。
CTA_PROSPECT = [
    "每个人情况不太一样，方便的话可以约个时间，跟律师细聊下您的情况。",
    "您这个情况具体怎么处理最稳妥，还是当面跟律师过一遍比较清楚，方便的话约个时间。",
]
# 索要联系方式的识别标记：三个变体都含「手机号」，一个词即可覆盖。
# 与 CTA_MARKERS 分开是因为两者的复读容忍度不同——泛泛的「约个时间」可以隔一阵再来一次，
# 「留个电话」在同一通对话里问第二遍就变成催单了。
ASK_CONTACT_MARKERS = ("手机号",)
# 邀约到所的标记。与上面分开，是因为两档推进的复读容忍度不同：
# 轻推（「留个手机号也行」）之后再来一次完整邀约，多出的是所址和面谈邀请，
# 属于正常升级、不是催单；但完整邀约本身在同一通对话里只该出现一次。
OFFICE_INVITE_MARKERS = ("来所里", "当面聊")
CTA_MARKERS = ("约个时间", "再给您细说") + ASK_CONTACT_MARKERS


def ask_contact(
    group: GroupProfile, seed: str = "", settings: Settings | None = None
) -> str:
    """聊到一定程度仍未留联系方式时，主动要电话 + 邀约到所面谈。

    这是首轮筛查的收口动作：线上能说的是一般性框架，真要把事办了必须落到
    「谁来跟进、怎么找到他」。不开口要，八成客户聊完就走了。

    语气按真人来：先给「为什么要电话」的理由（律师回电更省事），再给一个
    可选项（也可以直接来所里），不逼客户二选一。地址报全，让人觉得是实体所。
    """
    settings = settings or get_settings()
    L = _lawyer(group)
    addr = settings.office_address
    where = f"{settings.office_name}（{addr}）" if addr else settings.office_name
    variants = [
        f"这样，您留个手机号吧，我安排{L}直接给您回个电话，比打字说得清楚。\n"
        f"要是您方便过来，也欢迎来所里坐坐、当面聊：{where}，来之前跟我说一声就行。",
        f"您方便留个手机号吗？我转给{L}，让他电话里跟您细说，比在这儿打字快。\n"
        f"或者您得空来所里也行，我们在{where}，喝杯茶把材料一起看看。",
        f"要不这样，您把手机号发我，{L}那边直接联系您。\n"
        f"当面聊也可以，地址是{where}，您定个时间我这边给您安排。",
    ]
    return _pick(variants, seed)


def next_step(group: GroupProfile, seed: str = "") -> str:
    """承接类回复的下一步引导（轻推一句）。

    业务决策 2026-08：承接话术本身是个死胡同——「我帮您问下律师，请您稍等」
    说完客户就没事干了，只能干等，很多人就这么走了。每条回复都要留一个
    下一步动作。这里只轻推（给个理由 + 一句话），完整的邀约留给 ask_contact，
    两者互斥，不叠着发。
    """
    L = _lawyer(group)
    variants = [
        f"要是急，您留个手机号，我让{L}直接给您打过来。",
        f"您方便的话留个手机号，{L}回头电话里跟您说，比在这儿等着强。",
        f"留个手机号也行，{L}一有空就给您回电话。",
    ]
    return _pick(variants, seed)


def winback(
    group: GroupProfile, spoke: bool, seed: str = "", settings: Settings | None = None
) -> str:
    """会话静默且仍未留联系方式时的挽留。一通对话只发一次。

    分两种人，话术不能混：
      - 没开过口的：他卡在「不知道怎么说」，要再给一次例句，降低开口门槛；
      - 聊过但没留电话的：情况已经说清楚了，直接收口要电话 + 邀约到所。
    """
    settings = settings or get_settings()
    L = _lawyer(group)
    if not spoke:
        variants = [
            "您好，刚才的消息可能没看到。\n"
            "您遇到的是什么事？简单说个大概就行，"
            "比如「对方借钱不还，有转账记录」，我帮您看看该怎么处理。",
            "您好，我还在的。\n"
            "您把情况说两句就行，怎么想的就怎么说，"
            "比如「公司欠我三个月工资」这样，我先帮您理一理。",
        ]
        return _pick(variants, seed)
    addr = settings.office_address
    where = f"{settings.office_name}（{addr}）" if addr else settings.office_name
    variants = [
        f"您的情况我这边大致了解了。\n"
        f"要不留个手机号，我安排{L}给您回个电话，比打字说得清楚。"
        f"您方便的话来所里当面聊也行，我们在{where}。",
        f"刚才说的我都记下了。\n"
        f"您留个手机号吧，{L}那边直接联系您；或者得空来所里坐坐也行，"
        f"地址是{where}。",
    ]
    return _pick(variants, seed)


def answer_scaffold(
    group: GroupProfile,
    body: str,
    include_disclaimer: bool = False,
    opening: str | None = None,
    include_cta: bool = True,
    seed: str = "",
) -> str:
    """将（模型生成或人工维护的）一般性法律框架装入合规结构。

    body 只应包含：法条依据 + 一般区间 + 影响因素，不针对本案下结论。
    未成交群（销售顾问定位）在 include_cta 时收尾带面谈引导，做 first screening 后的转化。
    """
    parts = [opening or "", body.strip()]
    if include_disclaimer:
        parts.append(DISCLAIMER)
    if include_cta:
        if group.client_status == ClientStatus.PROSPECT:
            parts.append(_pick(CTA_PROSPECT, seed))
        else:
            parts.append(f"具体到您这边，等{_lawyer(group)}看到再给您细说。")
    return "\n".join(p for p in parts if p)


def answer_without_llm(
    group: GroupProfile, include_disclaimer: bool = False, include_cta: bool = True
) -> str:
    """未接入模型时直接回答路径的确定性降级：不编造法律内容，转为承接。"""
    text = (
        f"收到您的咨询。这个为了给您说准确，我已转达{_lawyer(group)}，"
        f"他看到会{_in_group(group)}给您解答。"
    )
    if include_cta and group.client_status == ClientStatus.PROSPECT:
        text += "方便的话也可以约个时间，跟律师细聊下您的情况。"
    if include_disclaimer:
        text += "\n" + DISCLAIMER
    return text

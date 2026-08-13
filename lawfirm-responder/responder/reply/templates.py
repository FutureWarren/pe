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

      1. **可能说错人**：谁接这单是分案引擎按在办量算出来的
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


def free_claim(settings: Settings | None = None) -> str:
    """律所授权可以对客说出口的那句「免费」原话。**唯一真相来源。**

    出口闸门（`compliance/forbidden.py`）的做法是：先把 `approved_claims` 里的
    原话从文本里抠掉，再跑禁止事项规则。于是**授权的那句能说，别的关于钱的说法
    一句也漏不过去**（「打三折」「代理费一万」照拦）。

    这意味着话术里那句「免费」必须与配置**一字不差**。2026-08-12 体检发现它被
    硬编码在七个地方：谁把它润色成「首次咨询不收费」，或者律所改了授权措辞，
    出口闸门就会把**整条邀约**丢掉——地址、主任律师、带材料、免费四样一起没了，
    换成一句「这个得让律师来说比较准确」，恰好在最该请客户到所里的那一秒。

    所以所有话术一律从这里取。`approved_claims` 清空即全部不提「免费」，
    话术照样通顺——那是律所收回授权时应有的行为。
    """
    raw = (settings or get_settings()).approved_claims
    phrases = [p.strip() for p in raw.split("|") if p.strip()]
    return phrases[0] if phrases else ""


def _free_clause(settings: Settings | None = None, sep: str = "，") -> str:
    """把授权原话拼成一个可以直接嵌进句子的从句（未授权时返回空串）。"""
    claim = free_claim(settings)
    return f"{sep}{claim}" if claim else ""


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
        # **不带 OFFICE_INVITE_MARKERS 里的词。** 原来第二条写着「来所里坐坐」，
        # 于是这句纯费用承接被判成「已经邀约过了」，此后半小时压掉唯一那条
        # 带地址、带主任律师、带「免费」的正式邀约——恰好在最该请他来的那一秒。
        variants = [
            f"收到。这块跟案子的具体情况关系很大，得{L}了解清楚才能给您准数，我已经转达了。\n"
            f"方便的话约个时间当面聊，把您的情况一次说透。",
            f"这个得{L}结合您的情况来说才准确，我让他尽快跟您联系哈。\n"
            f"方便的话约个时间当面聊，材料先发我也行，他看了心里更有数。",
        ]
    else:
        variants = [
            f"收到，费用的事由{L}跟您直接确认，我已经提醒他了，稍等哈。",
            f"看到您消息了。这块{L}会跟您直接沟通，我这边也催着，请您稍等。",
        ]
    return _pick(variants, seed)


def consult_is_free(
    group: GroupProfile, seed: str = "", settings: Settings | None = None
) -> str:
    """客户直接问「你们咨询要钱吗」。**正面回答，不绕。**

    2026-08-12 体检发现的三层叠加里的第一层：AI 在开场白、邀约、挽留三处主动说
    「咨询是免费的」，可客户一反问就变成「这个得律师结合您的情况来说才准确」——
    广告写免费、一问就含糊。他不会问第二遍，直接去问下一家。
    问这句的人最常是被辞退、被欠薪、手头正紧的，这是他敢不敢往下说的唯一门槛。

    两句话，界线画清楚：
      1. **咨询这一次不收钱**——用律所授权的那句原话（`free_claim`，一字不差，
         否则出口闸门会把整段拦掉）；
      2. **案子怎么收由律师跟您谈**——绝不报价、绝不给区间。费用闸门一个字没动。

    授权被收回（`approved_claims` 清空）时自动退回纯承接：不能替律所许一个
    它没许过的承诺。
    """
    settings = settings or get_settings()
    L = _lawyer(group)
    claim = free_claim(settings)
    if not claim:
        return handoff_fee(group, seed)
    variants = [
        f"{claim}，您先把情况说给我就行。\n"
        f"真要办的话具体怎么收费，{L}会当面跟您讲清楚，不会有别的名目。",
        f"不用担心这个，{claim}。您有什么想问的尽管说。\n"
        f"后面如果决定要办，费用怎么算{L}会跟您当面谈明白。",
    ]
    return _pick(variants, seed)


def handoff_urgent(group: GroupProfile, seed: str = "") -> str:
    """紧急情形的第一句：安抚 + 说明已经加急。

    四个变体不是为了好看。真机里客户第三条又说「我快撑不住了」，
    收到的是**一字不差的同一句**——那一刻他要的是有人在听，
    而复读恰恰证明没有。
    """
    L = _lawyer(group)
    variants = [
        f"看到您的消息了，您先别急。这个情况比较要紧，我已经第一时间加急联系{L}了。",
        f"收到，您先别慌，这个情况我们很重视，已经加急通知{L}了。",
        f"我看到了，这事确实急。已经把您的情况标为加急报给{L}那边了。",
        f"别着急，我在的。这个情况我已经加急转给{L}，他会优先处理。",
    ]
    return _pick(variants, seed)


def urgent_next_step(group: GroupProfile, seed: str = "") -> str:
    """紧急情形的下一步。**必须与「加急」自洽。**

    体检发现的原样：「我弟弟昨天被刑事拘留了」换回的是
    「已经加急通知律师了……留个手机号也行，律师**一有空**就给您回电话」——
    刑拘只有 37 天，家属此刻正在比谁反应最快，一句「一有空」当场否掉前半句。

    所以这里做两件事：给一个**明确的时间口径**，再请他把律师立刻要用的信息发过来。
    让一个正慌着的人有具体的事可做，本身就是最有效的安抚。
    """
    variants = [
        "您现在把这几样发我：当事人姓名、在哪个看守所（或哪个派出所办的）、"
        "涉嫌什么。我直接转给律师，他好判断怎么最快介入。",
        "麻烦您先说三件事：人是谁、关在哪儿、什么罪名。我这就一并发给律师，"
        "省得他还要来回问。",
        "您把手上知道的先发我——时间、地点、涉及哪些人。律师看到就能直接接着办。",
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


def contact_received(
    group: GroupProfile, seed: str = "", settings: Settings | None = None
) -> str:
    """客户刚把手机号打出来。**这是整通对话里最强的成交动作。**

    2026-08-12 复查前，这一刻走的是 `handoff_contact`——三条变体里两条开口是
    「律师这会儿应该在忙 / 可能暂时腾不出手」。客户交出号码，换回一句「他在忙」，
    合理解读只有一个：**我白给了，这边根本没人看。**

    所以这句话必须先做一件事：**当着他的面确认号码收到了**。
    然后才是「谁来打、大概多久」——一个具体的时间预期，比任何安抚都管用。
    不点名具体律师（见 `_lawyer`），因为谁接这单是分案引擎算出来的。
    """
    settings = settings or get_settings()
    L = _lawyer(group)
    variants = [
        f"号码收到了。我这就把您的情况整理给{L}，他会直接打给您。",
        f"好的，您的手机号我记下了，转给{L}那边，会尽快跟您联系。",
        f"收到，号码我存下了。这边把您说的情况一并转给{L}，等他电话就行。",
    ]
    return _pick(variants, seed)


def affirm_followthrough(
    group: GroupProfile, kind: str, seed: str = "", settings: Settings | None = None
) -> str:
    """客户对我们上一句问话点了头，把这一拍接住。

    2026-08-12 复查里最贵的一条：AI 发完完整邀约，客户回一句「好的」，
    **然后对面再没有任何声音**——那声「好的」被当成闲聊判了沉默。
    没约哪天、没说带什么、没有任何东西告诉他对面还有人；
    半小时后补发的挽留，内容恰好是把他刚答应过的事再邀请一遍。

    每一档都必须落到**一个他此刻就能做的具体动作**上，不能只回一句「好的呢」——
    那和沉默的差别只是多了一条消息。
    """
    settings = settings or get_settings()
    L = _lawyer(group)
    who = settings.office_senior_title.strip() or "律师"
    if kind == "office":
        # 答应来所里。定时间 + 说带什么——「带了材料的人」到场率高得多
        variants = [
            f"好嘞。您看这两天哪天方便？我先帮您跟{who}那边排个时间。\n"
            f"过来的时候材料带上就行——合同、聊天记录、工资条这类，有什么带什么。",
            "那太好了。您方便的话说个大概时间，上午下午都行，我这边去排。\n"
            "手上的材料记得带着，一次说清楚，省得来回跑。",
        ]
    elif kind == "contact":
        # 答应留号。别再客套，直接请他发过来，并给一个时间预期
        variants = [
            f"好，那您把号码发过来就行。我转给{L}，一般当天就会给您回电话。",
            f"行，手机号发我这儿。{L}那边看到会直接联系您，不用您再等消息。",
        ]
    else:
        # 答应说案情。接着问最要紧的两件事，别让他自己想「该说什么」
        variants = [
            "那您说说看——这事大概什么时候开始的，现在走到哪一步了？",
            "好，您大概讲一下：什么时候的事，现在是个什么状况？",
        ]
    return _pick(variants, seed)


def handoff_generic(group: GroupProfile, seed: str = "") -> str:
    L = _lawyer(group)
    variants = [
        f"收到您的消息，这个得让{L}来说比较准确，我已经转达了，请您稍等。",
        f"看到您消息了，这个我帮您转给{L}确认下，有回复马上告诉您。",
    ]
    return _pick(variants, seed)


def privacy_notice(settings: Settings | None = None) -> str:
    """给律所粘进**企微后台欢迎语**的那段告知。代码里不发这一句。

    《个人信息保护法》第 17 条要求处理前以显著方式、清晰易懂地告知处理者、
    目的、方式、种类、保存期限；第 23 条要求向第三方提供时另行告知并取得单独同意——
    而每条咨询原文都会随上下文发给技术服务商的大模型。
    客户在这里讲的是欠薪、离婚、伤情、家人有没有被拘留，我们还主动向他要手机号，
    全流程此前没有一句告知。

    **为什么不由代码发**：一个窗口只该有一个人在说话（同 `kf_welcome_on_enter`
    的理由）。企微后台的欢迎语律所自己就能改，那是律所署名的告知，
    比 AI 逐句生成的更合适，也更经得起看。抖音私信同理。

    这段文字是**草稿**，措辞须律所定稿——它是律所对客户作出的法律承诺，
    不是一段话术。
    """
    settings = settings or get_settings()
    return (
        f"您好，这里是{settings.office_name}。\n"
        f"为了给您提供法律咨询与后续服务，本所会接待并留存本次对话内容，"
        f"其中会借助技术服务商处理您描述的情况。除此之外不作他用，也不会提供给无关第三方。\n"
        f"如需查询、更正或删除您的信息，直接在本对话中告诉我们即可。"
    )


def intro_line(settings: Settings | None = None) -> str:
    """一通对话里的第一句自报家门。

    客户扫码进来第一句就直接说事的时候，走的是承接/追问路径，
    拿不到 greeting_opener 里那句律所全称——而那句话不能丢：
    对面得先知道自己在跟谁说话，这既是礼貌，也是「这不是个野鸡账号」的凭据。
    所以把它拆出来，由管道在本通对话的第一条回复上加一次。
    """
    return f"您好，这里是{(settings or get_settings()).office_name}。"


def handoff_noted(group: GroupProfile, seed: str = "") -> str:
    """一对一窗口里「收下并继续」的承接。取代泛泛的 handoff_generic。

    「这个我帮您转给律师确认下」在群里没问题（律师本人就在群里，客户看得见），
    但在一对一窗口里连着说三遍，客户读到的是「你说什么我都这一句」。
    真机测试里客户连答了两条我们问的问题，换回的都是这句——对面立刻知道没人在听。

    改成「记下了 + 还有什么接着说」：一样不下结论，但它是开着的门，不是句号。
    这里不再问手机号——追问阶段已经问过一次了，隔一条又问就成了催单。
    """
    variants = [
        "记下了，这些我一并整理给律师。您要是还有细节或者材料，随时发我。",
        "好的，我都记着呢，会连同前面说的一起转给律师。还有别的情况也可以接着说。",
        "收到，这条我也记下了。您继续说，我一起整理给律师。",
    ]
    return _pick(variants, seed)


def greeting_opener(
    group: GroupProfile, seed: str = "", contact_left: bool = False,
    settings: Settings | None = None,
) -> str:
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
    settings = settings or get_settings()
    if contact_left:
        L = _lawyer(group)
        variants = [
            # 不用「他」：接单律师由分案引擎按在办量算出，性别未知，
            # 而这句话是发给客户看的，猜错就是当着人面把人叫错。
            f"好的，您的联系方式我已经记下，稍后由{L}与您电话联系。",
            f"收到，电话我记下来了。我先把您的情况整理给{L}，尽快与您联系。",
            f"好的，联系方式收到。我这边转给{L}，请您留意来电。",
        ]
        return _pick(variants, seed)
    # 「咨询是免费的」放在开场（律所方 2026-08-12：「我们就是主打免费的
    # 法律咨询」）。放这儿而不是别处，是因为漏斗上最贵的断点就在这一句：
    # 416 人进私信只有 90 人开口，78% 的人看完第一句就走了——
    # 而挡住他们的除了「不知道怎么说」，就是「随便问一句会不会要钱」。
    free = _free_clause(settings)
    variants = [
        f"您好，这里是{settings.office_name}{free}。\n"
        "您把遇到的情况说两句就行，不用特意组织语言——"
        "比如「公司欠了我三个月工资，还把我辞退了」这样，我先帮您理一理。",
        f"您好，{settings.office_name}，我在的{free}。\n"
        "您遇到的是什么事？简单说个大概就可以，"
        "比如「对方借钱不还，有转账记录」，我帮您看看该怎么处理。",
        f"您好，这里是{settings.office_name}{free}。\n"
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


def intake_probe(
    group: GroupProfile, seed: str = "", settings: Settings | None = None,
    ask_phone: bool = True,
) -> str:
    """客户第一次把自己的事说出来时的回应——问下去，而不是「我帮您转达」。

    真机测试里客户说「我现在遇到了劳务仲裁的问题，拖欠工资」，AI 回
    「看到您消息了，这个我帮您转给律师确认下」。他刚把事情交出来，
    换回一句套话，对面立刻就知道没人在听——律所方的原话是「显得非常的笨」。

    这一刻正确的动作是**追问**。理由有三层：
      1. 追问是「在听」最有力的证明，比任何安抚话术都管用；
      2. 问出来的时间点、进展、材料，正是律师接手时最需要的三样，
         客户答完，交接单的含金量完全不同；
      3. 追问不是下结论——合规红线拦的是对本案作判断，问情况不在其列。

    末尾仍留一条快车道（留手机号），但放在追问之后：先让他觉得被理解，
    再谈下一步，顺序反了就成了推销。
    """
    settings = settings or get_settings()
    L = _lawyer(group)
    variants = [
        "明白了，这类情况我们处理得比较多。为了让{L}一次就说到点子上，"
        "您再补两句：这事大概是什么时候开始的？现在走到哪一步了？\n"
        "手上有合同、聊天记录、转账记录这些材料的话，也可以直接发过来。",
        "您说的我记下了。想让{L}给您准信儿，还得再问两句："
        "从什么时候开始的？中间跟对方沟通过没有、结果怎么样？\n"
        "相关的材料（合同、聊天记录、转账记录）方便的话也先发我。",
    ]
    text = _pick(variants, seed)
    # 已经问过电话的不再问第二遍——同一通对话里问两次就成了催单
    if ask_phone:
        text += f"要是着急，留个手机号，{L}直接给您打过来。"
    return text.replace("{L}", L)


def office_fact(
    group: GroupProfile, seed: str = "", settings: Settings | None = None
) -> str:
    """所在哪儿、怎么走、几点上班——一句话能给的，就当场给。

    这条不进模型：地址是确定的事实，让模型复述只会多一个说错的机会。
    末尾带一个下一步（约时间），因为「知道地址」离「真的来」还差一步，
    而问路的人本来就是最接近成交的那一批。
    """
    settings = settings or get_settings()
    addr = settings.office_address
    if not addr:
        # 没配地址就别硬编——说错地址比不说更糟，客户白跑一趟
        return (
            f"我们是{settings.office_name}。具体地址和到所时间我确认一下再回您，"
            "您也可以先说说情况，我这边同步帮您理一理。"
        )
    variants = [
        f"我们在{addr}。\n您方便的话说个时间，我提前跟律师约好，来了直接聊，不用等。",
        f"地址是{addr}。\n您要是打算过来，先跟我说一声，我这边给您排个时间，省得白跑。",
        f"{settings.office_name}，{addr}。\n您定个方便的时间，我帮您安排好再过来。",
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


def third_touch(group: GroupProfile, seed: str = "", settings=None) -> str:
    """客户**第三次**问同一件事。

    群聊到这一步 AI 就闭嘴了（律师在场，再说就成了刷屏）。一对一窗口不行——
    那里没有别人。客户问了三遍还是这句话，说明他越来越急，
    **这时候闭嘴是最坏的回应**：他下一步就是关掉窗口走人。

    所以这句话要做三件事：认下来（别再说「我催了」，他已经听过两遍了）、
    给一个具体的时间感、给一件此刻他自己能做的事。
    """
    L = _lawyer(group)
    tail = ask_contact(group, seed=seed, settings=settings)
    return (
        f"是我们让您久等了。{L}那边我已经标成加急，"
        f"您先把手上跟这件事有关的材料拍照存好，回头一次性给他看，能省不少时间。\n"
        + tail
    )


HANDOFF_BY_CATEGORY = {
    Category.CASE_STATUS: handoff_case_status,
    Category.FEE: handoff_fee,
    Category.URGENT: handoff_urgent,
    Category.CONTACT: handoff_contact,
}


def build_handoff(category: Category, group: GroupProfile, seed: str = "") -> str:
    if category not in HANDOFF_BY_CATEGORY:
        # 一对一窗口的兜底承接要留个开口，别把话说死（见 handoff_noted）
        return (handoff_noted if group.is_kf else handoff_generic)(group, seed)
    return HANDOFF_BY_CATEGORY[category](group, seed)


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
OFFICE_INVITE_MARKERS = ("来所里", "当面聊", "过来一趟")
# 追问话术的标记：同一通对话里只追问一次，问第二遍就成了查户口
INTAKE_MARKERS = ("走到哪一步", "什么时候开始")
CTA_MARKERS = ("约个时间", "再给您细说") + ASK_CONTACT_MARKERS


def ask_contact(
    group: GroupProfile, seed: str = "", settings: Settings | None = None
) -> str:
    """聊到一定程度仍未留联系方式时，主动要电话 + 邀约到所面谈。

    这是首轮筛查的收口动作：线上能说的是一般性框架，真要把事办了必须落到
    「谁来跟进、怎么找到他」。不开口要，八成客户聊完就走了。

    **一次只做一件事。** 原来这段话把「留个电话」和「地址是××路 88 号平高广场
    11 楼、欢迎来所里坐坐」塞在同一口气里，律所方实测的原话是
    「这一长串的说话方式，让客户一看就会觉得这是不是 AI」——真人不会在
    刚听完你一句话之后，就把电话和地址一起报出来。

    所以这里只要电话，短句、给一个理由、留一个台阶。所址走 `office_invite`，
    等客户真的表现出想来了再说（或者他直接问地址，那时 `office_fact` 会答）。
    """
    settings = settings or get_settings()
    L = _lawyer(group)
    variants = [
        f"您留个手机号吧，我让{L}直接给您回电话，比在这儿打字说得清楚。",
        f"方便留个手机号吗？我转给{L}，电话里三两句就说明白了。",
        f"要不您把手机号发我，{L}那边直接联系您，比打字快。",
    ]
    return _pick(variants, seed)


def office_invite(
    group: GroupProfile, seed: str = "", settings: Settings | None = None
) -> str:
    """邀约到所面谈。**这是转化上最重的一步，不是顺口提一句地址。**

    律所方 2026-08-12 跟所里同事复盘后的结论：**真正高客单价的单子，
    几乎都是线下见过面之后才签的。** 线上聊得再好也只是筛查——把人请到所里，
    成交率是另一个量级。所以这段话的目标不是「告知地址」，是「让他愿意跑一趟」。

    三样东西缺一不可：

      · **谁接待** —— `office_senior_title`（默认「主任律师」）。稀缺性是
        客户愿意专程过来的主要理由；一句「律师会接待您」换不动任何人。
        留空即不提：律协禁止虚假宣传，这句必须是真的。
      · **带什么** —— 让他带材料。一个具体的准备动作，比空邀约有效得多，
        而且带了材料的人来的概率高很多。
      · **没有压力** —— 「聊完您心里有个数，接下来怎么走再定」。
      · **免费** —— 由 `free_claim()` 取律所授权的那句原话（2026-08-12 拍板）。
        **必须从那里取，不能在这里另写一句。** 出口闸门是靠「与授权原话逐字相同」
        放行的：谁把它润色成「首次咨询不收费」，整条邀约会被当成报价整段丢掉，
        地址、主任律师、带材料、免费四样一起没，换成一句「这个得让律师来说」——
        恰好在最该请客户到所里的那一秒。授权收回（配置清空）时这句自动消失，
        其余三样照常，句子仍然通顺。

    > 落这条之前我两次提出全国律协《律师业务推广行为规则》第十条把
    > 「以不收费或减低收费招揽业务」列为禁止行为（法律援助案件除外），
    > 律所方重申「你不要管，按我说的做」。**这是律所的执业判断，由律所承担。**
    > 让步只有一处，且不再让：只放行 `approved_claims` 里逐字定下的那几句，
    > 费用闸门本身一个字不动——模型仍然编不出律所没授权的价格承诺。

    和要电话分开、且晚一步（见 `service._should_invite_office`）：真人不会在
    刚听完一句话之后，把电话和地址一口气报出来。
    """
    settings = settings or get_settings()
    addr = settings.office_address
    where = f"{settings.office_name}（{addr}）" if addr else settings.office_name
    who = settings.office_senior_title.strip()
    by = f"{who}" if who else "律师"
    free = _free_clause(settings, sep="——") or "——"
    free_mid = _free_clause(settings)
    variants = [
        f"这种事当面说清楚得多。您方便的话来所里一趟，"
        f"{by}帮您把材料过一遍{free}，看看能走哪条路。我们在{where}。",
        f"要不您找个时间过来一趟？带上手上的材料，{by}当面帮您理一遍"
        f"{free_mid}，聊完您心里有个数，接下来怎么走再定。{where}。",
        f"建议还是来所里当面聊一次，{by}会亲自看您这个情况{free_mid}，"
        f"材料带上一次说清楚，比在这儿来回打字省事。地址在{where}。",
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
    who = settings.office_senior_title.strip() or "律师"
    free = _free_clause(settings)
    variants = [
        f"您的情况我这边大致了解了。\n"
        f"要不找个时间来所里一趟？{who}当面帮您把材料过一遍{free}，"
        f"我们在{where}。来不了的话留个手机号也行，我安排{L}给您回电话。",
        f"刚才说的我都记下了。\n"
        f"方便的话来所里当面聊聊，{who}亲自看看您这个情况{free}，地址是{where}。"
        f"不方便过来就留个手机号，{L}那边直接联系您。",
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


# 复述客户问的是什么。截断到一行，够让他确认「我说的话被听见了」即可。
_ECHO_MAX = 22


def answer_without_llm(
    group: GroupProfile, include_disclaimer: bool = False, include_cta: bool = True,
    question: str = "", seed: str = "",
) -> str:
    """模型不可用时的确定性降级：不编造法律内容，转为承接。

    **这条路是静默的，而且比想象中常走**——超时、限流、密钥过期都会落到这里，
    健康页只报「密钥配没配」，可能连着几天没人发现。而客户那边看到的是：
    连问「仲裁一般要多久」「那我该准备什么材料」，两条回复开头一字不差。
    「免费法律咨询」这个卖点当场归零，AI 退回成一个复读的转达员。

    所以两件事：**给变体**，以及**复述他问的是什么**——
    哪怕答不了，也得让他知道这句话被听见了。
    """
    L, G = _lawyer(group), _in_group(group)
    q = re.sub(r"\s+", " ", (question or "").strip())
    echo = f"关于「{q[:_ECHO_MAX]}{'…' if len(q) > _ECHO_MAX else ''}」，" if q else ""
    variants = [
        f"{echo}这个我得让{L}给您说才准确，已经转过去了，他看到会{G}回您。",
        f"{echo}我先记下了。要说得准还得{L}来，我这就转给他。",
        f"{echo}收到。这块我不敢给您说个大概，让{L}看过再答复您。",
    ]
    text = _pick(variants, seed or q)
    if include_cta and group.client_status == ClientStatus.PROSPECT:
        text += "方便的话也可以约个时间，跟律师细聊下您的情况。"
    if include_disclaimer:
        text += "\n" + DISCLAIMER
    return text

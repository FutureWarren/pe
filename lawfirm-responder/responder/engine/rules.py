"""确定性规则分类器：三分类（直接回答 / 承接 / 沉默）。

分层判断，命中即停，优先级从高到低：
  1. 非文本 / 空消息 → 沉默
  2. 紧急情形（拘留/传唤/开庭临近/情绪崩溃/投诉）→ 承接（urgent）
  3. 报价 / 费用试探 → 承接（AI 绝不报价）
  4. 案件特定问题（需要本案具体信息才能回答）→ 承接
  5. 找人 / 催回复 → 承接
  6. 通用法律知识问题 → 直接回答
  7. 其余（闲聊、表情、致谢、客户互聊、陈述句）→ 沉默

规则可解释、可测试；LLM 仅作为可选的边界样本复核（见 llm.py），
规则命中高优先级层时不交给模型改判。
"""

import re

from responder.models import Action, Category

# ---------------------------------------------------------------- 紧急情形
URGENT_PATTERNS = [
    r"(被|把.{0,6})?(拘留|刑拘|抓走|带走|逮捕|收监)",
    r"(接到|收到).{0,8}(传唤|传票)",
    r"传唤",
    r"(明天|后天|下周[一二三四五六日天]?|马上|这周|周[一二三四五六日天]).{0,6}开庭",
    r"开庭.{0,6}(通知|时间到|就在|提前)",
    r"(不想活|活不下去|撑不住|崩溃|睡不着.{0,10}(想不开|绝望)|绝望)",
    r"(投诉|举报).{0,8}(你们|律师|律所)",
    r"要.{0,4}投诉",
    r"(退费|解除委托|终止委托|换律师)",
    r"(警察|公安|派出所).{0,10}(找我|上门|打电话|叫我去)",
    # 被施压当场签字/离职/认罪等，需律师立即介入
    r"(让|逼|要求)我.{0,6}(签|辞职|离职|认罪|写保证|按手印)",
]

# 情绪激动 / 强烈不满：一句安抚 + 强提醒，不展开
ANGRY_PATTERNS = [
    r"(到底|究竟).{0,6}(管不管|办不办|做不做事)",
    r"(太让|真让).{0,4}失望",
    r"你们(这是|就是|根本).{0,8}(不负责|骗|糊弄|敷衍)",
    r"(什么破|什么烂)(律所|服务)",
    r"再不.{0,6}(回复|处理).{0,8}(投诉|曝光|找别人)",
]

# ---------------------------------------------------------------- 报价试探
FEE_PATTERNS = [
    r"(多少钱|几多钱|什么价|啥价)",
    r"(收费|费用|价格|报价|价位)(是|大概|多少|怎么|如何|标准)?",
    r"律师费",
    r"(便宜|优惠|打折|折扣|少收|减免)(点|一点|些)?",
    r"(分期|先办后付|风险代理)",
    r"(尾款|余款)",
    r"还要(交|付|给).{0,6}(钱|费)",
    r"(要|得|是不是要)?加(钱|价|收费?)",
]

# ---------------------------------------------------------------- 案件特定
CASE_SELF_REF = (
    r"(我|我们|我家|我老公|我老婆|我丈夫|我妻子|我儿子|我女儿|我爸|我妈|我父亲|我母亲|"
    r"我哥|我弟|我姐|我妹|我朋友|他)(的)?(这个|那个)?(案子|案件|事情|官司|事儿|事)"
)
CASE_PATTERNS = [
    CASE_SELF_REF + r".{0,20}(到哪|进展|进度|怎么样|什么阶段|啥情况|有.{0,4}消息|结果)",
    r"(案子|案件|官司).{0,10}(到哪一?步|进展|进度|什么阶段|啥阶段|有消息|怎么样了|结果)",
    r"(进展|进度).{0,6}(怎么样|如何|咋样)",
    r"(判决书?|裁定书?|结果|结论).{0,10}(什么时候|啥时候|何时|多久).{0,6}(下来|出来|能出|出)",
    r"(什么时候|啥时候|何时).{0,10}(开庭|判|出结果|有消息|立案|放出来|见到|会见)",
    r"(材料|资料|证据|合同|文件).{0,10}(交了|收到|提交|递交|补充|还差|齐了)(了)?(吗|没|么|嘛)",
    r"(立案|批捕|起诉|移送|开庭|判决|执行|保全|鉴定).{0,6}(了吗|了没|没有|成功|下来)(吗|么|嘛)?",
    r"(法官|检察官|办案|承办).{0,10}(联系|说什么|态度|见了|回复)",
    r"(取保|会见|探视).{0,10}(办得|办的|申请).{0,6}(怎么样|如何|了吗|了没)",
    r"我(的|们)?(那个)?(取保|上诉|申诉|赔偿|工伤认定|劳动仲裁).{0,10}(怎么样|进展|有消息|办了吗)",
    r"帮我(问|催|看看|查查?|盯着?)",
    r"(签的|我们的)(合同|协议).{0,10}(什么时候|生效|盖章)",
    r"(之前|上次|上午|昨天|刚才|前几天).{0,6}(问|发|说|提)(的|过).{0,10}(问题|消息|事|资料|材料|方案|那个)",
    r"(上次|昨天)开庭",
    r"(需要|要)我(带|准备|补)",
    r"(对方|法院|法官|检察院|检察官).{0,10}(打电话|来电|发短信|联系我|给我发)",
    r"(群里|微信).{0,4}(能|可以)?发.{0,6}(材料|资料|文件|证据)",
    r"(那边|法院|仲裁|检察院).{0,8}(有|来)(回复|消息|通知)",
]

# ---------------------------------------------------------------- 找人 / 催回复
# 「在吗」这类纯探问：群里问的是「有没有人在」，被 @ 的助手本人就在，
# 此时不该回「我帮您叫律师」，而应直接应答（见 engine/decision.py）
PRESENCE_PATTERN = r"^(在吗|在么|在不在|有人吗|有人在吗)[?？!！。~～]*$"

CONTACT_PATTERNS = [
    PRESENCE_PATTERN,
    r"(律师|王律|李律|张律|刘律|陈律|.{1,3}律师)(在吗|在么|在不在|方便吗|有空吗|忙吗)",
    r"(怎么|咋)(还)?(没|不)(人)?(回|回复|理|说话)",
    r"(有人|谁)(能|可以)?(回|回复|理|看)一?下(吗|么|嘛)?",
    r"(又|都)过(了)?.{0,6}(天|小时|周).{0,6}(了)?[，,]?.{0,8}(消息|回复|动静)",
    r"(麻烦|请|拜托).{0,6}(尽快|快点|抓紧).{0,4}(回复|回|处理)",
    r"看到(请|麻烦)?回(复|一下)?",
    r"方便(打个电话|通话|语音)(吗|么|嘛)?",
    r"(单独|私下|私聊).{0,8}(说|聊|沟通|讲)",
    r"再问一(遍|次)",
]

# ---------------------------------------------------------------- 身份试探
# 「你是人还是机器？」几乎每通对话都会被问一次。此前它落进默认沉默，
# 客户就真的看不到任何回应——而问这句话的人本来就在怀疑，没人应更坐实了怀疑。
IDENTITY_PATTERNS = [
    r"是(真)?人还是(机器|机器人|ai|智能|人工)",
    r"(机器|机器人|ai)还是(真)?人",
    r"(你|您)(们)?(是|是不是)(不是)?(真人|机器人|机器|ai|智能客服|人工智能)",
    r"(真人|机器人|机器|ai)(吗|么|嘛)",
    r"(你|您)(们)?(是)?(不是)?律师(吗|么|嘛)",
]

# ---------------------------------------------------------------- 索要律师联系方式
# 客户主动开口要律师电话，是整通对话里最强的成交信号——比留下自己的号码还强，
# 因为那是他自己要往前走。此前它落进泛泛承接（「我帮您转给律师确认下」），
# 等于在客户伸手的那一刻给了他一句空话。
WANT_LAWYER_CONTACT_PATTERNS = [
    r"(律师|你们|你)(的)?(电话|手机|号码|联系方式|微信)[^。！？!?]{0,4}(多少|给我|发我|留给我)",
    r"(给|发|留)(我|一下|个)?(律师|你们)(的)?(电话|手机号|号码|微信|联系方式)",
    r"我(直接|自己)?(打|加|联系)(给)?(律师|你们)",
    r"(怎么|如何|哪里)(能|可以)?(联系|找到)(上)?(律师|你们)",
]

# ---------------------------------------------------------------- 通用法律知识
# 判据：不需要本案具体信息即可给一般性法律框架
GENERAL_TOPIC = (
    r"(判几年|判多久|判多长|量刑|刑期|缓刑|取保候审|保释|假释|减刑|自首|立功|谅解书|"
    r"离婚|抚养权|抚养费|探视权|财产分割|彩礼|婚前财产|遗产|继承|遗嘱|赡养|"
    r"赔偿|误工费|精神损失|伤残鉴定|工伤|辞退|裁员|欠薪|加班费|仲裁|社保|"
    r"合同(违约|无效|解除)?|违约金|定金|订金|借条|欠条|利息|诉讼时效|担保|抵押|"
    r"起诉|上诉|申诉|开庭流程|立案(条件|流程|标准)|证据|举证|管辖|强制执行|失信|"
    r"酒驾|醉驾|肇事|交通事故|盗窃|诈骗|故意伤害|寻衅滋事|正当防卫|帮信|"
    r"房产|买房|租房|物业|拆迁|宅基地|股权|公司注销|债务|破产|"
    r"名誉权|隐私|网暴|造谣|骚扰|家暴|保护令|监护|收养)"
)
QUESTION_HINT = (
    r"([?？]|吗[。!！~～]?$|呢[。!！~～]?$|如何|怎么(办|判|算|赔|分|处理|规定)|"
    r"什么(条件|标准|流程|规定|后果)|有什么|需要什么|能不能|可不可以|可以.{0,8}吗|"
    r"要不要|该不该|算不算|是不是|一般(判|赔|怎么|多久|多长)|大概(多久|多长|几年)|"
    r"想(问|咨询|了解)|请问|咨询一?下|(心里)?没底)"
)

GENERAL_EXCLUDE_SELF_CASE = re.compile(CASE_SELF_REF)

_URGENT = [re.compile(p) for p in URGENT_PATTERNS]
_ANGRY = [re.compile(p) for p in ANGRY_PATTERNS]
_FEE = [re.compile(p) for p in FEE_PATTERNS]
_CASE = [re.compile(p) for p in CASE_PATTERNS]
_CONTACT = [re.compile(p) for p in CONTACT_PATTERNS]
_PRESENCE = re.compile(PRESENCE_PATTERN)
_IDENTITY = [re.compile(p, re.I) for p in IDENTITY_PATTERNS]
_WANT_LAWYER_CONTACT = [re.compile(p) for p in WANT_LAWYER_CONTACT_PATTERNS]


def is_identity_question(text: str) -> bool:
    """客户在问「你到底是谁/是不是机器人」。"""
    return bool(_match_any(_IDENTITY, (text or "").strip()))


def wants_lawyer_contact(text: str) -> bool:
    """客户主动要律师的电话/微信。"""
    return bool(_match_any(_WANT_LAWYER_CONTACT, (text or "").strip()))


def is_chasing(text: str, category: Category) -> bool:
    """这条消息是在「催」，而不是在问新问题。

    区分这两者是有代价的教训：客户接连问了两个费用问题，第二个被当成
    「又问了一遍」，于是 AI 回「抱歉让您久等了，我刚又催了一下」——
    客户没在等，他在问。答非所问比复读更伤。
    """
    if wants_lawyer_contact(text):
        return False  # 「怎么联系律师」是往前走，不是在催
    return category == Category.CONTACT or is_presence_check(text)


def is_presence_check(text: str) -> bool:
    """纯「在吗」式探问（未点名任何律师）。"""
    return bool(_PRESENCE.match((text or "").strip()))


# 光打招呼、没说事。回访客户最常见的第一句就是这个。
_BARE_HELLO = re.compile(
    r"^(你好|您好|哈喽|哈啰|嗨|hi|hello|在吗|在么|在不在|有人吗|有人在吗|早上好|上午好|中午好|下午好|晚上好)[\s,，。.!！?？~～、]*$",
    re.I,
)


def is_bare_greeting(text: str) -> bool:
    """只是打了个招呼，没有任何可承接的内容。

    区分它有实际意义：客户把案情打出来时「我帮您转给律师」是正常承接；
    但对一句「你好」说同样的话，等于「转什么？他什么都没说」——
    这种时候该回一句招呼，不是回一句承接。
    """
    return bool(_BARE_HELLO.match((text or "").strip()))
_GENERAL_TOPIC = re.compile(GENERAL_TOPIC)
_QUESTION = re.compile(QUESTION_HINT)

# 纯闲聊快速通道（可读性优先，不追求穷尽——默认路径本就是沉默）
_CHITCHAT = re.compile(
    r"^(早上好|早安|上午好|中午好|下午好|晚上好|晚安|大家好|新年快乐|节日快乐|周末愉快|"
    r"谢谢|感谢|辛苦了|麻烦你们了|好的|好嘞|收到|嗯嗯?|哦哦?|行|可以|没问题|OK|ok|好滴|"
    r"\[[^\]]{1,8}\]|\W+)+[!！。~～,，\s]*$"
)


# 礼貌语兜底（放在所有承接层之后判定，避免吞掉「麻烦帮我催一下」这类请求）
_COURTESY = re.compile(r"^(谢谢|多谢|感谢|辛苦|麻烦)[^？?！!]{0,10}[!！。~～]*$")


def _match_any(patterns: list[re.Pattern], text: str) -> str | None:
    for p in patterns:
        if p.search(text):
            return p.pattern
    return None


def classify(content: str, msg_type: str = "text") -> tuple[Action, Category, bool, list[str]]:
    """返回 (action, category, urgent, reasons)。"""
    text = (content or "").strip()

    if msg_type != "text" or not text:
        return Action.SILENCE, Category.CHITCHAT, False, ["non-text-or-empty"]

    if _CHITCHAT.match(text):
        return Action.SILENCE, Category.CHITCHAT, False, ["chitchat-fastpath"]

    # @某人 的消息是点名对话（客户互聊或点名律师，律师会收到原生提醒），AI 不插话
    if text.startswith("@"):
        return Action.SILENCE, Category.CHITCHAT, False, ["at-mention"]

    if hit := _match_any(_URGENT, text):
        return Action.HANDOFF, Category.URGENT, True, [f"urgent:{hit}"]
    if hit := _match_any(_ANGRY, text):
        return Action.HANDOFF, Category.URGENT, True, [f"angry:{hit}"]

    # 「你是人还是机器」：先于费用/案件判定，否则「你们是律师吗」会被别的层吞掉
    if _match_any(_IDENTITY, text):
        return Action.HANDOFF, Category.OTHER, False, ["identity-question"]

    # 要律师电话：比费用问题更强的信号，先判，别让「多少钱」把它盖过去
    if _match_any(_WANT_LAWYER_CONTACT, text):
        return Action.HANDOFF, Category.CONTACT, False, ["want-lawyer-contact"]

    if hit := _match_any(_FEE, text):
        return Action.HANDOFF, Category.FEE, False, [f"fee:{hit}"]

    if hit := _match_any(_CASE, text):
        return Action.HANDOFF, Category.CASE_STATUS, False, [f"case:{hit}"]

    if hit := _match_any(_CONTACT, text):
        return Action.HANDOFF, Category.CONTACT, False, [f"contact:{hit}"]

    # 兜底：凡是自指本案（我的案子/我这个事…）一律承接，不针对本案作答
    if GENERAL_EXCLUDE_SELF_CASE.search(text):
        return Action.HANDOFF, Category.CASE_STATUS, False, ["self-case-ref"]

    if _GENERAL_TOPIC.search(text) and _QUESTION.search(text):
        # 含「我的案子」类自指的，即便话题通用也走承接，避免针对本案下结论
        if GENERAL_EXCLUDE_SELF_CASE.search(text):
            return Action.HANDOFF, Category.CASE_STATUS, False, ["general-but-self-case"]
        return Action.ANSWER, Category.GENERAL_LAW, False, ["general-law-question"]

    # 礼貌语（谢谢王律师/辛苦各位了…）：明确沉默，且不进 LLM 复核
    if _COURTESY.match(text):
        return Action.SILENCE, Category.CHITCHAT, False, ["courtesy"]

    return Action.SILENCE, Category.CHITCHAT, False, ["default-silence"]

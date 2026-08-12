"""进线主动问候 + 聊到一定程度主动要电话。

这两件事对应漏斗上最贵的两个断点（抖音后台实测）：
  进私 416 → 开口 90（78% 的人看完空窗口就走了）→ 留资 50（四成聊完不留电话）。
前者靠「客户一进来就有人说话」，后者靠「聊够了主动开口要」。

不出网：以记录桩替代 KfClient。
"""

from responder.config import Settings
from responder.gateway.wecom_kf import ORIGIN_CUSTOMER
from responder.models import ClientStatus, GroupProfile
from responder.service import Pipeline
from responder.store.db import Store
from responder.worker import KfSyncJob, Worker

OPEN_KFID = "wk-777"
EXT_USER = "wmCustomerXyz789"
GID = f"kf:{OPEN_KFID}:{EXT_USER}"
ADDRESS = "上海市松江区九峰路88号平高广场11楼"


class FakeKf:
    def __init__(self, batches, servicers=("wei",)):
        self.batches = list(batches)
        self.sent: list[tuple[str, str, str]] = []
        self.servicers = list(servicers)

    def available(self):
        return True

    def servicer_list(self, open_kfid):
        return list(self.servicers)

    def sync_msg(self, token, open_kfid, cursor="", limit=1000):
        if self.batches:
            return self.batches.pop(0)
        return {"msg_list": [], "next_cursor": cursor, "has_more": 0}

    def send_text(self, open_kfid, external_userid, text):
        self.sent.append((open_kfid, external_userid, text))
        return True

    def texts(self) -> str:
        return "\n".join(t for _, _, t in self.sent)


class DirectSender:
    def __init__(self):
        self.direct: list[tuple[str, str]] = []

    def send_direct_text(self, userid, text):
        self.direct.append((userid, text))
        return True


def kf_msg(msgid, content, origin=ORIGIN_CUSTOMER):
    return {
        "msgid": msgid, "open_kfid": OPEN_KFID, "external_userid": EXT_USER,
        "origin": origin, "msgtype": "text", "text": {"content": content},
    }


def kf_event(msgid, event_type="enter_session"):
    return {
        "msgid": msgid, "open_kfid": OPEN_KFID, "external_userid": EXT_USER,
        "msgtype": "event", "event": {"event_type": event_type},
    }


def make_env(tmp_path, batches, **over):
    db = str(tmp_path / "wc.db")
    store = Store(db)
    cfg = dict(
        mode="live", db_path=db, split_delay_seconds=0, split_messages=False,
        wecom_kf_secret="kf-secret", kf_default_lawyer_name="魏",
        kf_default_case_type="劳动仲裁", llm_answer_enabled=False,
        llm_refine_enabled=False, lead_brief_enabled=False,
    )
    cfg.update(over)
    settings = Settings(**cfg)
    kf = FakeKf(batches)
    sender = DirectSender()
    pipeline = Pipeline(store, sender=sender, settings=settings, kf_client=kf)
    worker = Worker(pipeline, store, sender, kf_client=kf)
    return store, kf, worker


def run(worker, token="tk"):
    worker.process_kf(KfSyncJob(token=token, open_kfid=OPEN_KFID))


# ---------------------------------------------------------------- 进线问候
def test_enter_event_sends_welcome(tmp_path):
    """客户扫码进入会话 → 不等他开口，先自报家门并请他说明情况。"""
    store, kf, worker = make_env(tmp_path, [
        {"msg_list": [kf_event("e1")], "next_cursor": "c1", "has_more": 0},
    ])
    run(worker)

    assert len(kf.sent) == 1, "进线必须有且只有一句问候"
    text = kf.sent[0][2]
    assert "上海松沪律师事务所" in text  # 自报家门
    assert "比如" in text  # 给例句，降低开口门槛
    assert store.get_group(GID) is not None  # 顺带建档
    replies = store.list_replies(GID, limit=5)
    assert replies and replies[0]["mode"] == "live"


def test_welcome_is_idempotent(tmp_path):
    """企微重复推送同一个进线事件 → 只问候一次（msg_id 入库去重）。"""
    _, kf, worker = make_env(tmp_path, [
        {"msg_list": [kf_event("e1"), kf_event("e1")], "next_cursor": "c1", "has_more": 0},
    ])
    run(worker)
    assert len(kf.sent) == 1


def test_welcome_skipped_for_returning_customer(tmp_path):
    """老客户再次点进来不再自我介绍一遍——他上一轮的上下文还在。"""
    _, kf, worker = make_env(tmp_path, [
        {"msg_list": [kf_msg("m1", "拖欠工资多久可以申请劳动仲裁？")],
         "next_cursor": "c1", "has_more": 0},
        {"msg_list": [kf_event("e9")], "next_cursor": "c2", "has_more": 0},
    ])
    run(worker)
    before = len(kf.sent)
    run(worker, token="tk2")
    assert len(kf.sent) == before, "回访不应再发开场白"


def test_welcome_can_be_disabled(tmp_path):
    """开关关掉就彻底不发（企微后台自带欢迎语的部署方式）。"""
    _, kf, worker = make_env(tmp_path, [
        {"msg_list": [kf_event("e1")], "next_cursor": "c1", "has_more": 0},
    ], kf_welcome_on_enter=False)
    run(worker)
    assert kf.sent == []


def test_welcome_respects_ai_switch(tmp_path):
    """控制台把该会话的 AI 关了 → 进线也不出声。"""
    store, kf, worker = make_env(tmp_path, [
        {"msg_list": [kf_event("e1")], "next_cursor": "c1", "has_more": 0},
    ])
    store.upsert_group(GroupProfile(
        group_id=GID, ai_enabled=False,
        kf_open_kfid=OPEN_KFID, kf_external_userid=EXT_USER,
    ))
    run(worker)
    assert kf.sent == []


def test_greeting_never_repeated_in_one_conversation(tmp_path):
    """进线问候过之后，客户把事情打出来（陈述句、没问号）不能再收到一遍开场白。

    这是问候上线后新增的复读风险：规则判不出陈述句是不是法律问题 → 回落引导型开场白
    →「请您说说什么情况」正好问了他刚说完的事。此时应改走承接。
    """
    store, kf, worker = make_env(tmp_path, [
        {"msg_list": [kf_event("e1")], "next_cursor": "c1", "has_more": 0},
        {"msg_list": [kf_msg("m1", "公司拖欠我三个月工资，还把我辞退了")],
         "next_cursor": "c2", "has_more": 0},
    ])
    run(worker)
    run(worker, token="tk2")

    assert len(kf.sent) == 2
    reply = kf.sent[1][2]
    assert "比如" not in reply, "第二句不该又是开场白"
    assert "律师" in reply  # 改走承接话术
    assert "魏律师" not in reply, "一对一进线不点名具体律师（谁接单由分案引擎后定）"
    # 客户把事情说出来了，回应必须接着他说的往下问，而不是让他再讲一遍
    assert "什么时候开始" in reply or "走到哪一步" in reply
    reasons = [d["reasons"] for d in store.list_decisions(GID)]
    # 2026-08-10 判断层放开后，这句话先被认成法律话题（走作答层），
    # 再由 `_maybe_intake` 改判成追问。走哪条路不重要，**不能走开场白那条**
    # 才重要——那条路的兜底是「请您说说什么情况」，而他刚说完。
    assert any("kf:substance" in r or "kf:intake" in r for r in reasons), (
        "有内容的消息不该再绕开场白那条路"
    )
    assert not any("greeting:already-sent" in r for r in reasons)


def test_kf_replies_never_mention_group(tmp_path):
    """一对一客服窗口里没有群——话术不能出现「在群里」。"""
    _, kf = _chat(tmp_path, [
        "我的案子现在到哪一步了？",
        "拖欠工资多久可以申请劳动仲裁？",
        "材料交上去了吗",
        "帮我催一下",
    ])
    assert kf.sent, "应有回复"
    assert "群里" not in kf.texts()


def test_group_replies_still_mention_group():
    """群聊里「在群里回您」是自然的，不能被一刀切改掉。"""
    from responder.reply.templates import handoff_case_status, second_touch

    g = GroupProfile(group_id="g1", lawyer_name="魏")
    both = handoff_case_status(g, seed="a") + handoff_case_status(g, seed="c")
    assert "群里" in both
    assert "群里" in second_touch(g)


# ---------------------------------------------------------------- 索要联系方式
def _chat(tmp_path, contents, **over):
    store, kf, worker = make_env(tmp_path, [{
        "msg_list": [kf_msg(f"m{i}", c) for i, c in enumerate(contents)],
        "next_cursor": "c1", "has_more": 0,
    }], **over)
    run(worker)
    return store, kf


THREE = [
    "公司拖欠我三个月工资，还把我辞退了",
    "拖欠工资多久可以申请劳动仲裁？",
    "仲裁需要准备哪些材料？",
]


def test_the_first_close_is_an_invitation_to_the_office(tmp_path):
    """聊到第 3 条还没留电话 → **先请他来所里**，而不是先要号码。

    律所方 2026-08-12 跟所里同事复盘后的结论：真正高客单价的单子，
    几乎都是线下见过面之后才签的。线上聊得再好也只是筛查。
    所以第一拍是邀约，号码是退而求其次的第二拍——从大到小是让步，
    读起来自然；反过来先要号码再请人跑一趟，是层层加码，客户会觉得被追。
    """
    _, kf = _chat(tmp_path, THREE)
    text = kf.texts()
    assert ADDRESS in text, "第一拍应该是邀约到所"
    assert "主任律师" in text, "稀缺性是客户愿意专程跑一趟的主要理由"
    assert "手机号" not in text, "号码是下一拍的事"


def test_asking_for_a_number_is_the_fallback_beat(tmp_path):
    """他没接邀约那一茬，才退而求其次要个号码。"""
    _, kf = _chat(tmp_path, THREE + ["那我先准备着，还有别的要注意吗"])
    text = kf.texts()
    assert ADDRESS in text and "手机号" in text
    for _, _, one in kf.sent:
        assert not ("手机号" in one and ADDRESS in one), f"又挤一条里了：{one}"


def test_the_free_consult_offer_is_actually_said(tmp_path):
    """律所方 2026-08-12 拍板主打「免费法律咨询」，并在我两次提出
    《律师业务推广行为规则》第十条的顾虑后重申「你不要管，按我说的做」。
    这是律所的执业判断，由律所承担。

    技术上要守住的是**只放行律所逐字授权的那几句**（`approved_claims`），
    而不是把费用闸门拆掉——拆掉的后果是模型自己编出「打三折」
    「代理费一万」，那是律所没授权也不知情的话。
    """
    _, kf = _chat(tmp_path, THREE)
    text = kf.texts()
    assert "免费" in text, "律所授权的这张牌要真的打出去"
    assert "主任律师" in text


def test_only_the_authorised_sentence_gets_through():
    """授权一句话 ≠ 授权谈钱。别的关于钱的说法一句也不许漏。"""
    from responder.compliance import forbidden

    assert not forbidden.check("咨询是免费的，聊完您心里有个数")
    assert not forbidden.check("来所里免费咨询，主任律师帮您看看")
    for banned in ("我们可以给你打折", "代理费一万块", "这个案子不收费",
                   "律师费大概三万", "按标的额百分之十收"):
        assert forbidden.check(banned), f"这句不该放行：{banned}"


def test_ask_contact_not_before_threshold(tmp_path):
    """还没聊够就问电话像推销——不问。

    阈值 2026-08 从 3 下调到 2（主动要电话是提高变现率的正当动作），
    故这里用 1 条消息验证「未达阈值不问」。
    """
    _, kf = _chat(tmp_path, THREE[:1])
    assert "手机号" not in kf.texts()


def test_ask_contact_skipped_when_contact_already_given(tmp_path):
    """客户自己留了电话就别再问一遍。"""
    _, kf = _chat(tmp_path, [
        "公司拖欠我三个月工资，还把我辞退了",
        "我电话13812345678，你们联系我",
        "仲裁需要准备哪些材料？",
    ])
    assert "手机号" not in kf.texts()


def test_ask_contact_not_repeated(tmp_path):
    """同一通对话里问第二遍就成了催单——接管窗口内只问一次。"""
    _, kf = _chat(tmp_path, THREE + ["那我这种情况能要回多少赔偿？", "大概要多久能有结果？"])
    assert kf.texts().count("手机号") == 1


def test_ask_contact_skipped_for_signed_client(tmp_path):
    """已委托客户的电话我们本来就有，再问很怪。"""
    store, kf, worker = make_env(tmp_path, [{
        "msg_list": [kf_msg(f"m{i}", c) for i, c in enumerate(THREE)],
        "next_cursor": "c1", "has_more": 0,
    }])
    store.upsert_group(GroupProfile(
        group_id=GID, client_status=ClientStatus.SIGNED, lawyer_name="魏",
        kf_open_kfid=OPEN_KFID, kf_external_userid=EXT_USER,
    ))
    run(worker)
    assert "手机号" not in kf.texts()


def test_ask_contact_threshold_configurable(tmp_path):
    """阈值为 0 = 关掉这条收口动作。"""
    _, kf = _chat(tmp_path, THREE, ask_contact_after_messages=0)
    assert "手机号" not in kf.texts()


def test_the_invitation_survives_a_missing_address(tmp_path):
    """所址没配时仍然发得出邀约，只是不报门牌——**不能出现空括号**。

    邀约的价值不在那串地址，在「主任律师当面帮您看」。地址缺了照样能约，
    而一个「（）」会让整条消息看起来像坏掉的模板。
    """
    _, kf = _chat(tmp_path, THREE, office_address="")
    text = kf.texts()
    assert "来所里" in text or "过来一趟" in text
    assert "（）" not in text and "()" not in text


# ---------------------------------------------------------------- 下一步引导
def test_handoff_reply_always_has_a_next_step(tmp_path):
    """承接话术不能是死胡同。

    业务决策 2026-08：「我帮您问下律师，请您稍等」说完客户只能干等，
    很多人就这么走了。每条回复都要留一个下一步动作。

    「下一步」不等于「要电话」：第一条就问号码像推销（要电话有自己的阈值），
    但让他回答两个问题、把材料发过来，同样是明确的下一步——而且这一步
    问出来的东西律师接手时正好要用。
    """
    _, kf = _chat(tmp_path, ["我的案子现在到哪一步了？"])
    text = kf.texts()
    assert any(k in text for k in ("手机号", "材料", "什么时候开始", "走到哪一步")), (
        "承接类回复必须给客户一件此刻能做的事"
    )


def test_phone_and_address_are_never_asked_in_one_breath(tmp_path):
    """要电话和报所址**必须分成两拍**（律所方 2026-08-08 实测要求）。

    原来这两件事挤在同一条消息里——「留个手机号吧……当面聊也可以，
    地址是××路 88 号平高广场 11 楼」——律所方的原话是
    「这一长串的说话方式，让客户一看就会觉得这是不是 AI」。
    真人不会在刚听完一句话之后，把电话和地址一口气报出来。
    """
    _, kf = _chat(tmp_path, THREE)
    for _, _, t in kf.sent:
        if ADDRESS in t:
            assert "手机号" not in t, f"地址和要电话又挤在一条里了：{t}"
        assert t.count("手机号") <= 1


def test_no_next_step_in_group_chat(tmp_path):
    """群聊里承办律师本人在场，AI 再追着要电话既多余又越界。"""
    from responder.models import IncomingMessage

    store, kf, worker = make_env(tmp_path, [])
    store.upsert_group(GroupProfile(
        group_id="g-plain", client_status=ClientStatus.PROSPECT,
        lawyer_name="魏", robot_webhook="rk-1",
    ))
    worker.pipeline.handle(IncomingMessage(
        msg_id="g1", group_id="g-plain", sender_id="u1",
        content="我的案子现在到哪一步了？"))
    drafts = "\n".join(r["text"] for r in store.list_replies("g-plain"))
    assert "手机号" not in drafts


def test_next_step_skipped_once_contact_known(tmp_path):
    """客户已经留了电话就不再推——再问就成了没在听。"""
    _, kf = _chat(tmp_path, [
        "我电话13812345678，你们联系我",
        "我的案子现在到哪一步了？",
    ])
    assert kf.texts().count("手机号") == 0


# ---------------------------------------------------------------- 挽留
def _idle(store, group_id, seconds):
    """把该会话的全部消息往前挪，制造「静默了这么久」的状态。"""
    from datetime import datetime, timedelta

    cutoff = (datetime.now() - timedelta(seconds=seconds)).isoformat()
    with store._conn() as conn:
        conn.execute("UPDATE messages SET created_at=? WHERE group_id=?",
                     (cutoff, group_id))
        conn.execute("UPDATE replies SET created_at=? WHERE group_id=?",
                     (cutoff, group_id))


def test_winback_after_idle_without_contact(tmp_path):
    """聊完没留电话、会话静默 → 补一条挽留（此前完全没有这一环）。"""
    store, kf, worker = make_env(tmp_path, [{
        "msg_list": [kf_msg("m1", "公司拖欠我三个月工资，还把我辞退了")],
        "next_cursor": "c1", "has_more": 0,
    }])
    run(worker)
    n = len(kf.sent)
    _idle(store, GID, 3600)
    worker.tick()

    assert len(kf.sent) > n, "静默后应补一条挽留"
    last = kf.sent[-1][2]
    assert "手机号" in last and ADDRESS in last


def test_winback_sent_only_once(tmp_path):
    """挽留只发一次，第二次就成了骚扰。"""
    store, kf, worker = make_env(tmp_path, [{
        "msg_list": [kf_msg("m1", "公司拖欠我三个月工资")],
        "next_cursor": "c1", "has_more": 0,
    }])
    run(worker)
    _idle(store, GID, 3600)
    worker.tick()
    n = len(kf.sent)
    _idle(store, GID, 7200)
    worker.tick()
    assert len(kf.sent) == n


def test_winback_skipped_when_contact_left(tmp_path):
    """已经留了电话就没什么好挽的。"""
    store, kf, worker = make_env(tmp_path, [{
        "msg_list": [kf_msg("m1", "我电话13812345678，你们联系我")],
        "next_cursor": "c1", "has_more": 0,
    }])
    run(worker)
    n = len(kf.sent)
    _idle(store, GID, 3600)
    worker.tick()
    assert len(kf.sent) == n


def test_winback_for_silent_visitor_gives_an_example(tmp_path):
    """进来被问候后一句没说的人，卡在「不知道怎么开口」——要再给一次例句。"""
    store, kf, worker = make_env(tmp_path, [
        {"msg_list": [kf_event("e1")], "next_cursor": "c1", "has_more": 0},
    ])
    run(worker)
    _idle(store, GID, 3600)
    worker.tick()

    assert len(kf.sent) == 2
    assert "比如" in kf.sent[-1][2], "没开过口的人要给例句，不是直接要电话"


def test_winback_can_be_disabled(tmp_path):
    store, kf, worker = make_env(tmp_path, [{
        "msg_list": [kf_msg("m1", "公司拖欠我三个月工资")],
        "next_cursor": "c1", "has_more": 0,
    }], winback_enabled=False)
    run(worker)
    n = len(kf.sent)
    _idle(store, GID, 3600)
    worker.tick()
    assert len(kf.sent) == n


def test_intake_never_names_a_specific_lawyer(tmp_path):
    """一对一进线的对话里不出现具体律师姓名。

    业务决策 2026-08：谁接这单由分案引擎按专长与负载算出来，
    客服会话建档时的 lawyer_name 只是配置默认值。说「魏律师会给您回电话」
    而实际派给了别人，客户等的就是个不会来的电话。
    """
    _, kf = _chat(tmp_path, THREE + ["帮我催一下", "我的案子到哪一步了"])
    text = kf.texts()
    assert text, "应有回复"
    assert "魏律师" not in text
    assert "律师" in text  # 但仍然说「律师」，不是含糊过去


def test_group_chat_still_names_the_lawyer():
    """群聊里的律师名是人工维护的真名，律师本人也在群里，点名不能抹掉。"""
    from responder.reply.templates import handoff_generic

    g = GroupProfile(group_id="g1", lawyer_name="魏")
    assert "魏律师" in handoff_generic(g, seed="a")


# ---------------------------------------------------------------- 会话转接
def test_ai_silent_after_handoff(tmp_path):
    """转接给律师后 AI 必须闭嘴。

    现有的 gate:human-takeover 靠律师**发言**触发，而转接发生在律师说第一句话
    之前——少了这道门，客户会看到两个「人」同时在回他。
    """
    store, kf, worker = make_env(tmp_path, [
        {"msg_list": [kf_msg("m1", "公司拖欠我三个月工资")],
         "next_cursor": "c1", "has_more": 0},
        {"msg_list": [kf_msg("m2", "那我该准备什么材料")],
         "next_cursor": "c2", "has_more": 0},
    ])
    run(worker)
    n = len(kf.sent)
    store.set_handoff(GID, "wei")
    run(worker, token="tk2")

    assert len(kf.sent) == n, "已转人工后 AI 不应再发言"
    reasons = [d["reasons"] for d in store.list_decisions(GID)]
    assert any("gate:handed-off" in r for r in reasons)


def test_ai_reclaims_when_lawyer_never_picks_up(tmp_path):
    """律师迟迟不接手 → AI 收回来继续兜着。

    客户被转给一个不看企微的律师、晾在那儿没人理，比 AI 一直陪着更糟。
    """
    from datetime import datetime, timedelta

    store, kf, worker = make_env(tmp_path, [
        {"msg_list": [kf_msg("m1", "公司拖欠我三个月工资")],
         "next_cursor": "c1", "has_more": 0},
        {"msg_list": [kf_msg("m2", "还有人在吗")],
         "next_cursor": "c2", "has_more": 0},
    ], handoff_reclaim_seconds=600)
    run(worker)
    n = len(kf.sent)
    store.set_handoff(GID, "wei")
    # 把转接时刻推到很久以前：律师一直没接手
    stale = (datetime.now() - timedelta(seconds=3600)).isoformat()
    with store._conn() as conn:
        conn.execute("UPDATE groups SET handoff_at=? WHERE group_id=?", (stale, GID))
    run(worker, token="tk2")

    assert len(kf.sent) > n, "超时未接手应由 AI 收回，不能把客户晾着"
    reasons = [d["reasons"] for d in store.list_decisions(GID)]
    assert any("handoff:reclaimed" in r for r in reasons)


def test_handoff_state_round_trips(tmp_path):
    store, _, _ = make_env(tmp_path, [])
    store.upsert_group(GroupProfile(
        group_id=GID, kf_open_kfid=OPEN_KFID, kf_external_userid=EXT_USER))
    store.set_handoff(GID, "wei")
    g = store.get_group(GID)
    assert g.handoff_userid == "wei" and g.handoff_at is not None
    store.set_handoff(GID, "")
    g = store.get_group(GID)
    assert g.handoff_userid == "" and g.handoff_at is None


# ---------------------------------------------------------------- 回访客户
def test_returning_customer_saying_hello_gets_a_greeting_not_a_handoff(tmp_path):
    """老客户回来说一句「你好」，不能回「我帮您转给律师确认下」。

    线上实测踩到的：这通对话以前发过开场白，「一通对话只许一次开场白」
    就把它降级成了承接——可客户什么都还没说，转什么？
    """
    store, kf, worker = make_env(tmp_path, [
        {"msg_list": [kf_msg("m1", "公司拖欠我三个月工资，还把我辞退了")],
         "next_cursor": "c1", "has_more": 0},
        {"msg_list": [kf_msg("m2", "你好")], "next_cursor": "c2", "has_more": 0},
    ])
    run(worker)
    run(worker, token="tk2")

    last = kf.sent[-1][2]
    assert "转给" not in last, "光打招呼没内容可转，不该回承接"
    assert "我在的" in last or "您说" in last or "看到您消息了" in last
    assert "上海松沪律师事务所" not in last, "老客户不用把律所全称再报一遍"
    reasons = [d["reasons"] for d in store.list_decisions(GID)]
    assert any("greeting:again" in r for r in reasons)


def test_returning_customer_with_real_content_still_handed_off(tmp_path):
    """但客户真说了事，仍然走承接——这条规则本身是对的，只是之前一刀切了。"""
    store, kf, worker = make_env(tmp_path, [
        {"msg_list": [kf_msg("m1", "公司拖欠我三个月工资")],
         "next_cursor": "c1", "has_more": 0},
        {"msg_list": [kf_msg("m2", "我老公昨天被拘留了")],
         "next_cursor": "c2", "has_more": 0},
    ])
    run(worker)
    run(worker, token="tk2")
    assert "律师" in kf.sent[-1][2]


def test_returning_visitor_gets_short_regreeting_on_enter(tmp_path):
    """隔了一段时间再点进来的老客户：不重新自我介绍，但也不能一声不吭。

    对着空窗口，老客户和新客户一样会走。
    """
    from datetime import datetime, timedelta

    store, kf, worker = make_env(tmp_path, [
        {"msg_list": [kf_msg("m1", "拖欠工资多久可以申请劳动仲裁？")],
         "next_cursor": "c1", "has_more": 0},
        {"msg_list": [kf_event("e9")], "next_cursor": "c2", "has_more": 0},
    ])
    run(worker)
    n = len(kf.sent)
    # 把上一轮对话推到很久以前 → 这次点进来算「回访」
    old = (datetime.now() - timedelta(hours=8)).isoformat()
    with store._conn() as conn:
        conn.execute("UPDATE messages SET created_at=? WHERE group_id=?", (old, GID))
    run(worker, token="tk2")

    assert len(kf.sent) == n + 1, "回访也要有人招呼一声"
    assert "上海松沪律师事务所" not in kf.sent[-1][2], "不重新自我介绍"


def test_reentering_right_after_talking_stays_quiet(tmp_path):
    """刚聊完又点回会话页——那不是回访，是同一次对话，别再打招呼。"""
    store, kf, worker = make_env(tmp_path, [
        {"msg_list": [kf_msg("m1", "拖欠工资多久可以申请劳动仲裁？")],
         "next_cursor": "c1", "has_more": 0},
        {"msg_list": [kf_event("e9")], "next_cursor": "c2", "has_more": 0},
    ])
    run(worker)
    n = len(kf.sent)
    run(worker, token="tk2")
    assert len(kf.sent) == n

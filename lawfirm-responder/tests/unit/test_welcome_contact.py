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
    assert "魏律师" in reply  # 改走承接话术
    reasons = [d["reasons"] for d in store.list_decisions(GID)]
    assert any("greeting:already-sent" in r for r in reasons)


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


def test_ask_contact_after_threshold(tmp_path):
    """聊到第 3 条还没留电话 → 主动要手机号 + 报全地址邀约到所。"""
    _, kf = _chat(tmp_path, THREE)
    text = kf.texts()
    assert "手机号" in text
    assert ADDRESS in text
    assert "上海松沪律师事务所" in text


def test_ask_contact_not_before_threshold(tmp_path):
    """才聊两句就问电话像推销——不问。"""
    _, kf = _chat(tmp_path, THREE[:2])
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


def test_ask_contact_falls_back_without_address(tmp_path):
    """地址留空时只留电话、不报地址——不能出现空括号。"""
    _, kf = _chat(tmp_path, THREE, office_address="")
    text = kf.texts()
    assert "手机号" in text
    assert "（）" not in text and "()" not in text

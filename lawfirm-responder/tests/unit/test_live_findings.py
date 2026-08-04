"""真机测试暴露的三个话术问题（2026-08，律所方在微信客服窗口实测）。

三条都不是「说得不够好」，是**答非所问**——比复读更伤客户：

1. 客户接着问了第二个费用问题，AI 回「抱歉让您久等了，我刚又催了一下」。
   他没在等，他在问。
2. 客户问「你是人还是机器？」，AI 一声不吭。问这句的人本来就在怀疑，
   没人应等于坐实了怀疑。
3. 客户说「律师电话多少？你给我，我打给律师」——全程最强的成交信号——
   AI 回「我帮您转给律师确认下」，等于在他伸手的那一刻给了句空话。
"""

from responder.config import Settings
from responder.engine import rules
from responder.gateway.wecom_kf import ORIGIN_CUSTOMER
from responder.models import Category, ClientStatus, GroupProfile
from responder.service import Pipeline
from responder.store.db import Store
from responder.worker import KfSyncJob, Worker

OPEN_KFID = "wk-live"
EXT_USER = "wmLiveTester"
GID = f"kf:{OPEN_KFID}:{EXT_USER}"


class FakeKf:
    def __init__(self, batches):
        self.batches = list(batches)
        self.sent: list[str] = []

    def available(self):
        return True

    def servicer_list(self, open_kfid):
        return ["wei"]

    def sync_msg(self, token, open_kfid, cursor="", limit=1000):
        if self.batches:
            return self.batches.pop(0)
        return {"msg_list": [], "next_cursor": cursor, "has_more": 0}

    def send_text(self, open_kfid, external_userid, text):
        self.sent.append(text)
        return True


class Snd:
    def __init__(self):
        self.direct = []

    def send_direct_text(self, userid, text):
        self.direct.append((userid, text))
        return True


def kf_msg(msgid, content):
    return {
        "msgid": msgid, "open_kfid": OPEN_KFID, "external_userid": EXT_USER,
        "origin": ORIGIN_CUSTOMER, "msgtype": "text", "text": {"content": content},
    }


def run_conversation(tmp_path, contents, **over):
    """把几条客户消息按顺序灌进去，返回 AI 实际发出的每一条。"""
    db = str(tmp_path / "live.db")
    store = Store(db)
    cfg = dict(
        mode="live", db_path=db, split_messages=False, split_delay_seconds=0,
        wecom_kf_secret="s", llm_answer_enabled=False, llm_refine_enabled=False,
        lead_brief_enabled=False, kf_welcome_on_enter=False, handoff_enabled=False,
    )
    cfg.update(over)
    settings = Settings(**cfg)
    kf = FakeKf([
        {"msg_list": [kf_msg(f"m{i}", c)], "next_cursor": f"c{i}", "has_more": 0}
        for i, c in enumerate(contents)
    ])
    sender = Snd()
    worker = Worker(Pipeline(store, sender, settings, kf_client=kf), store, sender,
                    kf_client=kf)
    for _ in contents:
        worker.process_kf(KfSyncJob(token="tk", open_kfid=OPEN_KFID))
    return store, kf.sent


# ----------------------------------------------- ① 同类别的新问题不是「又问一遍」
def test_second_fee_question_gets_an_answer_not_a_chase_apology(tmp_path):
    """两个不同的费用问题 → 第二个照常回答，不能回「我刚又催了一下」。"""
    _, sent = run_conversation(tmp_path, [
        "离婚官司你们怎么收费？",
        "婚姻案件一般收多少钱？",
    ])
    assert len(sent) == 2
    assert "久等" not in sent[1] and "催" not in sent[1]


def test_literally_repeating_yourself_still_gets_the_chase_reply(tmp_path):
    """真把同一句话再发一遍，那就是在催了——这时候「我又催了一下」才对题。"""
    _, sent = run_conversation(tmp_path, [
        "离婚官司你们怎么收费？",
        "离婚官司你们怎么收费？",
    ])
    assert "催" in sent[1]


def test_repeat_detection_ignores_punctuation(tmp_path):
    """「怎么收费」和「怎么收费？？」是同一句话，别因为标点判成新问题。"""
    _, sent = run_conversation(tmp_path, [
        "离婚官司你们怎么收费",
        "离婚官司你们怎么收费？？",
    ])
    assert "催" in sent[1]


# ------------------------------------------------------- ② 「你是人还是机器？」
def test_identity_question_is_answered_not_ignored(tmp_path):
    """此前它落进默认沉默，客户什么也收不到——最差的处理。"""
    _, sent = run_conversation(tmp_path, ["公司欠我三个月工资怎么办？", "你是人还是机器？"])
    assert len(sent) == 2
    reply = sent[1]
    assert "律师" in reply  # 讲清楚专业意见谁给
    assert "机器" not in reply and "AI" not in reply  # 不明示 AI 身份（业务决策 2026-07）


def test_identity_reply_does_not_claim_to_be_a_lawyer(tmp_path):
    """合规红线：冒充律师身份。说自己是接待可以，说自己是律师不行。"""
    _, sent = run_conversation(tmp_path, ["你们是律师吗"])
    assert "我是律师" not in sent[0] and "我们是律师" not in sent[0]


def test_identity_question_hands_the_floor_back(tmp_path):
    """不能停在一个关于我们自己的话题上——一句话说完，把话头交回客户。"""
    _, sent = run_conversation(tmp_path, ["你是真人吗？"])
    assert any(k in sent[0] for k in ("您接着说", "您继续说"))


# --------------------------------------------------- ③ 客户主动要律师电话
def test_asking_for_the_lawyers_phone_flips_into_asking_for_theirs(tmp_path):
    """全程最强的成交信号。回一句「我帮您转达」等于把伸出来的手放下了。"""
    _, sent = run_conversation(tmp_path, ["律师的电话号码多少？你给我，我打给律师"])
    reply = sent[0]
    assert "手机号" in reply  # 反手要他的号码
    assert "九峰路" in reply  # 给第二条路：直接来所里


def test_asking_for_contact_does_not_ask_for_the_phone_twice(tmp_path):
    """这条话术自带索要联系方式，不能再被追加一段「留个手机号吧」。"""
    _, sent = run_conversation(tmp_path, [
        "公司欠我三个月工资怎么办？",
        "我想找律师聊聊",
        "怎么联系律师",
    ])
    assert sent[-1].count("手机号") == 1


def test_want_lawyer_contact_is_not_treated_as_chasing(tmp_path):
    """「怎么联系律师」是往前走，不是在催——不能被二次安抚顶掉。"""
    assert rules.is_chasing("怎么联系律师", Category.CONTACT) is False
    assert rules.is_chasing("怎么还没人回复", Category.CONTACT) is True


# --------------------------------------------------------------- 分类层单测
def test_classification_of_the_three_cases():
    assert rules.classify("你是人还是机器？")[3] == ["identity-question"]
    assert rules.classify("给我律师的微信")[3] == ["want-lawyer-contact"]
    # 费用问题不能被上面两条误吞
    assert rules.classify("婚姻案件一般收多少钱？")[1] is Category.FEE


def test_group_chat_reply_is_untouched(tmp_path):
    """群聊里承办律师本人在场，这三条话术都只对一对一窗口生效。"""
    db = str(tmp_path / "g.db")
    store = Store(db)
    settings = Settings(mode="live", db_path=db, llm_answer_enabled=False,
                        llm_refine_enabled=False, lead_brief_enabled=False)
    store.upsert_group(GroupProfile(
        group_id="g-1", client_status=ClientStatus.PROSPECT,
        lawyer_userid="wei", lawyer_name="魏",
    ))
    from responder.models import IncomingMessage

    p = Pipeline(store, Snd(), settings)
    d = p.handle(IncomingMessage(msg_id="g1", group_id="g-1", sender_id="cust",
                                 content="你是人还是机器？"))
    # 群里同样该答（沉默更差），但话术里点名承办律师——那是群聊的规矩
    assert "identity-question" in d.reasons

"""客户点头与留号的那两秒（2026-08-12 全链路体检，最贵的两条）。

体检结论原话：「客户已经点头的那几秒钟，系统要么沉默、要么答非所问——
最贵的三个缺陷全挤在这里。」这一组守的就是那两秒：

1. AI 发完完整邀约，客户回一句「好的」→ 旧版判成闲聊、**一个字都不回**。
   没约哪天、没说带什么材料、没有任何东西告诉他对面还有人；
   而这次点头连线索里都没记，律师拿到的是一张既没电话也没写「他答应来了」的冷单。
2. 客户把手机号打出来 → 旧版回「律师这会儿应该在忙」。
   客户交出号码换回一句「他在忙」，合理解读只有一个：我白给了，这边没人看。

两条的共同点：**系统自认为处理完了，客户那头什么都没发生。**
"""

import json
from datetime import datetime, timedelta

from responder.config import Settings
from responder.engine import rules
from responder.models import (
    Action,
    Category,
    ClientStatus,
    GroupProfile,
    IncomingMessage,
)
from responder.reply import templates
from responder.service import Pipeline
from responder.store.db import Store

OPEN_KFID = "wk-affirm"
EXT = "wmAffirmCustomer"
GID = f"kf:{OPEN_KFID}:{EXT}"


class Snd:
    def __init__(self):
        self.direct: list[tuple[str, str]] = []

    def send_direct_text(self, userid, text):
        self.direct.append((userid, text))
        return True


class Kf:
    """只记不发。"""

    def __init__(self):
        self.sent: list[str] = []

    def available(self):
        return True

    def servicer_list(self, kfid):
        return ["wei"]

    def send_text(self, kfid, ext, text):
        self.sent.append(text)
        return True

    def transfer(self, kfid, ext, userid):
        return True


def make(tmp_path, **over):
    db = str(tmp_path / "a.db")
    store = Store(db)
    cfg = dict(
        mode="live", db_path=db, split_messages=False, split_delay_seconds=0,
        wecom_kf_secret="s", llm_answer_enabled=False, llm_refine_enabled=False,
        lead_brief_enabled=False,
    )
    cfg.update(over)
    settings = Settings(**cfg)
    kf = Kf()
    return store, kf, Pipeline(store, sender=Snd(), settings=settings, kf_client=kf)


def kf_group(**over) -> GroupProfile:
    fields = dict(
        client_status=ClientStatus.PROSPECT, group_id=GID,
        kf_open_kfid=OPEN_KFID, kf_external_userid=EXT, case_type="劳动仲裁",
    )
    fields.update(over)
    return GroupProfile(**fields)


def msg(text, mid="m1") -> IncomingMessage:
    return IncomingMessage(
        msg_id=mid, group_id=GID, sender_id=EXT, content=text,
        msg_type="text", created_at=datetime.now(), sender_is_staff=False,
    )


def say(store, text, mode="live"):
    """假装 AI 刚说过这句话（走 replies 表，_awaiting 就是读它）。"""
    store.save_reply(f"r{abs(hash(text)) % 99999}", GID, text, mode, True,
                     category="handoff", parts=1)


# ------------------------------------------------------- 规则层：一声「好的」
def test_bare_ok_stays_silent_without_a_pending_question():
    """没有上下文时「好的」判沉默是**对的**，不许因为这次改动松掉。

    客户自己说完一段话之后补一句「好的」，AI 跟着应一声是纯噪音——
    真人不会这么说话，而多说一句正是「一看就是机器人」的来源。
    """
    action, _, _, reasons = rules.classify("好的", is_one_on_one=True, awaiting="")
    assert action == Action.SILENCE
    assert "chitchat-fastpath" in reasons


def test_bare_ok_after_an_office_invite_is_an_acceptance():
    action, _, _, reasons = rules.classify(
        "好的", is_one_on_one=True, awaiting=rules.AWAIT_OFFICE
    )
    assert action == Action.HANDOFF
    assert reasons == ["affirm:office"]


def test_group_chat_never_treats_ok_as_acceptance():
    """群聊里承办律师本人在场，客户应一声不需要 AI 接话。"""
    action, _, _, _ = rules.classify(
        "好的", is_one_on_one=False, awaiting=rules.AWAIT_OFFICE
    )
    assert action == Action.SILENCE


def test_farewell_words_are_not_acceptance():
    """「谢谢」「辛苦了」是收尾语——客户说完就准备走，追一句反而黏人。

    这是 `_AFFIRM` 比 `_CHITCHAT` 窄的全部理由：只收「他答应了」，
    不收「他要结束了」。
    """
    for word in ("谢谢", "感谢", "辛苦了", "再见", "不用了"):
        _, _, _, reasons = rules.classify(
            word, is_one_on_one=True, awaiting=rules.AWAIT_OFFICE
        )
        assert not any(r.startswith("affirm:") for r in reasons), word


def test_a_real_sentence_is_not_swallowed_by_the_affirm_layer():
    """「好的，那我明天过去」比一声「好的」信息量大得多，要走正常分层。"""
    _, _, _, reasons = rules.classify(
        "好的，那我明天过去一趟", is_one_on_one=True, awaiting=rules.AWAIT_OFFICE
    )
    assert not any(r.startswith("affirm:") for r in reasons)


# ------------------------------------------------------- 管道层：接住那一拍
def test_ok_after_invite_gets_a_time_and_a_what_to_bring(tmp_path):
    """接住不等于应一声。必须落到一个他此刻就能做的动作上。"""
    store, kf, p = make(tmp_path)
    store.upsert_group(kf_group())
    say(store, templates.office_invite(kf_group(), seed="x"))

    d = p.handle(msg("好的"))

    assert d.action == Action.HANDOFF
    assert "affirm:office" in d.reasons
    assert kf.sent, "客户答应来所里之后，AI 不能一个字都不回"
    text = kf.sent[0]
    assert "方便" in text or "时间" in text, "要把时间定下来"
    assert "材料" in text, "要告诉他带什么——带了材料的人到场率高得多"


def test_ok_after_asking_for_phone_asks_him_to_send_it(tmp_path):
    store, kf, p = make(tmp_path)
    store.upsert_group(kf_group())
    say(store, templates.ask_contact(kf_group(), seed="x"))

    d = p.handle(msg("可以"))

    assert "affirm:contact" in d.reasons
    assert kf.sent and ("号码" in kf.sent[0] or "手机号" in kf.sent[0])


def test_a_stale_question_no_longer_counts_as_pending(tmp_path):
    """隔了太久的那句问话不算「在等他点头」——那声「好的」答的多半不是它。"""
    store, _, p = make(tmp_path, takeover_seconds=60)
    store.upsert_group(kf_group())
    say(store, templates.office_invite(kf_group(), seed="x"))
    with store._conn() as conn:
        old = (datetime.now() - timedelta(hours=2)).isoformat()
        conn.execute("UPDATE replies SET created_at=?", (old,))

    assert p._awaiting(store.get_group(GID)) == ""


def test_a_draft_reply_does_not_count_as_having_asked(tmp_path):
    """影子模式的草稿客户根本没看到，不能拿它当「我们问过了」。"""
    store, _, p = make(tmp_path)
    store.upsert_group(kf_group())
    say(store, templates.office_invite(kf_group(), seed="x"), mode="shadow")

    assert p._awaiting(store.get_group(GID)) == ""


def test_saying_yes_to_a_meeting_reaches_the_lawyer_as_a_hard_signal(tmp_path):
    """**这条是三条里最贵的。**

    一声「好的」字面上一个信号词都不命中，可它是「客户已答应来所面谈」——
    整条漏斗上最值钱的一个信号（律所方：高客单价的单子几乎都是线下见过面才签的）。
    不把它带进线索，律师拿到的就是一张既没电话、也没写「他答应来了」的冷单。
    """
    store, _, p = make(tmp_path, lead_brief_enabled=True, notify_all_leads=True,
                       default_notify_userid="reception")
    store.upsert_group(kf_group())
    say(store, templates.office_invite(kf_group(), seed="x"))

    p.handle(msg("好的"))

    row = store.get_lead(GID)
    assert row is not None, "客户答应面谈之后必须有一条线索"
    assert "meeting" in json.loads(row["signals"]), "「他答应来所里」必须进线索信号"


# ------------------------------------------------------- 客户把号码打出来
def test_phone_number_gets_acknowledged_first(tmp_path):
    """留电话是整通对话里最强的成交动作，第一句必须是「收到了」。"""
    store, kf, p = make(tmp_path)
    store.upsert_group(kf_group())

    d = p.handle(msg("我手机号13800138000，你们联系我"))

    assert "contact-left" in d.reasons
    assert kf.sent, "客户给了号码，不能不回"
    text = kf.sent[0]
    assert "收到" in text or "记下" in text or "存下" in text
    assert "在忙" not in text and "腾不出手" not in text, (
        "客户交出号码换回一句「他在忙」，等于告诉他没人看"
    )


def test_a_bare_phone_number_is_not_chasing():
    """判成「在催」的后果是回一句「抱歉让您久等了」——他没在等，他刚给完号码。"""
    assert rules.is_chasing("13800138000", Category.CONTACT) is False
    # 反向不放松：真正的催问照旧认得出来
    assert rules.is_chasing("在吗", Category.CHITCHAT) is True


def test_contact_reply_does_not_name_a_specific_lawyer(tmp_path):
    """业务决策 2026-08：一对一窗口不点名——谁接这单是分案引擎算出来的。"""
    store, kf, p = make(tmp_path)
    store.upsert_group(kf_group(lawyer_name="魏"))

    p.handle(msg("13800138000"))

    assert kf.sent and "魏" not in kf.sent[0]

"""「本该成交的客户被静默丢掉」的七条路径（2026-08-10 整链复查）。

律所方原话：「我们不能为了提效，反而导致原本会成交的客户没有成交。」

这一组测的全是**同一种失败**：系统自认为做完了，客户那头什么都没发生，
而后台一切正常。它比崩溃贵得多——崩溃有人看得见。
"""



from responder.config import Settings
from responder.models import (
    Action,
    Category,
    ClientStatus,
    Decision,
    GroupProfile,
    IncomingMessage,
)
from responder.service import Pipeline
from responder.store.db import Store

OPEN_KFID, EXT = "wk1", "cust1"
GID = f"kf:{OPEN_KFID}:{EXT}"


class Kf:
    """可以指定「发不出去」的客服桩。"""

    def __init__(self, *, can_send=True, servicers=("wei",)):
        self.can_send = can_send
        self.servicers = list(servicers)
        self.sent, self.transfers = [], []

    def available(self):
        return True

    def servicer_list(self, kfid):
        return list(self.servicers)

    def send_text(self, kfid, ext, text):
        if not self.can_send:
            return False
        self.sent.append(text)
        return True

    def transfer(self, kfid, ext, userid):
        self.transfers.append(userid)
        return True

    def service_state(self, kfid, ext):
        return 1

    def to_robot(self, kfid, ext):
        return True


def _pipe(tmp_path, kf=None, **over):
    cfg = dict(mode="live", db_path=str(tmp_path / "x.db"), llm_provider="none",
               wecom_kf_secret="s", split_messages=False, split_delay_seconds=0,
               llm_answer_enabled=False, llm_refine_enabled=False)
    cfg.update(over)
    s = Settings(**cfg)
    store = Store(s.db_path)
    store.upsert_lawyer("wei", {"name": "魏", "active": True})
    return store, Pipeline(store, sender=None, settings=s, kf_client=kf or Kf())


def _group(**over):
    f = dict(group_id=GID, kf_open_kfid=OPEN_KFID, kf_external_userid=EXT,
             client_status=ClientStatus.PROSPECT, case_type="劳动仲裁")
    f.update(over)
    return GroupProfile(**f)


# ------------------------------------------------------- 1. 转接前那句话没发出去
def test_no_transfer_when_the_handover_line_never_reached_the_customer(tmp_path):
    """过渡话术发不出去还照转，客户就落进一个**双向静默**的窗口：
    AI 被 `gate:handed-off` 按住，律师还没露面，而他从头到尾一个字没收到。
    比不转更糟——不转的话 AI 至少还在陪着。
    """
    import json

    kf = Kf(can_send=False)
    store, p = _pipe(tmp_path, kf)
    g = _group()
    store.upsert_group(g)
    row = {"priority": "P0", "assigned_userid": "wei",
           "signals": json.dumps(["contact"])}

    assert p._maybe_handoff(g, row, urgent=False) is False
    assert not kf.transfers, "话没送到就不该转"
    assert store.get_group(GID).handoff_userid == ""
    assert "没发出去" in store.get_note(f"handoff_skip:{GID}")


# ------------------------------------------------------- 2. 问候没发出去却记了账
def test_a_failed_welcome_is_not_recorded_as_a_greeting(tmp_path):
    """记了账 `has_greeting` 就为真，AI 从此认定「打过招呼了」。
    客户扫码进来看到的是**一片空白**——而空窗口正是最大的流失点。
    """
    from responder.worker import Worker

    kf = Kf(can_send=False)
    # 进线问候默认已关（企微后台自带欢迎语），这条测的是机制本身
    store, p = _pipe(tmp_path, kf, kf_welcome_on_enter=True)
    store.upsert_group(_group())
    w = Worker(p, store, kf_client=kf)

    w._kf_welcome(GID, OPEN_KFID, EXT, "enter-1")

    assert not store.has_greeting(GID), "没送到的问候不算问候"
    assert "空窗口" in store.get_note(f"welcome_failed:{GID}")
    rows = store.list_replies(GID, limit=5)
    assert rows and rows[0]["mode"] == "failed"


# ------------------------------------------------------- 3. 第三次追问不能变哑巴
def test_the_third_time_he_asks_we_still_say_something(tmp_path):
    """群聊到第三遍闭嘴是对的——律师在场，再说就是刷屏。
    进线窗口里没有别人：客户问了三遍还是同一句，说明他**越来越急**，
    这时候静默是最坏的回应，他下一步就是关掉窗口走人。
    """
    store, p = _pipe(tmp_path)
    g = _group()
    store.upsert_group(g)
    msg = IncomingMessage(msg_id="m3", group_id=GID, sender_id=EXT,
                          content="到底什么时候能回我")
    d = Decision(msg_id="m3", group_id=GID, action=Action.HANDOFF,
                 category=Category.CONTACT)
    # 已经答过两轮同类别
    for i in range(2):
        store.save_reply(f"r{i}", GID, "抱歉让您久等了", "live", True,
                         category=Category.CONTACT.value)

    out = p._apply_followup_policy(msg, d, g, "原文", "live", ask_contact=False)
    assert out is not None, "一对一窗口不许静默"
    assert "followup:third-touch-kf" in d.reasons
    assert "催" not in out, "他已经听过两遍「我催了」，第三遍得换个说法"


def test_a_group_chat_still_goes_quiet_on_the_third_ask(tmp_path):
    """群里承办律师本人在场，AI 说到第三遍就成了刷屏。这条不动。"""
    store, p = _pipe(tmp_path)
    g = GroupProfile(group_id="g-1", client_status=ClientStatus.PROSPECT,
                     lawyer_userid="wei")
    msg = IncomingMessage(msg_id="m3", group_id="g-1", sender_id="c",
                          content="到底什么时候能回我")
    d = Decision(msg_id="m3", group_id="g-1", action=Action.HANDOFF,
                 category=Category.CONTACT)
    for i in range(2):
        store.save_reply(f"r{i}", "g-1", "抱歉让您久等了", "live", True,
                         category=Category.CONTACT.value)
    assert p._apply_followup_policy(msg, d, g, "原文", "live", ask_contact=False) is None


# ------------------------------------------------------- 4. 处理炸了要给客户兜底
def test_a_crashed_message_still_gets_the_customer_an_answer(tmp_path):
    """判断链炸了一条，**游标照常往前走**——那条消息不会再来第二次。
    客户那头的表现是：发了一句话，然后什么也没有。
    日志里有异常，但没有人会去看一个「运行正常」的系统的日志。
    """
    from responder.worker import Worker

    kf = Kf()
    store, p = _pipe(tmp_path, kf)
    store.upsert_group(_group())
    w = Worker(p, store, kf_client=kf)

    w._rescue_failed_kf_message({
        "origin": 3, "open_kfid": OPEN_KFID, "external_userid": EXT,
        "msgid": "boom-1", "msgtype": "text", "text": {"content": "我被辞退了"},
    })

    assert kf.sent, "客户必须收到点什么，哪怕是一句兜底"
    assert "人工看一眼" in store.get_note(f"pipeline_failed:{GID}")


def test_the_rescue_never_becomes_the_second_crash(tmp_path):
    """兜底是最后一道，它自己再炸也只能吞掉——否则整批消息一起陪葬。"""
    from responder.worker import Worker

    store, p = _pipe(tmp_path)
    w = Worker(p, store, kf_client=None)
    w._rescue_failed_kf_message({"origin": 3})  # 缺字段，不该抛


# ------------------------------------------------------- 5. 交接单推不出去要响
def test_an_undelivered_brief_screams(tmp_path):
    """线索在、评分在、状态是「待跟进」，唯独那张单一个人也没收到。
    这是 2026-08-06 那条最贵的静默失败，只是换了个出口。"""
    from responder import lead

    class DeadSender:
        def send_direct_text(self, userid, text):
            return False

    store, p = _pipe(tmp_path)
    g = _group()
    store.upsert_group(g)
    convo = [{"content": "我要委托你们，电话13800001111", "sender_is_staff": 0}]
    lead.dispatch(store, g, convo, DeadSender(), settings=p.settings, force=True)

    assert "可见范围" in store.get_note(f"brief_undelivered:{GID}")
    row = store.get_lead(GID)
    assert not row["notified_at"], "没送到就不能记成已通知，否则永远不会重试"


# ------------------------------------------------------- 6/7. 导出表
def test_export_cannot_smuggle_a_formula_into_excel():
    """客户诉求是**客户自己打的字**。一句「=1+1」在表里会变成 2，
    而这张表会被转发给合伙人、发进微信群。"""
    from responder.exporter import _safe_cell

    for danger in ("=1+1", "+SUM(A1)", "-2", "@echo"):
        assert _safe_cell(danger).startswith("'")
    assert _safe_cell("公司拖欠工资") == "公司拖欠工资"


def test_export_link_opens_inside_wecom(tmp_path):
    """`/ui#g=` 在企业微信里点开是 404（企微把 `#` 转义成 `%23`），
    而这张表就是拿去转发的。"""
    from responder import exporter
    from responder.config import Settings

    store, p = _pipe(tmp_path)
    rows = exporter.build_rows(
        store,
        [{"group_id": "kf:wk:a", "status": "new", "created_at": "2026-08-10T10:00:00"}],
        settings=Settings(public_base_url="http://1.2.3.4",
                          db_path=str(tmp_path / "x.db")),
    )
    link = rows[1][exporter._COLUMNS.index(("完整对话", "_link"))]
    assert link.startswith("http://1.2.3.4/g/"), link


# ------------------------------------------------------- 抖音回调默认拒绝
def test_douyin_callback_is_closed_by_default(tmp_path):
    """没配校验 Token = 接入口关闭。敞着的话任何人都能灌进伪造的客户消息。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from responder.gateway.callback import router

    s = Settings(mode="live", db_path=str(tmp_path / "d.db"), llm_provider="none",
                 douyin_enabled=True, douyin_callback_token="")
    store = Store(s.db_path)
    app = FastAPI()
    app.state.store = store
    app.state.pipeline = Pipeline(store, None, s)
    app.include_router(router)
    r = TestClient(app).post("/douyin/callback", json={"event": "imReceiveMsg"})
    assert r.status_code == 403


# ---------------------------------- 8. 客户的话被记成「我方发言」→ 全程静默
def test_a_customer_message_is_never_filed_as_staff_speech(tmp_path):
    """企微在**客户消息**上也会带 `servicer_userid`（标明这通会话归谁接）。

    旧判据 `origin == 5 or bool(servicer_userid)` 于是把客户自己说的话
    记成了我方发言：`handle()` 走进 staff 分支 → 就地标记已转人工 →
    AI 从此闭嘴 → 这条消息一个字的回复也不会有。

    真机症状：客户连发五次「你好」，跨两天，全程静默，
    而判断日志里每一条都写着「staff-message，不需要 AI 回」。
    """
    from responder.worker import Worker

    kf = Kf()
    store, p = _pipe(tmp_path, kf)
    store.upsert_group(_group())
    w = Worker(p, store, kf_client=kf)

    w._handle_kf_message({
        "origin": 3,                      # 客户发的
        "servicer_userid": "wei",         # 但报文里带着接待人
        "open_kfid": OPEN_KFID, "external_userid": EXT,
        "msgid": "c-1", "msgtype": "text", "text": {"content": "你好"},
    })

    msgs = store.recent_messages(GID, 10)
    assert msgs and not msgs[-1]["sender_is_staff"], "客户的话不能记成我方发言"
    reasons = [d["reasons"] for d in store.list_decisions(GID)]
    assert not any("staff-message" in r for r in reasons)
    assert store.get_group(GID).handoff_userid == "", "更不能因此把会话标成已转人工"


def test_a_real_staff_message_is_still_recognised(tmp_path):
    """反过来不能漏：律师在企微里回一句就是接管，这条是「回一句就接手」的地基。"""
    from responder.worker import Worker

    kf = Kf()
    store, p = _pipe(tmp_path, kf)
    store.upsert_group(_group())
    w = Worker(p, store, kf_client=kf)

    w._handle_kf_message({
        "origin": 5, "servicer_userid": "wei",
        "open_kfid": OPEN_KFID, "external_userid": EXT,
        "msgid": "s-1", "msgtype": "text", "text": {"content": "您好，我是魏律师"},
    })
    assert store.get_group(GID).handoff_userid == "wei"

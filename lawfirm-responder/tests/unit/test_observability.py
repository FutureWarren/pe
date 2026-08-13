"""出错和「为什么没转律师」，律所侧要看得见（2026-08-12 体检「值得做」）。

律所侧没有 SSH，也不会去翻服务器日志。所以任何只写进 `logger` 的失败，
在业务上等同于**没有发生过**——而系统自认为处理完了。

三件事：
1. 线索链路一抛异常，交接单和逐条提醒会**同时**消失（后者的豁免条件正是
   「文字消息已由交接单统一承载」，而交接单根本没生成）。控制台线索页少一条、
   待办页少一条，没有任何红字。
2. 「为什么没自动转给律师」那几条理由是在判断日志落库**之后**才追加的，
   于是一条都记不进去——而那正是这些理由存在的全部意义。
3. 定时事务和客户消息跑在同一条线程上，一轮 sweep 慢起来是秒级的。
"""

import queue
from datetime import datetime

from responder import lead as lead_mod
from responder.config import Settings
from responder.models import ClientStatus, GroupProfile, IncomingMessage
from responder.service import Pipeline
from responder.store.db import Store

OPEN_KFID = "wk-obs"
EXT = "wmObs"
GID = f"kf:{OPEN_KFID}:{EXT}"


class Kf:
    def __init__(self):
        self.sent = []

    def available(self):
        return True

    def servicer_list(self, kfid):
        return ["wei"]

    def send_text(self, kfid, ext, text):
        self.sent.append(text)
        return True

    def transfer(self, kfid, ext, userid):
        return True


class Snd:
    def __init__(self):
        self.direct = []

    def send_direct_text(self, userid, text):
        self.direct.append((userid, text))
        return True


def make(tmp_path, **over):
    db = str(tmp_path / "o.db")
    store = Store(db)
    cfg = dict(
        mode="live", db_path=db, wecom_kf_secret="s", split_messages=False,
        split_delay_seconds=0, llm_answer_enabled=False, llm_refine_enabled=False,
        lead_brief_enabled=True, default_notify_userid="reception",
    )
    cfg.update(over)
    snd = Snd()
    kf = Kf()
    return store, snd, kf, Pipeline(store, sender=snd, settings=Settings(**cfg),
                                    kf_client=kf)


def kf_group() -> GroupProfile:
    return GroupProfile(
        client_status=ClientStatus.PROSPECT, group_id=GID,
        kf_open_kfid=OPEN_KFID, kf_external_userid=EXT,
    )


def msg(text, mid="m1") -> IncomingMessage:
    return IncomingMessage(
        msg_id=mid, group_id=GID, sender_id=EXT, content=text,
        msg_type="text", created_at=datetime.now(), sender_is_staff=False,
    )


def test_a_broken_lead_pipeline_leaves_a_trace_the_firm_can_see(tmp_path, monkeypatch):
    """只写 logger 等于没发生过——律所侧没有 SSH。"""
    store, _, _, p = make(tmp_path)
    store.upsert_group(kf_group())

    def boom(*a, **kw):
        raise RuntimeError("模型接口挂了")

    monkeypatch.setattr(lead_mod, "dispatch", boom)

    # 用一条会真的走到出单路径的：冷消息在首次进线时本就不出单
    d = p.handle(msg("我想委托你们，我电话13800138000"))

    assert "lead:failed" in d.reasons
    note = store.get_note(f"lead_failed:{GID}")
    assert "交接单没能生成" in note and "人工看一眼" in note
    assert store.counters().get("lead_failed", {}).get("n", 0) == 1


def test_the_per_message_reminder_takes_over_when_the_brief_fails(tmp_path, monkeypatch):
    """**这条是这一组里最贵的。**

    逐条提醒的豁免条件是「客服会话的文字消息已由交接单统一承载」。
    交接单一炸，两者同时消失——这个客户在律师那边彻底没有任何痕迹。
    """
    store, snd, _, p = make(tmp_path)
    store.upsert_group(kf_group())
    monkeypatch.setattr(
        lead_mod, "dispatch",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("炸了")),
    )

    p.handle(msg("我想找律师帮我处理，麻烦联系我"))

    assert snd.direct, "交接单没生成时，逐条提醒必须顶上"
    assert store.has_reminder("m1")


def test_the_reason_a_transfer_did_not_happen_reaches_the_decision_log(tmp_path):
    """判断日志原来落在线索链路**之前**，于是这几条理由一条都记不进去——
    而它们存在的全部意义就是让人看出「为什么没转」。"""
    store, _, _, p = make(tmp_path, handoff_enabled=True)
    store.upsert_group(kf_group())

    p.handle(msg("我想委托你们，麻烦让律师联系我"))

    rows = store.list_decisions(GID, limit=5)
    assert rows
    assert "handoff" in rows[0]["reasons"], f"没记下为什么没转：{rows[0]['reasons']}"


def test_customer_messages_are_not_stuck_behind_the_sweeps(tmp_path):
    """首响时长是北极星指标。定时事务里有企微调用和模型归纳，慢起来是秒级的——
    不能让一个正在等的客户排在「给知识库沉淀记忆」后面。"""
    from responder.worker import Worker

    store, _, kf, p = make(tmp_path)
    store.upsert_group(kf_group())
    w = Worker(p, store, sender=Snd(), kf_client=kf)
    seen = []

    def slow_sweep(now):
        # 模拟一轮慢 sweep：期间队列里进来一条客户消息
        w.q.put(msg("公司把我辞退了", mid="hot-1"))

    w._sweep_customer_memory = slow_sweep
    w.tick()

    seen = [r["msg_id"] for r in store.list_decisions(GID, limit=10)]
    assert "hot-1" in seen, "这条应该在本轮 tick 里就被处理掉，而不是等下一轮"


def test_draining_is_bounded(tmp_path):
    """让路要有上限：消息一直来就永远轮不到定时事务，而自动升级正跑在里面——
    那是我们唯一能远程推修复的通道。"""
    from responder.worker import Worker

    store, _, kf, p = make(tmp_path)
    w = Worker(p, store, sender=Snd(), kf_client=kf)
    for i in range(50):
        w.q.put(msg("你好", mid=f"flood-{i}"))

    w._drain_hot()

    assert w.q.qsize() == 30, "一轮最多让 20 条"


def test_the_queue_being_empty_is_not_an_error(tmp_path):
    from responder.worker import Worker

    store, _, kf, p = make(tmp_path)
    w = Worker(p, store, sender=Snd(), kf_client=kf)
    w._drain_hot()  # 不应抛
    assert w.q.empty()
    assert isinstance(w.q, queue.Queue)

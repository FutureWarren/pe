"""微信客服通道：拉取 → 自动建档 → 判断 → 客服通道回复 → 接管 → 游标持久化。

不出网：以记录桩替代 KfClient。
"""

from responder.config import Settings
from responder.gateway.wecom_kf import ORIGIN_CUSTOMER, ORIGIN_SERVICER, ORIGIN_SYSTEM
from responder.service import Pipeline
from responder.store.db import Store
from responder.worker import KfSyncJob, Worker

OPEN_KFID = "wk-001"
EXT_USER = "wmExternalUserAbc123"
GID = f"kf:{OPEN_KFID}:{EXT_USER}"


class FakeKf:
    """KfClient 记录桩：按批次吐消息，记录发出的回复。"""

    def __init__(self, batches, servicers=("wei",)):
        self.batches = list(batches)
        self.sent: list[tuple[str, str, str]] = []
        self.sync_calls: list[tuple[str, str, str]] = []
        self.servicers = list(servicers)
        self.servicer_calls = 0

    def available(self):
        return True

    def servicer_list(self, open_kfid):
        self.servicer_calls += 1
        return list(self.servicers)

    def sync_msg(self, token, open_kfid, cursor="", limit=1000):
        self.sync_calls.append((token, open_kfid, cursor))
        if self.batches:
            return self.batches.pop(0)
        return {"msg_list": [], "next_cursor": cursor, "has_more": 0}

    def send_text(self, open_kfid, external_userid, text):
        self.sent.append((open_kfid, external_userid, text))
        return True


def kf_msg(msgid, content, origin=ORIGIN_CUSTOMER, servicer=""):
    m = {
        "msgid": msgid, "open_kfid": OPEN_KFID, "external_userid": EXT_USER,
        "origin": origin, "msgtype": "text", "text": {"content": content},
    }
    if servicer:
        m["servicer_userid"] = servicer
    return m


class DirectSender:
    """企微单聊记录桩（线索交接单/律师提醒走这条通道）。"""

    def __init__(self):
        self.direct: list[tuple[str, str]] = []

    def send_direct_text(self, userid, text):
        self.direct.append((userid, text))
        return True


def make_env(tmp_path, batches, mode="live", servicers=("wei",)):
    db = str(tmp_path / "kf.db")
    store = Store(db)
    settings = Settings(
        mode=mode, db_path=db, split_delay_seconds=0,
        wecom_kf_secret="kf-secret", kf_default_lawyer_name="魏",
        kf_default_case_type="劳动仲裁",
    )
    kf = FakeKf(batches, servicers=servicers)
    sender = DirectSender()
    pipeline = Pipeline(store, sender=sender, settings=settings, kf_client=kf)
    worker = Worker(pipeline, store, sender, kf_client=kf)
    kf.direct = sender.direct  # 测试里统一从 kf 桩上读
    return store, kf, worker


def test_customer_message_creates_profile_and_replies(tmp_path):
    """客户进线：自动建档（新咨询/魏律师）→ 判断 → 经客服通道回复。"""
    store, kf, worker = make_env(tmp_path, [{
        "msg_list": [kf_msg("m1", "公司把我辞退了我要投诉你们这样处理")],
        "next_cursor": "c1", "has_more": 0,
    }])
    worker.process_kf(KfSyncJob(token="tk", open_kfid=OPEN_KFID))

    g = store.get_group(GID)
    assert g is not None and g.client_status.value == "prospect"
    assert g.lawyer_name == "魏" and g.kf_open_kfid == OPEN_KFID
    assert g.kf_external_userid == EXT_USER

    assert kf.sent, "应经客服通道回复"
    assert kf.sent[0][0] == OPEN_KFID and kf.sent[0][1] == EXT_USER
    # 判「安抚 + 已加急」这两层意思，不锁死字面：紧急话术有四个变体
    # （客户第三条又说「我快撑不住了」时收到一字不差的同一句，本身就是问题）
    body = kf.sent[0][2]
    assert any(w in body for w in ("别急", "别慌", "别着急"))
    assert "加急" in body
    assert store.list_decisions(GID)


def test_cursor_persisted_and_reused(tmp_path):
    """游标入库并在下次拉取时带上：重启不重复处理历史消息。"""
    store, kf, worker = make_env(tmp_path, [
        {"msg_list": [kf_msg("m1", "谢谢")], "next_cursor": "cursor-A", "has_more": 0},
        {"msg_list": [], "next_cursor": "cursor-B", "has_more": 0},
    ])
    worker.process_kf(KfSyncJob(token="tk", open_kfid=OPEN_KFID))
    assert store.get_kf_cursor(OPEN_KFID) == "cursor-A"
    worker.process_kf(KfSyncJob(token="tk2", open_kfid=OPEN_KFID))
    assert kf.sync_calls[1][2] == "cursor-A"  # 第二次带上了上次的游标
    assert store.get_kf_cursor(OPEN_KFID) == "cursor-B"


def test_has_more_pagination(tmp_path):
    """has_more 时继续翻页，两批消息都进管道。"""
    store, kf, worker = make_env(tmp_path, [
        {"msg_list": [kf_msg("p1", "拖欠工资多久可以申请劳动仲裁？")],
         "next_cursor": "c1", "has_more": 1},
        {"msg_list": [kf_msg("p2", "我老公被拘留了怎么办")],
         "next_cursor": "c2", "has_more": 0},
    ])
    worker.process_kf(KfSyncJob(token="tk", open_kfid=OPEN_KFID))
    assert store.get_message("p1") and store.get_message("p2")
    assert store.get_kf_cursor(OPEN_KFID) == "c2"


def test_servicer_message_triggers_takeover(tmp_path):
    """真人客服/律师发言（origin=5）→ 记为 staff，随后客户消息被接管门拦下。

    2026-08 起这一步更进一步：一对一窗口里客服说的第一句话直接记成**正式转接**
    （律所方要的「在企微里回一个字就接管」）。所以拦下它的从限时的
    human-takeover 变成了持久的 handed-off——AI 不再是「安静一会儿」，
    而是这通对话已经归人了。
    """
    store, kf, worker = make_env(tmp_path, [
        {"msg_list": [
            kf_msg("s1", "我来给您解释一下", origin=ORIGIN_SERVICER, servicer="wei"),
            kf_msg("c1", "我的案子到哪一步了？"),
        ], "next_cursor": "c1", "has_more": 0},
    ])
    worker.process_kf(KfSyncJob(token="tk", open_kfid=OPEN_KFID))
    assert kf.sent == []  # 人工刚说过话，AI 不抢答
    reasons = [d["reasons"] for d in store.list_decisions(GID)]
    assert any(
        "gate:human-takeover" in r or "gate:handed-off" in r for r in reasons
    ), reasons
    assert store.get_group(GID).handoff_userid == "wei"  # 说一句话就算接手


def test_system_message_ignored(tmp_path):
    """系统推送（欢迎语等，origin=4）不进判断。"""
    store, kf, worker = make_env(tmp_path, [{
        "msg_list": [kf_msg("sys1", "欢迎咨询", origin=ORIGIN_SYSTEM)],
        "next_cursor": "c1", "has_more": 0,
    }])
    worker.process_kf(KfSyncJob(token="tk", open_kfid=OPEN_KFID))
    assert store.get_message("sys1") is None
    assert store.list_decisions(GID) == []


def test_duplicate_msgid_processed_once(tmp_path):
    """同一 msgid 重复推送只处理一次。"""
    batch = {"msg_list": [kf_msg("dup", "我要投诉你们的服务态度")],
             "next_cursor": "c1", "has_more": 0}
    store, kf, worker = make_env(tmp_path, [batch, dict(batch)])
    worker.process_kf(KfSyncJob(token="tk", open_kfid=OPEN_KFID))
    n = len(kf.sent)
    assert n >= 1
    worker.process_kf(KfSyncJob(token="tk", open_kfid=OPEN_KFID))
    # 数「条数」会随分条策略变化（承接话术现在会带一句下一步引导），
    # 这里要断言的是「没有被处理第二遍」，所以比对前后是否新增
    assert len(kf.sent) == n
    assert len(store.list_replies(GID)) == 1


def test_shadow_mode_drafts_only(tmp_path):
    """影子模式：照常收、照常判断起草，但不向客户发送。"""
    store, kf, worker = make_env(tmp_path, [{
        "msg_list": [kf_msg("sh1", "我要投诉你们的服务态度")],
        "next_cursor": "c1", "has_more": 0,
    }], mode="shadow")
    worker.process_kf(KfSyncJob(token="tk", open_kfid=OPEN_KFID))
    assert kf.sent == []
    replies = store.list_replies(GID)
    assert replies and replies[0]["mode"] == "shadow"


def test_ai_disabled_profile_stays_silent(tmp_path):
    """控制台关掉该会话 AI 后不再发言（人工优先开关对客服通道同样有效）。"""
    store, kf, worker = make_env(tmp_path, [
        {"msg_list": [kf_msg("a1", "我要投诉你们的服务态度")],
         "next_cursor": "c1", "has_more": 0},
        {"msg_list": [kf_msg("a2", "我要投诉你们的服务态度啊")],
         "next_cursor": "c2", "has_more": 0},
    ])
    worker.process_kf(KfSyncJob(token="tk", open_kfid=OPEN_KFID))
    n = len(kf.sent)
    assert n >= 1
    store.set_group_ai(GID, False)
    worker.process_kf(KfSyncJob(token="tk", open_kfid=OPEN_KFID))
    assert len(kf.sent) == n  # 未新增


def test_no_wait_gate_in_kf(tmp_path):
    """客服会话 AI 是第一响应人：非紧急问题也立即回复，不等 2.5 分钟。"""
    store, kf, worker = make_env(tmp_path, [{
        "msg_list": [kf_msg("w1", "试用期被辞退有补偿吗？")],
        "next_cursor": "c1", "has_more": 0,
    }])
    worker.process_kf(KfSyncJob(token="tk", open_kfid=OPEN_KFID))
    assert kf.sent, "客服会话不应被补位等待门拦下"
    reasons = [d["reasons"] for d in store.list_decisions(GID)]
    assert not any("gate:waiting" in r for r in reasons)


def test_greeting_gets_opener_not_silence(tmp_path):
    """一对一窗口里「你好」必须有回应并引导说明情况，不能把客户晾着。"""
    store, kf, worker = make_env(tmp_path, [{
        "msg_list": [kf_msg("g1", "你好")], "next_cursor": "c1", "has_more": 0,
    }])
    worker.process_kf(KfSyncJob(token="tk", open_kfid=OPEN_KFID))
    assert kf.sent and "松沪律所" in kf.sent[0][2] or "我在的" in kf.sent[0][2]
    d = store.list_decisions(GID)[0]
    assert d["action"] == "answer" and d["category"] == "greeting"
    assert store.pending_reminders() == []  # 一句「你好」不惊动律师


def test_courtesy_still_silent_in_kf(tmp_path):
    """「谢谢」这类收尾应答仍保持沉默，避免无谓刷屏。"""
    store, kf, worker = make_env(tmp_path, [{
        "msg_list": [kf_msg("c1", "谢谢")], "next_cursor": "c1", "has_more": 0,
    }])
    worker.process_kf(KfSyncJob(token="tk", open_kfid=OPEN_KFID))
    assert kf.sent == []
    assert store.list_decisions(GID)[0]["action"] == "silence"


def test_urgent_kf_pushes_one_brief_to_servicer(tmp_path):
    """紧急进线：接收人自动取客服账号接待人，且只推一条交接单（不再逐条提醒）。"""
    store, kf, worker = make_env(
        tmp_path,
        [{"msg_list": [kf_msg("u1", "我老公被拘留了怎么办")],
          "next_cursor": "c1", "has_more": 0}],
        servicers=("weilai", "libackup"),
    )
    worker.process_kf(KfSyncJob(token="tk", open_kfid=OPEN_KFID))

    g = store.get_group(GID)
    assert g.notify_userid == "weilai" and g.backup_userid == "libackup"
    assert g.lawyer_userid == "", (
        "接待人只是「该通知谁」。写进 lawyer_userid 就成了「这是谁的数据」，"
        "名册里排第一的那位普通律师会拿到全所会话的可见权"
    )
    lead = store.get_lead(GID)
    assert lead and lead["notified_at"] and lead["urgency"] == "high"
    assert kf.sent, "群里仍应立即安抚客户"

    # 只推一条 DM（交接单），不再逐条提醒——同一件事不打扰两次
    dm = [t for t in worker.sender.direct if "客户诉求" in t[1]]
    assert len(dm) == 1 and dm[0][0] == "weilai"
    assert len(worker.sender.direct) == 1
    # 但紧急提醒必须入库：600 秒未处理升级第二责任人这条链扫的就是它，
    # 不落库＝主进线通道整体没有紧急兜底
    todo = store.pending_reminders()
    assert len(todo) == 1 and todo[0]["urgent"] and todo[0]["status"] == "pending"


def test_backfills_missing_notify_target(tmp_path):
    """早期建的档案没有接待人 → 下条消息到达时补齐，否则线索永远推不出去。"""
    store, kf, worker = make_env(tmp_path, [
        {"msg_list": [kf_msg("b1", "我要投诉你们的服务态度")],
         "next_cursor": "c1", "has_more": 0},
    ], servicers=("weilai",))
    # 模拟历史遗留：档案存在但无接收人
    from responder.models import ClientStatus, GroupProfile
    store.upsert_group(GroupProfile(
        group_id=GID, name="旧档案", client_status=ClientStatus.PROSPECT,
        kf_open_kfid=OPEN_KFID, kf_external_userid=EXT_USER,
    ))
    assert store.get_group(GID).reminder_userid == ""

    worker.process_kf(KfSyncJob(token="tk", open_kfid=OPEN_KFID))
    assert store.get_group(GID).notify_userid == "weilai"


def test_servicer_lookup_cached(tmp_path):
    """接待人列表按客服账号缓存，不为每个新客户重复查询。"""
    store, kf, worker = make_env(tmp_path, [
        {"msg_list": [kf_msg("s1", "我要投诉你们的服务态度")],
         "next_cursor": "c1", "has_more": 0},
    ])
    worker.process_kf(KfSyncJob(token="tk", open_kfid=OPEN_KFID))
    worker._ensure_kf_profile(f"kf:{OPEN_KFID}:other", OPEN_KFID, "other")
    assert kf.servicer_calls == 1


def test_unavailable_client_is_noop(tmp_path):
    """未配置客服 Secret 时收到回调不炸，只记日志。"""
    db = str(tmp_path / "n.db")
    store = Store(db)
    settings = Settings(mode="live", db_path=db)
    worker = Worker(Pipeline(store, None, settings), store, None, kf_client=None)
    worker.process_kf(KfSyncJob(token="tk", open_kfid=OPEN_KFID))
    assert store.list_decisions() == []

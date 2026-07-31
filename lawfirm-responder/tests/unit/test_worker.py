"""后台工作线程：回调去重、补位等待到点复评、接管抑制、提醒不重复。

全部同步调用 worker 的处理方法（不起线程），确保确定性。
"""

from datetime import datetime, timedelta

from responder.config import Settings
from responder.models import ClientStatus, GroupProfile, IncomingMessage
from responder.service import Pipeline
from responder.store.db import Store
from responder.worker import Worker


class RecordingSender:
    def __init__(self):
        self.robot: list[tuple[str, str]] = []
        self.direct: list[tuple[str, str]] = []

    def send_robot_text(self, webhook, text):
        self.robot.append((webhook, text))
        return True

    def send_group_text(self, chat_id, text):
        self.robot.append((chat_id, text))
        return True

    def send_direct_text(self, userid, text):
        self.direct.append((userid, text))
        return True


def make_env(tmp_path, mode="live"):
    db = str(tmp_path / "w.db")
    store = Store(db)
    store.upsert_group(
        GroupProfile(
            group_id="g1", name="劳动仲裁咨询群", client_status=ClientStatus.PROSPECT,
            case_type="劳动仲裁", lawyer_name="魏", lawyer_userid="wei",
            backup_userid="bk", robot_webhook="rk-1",
        )
    )
    settings = Settings(mode=mode, db_path=db, split_delay_seconds=0)
    sender = RecordingSender()
    pipeline = Pipeline(store, sender, settings)
    worker = Worker(pipeline, store, pipeline.sender)
    return store, sender, worker


def _msg(content, msg_id="m1", age_seconds=0.0, staff=False):
    return IncomingMessage(
        msg_id=msg_id, group_id="g1", sender_id="wei" if staff else "c1",
        content=content, sender_is_staff=staff,
        created_at=datetime.now() - timedelta(seconds=age_seconds),
    )


def test_duplicate_callback_processed_once(tmp_path):
    """企微超时重发同一 msg_id：只处理一次，不重复入库/发言。"""
    store, sender, worker = make_env(tmp_path)
    worker._process_new(_msg("公司把我辞退了我要投诉", "dup-1"))
    replies, robots = len(store.list_replies("g1")), len(sender.robot)
    worker._process_new(_msg("公司把我辞退了我要投诉", "dup-1"))
    assert len(store.list_replies("g1")) == replies
    assert len(sender.robot) == robots == 1  # 紧急免等待，只发了一次


def test_waiting_message_rechecked_and_sent(tmp_path):
    """非紧急消息：等待期内不发言，到点无人接管 → live 补位发言。"""
    store, sender, worker = make_env(tmp_path)
    worker._process_new(_msg("拖欠工资多久可以申请仲裁？", "w1", age_seconds=600))
    assert sender.robot == []  # 首判处于等待门，未发言
    assert store.due_pending_checks(datetime.now())  # 复评任务已登记且已到点
    worker.tick()
    assert sender.robot  # 到点补位发言
    assert store.due_pending_checks(datetime.now()) == []  # 任务已消费


def test_recheck_suppressed_after_staff_reply(tmp_path):
    """等待期内律师回了 → 到点复评撞上接管门，AI 保持沉默。"""
    store, sender, worker = make_env(tmp_path)
    worker._process_new(_msg("拖欠工资多久可以申请仲裁？", "w2", age_seconds=600))
    worker._process_new(_msg("我来解释一下仲裁时效", "s1", staff=True))
    worker.tick()
    assert sender.robot == []
    reasons = [d["reasons"] for d in store.list_decisions("g1")]
    assert any("gate:human-takeover" in r for r in reasons)


def test_reminder_not_duplicated_on_recheck(tmp_path):
    """首判已提醒律师，到点复评不再重复提醒同一条消息。"""
    store, sender, worker = make_env(tmp_path)
    # 用不触发线索简报的通用问题：费用类现在由交接单统一承载，不进逐条提醒
    worker._process_new(_msg("拖欠工资多久可以申请劳动仲裁？", "f1", age_seconds=600))
    assert len(store.pending_reminders()) == 1
    worker.tick()
    assert len(store.pending_reminders()) == 1


def test_staff_message_decision_logged(tmp_path):
    """律师发言的沉默判定也入日志（全量留痕）。"""
    store, _, worker = make_env(tmp_path)
    worker._process_new(_msg("我来说两句", "s2", staff=True))
    reasons = [d["reasons"] for d in store.list_decisions("g1")]
    assert any("staff-message" in r for r in reasons)


def test_drain_processes_queue(tmp_path):
    """drain：清空队列 + 跑一轮定时事务（submit 入口冒烟）。"""
    store, sender, worker = make_env(tmp_path)
    worker.submit(_msg("律师在吗？麻烦回复一下", "q1", age_seconds=600))
    worker.drain()
    assert store.get_message("q1") is not None
    assert sender.robot  # 催回复=承接类，到点后已发出

"""后台工作线程：企微回调「秒回 success」，实际处理异步完成。

为什么必须异步：企微要求回调 5 秒内应答，否则会重发回调（最多数次）。
判断链路里有 LLM 调用（最长十几秒）和分条发送的打字间隔，同步处理必然超时，
后果是企微重发 → 消息被处理两遍 → AI 在群里重复说话（真人感崩塌）。

单工作线程串行处理，同时兼任三件定时事务：
1. 新消息队列：回调线程只入队；重复投递以 msg_id 入库结果去重。
2. 补位等待到点复评：首判 gate:waiting 的消息由管道写入 pending_checks，
   到点重跑判断——期间律师已回则保持沉默，仍无人回则（live 模式）真正发言。
   没有这一步，live 模式下非紧急消息将永远停在「等待中」不会发出。
3. 紧急提醒超时升级：escalate_overdue 周期扫描（此前无人调度，链路是断的）。
"""

import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from responder.gateway import wecom_kf
from responder.models import ClientStatus, GroupProfile, IncomingMessage
from responder.notify import escalation

logger = logging.getLogger(__name__)


@dataclass
class KfSyncJob:
    """微信客服回调只给 Token，需据此拉取真实消息（见 gateway/wecom_kf.py）。"""

    token: str
    open_kfid: str


class Worker:
    def __init__(self, pipeline, store, sender=None, poll_seconds: float = 10.0,
                 kf_client=None):
        self.pipeline = pipeline
        self.store = store
        self.sender = sender
        self.kf_client = kf_client
        self.poll_seconds = poll_seconds
        self.q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="responder-worker"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def qsize(self) -> int:
        return self.q.qsize()

    # ------------------------------------------------------------ 入口
    def submit(self, item) -> None:
        """入队：IncomingMessage（应用回调）或 KfSyncJob（微信客服回调）。"""
        self.q.put(item)

    def drain(self) -> None:
        """同步清空队列并跑一轮定时事务（测试/停机前用）。"""
        while True:
            try:
                self._dispatch(self.q.get_nowait())
            except queue.Empty:
                break
        self.tick()

    def _dispatch(self, item) -> None:
        if isinstance(item, KfSyncJob):
            self.process_kf(item)
        else:
            self._process_new(item)

    def tick(self, now: datetime | None = None) -> None:
        """跑一轮定时事务：补位等待到点复评 + 紧急提醒超时升级。"""
        now = now or datetime.now()
        for row in self.store.due_pending_checks(now):
            self.store.delete_pending_check(row["msg_id"])
            msg = self.store.get_message(row["msg_id"])
            if msg is None:
                continue
            waited = (now - msg.created_at).total_seconds()
            try:
                self.pipeline.handle(msg, seconds_unanswered=waited)
            except Exception:
                logger.exception("recheck failed: %s", row["msg_id"])
        try:
            # 取实时的 sender（模式可在运行时切换）
            escalation.escalate_overdue(
                self.store, self.pipeline.sender, settings=self.pipeline.settings
            )
        except Exception:
            logger.exception("escalate_overdue failed")

    # ------------------------------------------------------------ 主循环
    def _run(self) -> None:
        last_tick = 0.0
        while not self._stop.is_set():
            try:
                item = self.q.get(timeout=1.0)
            except queue.Empty:
                item = None
            if item is not None:
                self._dispatch(item)
            if time.time() - last_tick >= self.poll_seconds:
                last_tick = time.time()
                self.tick()

    def _process_new(self, msg: IncomingMessage) -> None:
        try:
            if not self.store.save_message(msg):
                logger.info("duplicate callback ignored: %s", msg.msg_id)
                return
            self.pipeline.handle(msg)
        except Exception:
            logger.exception("message processing failed: %s", msg.msg_id)

    # ------------------------------------------------------------ 微信客服
    def process_kf(self, job: KfSyncJob) -> None:
        """按回调 Token 拉取客服消息，逐条进判断管道。

        游标持久化到库：重启不会重复处理历史消息（重复也会被 msg_id 去重兜底）。
        """
        if self.kf_client is None or not self.kf_client.available():
            logger.warning("kf callback received but kf client unavailable")
            return
        cursor = self.store.get_kf_cursor(job.open_kfid)
        for _ in range(20):  # has_more 循环上限，防异常游标导致死循环
            batch = self.kf_client.sync_msg(job.token, job.open_kfid, cursor)
            for raw in batch["msg_list"]:
                try:
                    self._handle_kf_message(raw)
                except Exception:
                    logger.exception("kf message failed: %s", raw.get("msgid"))
            cursor = batch["next_cursor"]
            if cursor:
                self.store.set_kf_cursor(job.open_kfid, cursor)
            if not batch["has_more"]:
                break

    def _handle_kf_message(self, raw: dict) -> None:
        origin = raw.get("origin")
        if origin == wecom_kf.ORIGIN_SYSTEM:
            return  # 系统推送（欢迎语等）不进判断
        open_kfid = raw.get("open_kfid", "")
        external_userid = raw.get("external_userid", "")
        if not open_kfid or not external_userid:
            return
        # 一个「客服账号 × 客户」= 一个会话档案，复用群档案的全部能力（开关/律师/留痕）
        group_id = f"kf:{open_kfid}:{external_userid}"
        self._ensure_kf_profile(group_id, open_kfid, external_userid)

        is_staff = origin == wecom_kf.ORIGIN_SERVICER or bool(raw.get("servicer_userid"))
        content = (raw.get("text") or {}).get("content", "")
        msg = IncomingMessage(
            msg_id=raw.get("msgid") or "",
            group_id=group_id,
            sender_id=raw.get("servicer_userid") or external_userid,
            sender_is_staff=is_staff,
            content=content,
            msg_type="text" if raw.get("msgtype") == "text" else (raw.get("msgtype") or "other"),
        )
        if not msg.msg_id:
            return
        if not self.store.save_message(msg):
            return  # 重复投递
        self.pipeline.handle(msg)

    def _ensure_kf_profile(self, group_id: str, open_kfid: str, external_userid: str) -> None:
        """客服会话首次出现时自动建档——员工零操作即可让 AI 上岗。"""
        if self.store.get_group(group_id) is not None:
            return
        s = self.pipeline.settings
        self.store.upsert_group(
            GroupProfile(
                group_id=group_id,
                name=f"微信客服 · 客户{external_userid[-6:]}",
                client_status=ClientStatus.PROSPECT,  # 客服进线默认为新咨询
                case_type=s.kf_default_case_type,
                lawyer_name=s.kf_default_lawyer_name,
                ai_enabled=s.kf_enabled,
                kf_open_kfid=open_kfid,
                kf_external_userid=external_userid,
            )
        )

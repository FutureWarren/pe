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
from datetime import datetime

from responder.models import IncomingMessage
from responder.notify import escalation

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, pipeline, store, sender=None, poll_seconds: float = 10.0):
        self.pipeline = pipeline
        self.store = store
        self.sender = sender
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
    def submit(self, msg: IncomingMessage) -> None:
        self.q.put(msg)

    def drain(self) -> None:
        """同步清空队列并跑一轮定时事务（测试/停机前用）。"""
        while True:
            try:
                self._process_new(self.q.get_nowait())
            except queue.Empty:
                break
        self.tick()

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
            escalation.escalate_overdue(
                self.store, self.sender, settings=self.pipeline.settings
            )
        except Exception:
            logger.exception("escalate_overdue failed")

    # ------------------------------------------------------------ 主循环
    def _run(self) -> None:
        last_tick = 0.0
        while not self._stop.is_set():
            try:
                msg = self.q.get(timeout=1.0)
            except queue.Empty:
                msg = None
            if msg is not None:
                self._process_new(msg)
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

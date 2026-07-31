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
from datetime import datetime, timedelta

from responder import lead
from responder.gateway import bot, wecom_kf
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
        self._servicer_cache: dict[str, list[str]] = {}

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
        elif isinstance(item, bot.BotEnvelope):
            self.process_bot(item)
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
        self._sweep_idle_leads(now)
        self._sweep_lead_sla(now)

    def _sweep_idle_leads(self, now: datetime) -> None:
        """对话安静下来后补一份线索简报——聊完没留电话的咨询同样有跟进价值。

        冷线索只归档进控制台，不推送打扰律师（见 lead.should_notify）。
        """
        s = self.pipeline.settings
        if not s.lead_brief_enabled:
            return
        until = now - timedelta(seconds=s.lead_idle_seconds)
        # 回看 24 小时而不是 4×idle（=1 小时）：服务重启/停机超过一小时期间
        # 安静下来的会话会被永久跳过。get_lead 幂等去重兜住重复处理。
        since = now - timedelta(seconds=max(s.lead_idle_seconds * 4, 86400))
        for gid in self.store.idle_conversations(since, until):
            if self.store.get_lead(gid):
                continue  # 已有线索，交由实时触发路径更新
            group = self.store.get_group(gid)
            if group is None:
                continue
            try:
                lead.dispatch(
                    self.store, group,
                    self.store.recent_messages(gid, s.lead_history_window),
                    self.pipeline.sender, settings=s,
                )
            except Exception:
                logger.exception("idle lead sweep failed: %s", gid)

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

    def _sweep_lead_sla(self, now: datetime) -> None:
        """P0 强意愿线索超时未联系 → 追加提醒并抄送第二责任人。

        「一小时内联系」是分层规则对律师的要求（docs/lead-routing.md），没有督办
        它就只是一句口号。每单只追一次（sla_nudged），避免变成骚扰。
        """
        s = self.pipeline.settings
        sender = self.pipeline.sender
        if not (s.lead_brief_enabled and s.lead_sla_enabled and sender):
            return
        cutoff = now - timedelta(seconds=s.lead_p0_sla_seconds)
        for row in self.store.overdue_p0_leads(cutoff):
            group = self.store.get_group(row["group_id"])
            if group is None:
                continue
            to = row.get("assigned_userid") or group.lawyer_userid or s.default_notify_userid
            if not to:
                continue
            mins = int(s.lead_p0_sla_seconds // 60)
            text = (
                f"【督办】强意愿线索已超 {mins} 分钟未标记联系\n"
                f"客户：{group.name or row['group_id']}\n"
                f"诉求：{(row.get('summary') or '')[:60]}\n"
                f"联系方式：{row.get('contact') or '见会话'}\n"
                "请尽快联系并在工作台标记「已联系」。"
            )
            ok = sender.send_direct_text(to, text)
            # 抄送必须在主送成功之后：否则主送失败 → sla_nudged 不置位 →
            # 每 10 秒的 tick 重跑 → 第二责任人一小时收 360 条「（抄送）督办」
            if ok:
                cc = group.backup_userid or s.default_notify_userid
                if cc and cc != to:
                    sender.send_direct_text(cc, f"（抄送）{text}")
                self.store.mark_lead_nudged(row["group_id"])
                logger.info("lead SLA nudge: %s → %s", row["group_id"], to)
            else:
                # 发不出去也要停止重试并留痕，否则轮询会把失败放大成风暴
                self.store.mark_lead_nudged(row["group_id"])
                logger.error("lead SLA nudge failed, giving up: %s → %s",
                             row["group_id"], to)

    # ------------------------------------------------------------ 群聊助手
    def process_bot(self, env: bot.BotEnvelope) -> None:
        """智能机器人回调：刷新会话档案（含发送地址）后进判断管道。

        每条回调都先落档案：一是让新群立刻出现在控制台「群管理」里，人工能马上
        补承办律师；二是刷新回调下发的会话 webhook——那是这条通道的发送地址。
        """
        try:
            self._ensure_bot_profile(env)
        except Exception:
            logger.exception("bot profile upsert failed: %s", env.group_id)
        if env.msg is None:
            return  # 入群等事件：建档即可
        self._process_new(env.msg)

    def _ensure_bot_profile(self, env: bot.BotEnvelope) -> None:
        s = self.pipeline.settings
        group_id = env.group_id
        if not group_id:
            return
        existing = self.store.get_group(group_id)
        if existing is not None:
            if env.webhook_url:
                existing.bot_webhook = env.webhook_url
                existing.bot_webhook_at = datetime.now()
            # 建档时后台还没配兜底接收人的旧档案：补齐，否则简报无人可推
            if not existing.lawyer_userid:
                existing.lawyer_userid = self._bot_notify_userid()
            self.store.upsert_group(existing)
            return
        self.store.upsert_group(
            GroupProfile(
                group_id=group_id,
                name=(
                    f"助手单聊 · {env.sender_name or env.sender_id}"
                    if env.is_single
                    else f"客户群 · {group_id[-6:]}"
                ),
                # 群聊助手同样先按「新咨询」处理；成交客户的服务群由人工在控制台改状态
                client_status=ClientStatus.PROSPECT,
                case_type=s.kf_default_case_type,
                lawyer_name=s.kf_default_lawyer_name,
                lawyer_userid=self._bot_notify_userid(),
                ai_enabled=s.bot_enabled,
                bot_webhook=env.webhook_url,
                bot_webhook_at=datetime.now() if env.webhook_url else None,
            )
        )

    def _bot_notify_userid(self) -> str:
        s = self.pipeline.settings
        return s.bot_default_notify_userid or s.default_notify_userid

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
        """客服会话首次出现时自动建档——员工零操作即可让 AI 上岗。

        提醒接收人取该客服账号在企微后台配置的接待人（首位），保证
        「已通知律师」这句承诺真的兑现；取不到时回落全局兜底配置。
        """
        s = self.pipeline.settings
        existing = self.store.get_group(group_id)
        if existing is not None:
            # 旧档案（建档时还没有接待人查询能力，或当时后台尚未配置接待人）
            # 会导致简报无人可推——在这里补齐，否则线索只会静静躺在库里。
            if not existing.lawyer_userid:
                servicers = self._kf_servicers(open_kfid)
                target = servicers[0] if servicers else s.default_notify_userid
                if target:
                    existing.lawyer_userid = target
                    if len(servicers) > 1 and not existing.backup_userid:
                        existing.backup_userid = servicers[1]
                    self.store.upsert_group(existing)
                    logger.info("backfilled notify target for %s → %s", group_id, target)
            return
        servicers = self._kf_servicers(open_kfid)
        self.store.upsert_group(
            GroupProfile(
                group_id=group_id,
                name=f"微信客服 · 客户{external_userid[-6:]}",
                client_status=ClientStatus.PROSPECT,  # 客服进线默认为新咨询
                case_type=s.kf_default_case_type,
                lawyer_name=s.kf_default_lawyer_name,
                lawyer_userid=servicers[0] if servicers else s.default_notify_userid,
                backup_userid=servicers[1] if len(servicers) > 1 else "",
                ai_enabled=s.kf_enabled,
                kf_open_kfid=open_kfid,
                kf_external_userid=external_userid,
            )
        )

    def _kf_servicers(self, open_kfid: str) -> list[str]:
        """接待人列表按客服账号缓存：每个新客户会话都查一次太浪费。

        只缓存非空结果——接口抖一下或后台当时还没配接待人，空列表一旦进缓存
        就到进程重启前都不再重查，之后所有新会话都无人可推。
        """
        if open_kfid in self._servicer_cache:
            return self._servicer_cache[open_kfid]
        try:
            got = self.kf_client.servicer_list(open_kfid) if self.kf_client else []
        except Exception:
            logger.exception("servicer_list failed: %s", open_kfid)
            return []
        if got:
            self._servicer_cache[open_kfid] = got
        return got

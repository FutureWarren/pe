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

from responder import kfroster, lead, memory
from responder.compliance.guard import guard
from responder.engine import signals
from responder.gateway import bot, douyin, wecom_kf
from responder.models import Action, ClientStatus, GroupProfile, IncomingMessage
from responder.notify import escalation
from responder.reply import templates


def _fmt_when(iso: str) -> str:
    """ISO 时间戳不是给人读的——告警是要人当场看懂并行动的。"""
    try:
        return datetime.fromisoformat(iso).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return iso or "（时间未知）"

logger = logging.getLogger(__name__)


@dataclass
class KfSyncJob:
    """微信客服回调只给 Token，需据此拉取真实消息（见 gateway/wecom_kf.py）。"""

    token: str
    open_kfid: str


class Worker:
    def __init__(self, pipeline, store, sender=None, poll_seconds: float = 10.0,
                 kf_client=None, douyin_client=None):
        self.pipeline = pipeline
        self.store = store
        self.sender = sender
        self.kf_client = kf_client
        self.douyin_client = douyin_client
        self.poll_seconds = poll_seconds
        self.q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._servicer_cache: dict[str, list[str]] = {}
        # 会话归属状态的查询节流（见 _ensure_robot_state）
        self._robot_state_checked: dict[str, float] = {}
        # 心跳与看门狗（见 start()）：后台线程停了必须有人知道、有人扶起来
        self._last_beat = 0.0
        self._guard: threading.Thread | None = None
        # 启动后先隔一个间隔再查更新：刚重启完不该立刻又想着升级
        self._last_update_check = datetime.now()
        # 接待人同步：启动后先跑一轮（epoch 时间戳保证第一次 tick 就到期），
        # 这样重启即自愈——不用等到下一个整点，也不用人记得去点按钮
        self._last_servicer_sync = datetime.fromtimestamp(0)

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """拉起后台线程，并派一个看门狗盯着它。

        **后台线程停了是整个系统里最致命、也最安静的一种坏**：队列照常收消息，
        只是再也没人取——所有客户从此一句回复都收不到，控制台看着一切正常，
        日志里只有最初那一条 traceback。更糟的是自动升级本身也跑在这个线程里，
        它一停，我们连远程推一版修复的通道都没了。

        `_run` 里已经把异常逐层隔离过，但那只能挡住 `Exception`。
        真正兜底的是这两样：
          · 心跳——每轮记一次时间，任何人（控制台 / 自检 / 战报）都查得到；
          · 看门狗——发现线程没了就地拉起来，不用等人发现。
        """
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._beat()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="responder-worker"
        )
        self._thread.start()
        if self._guard is None or not self._guard.is_alive():
            self._guard = threading.Thread(
                target=self._watch, daemon=True, name="responder-watchdog"
            )
            self._guard.start()

    def _beat(self) -> None:
        """记一次「我还活着」。写内存 + 落库：内存给看门狗用（快），
        库里那份给控制台和自检用（跨进程、重启后还在）。"""
        self._last_beat = time.time()
        try:
            self.store.set_note("worker_heartbeat", datetime.now().isoformat())
        except Exception:
            logger.exception("heartbeat write failed")

    def seconds_since_beat(self) -> float:
        return time.time() - self._last_beat if self._last_beat else 1e9

    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _watch(self) -> None:
        """看门狗：线程没了就地重启。

        重启是安全的——队列还在（`queue.Queue` 属于 Worker 不属于线程），
        游标、待办、提醒全在库里，最坏是一条消息重复处理一次，而 `save_message`
        按 msg_id 幂等。**重复一句远比永远静默便宜。**
        """
        while not self._stop.is_set():
            self._stop.wait(5.0)
            if self._stop.is_set():
                return
            if self._thread is not None and not self._thread.is_alive():
                logger.error("worker thread died — 看门狗重新拉起")
                try:
                    self.store.set_note(
                        "worker_restarted",
                        f"{datetime.now().isoformat()} 后台线程意外退出，已由看门狗重启",
                    )
                except Exception:
                    logger.exception("restart note failed")
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
        elif isinstance(item, douyin.DouyinEnvelope):
            self.process_douyin(item)
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
        # 逐个隔离：这些定时事务彼此无关，一个炸了不该把后面的一起带走。
        # 尤其自动升级排在后面——它挂掉意味着**服务器再也拉不到新版本**，
        # 而那正是我们唯一能远程修东西的通道。
        for sweep in (
            self._sweep_idle_leads, self._sweep_customer_memory,
            self._sweep_lead_sla, self._sweep_winback,
            self._sweep_channel_health, self._sweep_servicers,
            self._maybe_auto_update,
            self._maybe_daily_digest,
        ):
            try:
                sweep(now)
            except Exception:
                logger.exception("定时事务失败: %s", sweep.__name__)

    def _sweep_servicers(self, now: datetime) -> None:
        """让企微的「接待人」名单自动跟上律师名册。

        律所侧只该维护两份名单：**律师名册**（控制台「团队」页）和**企微后台**
        「微信客服 → 升级服务」里的成员范围。企微的接待人是第三份，但它的正确内容
        永远等于第一份——所以由程序保持一致，不让它成为又一件要人记着做的事。

        触发有两种，缺一不可：
          · 名册刚改过（脏标记）——加了人立刻就得能接单，不能等下一个整点；
          · 定期兜底——有人在企微后台手工删过接待人，或上次同步正好赶上网络抖动。
            这类漂移没有任何征兆，只有下一次转接失败时才暴露。
        """
        s = self.pipeline.settings
        client = self.kf_client
        if client is None or not client.available():
            return
        if not s.kf_servicer_sync_seconds:  # 置 0 = 关掉自动同步，只留手动按钮
            return
        dirty = kfroster.is_dirty(self.store)
        due = (now - self._last_servicer_sync).total_seconds() >= max(
            60, s.kf_servicer_sync_seconds
        )
        if not dirty and not due:
            return
        self._last_servicer_sync = now
        if not self.store.list_lawyers(active_only=True):
            # 名册为空：转接功能本就未启用，别去空跑企微接口。
            # 标记要清掉——留着它会让「有事要做」这个状态永远为真，
            # 而下次真有事时就分不出是新的还是上次剩的。
            kfroster.clear_dirty(self.store)
            return
        # 先清脏标记再同步：同步中途万一有人改名册，下一轮还会再跑一次；
        # 反过来（后清）则会把那次改动一起吞掉。宁可多跑一轮。
        kfroster.clear_dirty(self.store)
        try:
            result = kfroster.sync(
                self.store, client, kfroster.accounts_in_use(self.store)
            )
        except Exception:
            logger.exception("servicer sync failed")
            kfroster.mark_dirty(self.store)  # 没跑成就还给下一轮
            return
        kfroster.record(self.store, result)
        if not result.get("ok"):
            logger.warning("servicer sync incomplete: %s", result)

    def _sweep_channel_health(self, now: datetime) -> None:
        """外部渠道的死活自查。

        这是整套多渠道方案里最容易被省掉、也最不该省的一块。RPA 跑在一台
        普通电脑上：系统弹个更新窗口、浏览器改版、被人误关——它就停了。
        **而停了是没有任何动静的**：客户在美团上问了话没人理，我们后台一片安静，
        日志里连一行错误都没有。等发现时已经过去三天。

        两个不同的故障，分开报：
          1. **话发不出去**——回复排进发件箱很久没人来取，客户正在等（分钟级）。
          2. **人没了**——整个渠道连心跳都没有（小时级）。半天没客户说话是
             正常的，半天连不上不是。

        报过一次就记下来，等那头恢复了自动清零——否则每 10 秒轰炸一次，
        很快就没人看了，而下一次真出事时也一样没人看。
        """
        s = self.pipeline.settings
        if not s.channel_enabled or self.sender is None:
            return
        target = s.default_notify_userid or s.bot_default_notify_userid
        if not target:
            return
        try:
            state = {c["channel"]: c for c in self.store.list_channel_state()}
            alarms: list[str] = []

            stuck = self.store.stale_outbound(
                now - timedelta(minutes=max(1, s.channel_stale_alert_minutes))
            )
            for row in stuck:
                ch = row["channel"] or "（未标渠道）"
                if (state.get(ch) or {}).get("alerted_at"):
                    continue
                alarms.append(
                    f"「{ch}」有 {row['n']} 条回复排队发不出去，最早一条在"
                    f" {_fmt_when(row['oldest'])}——客户正在那头等着。"
                    "多半是那台跑自动化的电脑卡住了，去看一眼。"
                )

            silent_for = timedelta(hours=max(1, s.channel_silent_alert_hours))
            for ch, row in state.items():
                if row.get("alerted_at") or not row.get("last_seen_at"):
                    continue
                if any(ch in a for a in alarms):
                    continue  # 已经因为发不出去报过了，别重复说
                try:
                    last = datetime.fromisoformat(row["last_seen_at"])
                except (TypeError, ValueError):
                    continue
                if now - last > silent_for:
                    hours = int((now - last).total_seconds() // 3600)
                    alarms.append(
                        f"「{row.get('label') or ch}」已经 {hours} 小时没有任何动静了。"
                        "这不是「今天没客户」——连心跳都断了，那头应该是停了。"
                    )

            for text in alarms:
                if self.sender.send_direct_text(target, f"【渠道告警】\n{text}") is not False:
                    ch = text.split("「", 1)[-1].split("」", 1)[0]
                    for key, row in state.items():
                        if key == ch or row.get("label") == ch:
                            self.store.mark_channel_alerted(key)
        except Exception:
            logger.exception("channel health sweep failed")

    def _maybe_daily_digest(self, now: datetime) -> None:
        """每天固定时间给管理员推一份战报。

        幂等靠 ops_commands 表里的日期键：worker 每 10 秒 tick 一次，
        少了它，到点那一小时会推 360 条。
        """
        s = self.pipeline.settings
        if not s.daily_digest_enabled or now.hour != s.daily_digest_hour:
            return
        key = f"digest-{now:%Y%m%d}"
        if self.store.command_done(key):
            return
        try:
            from responder.digest import build_digest, digest_target

            target = digest_target(self.store, s)
            if not target or self.sender is None:
                return  # 没人可发就不占用当天的幂等键，等配好了照发
            text = build_digest(self.store, s, now=now)
            if self.sender.send_direct_text(target, text) is not False:
                self.store.mark_command_done(key, "daily digest sent")
        except Exception:
            logger.exception("daily digest failed")

    def _maybe_auto_update(self, now: datetime) -> None:
        """定时看远端分支有没有新版，有就自己拉下来重启。

        「忙」的判定有两层，都是为了别在重启时把消息弄丢：
          1. 队列里还有活没干完——那些是内存态，重启即丢；
          2. 客户刚说过话——他大概率还在等回复，这会儿重启等于当面挂电话。
        升级晚一轮（默认五分钟）没有任何代价，掉一条客户消息有。
        """
        s = self.pipeline.settings
        interval = max(60, s.auto_update_interval_seconds)
        if (now - self._last_update_check).total_seconds() < interval:
            return
        self._last_update_check = now
        busy = self.qsize() > 0
        if not busy and s.auto_update_quiet_seconds > 0:
            last = self.store.last_message_at()
            busy = bool(
                last and (now - last).total_seconds() < s.auto_update_quiet_seconds
            )
        try:
            from responder import ops

            ops.auto_update_tick(s, busy=busy)
        except Exception:
            logger.exception("auto update check failed")
        self._run_ops_commands()

    def _run_ops_commands(self) -> None:
        """执行仓库里待办的运维指令（见 responder/opscmd.py）。

        跟自动升级同一轮跑：那时候仓库刚好是新拉下来的，指令文件也是最新的。
        放在升级之后而不是之前，是因为指令往往依赖同一批提交里的新代码。
        """
        try:
            from responder.opscmd import Runner

            Runner(
                self.pipeline.settings, self.store,
                sender=self.sender, kf_client=self.kf_client,
            ).run_pending()
        except Exception:
            logger.exception("ops commands failed")

    def _sweep_winback(self, now: datetime) -> None:
        """挽留：会话静默且仍未留联系方式 → 补发一条，一通对话只发一次。

        业务决策 2026-08：回完消息就没有下一步是转化上最贵的沉默。
        抖音「自动挽留」官方数据留资率 +7.4%，微信侧此前完全没有这一环——
        聊完没留电话的人就这么走了，连一次挽回都没有。

        只对一对一会话（客服/抖音私信）做。群聊里律师本人在场，
        AI 单方面追着客户要电话既越界又尴尬。
        """
        s = self.pipeline.settings
        if not s.winback_enabled or s.winback_idle_seconds <= 0:
            return
        until = now - timedelta(seconds=s.winback_idle_seconds)
        # 与线索补位同样的回看窗口：停机期间安静下来的会话不能被永久跳过
        since = now - timedelta(seconds=max(s.winback_idle_seconds * 4, 86400))
        for gid in self.store.idle_conversations(since, until):
            try:
                self._winback_one(gid)
            except Exception:
                logger.exception("winback failed: %s", gid)

    def _winback_one(self, group_id: str) -> None:
        s = self.pipeline.settings
        if self.store.has_reply_category(group_id, "winback"):
            return  # 只挽留一次，第二次就成了骚扰
        group = self.store.get_group(group_id)
        if group is None or not group.ai_enabled or not group.is_kf:
            return
        if group.client_status != ClientStatus.PROSPECT:
            return
        # 已经有人接手了就别插话。今天「客服回一句就算接管」上线之后，
        # 这条从罕见变成常见：客服接过去、聊了两句、客户暂时没回，
        # 而挽留正好在这时候踩着人家的对话发出去——客户会看到两个「人」
        # 一前一后说话，客服则完全不知道系统替他说了什么。
        if group.handoff_userid:
            return
        convo = self.store.recent_messages(group_id, s.lead_history_window)
        if signals.scan(convo)[1]:
            return  # 已经留了联系方式，没什么好挽的
        # 说过话的人已经把情况讲清楚了，直接收口；一句没说的还卡在「不知道怎么开口」
        spoke = any(
            not m.get("sender_is_staff")
            and m.get("msg_type") != "event"
            and (m.get("content") or "").strip()
            for m in convo
        )
        if not self.pipeline._can_send(group):
            return
        marker = f"winback-{group_id}"
        text = templates.winback(group, spoke, seed=marker, settings=s)
        result = guard(text, Action.ANSWER, templates.safe_fallback(group))
        sent, parts = self.pipeline._send_group(group, group_id, result.text)
        self.store.save_reply(
            marker, group_id, result.text, "live" if sent else "failed",
            result.passed, category="winback", parts=parts,
        )
        logger.info("winback %s: %s", "sent" if sent else "failed", group_id)

    def _sweep_idle_leads(self, now: datetime) -> None:
        """对话安静下来后补一份线索简报——聊完没留电话的咨询同样有跟进价值。

        这也是**冷线索的推送时机**（`notify_all_leads`，业务决策 2026-08）。
        为什么等到静默而不是当场推：客户刚说一句「在吗」就给客服推一张交接单，
        三十秒后他把案情讲完了又得推第二张。等安静下来再推，一通对话一张单，
        而且那张单里已经有完整经过。
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
            # **异常绝不能逃出这个循环。** 逃出去线程就死了，而它是死得
            # 无声无息的：队列照常收消息，只是再也没人取——所有客户从此
            # 一句回复都收不到，控制台看着一切正常，日志里只有最初那一条
            # traceback。这是整个系统里爆炸半径最大的一处。
            if item is not None:
                try:
                    self._dispatch(item)
                except Exception:
                    logger.exception("worker dispatch failed, 继续下一条: %r", item)
            if time.time() - last_tick >= self.poll_seconds:
                last_tick = time.time()
                # 心跳在 tick **之前**打：tick 里任何一环卡住（网络挂起、
                # 数据库锁），线程其实还活着，但外面看是「没反应」。
                # 先记下来，才分得清「线程死了」和「线程被某件事卡住了」。
                self._beat()
                try:
                    self.tick()
                except Exception:
                    logger.exception("worker tick failed, 继续下一轮")

    def _process_new(self, msg: IncomingMessage) -> None:
        try:
            if not self.store.save_message(msg):
                logger.info("duplicate callback ignored: %s", msg.msg_id)
                return
            self.pipeline.handle(msg)
        except Exception:
            logger.exception("message processing failed: %s", msg.msg_id)

    def _sweep_customer_memory(self, now: datetime) -> None:
        """对话安静下来后，把这通咨询沉淀成客户记忆（见 responder/memory.py）。

        为什么在这里而不是每条消息都算：记忆是给**下一次**用的，
        对话进行中反复重算既无意义又白烧 CPU；而且聊到一半的事实
        往往还会被后面几句改写。

        每次都覆盖重算，不做增量：记忆全部由已入库事实拼装，
        重算的结果是确定的，反而比累积追加更不容易长歪。
        """
        s = self.pipeline.settings
        if not s.knowledge_enabled:
            return  # 与知识库共用一个开关：都属「长期记忆」
        until = now - timedelta(seconds=s.lead_idle_seconds)
        since = now - timedelta(seconds=max(s.lead_idle_seconds * 4, 86400))
        for gid in self.store.idle_conversations(since, until):
            group = self.store.get_group(gid)
            if group is None:
                continue
            try:
                text = memory.build_customer_memory(self.store, group, now=now)
                if text and text != group.memory:
                    self.store.set_memory(gid, text)
            except Exception:
                logger.exception("customer memory sweep failed: %s", gid)

    def _sweep_lead_sla(self, now: datetime) -> None:
        """线索超时未联系 → 追加提醒并抄送第二责任人。

        「一小时内联系」是分层规则对律师的要求（docs/lead-routing.md），没有督办
        它就只是一句口号。每单只追一次（sla_nudged），避免变成骚扰。

        P0 与 P1 都扫，只是时限差一个量级。**P1 一度完全没有督办**——
        单子推出去之后律师不跟，就再没有任何机制会提起它。而 P1 是
        「有意愿但还没留电话」，恰恰最需要有人推一把：它不该占用律师的
        即时注意力（那是 P0 的特权），但放着不管就是白丢。
        """
        s = self.pipeline.settings
        if not (s.lead_brief_enabled and s.lead_sla_enabled and self.pipeline.sender):
            return
        self._nudge_overdue("P0", s.lead_p0_sla_seconds, now, "强意愿线索")
        self._nudge_overdue("P1", s.lead_p1_sla_seconds, now, "有意愿线索")

    def _nudge_overdue(
        self, priority: str, sla_seconds: int, now: datetime, label: str
    ) -> None:
        s = self.pipeline.settings
        sender = self.pipeline.sender
        if sla_seconds <= 0:
            return  # 该档督办被关掉
        cutoff = now - timedelta(seconds=sla_seconds)
        for row in self.store.overdue_leads(priority, cutoff):
            group = self.store.get_group(row["group_id"])
            if group is None:
                continue
            to = row.get("assigned_userid") or group.lawyer_userid or s.default_notify_userid
            if not to:
                continue
            # 超过一小时就用小时说话：「已超 1440 分钟」没人算得过来
            mins = int(sla_seconds // 60)
            waited = f"{mins} 分钟" if mins < 120 else f"{mins // 60} 小时"
            text = (
                f"【督办】{label}已超 {waited} 未标记联系\n"
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
            # 拉回来几条。回调来了但这个数一直是 0 = 游标卡住或 Token 过期，
            # 跟「回调没来」是两码事，修法也完全不同。
            self.store.bump("kf_synced", len(batch["msg_list"]))
            if batch["msg_list"]:
                first = batch["msg_list"][0]
                self.store.set_note(
                    "kf_synced_last",
                    f"origin={first.get('origin')} type={first.get('msgtype')}"
                    f" event={(first.get('event') or {}).get('event_type', '-')}",
                )
            for raw in batch["msg_list"]:
                try:
                    self._handle_kf_message(raw)
                except Exception:
                    # 这条炸了，而**游标照常往前走**——它不会再来第二次。
                    # 客户那头的表现是：发了一句话，然后什么也没有。
                    # 日志里有一行异常，但没有人会去看一个「运行正常」的系统的日志。
                    # 所以这里必须做两件事：给客户一句兜底，给我们一条看得见的记录。
                    logger.exception("kf message failed: %s", raw.get("msgid"))
                    self._rescue_failed_kf_message(raw)
            cursor = batch["next_cursor"]
            if cursor:
                self.store.set_kf_cursor(job.open_kfid, cursor)
            if not batch["has_more"]:
                break

    def _rescue_failed_kf_message(self, raw: dict) -> None:
        """一条客户消息把判断链跑炸了之后的兜底。

        不重试（同一条大概率还会炸），但**不能让客户对着空气**：
        发一句确定性的承接话术，并把这一条记进运维小记，让它在控制台里看得见。
        救援本身再炸也只吞掉——它已经是最后一道了。
        """
        try:
            if (raw.get("origin") or 0) != wecom_kf.ORIGIN_CUSTOMER:
                return
            open_kfid = raw.get("open_kfid", "")
            external_userid = raw.get("external_userid", "")
            if not (open_kfid and external_userid):
                return
            group_id = f"kf:{open_kfid}:{external_userid}"
            self.store.set_note(
                f"pipeline_failed:{group_id}",
                f"消息 {raw.get('msgid', '')} 处理时出错，已发兜底话术。"
                "这条不会自动重来，请人工看一眼这通对话。",
            )
            client = self.pipeline.kf_client
            if client is None:
                return
            group = self.store.get_group(group_id)
            if group is None or not group.ai_enabled:
                return
            text = guard(
                templates.safe_fallback(group), Action.HANDOFF,
                templates.safe_fallback(group),
            ).text
            if client.send_text(open_kfid, external_userid, text):
                self.store.save_reply(
                    f"rescue-{raw.get('msgid', '')}", group_id, text, "live", True,
                    category="other",
                )
        except Exception:
            logger.exception("kf rescue failed: %s", raw.get("msgid"))

    def _handle_kf_message(self, raw: dict) -> None:
        origin = raw.get("origin")
        open_kfid = raw.get("open_kfid", "")
        external_userid = raw.get("external_userid", "")
        if not open_kfid or not external_userid:
            return
        # 一个「客服账号 × 客户」= 一个会话档案，复用群档案的全部能力（开关/律师/留痕）
        group_id = f"kf:{open_kfid}:{external_userid}"

        # 客户扫码进入会话：主动打招呼，不等他先开口。
        # 空窗口是最大的流失点——客户点进来看到一片空白，很多人当场就退了。
        if raw.get("msgtype") == "event":
            event = (raw.get("event") or {}).get("event_type", "")
            if event in wecom_kf.ENTER_EVENTS:
                self._ensure_kf_profile(group_id, open_kfid, external_userid)
                self._kf_welcome(group_id, open_kfid, external_userid,
                                 raw.get("msgid") or "")
            else:
                # 事件名不在白名单里 = 进线问候整条不触发，而客户看到的是空窗口。
                # 白名单是照着文档写的，企微换个名字我们就哑了——留个证据。
                self.store.set_note("kf_unknown_event", event or "(空)")
            return
        if origin == wecom_kf.ORIGIN_SYSTEM:
            return  # 系统推送（企微自带欢迎语等）不进判断
        self._ensure_kf_profile(group_id, open_kfid, external_userid)
        if origin == wecom_kf.ORIGIN_CUSTOMER:
            self._ensure_robot_state(group_id, open_kfid, external_userid)

        # **origin 说了算。** 原来是
        # `origin == ORIGIN_SERVICER or bool(raw.get("servicer_userid"))`，
        # 而企微在**客户消息**上也会带 `servicer_userid`（标明这通会话归谁接）——
        # 于是客户自己说的话被记成了「我方发言」，`handle()` 走进 staff 分支：
        # 就地标记为已转人工、AI 从此闭嘴、这条消息一个字的回复也不会有。
        # 症状正是真机看到的那一幕：客户连发五次「你好」，跨两天，全程静默，
        # 而后台判断日志里每一条都写着「staff-message，不需要 AI 回」。
        #
        # origin 是企微专门用来回答这个问题的字段，缺失时才回落到旧启发式。
        if origin == wecom_kf.ORIGIN_SERVICER:
            is_staff = True
        elif origin == wecom_kf.ORIGIN_CUSTOMER:
            is_staff = False
        else:
            is_staff = bool(raw.get("servicer_userid"))
        content = (raw.get("text") or {}).get("content", "")
        msg = IncomingMessage(
            msg_id=raw.get("msgid") or "",
            group_id=group_id,
            # 发件人同理：客户消息的发件人永远是客户，哪怕报文里带着接待人 id
            sender_id=(raw.get("servicer_userid") or external_userid) if is_staff
            else external_userid,
            sender_is_staff=is_staff,
            content=content,
            msg_type="text" if raw.get("msgtype") == "text" else (raw.get("msgtype") or "other"),
        )
        if not msg.msg_id:
            return
        if not self.store.save_message(msg):
            return  # 重复投递
        self.pipeline.handle(msg)

    def _ensure_robot_state(
        self, group_id: str, open_kfid: str, external_userid: str
    ) -> None:
        """确保这通会话在企微那边归「智能助手」接待。

        **这是长期缺失的一环，也是「转过一次人工之后 AI 就永远不说话了」的真因。**
        微信客服的会话有归属状态：0 未处理 / 1 智能助手 / 2 待接入 / 3 人工 / 4 已结束。
        我们一直只会把它转给人工（state 3），从来没有要回来过。于是客户被转过一次、
        或者会话被企微判成「已结束」之后，新消息进来是「未处理」——
        **未处理的会话没有任何人在接，客户发什么都石沉大海**。
        而我们这边判断照常跑、回复照常入库，日志里一切正常。

        只在「我们这边认为该由 AI 接」时才要回来：已转人工且人还在跟的不碰，
        否则会把正在聊的律师踢开。

        节流：同一通会话十分钟内只查一次。这是每条客户消息都会走的路径，
        不能每次都打两个企微接口。
        """
        if not self.kf_client or not self.kf_client.available():
            return
        group = self.store.get_group(group_id)
        if group is None or not group.ai_enabled:
            return
        if group.handoff_userid and self.pipeline._being_handled(group):
            return  # 人正在跟，别把他踢开
        now = time.time()
        if now - self._robot_state_checked.get(group_id, 0.0) < 600:
            return
        self._robot_state_checked[group_id] = now
        getter = getattr(self.kf_client, "service_state", None)
        if getter is None:
            return  # 老版本客户端/测试桩没有这个能力，跳过而不是报错
        try:
            state = getter(open_kfid, external_userid)
        except Exception:
            logger.exception("service_state get failed: %s", group_id)
            return
        if state is None or state == wecom_kf.STATE_ROBOT:
            return
        if self.kf_client.to_robot(open_kfid, external_userid):
            logger.info("kf session claimed back for AI: %s (was state=%s)",
                        group_id, state)

    def _kf_welcome(
        self, group_id: str, open_kfid: str, external_userid: str, msg_id: str
    ) -> None:
        """客户进入客服会话时的主动问候。

        只在「这个会话此前没说过话」时发：老客户第二次点进来再被介绍一遍
        律所全称会很怪，而且他上一轮的上下文还在。
        """
        s = self.pipeline.settings
        if not s.kf_welcome_on_enter:
            return
        # 幂等：企微可能重复推同一个事件，msg_id 入库即去重
        marker = f"kf-enter-{msg_id}" if msg_id else f"kf-enter-{group_id}"
        if not self.store.save_message(IncomingMessage(
            msg_id=marker, group_id=group_id, sender_id=external_userid,
            sender_is_staff=False, content="", msg_type="event",
        )):
            return
        group = self.store.get_group(group_id)
        if group is None or not group.ai_enabled:
            return
        client = self.pipeline.kf_client  # 受模式门控：影子模式不外发
        if client is None:
            return
        # 已有真实对话（事件占位不算）→ 是回访：不再自我介绍一遍，
        # 但也不能一声不吭。老客户点进来对着空窗口，跟新客户一样会走。
        history = self.store.recent_messages(group_id, 50)
        returning = any(m.get("msg_type") != "event" for m in history)
        if returning:
            # 刚聊完又点回来的不用打招呼——那不是回访，是同一次对话
            last = self.store.last_message_at_in(group_id)
            gap = (datetime.now() - last).total_seconds() if last else 1e9
            if gap < s.lead_session_gap_seconds:
                return
        text = (
            templates.greeting_again(group, seed=marker)
            if returning
            else templates.greeting_opener(group, seed=marker)
        )
        result = guard(text, Action.ANSWER, templates.safe_fallback(group))
        if not client.send_text(open_kfid, external_userid, result.text):
            # **没发出去就绝不能记账。** 记了 `has_greeting` 就为真，
            # `_avoid_repeat_greeting` 从此认定「打过招呼了」，于是客户扫码进来
            # 看到一片空白，而系统这边显示一切正常——空窗口是最大的流失点，
            # 而这个 bug 恰好制造的就是空窗口。
            self.store.save_reply(
                marker, group_id, result.text, "failed", result.passed,
                category="greeting",
            )
            self.store.set_note(
                f"welcome_failed:{group_id}",
                "进线问候没发出去（微信客服通道异常），客户扫码进来看到的是空窗口",
            )
            logger.error("kf welcome FAILED to send: %s", group_id)
            return
        self.store.save_reply(
            marker, group_id, result.text, "live", result.passed, category="greeting",
        )
        logger.info("kf welcome sent: %s", group_id)

    # ------------------------------------------------------------ 抖音私信
    def process_douyin(self, env: douyin.DouyinEnvelope) -> None:
        """抖音私信回调：建档 → 进线问候 / 进判断管道。

        与微信客服的差异全在发送侧（配额、24 小时窗口，见 service._douyin_budget），
        收这一侧完全同构，因此直接复用同一条判断与话术管道。
        """
        s = self.pipeline.settings
        if not s.douyin_enabled or not env.open_id:
            return
        group_id = env.group_id
        self._ensure_douyin_profile(group_id, env)
        if env.is_enter:
            self._douyin_welcome(group_id, env)
            return
        if env.msg is None:
            return
        if not self.store.save_message(env.msg):
            return  # 重复投递
        self.pipeline.handle(env.msg)

    def _ensure_douyin_profile(self, group_id: str, env: douyin.DouyinEnvelope) -> None:
        """抖音会话首次出现时自动建档。

        抖音侧查不到「接待人」（那是微信客服才有的概念），提醒接收人只能取配置：
        专用项 → 全局兜底。取不到就没人收线索简报，等于白接一条通道。
        """
        s = self.pipeline.settings
        if self.store.get_group(group_id) is not None:
            return
        who = env.nickname or f"用户{env.open_id[-6:]}"
        self.store.upsert_group(
            GroupProfile(
                group_id=group_id,
                name=f"抖音私信 · {who}",
                client_status=ClientStatus.PROSPECT,  # 私信进线一律按新咨询
                case_type=s.kf_default_case_type,
                lawyer_name=s.kf_default_lawyer_name,
                lawyer_userid=s.douyin_default_notify_userid or s.default_notify_userid,
                ai_enabled=s.douyin_enabled,
                douyin_open_id=env.open_id,
            )
        )

    def _douyin_welcome(self, group_id: str, env: douyin.DouyinEnvelope) -> None:
        """客户点进私信会话页时的主动问候。

        平台要求收到该事件后 30 秒内响应，所以这里走确定性模板、绝不进模型——
        LLM 一次要十几秒，赶上超时这条通道的问候能力就等于没有。

        ⚠️ 平台只允许「回复」：客户还没开过口时，发送接口会拒。因此这里发出去
        与否取决于抖音侧对进会话事件的放行规则，失败按预期处理、不视为故障。
        """
        s = self.pipeline.settings
        if not s.douyin_welcome_on_enter:
            return
        # 幂等：同一会话只问候一次，事件重复推送不刷屏
        marker = f"dy-enter-{env.open_id}-{env.conversation_short_id or 'x'}"
        if not self.store.save_message(IncomingMessage(
            msg_id=marker, group_id=group_id, sender_id=env.open_id,
            sender_is_staff=False, content="", msg_type="event",
        )):
            return
        if any(m.get("msg_type") != "event"
               for m in self.store.recent_messages(group_id, 50)):
            return  # 老客户回访，上下文还在，不再自我介绍
        if self.store.has_greeting(group_id):
            return
        group = self.store.get_group(group_id)
        if group is None or not group.ai_enabled:
            return
        client = self.pipeline.douyin_client  # 受模式门控：影子模式不外发
        if client is None:
            return
        text = templates.greeting_opener(group, seed=marker)
        result = guard(text, Action.ANSWER, templates.safe_fallback(group))
        ok = client.send_text(env.open_id, result.text)
        self.store.save_reply(
            marker, group_id, result.text, "live" if ok else "failed",
            result.passed, category="greeting", parts=1 if ok else 0,
        )
        logger.info("douyin welcome %s: %s", "sent" if ok else "rejected", group_id)

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

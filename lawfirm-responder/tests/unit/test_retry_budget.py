"""三处「无限重试」的止血，以及「出事有人知道」这条链（2026-08-12 体检）。

三处的共同点：**失败没有代价上限，而重试本身反过来把系统弄坏了。**

- 加急提醒：收件人不在应用可见范围里 → 每 10 秒重试上百次，
  占满处理所有客户消息的那唯一一条线程，同时把企微打到限流，
  之后交接单、督办、战报、告警全部一起静默失效——系统亲手拆掉告警链。
- 静默挽留：判重看的是「有没有实发过」，发失败不算实发 → 每 10 秒重发、
  连打 24 小时（约 8000 次），还把抖音那 6 条的发送配额算光。
- 自动升级：推一个起不来的版本 → 每 5 分钟重启两次、永远出不来，全程零告警。

止血之外还有一条同样要紧：**放弃的时候必须留下看得见的痕迹。**
静默地放弃是最坏的结果——系统自认为处理完了，而那件事根本没发生。
"""

from datetime import datetime, timedelta

from responder import ops, retry
from responder.config import Settings
from responder.models import GroupProfile, Reminder
from responder.notify import escalation
from responder.store.db import Store


class DeadSender:
    """企微一直拒收（收件人不在可见范围里 / 账号已停用）。"""

    def __init__(self):
        self.calls = 0

    def send_direct_text(self, userid, text):
        self.calls += 1
        return False


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "r.db"))


# ------------------------------------------------------------ 重试预算本身
def test_retry_backs_off_and_eventually_stops(tmp_path):
    store = _store(tmp_path)
    assert retry.should_try(store, "k", "1") is True
    retry.record_failure(store, "k", "1")
    # 刚失败过：还没到下次时间
    assert retry.should_try(store, "k", "1") is False
    # 一小时后可以再试
    later = datetime.now() + timedelta(hours=1)
    assert retry.should_try(store, "k", "1", now=later) is True
    for _ in range(5):
        retry.record_failure(store, "k", "1")
    assert retry.should_try(store, "k", "1", now=later) is False
    assert retry.exhausted(store, "k", "1") is True


def test_success_resets_the_budget(tmp_path):
    """成了就清零——不清的话下次真出问题时，重试次数已经用完了。"""
    store = _store(tmp_path)
    for _ in range(3):
        retry.record_failure(store, "k", "1")
    retry.succeeded(store, "k", "1")
    assert retry.should_try(store, "k", "1") is True


def test_giving_up_leaves_something_a_human_can_read(tmp_path):
    store = _store(tmp_path)
    retry.give_up(store, "escalation", "7", "加急提醒升级不出去，去企微后台确认可见范围")
    note = store.get_note("gaveup:escalation:7")
    assert "加急提醒" in note and "企微后台" in note


# ------------------------------------------------------------ 加急提醒升级
def test_escalation_stops_hammering_a_dead_recipient(tmp_path):
    """**这条是三处里最危险的。** 它占的是处理所有客户消息的那一条线程。"""
    store = _store(tmp_path)
    settings = Settings(db_path=store.path, default_notify_userid="gone",
                        escalation_seconds=0)
    store.upsert_group(GroupProfile(group_id="g1", backup_userid="gone"))
    store.save_reminder(Reminder(
        group_id="g1", msg_id="m1", to_userid="wei", urgent=True,
        summary="客户弟弟被刑拘",
    ))
    snd = DeadSender()

    # 一整天：每 10 秒一轮，旧版本会打出上千次
    now = datetime.now() + timedelta(seconds=10)
    for i in range(300):
        escalation.escalate_overdue(
            store, snd, settings=settings, now=now + timedelta(seconds=i * 10)
        )

    assert snd.calls <= retry.MAX_ATTEMPTS, (
        f"发了 {snd.calls} 次——这个循环会把企微打到限流，"
        f"之后交接单、督办、战报、告警全部一起失效"
    )
    assert store.get_note("gaveup:escalation:1"), "放弃了就得有人看得见"


def test_escalation_still_works_when_the_recipient_is_reachable(tmp_path):
    """止血不能误伤正路：能送到的照常送，而且不留放弃记录。"""
    store = _store(tmp_path)
    settings = Settings(db_path=store.path, default_notify_userid="ok",
                        escalation_seconds=0)
    store.upsert_group(GroupProfile(group_id="g1", backup_userid="ok"))
    store.save_reminder(Reminder(
        group_id="g1", msg_id="m1", to_userid="wei", urgent=True,
        summary="客户弟弟被刑拘",
    ))

    class Live:
        def __init__(self):
            self.sent = []

        def send_direct_text(self, userid, text):
            self.sent.append((userid, text))
            return True

    snd = Live()
    assert escalation.escalate_overdue(
        store, snd, settings=settings, now=datetime.now() + timedelta(seconds=10)
    )
    assert snd.sent and snd.sent[0][0] == "ok"
    assert not store.get_note("gaveup:escalation:1")


# ------------------------------------------------------------ 自动升级
def test_a_version_that_would_not_start_is_not_replayed(tmp_path, monkeypatch):
    """回滚做的正是 `git reset --hard $PREV`，于是 local != remote 立刻又成立——
    不拉黑的话，服务器每 5 分钟重启两次，而且永远出不来。"""
    store = _store(tmp_path)
    settings = Settings(db_path=store.path)
    monkeypatch.setattr(ops, "current_commit", lambda d: "old1234")
    monkeypatch.setattr(ops, "remote_commit", lambda s: "bad5678")
    started = []
    monkeypatch.setattr(ops, "start_update", lambda s: started.append(1) or {"ok": True})

    ops.auto_update_tick(settings, store=store)
    assert started == [1], "第一次当然要试"

    ops.mark_update_failed(store, "bad5678")
    ops.auto_update_tick(settings, store=store)
    ops.auto_update_tick(settings, store=store)
    assert started == [1], "拉黑之后不许再试同一个提交"
    assert "bad5678"[:8] in store.get_note("update_blocked")


def test_pushing_a_new_commit_resumes_updates(tmp_path, monkeypatch):
    """拉黑的是那**一个**提交，不是自动升级本身——修好推一版就该恢复。"""
    store = _store(tmp_path)
    settings = Settings(db_path=store.path)
    ops.mark_update_failed(store, "bad5678")
    monkeypatch.setattr(ops, "current_commit", lambda d: "old1234")
    monkeypatch.setattr(ops, "remote_commit", lambda s: "good999")
    started = []
    monkeypatch.setattr(ops, "start_update", lambda s: started.append(1) or {"ok": True})

    ops.auto_update_tick(settings, store=store)
    assert started == [1]


def test_manual_update_clears_the_blacklist(tmp_path):
    """人工点按钮＝「我知道上次没起来，再试一次」。"""
    store = _store(tmp_path)
    ops.mark_update_failed(store, "bad5678")
    ops.clear_update_failures(store)
    assert not store.get_note("update_blocked")
    assert ops._failed_before(store, "bad5678") is False


# ------------------------------------------------------------ 战报自检
def test_the_daily_digest_reports_a_stalled_worker(tmp_path):
    """律所没有 SSH、也不会去翻日志。战报是唯一一条推到眼前的通道。"""
    from responder import digest

    store = _store(tmp_path)
    settings = Settings(db_path=store.path)
    store.set_note("worker_heartbeat", (datetime.now() - timedelta(hours=3)).isoformat())

    text = digest.build_digest(store, settings)

    assert "系统自检" in text
    assert "后台处理线程" in text and "没动静" in text


def test_a_healthy_system_says_one_line_and_shuts_up(tmp_path):
    """每天都在喊的告警等于没有告警。"""
    from responder import digest

    store = _store(tmp_path)
    settings = Settings(db_path=store.path)
    store.set_note("worker_heartbeat", datetime.now().isoformat())

    text = digest.build_digest(store, settings)

    assert "系统自检：正常" in text
    assert "❌" not in text


def test_things_we_gave_up_on_show_up_in_the_digest(tmp_path):
    from responder import digest

    store = _store(tmp_path)
    settings = Settings(db_path=store.path)
    store.set_note("worker_heartbeat", datetime.now().isoformat())
    retry.give_up(store, "escalation", "7", "加急提醒升级不出去（收件人 gone）")

    assert "加急提醒升级不出去" in digest.build_digest(store, settings)

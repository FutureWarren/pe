"""自动升级：服务器自己发现新版就拉下来重启。

存在的理由很实在——运维侧不一定够得着这台服务器（网络策略/没有 SSH），
但服务器自己够得着 GitHub。

本文件重点测「什么时候**不**升级」：重启会丢掉内存队列里没处理完的消息，
升级晚五分钟没代价，掉一条客户消息有。
"""

from datetime import datetime, timedelta

from responder import ops
from responder.config import Settings
from responder.models import IncomingMessage
from responder.service import Pipeline
from responder.store.db import Store
from responder.worker import Worker


def make_env(tmp_path, **over):
    db = str(tmp_path / "au.db")
    store = Store(db)
    cfg = dict(mode="shadow", db_path=db, auto_update_interval_seconds=60,
               auto_update_quiet_seconds=120)
    cfg.update(over)
    settings = Settings(**cfg)
    worker = Worker(Pipeline(store, None, settings), store, None)
    return store, settings, worker


class Spy:
    """记录 auto_update_tick 收到的 busy 判定，不真的动 git。"""

    def __init__(self):
        self.calls = []

    def __call__(self, settings, *, busy=False, store=None):
        self.calls.append(busy)
        return {"checked": not busy}


def _arm(worker, seconds=600):
    """把上次检查时间推早，让下一次 tick 一定会检查。"""
    worker._last_update_check = datetime.now() - timedelta(seconds=seconds)


def test_checks_when_idle(tmp_path, monkeypatch):
    _, _, worker = make_env(tmp_path)
    spy = Spy()
    monkeypatch.setattr(ops, "auto_update_tick", spy)
    _arm(worker)
    worker.tick()
    assert spy.calls == [False], "空闲时应该检查更新，且不算忙"


def test_skips_while_queue_has_work(tmp_path, monkeypatch):
    """队列里还有活：那些是内存态，重启即丢。"""
    _, _, worker = make_env(tmp_path)
    spy = Spy()
    monkeypatch.setattr(ops, "auto_update_tick", spy)
    worker.q.put(IncomingMessage(msg_id="q1", group_id="g1", sender_id="u1",
                                 content="在吗"))
    _arm(worker)
    worker.tick()
    assert spy.calls == [True], "队列非空必须判定为忙"


def test_skips_right_after_a_customer_message(tmp_path, monkeypatch):
    """客户刚说过话，他大概率还在等回复——这会儿重启等于当面挂电话。"""
    store, _, worker = make_env(tmp_path)
    spy = Spy()
    monkeypatch.setattr(ops, "auto_update_tick", spy)
    store.save_message(IncomingMessage(msg_id="m1", group_id="g1", sender_id="u1",
                                       content="公司拖欠我工资"))
    _arm(worker)
    worker.tick()
    assert spy.calls == [True]


def test_not_busy_once_conversation_went_quiet(tmp_path, monkeypatch):
    store, _, worker = make_env(tmp_path)
    spy = Spy()
    monkeypatch.setattr(ops, "auto_update_tick", spy)
    store.save_message(IncomingMessage(
        msg_id="m1", group_id="g1", sender_id="u1", content="公司拖欠我工资",
        created_at=datetime.now() - timedelta(seconds=600)))
    _arm(worker)
    worker.tick()
    assert spy.calls == [False]


def test_throttled_between_checks(tmp_path, monkeypatch):
    """间隔没到不重复查，别把 git fetch 打成每 10 秒一次。"""
    _, _, worker = make_env(tmp_path)
    spy = Spy()
    monkeypatch.setattr(ops, "auto_update_tick", spy)
    _arm(worker)
    worker.tick()
    worker.tick()
    assert len(spy.calls) == 1


def test_tick_survives_update_errors(tmp_path, monkeypatch):
    """升级检查炸了不能带崩定时事务（提醒升级、挽留都在同一轮里）。"""
    _, _, worker = make_env(tmp_path)

    def boom(*a, **kw):
        raise RuntimeError("git 挂了")

    monkeypatch.setattr(ops, "auto_update_tick", boom)
    _arm(worker)
    worker.tick()  # 不应抛出


def test_disabled_switch_stops_everything(tmp_path, monkeypatch):
    """关掉开关就完全不动 git（回到人工点按钮）。"""
    _, settings, _ = make_env(tmp_path, auto_update_enabled=False)
    called = []
    monkeypatch.setattr(ops, "remote_commit", lambda s: called.append(1) or "")
    out = ops.auto_update_tick(settings)
    assert out["checked"] is False and not called


def test_no_update_when_commit_unchanged(tmp_path, monkeypatch):
    """远端没有新提交就什么都不做——避免无谓重启。"""
    _, settings, _ = make_env(tmp_path)
    monkeypatch.setattr(ops, "current_commit", lambda d: "abc1234")
    monkeypatch.setattr(ops, "remote_commit", lambda s: "abc1234")
    started = []
    monkeypatch.setattr(ops, "start_update", lambda s: started.append(1))
    out = ops.auto_update_tick(settings)
    assert out["updated"] is False and not started


def test_updates_when_remote_moved_ahead(tmp_path, monkeypatch):
    _, settings, _ = make_env(tmp_path)
    monkeypatch.setattr(ops, "current_commit", lambda d: "abc1234")
    monkeypatch.setattr(ops, "remote_commit", lambda s: "def5678")
    monkeypatch.setattr(ops, "start_update", lambda s: {"ok": True})
    out = ops.auto_update_tick(settings)
    assert out["updated"] is True and out["from"] == "abc1234" and out["to"] == "def5678"


def test_fetch_failure_is_treated_as_no_update(tmp_path, monkeypatch):
    """拉不到远端（断网/凭据失效）就当没有更新，绝不拿空值去重启。"""
    _, settings, _ = make_env(tmp_path)
    monkeypatch.setattr(ops, "current_commit", lambda d: "abc1234")
    monkeypatch.setattr(ops, "remote_commit", lambda s: "")
    started = []
    monkeypatch.setattr(ops, "start_update", lambda s: started.append(1))
    out = ops.auto_update_tick(settings)
    assert out["updated"] is False and not started

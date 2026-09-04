"""后台线程的死活必须看得见、且能自己站起来。

这是全系统里**爆炸半径最大**的一处：线程一停，队列照常收消息，只是再也没人取——
所有客户从此一句回复都收不到，而控制台上每一个别的指标看着都正常。
更糟的是自动升级本身也跑在这个线程里：它一停，我们连远程推一版修复的通道都没了。

`_run` 里的逐层 try/except 只挡得住 `Exception`。真正兜底的是这里测的两样：
心跳（任何人都查得到）和看门狗（不用等人发现）。
"""

import threading
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from responder.config import Settings
from responder.console.api import router as console_router
from responder.models import ClientStatus, GroupProfile
from responder.service import Pipeline
from responder.store.db import Store
from responder.worker import Worker

GID = "kf:wk1:c1"


def _worker(tmp_path, **over):
    cfg = dict(mode="live", db_path=str(tmp_path / "w.db"), llm_provider="none",
               worker_poll_seconds=0.05)
    cfg.update(over)
    s = Settings(**cfg)
    store = Store(s.db_path)
    return store, s, Worker(Pipeline(store, None, s), store, poll_seconds=0.05)


def test_the_worker_leaves_a_heartbeat(tmp_path):
    """没有心跳，「后台停了」和「今天没人咨询」在后台长得一模一样。"""
    store, _, w = _worker(tmp_path)
    w.start()
    try:
        deadline = time.time() + 3
        while time.time() < deadline and not store.get_note("worker_heartbeat"):
            time.sleep(0.05)
        assert store.get_note("worker_heartbeat"), "每轮都该记一次「我还活着」"
        assert w.alive()
        assert w.seconds_since_beat() < 2
    finally:
        w.stop()


def test_a_dead_thread_gets_picked_back_up(tmp_path):
    """线程真的死了要有人扶起来。

    重启是安全的：队列属于 Worker 不属于线程，游标/待办/提醒都在库里，
    最坏是一条消息被重复处理一次，而入库按 msg_id 幂等。
    **重复一句远比永远静默便宜。**
    """
    store, _, w = _worker(tmp_path)
    w.start()
    try:
        first = w._thread
        assert first is not None and first.is_alive()

        # 模拟线程猝死：不走 stop()，直接让它自己退出
        w._thread = threading.Thread(target=lambda: None, name="dead")
        w._thread.start()
        w._thread.join()
        assert not w._thread.is_alive()

        deadline = time.time() + 12
        while time.time() < deadline and not w.alive():
            time.sleep(0.2)
        assert w.alive(), "看门狗应该已经把它拉起来了"
        assert "看门狗重启" in store.get_note("worker_restarted")
    finally:
        w.stop()


def test_stopping_on_purpose_does_not_trigger_a_restart(tmp_path):
    """正常停机不该被看门狗当成事故反复拉起——那样服务就关不掉了。"""
    _, _, w = _worker(tmp_path)
    w.start()
    w.stop()
    time.sleep(0.6)
    assert not w.alive()


def test_the_console_says_so_when_the_thread_is_down(tmp_path):
    """「为什么没回复」必须能直接说出这一条，而不是让人去猜。"""
    store, s, w = _worker(tmp_path, admin_token="")
    store.upsert_group(GroupProfile(
        group_id=GID, kf_open_kfid="wk1", kf_external_userid="c1",
        client_status=ClientStatus.PROSPECT,
    ))

    class Down:
        def alive(self):
            return False

        def seconds_since_beat(self):
            return 9999.0

    app = FastAPI()
    app.state.store = store
    app.state.pipeline = Pipeline(store, None, s)
    app.state.worker = Down()
    app.include_router(console_router)

    r = TestClient(app).get("/console/diagnose", params={"group_id": GID}).json()
    assert any("后台处理线程已经停了" in b for b in r["blockers"]), r


def test_the_console_distinguishes_stuck_from_dead(tmp_path):
    """线程活着但半天没跑一轮 = 卡在某个外部接口上，跟「死了」是两码事，
    修法也不同。分不清就会去重启一个其实没死的东西。"""
    store, s, _ = _worker(tmp_path, admin_token="")
    store.upsert_group(GroupProfile(
        group_id=GID, kf_open_kfid="wk1", kf_external_userid="c1",
        client_status=ClientStatus.PROSPECT,
    ))

    class Stuck:
        def alive(self):
            return True

        def seconds_since_beat(self):
            return 600.0

    app = FastAPI()
    app.state.store = store
    app.state.pipeline = Pipeline(store, None, s)
    app.state.worker = Stuck()
    app.include_router(console_router)

    blockers = TestClient(app).get(
        "/console/diagnose", params={"group_id": GID}).json()["blockers"]
    assert any("没动静" in b for b in blockers)
    assert not any("已经停了" in b for b in blockers)


# ------------------------------------------------- 升级必须能把自己撤回来
def test_the_app_actually_assembles(tmp_path, monkeypatch):
    """升级脚本靠这一句判断「新版本起不起得来」，所以这里必须常绿。

    它同时是本仓库最基础的一条烟雾测试：配置校验、数据库迁移、
    所有 import——起不来的问题全在这一步暴露。
    """
    monkeypatch.setenv("RESPONDER_DB_PATH", str(tmp_path / "smoke.db"))
    monkeypatch.setenv("RESPONDER_MODE", "shadow")
    from responder.config import get_settings

    get_settings.cache_clear()
    try:
        from responder.app import create_app

        app = create_app()
        assert app.title
    finally:
        get_settings.cache_clear()


def test_the_update_script_can_undo_itself():
    """律所侧没有 SSH，控制台和后台线程在同一个进程里。
    新版本一旦起不来：服务死 → 控制台打不开 → 后台线程不跑 →
    自动升级不跑 → **再也推不进任何修复**，服务器永久失联。

    而「推送即上线」意味着这条路每天要走好几趟。所以脚本必须自带三道闸。
    """
    from responder.config import Settings
    from responder.ops import _SCRIPT

    s = Settings()
    sh = _SCRIPT.format(repo=s.update_repo_dir, branch=s.update_branch,
                        pip=s.update_pip, python=s.update_python, port=s.api_port)

    assert "PREV=$(git rev-parse HEAD)" in sh, "得先记下退路"
    assert "rollback()" in sh
    # ① 装完先在另一个进程里装配一遍，起不来就不重启
    assert "create_app()" in sh
    smoke = sh.index("create_app()")
    restart = sh.index("systemctl restart responder", smoke)
    assert smoke < restart, "冒烟检查必须在重启之前——顺序反了这道闸就是摆设"
    # ② 重启后确认真的活了
    assert "/health" in sh and "curl" in sh
    # ③ 每条失败路径都回滚，且回滚会把旧版重新装回去
    assert sh.count("rollback") >= 4
    assert 'git reset --hard "$PREV"' in sh

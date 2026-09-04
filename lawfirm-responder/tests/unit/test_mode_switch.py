"""运行模式远程切换：发送通道按模式实时门控，改动写回 .env。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from responder.config import Settings, persist_setting
from responder.console.api import router as console_router
from responder.models import ClientStatus, GroupProfile, IncomingMessage
from responder.service import Pipeline
from responder.store.db import Store


class RecordingSender:
    def __init__(self):
        self.robot: list = []

    def send_robot_text(self, webhook, text):
        self.robot.append(text)
        return True

    def send_group_text(self, chat_id, text):
        self.robot.append(text)
        return True

    def send_direct_text(self, userid, text):
        return True


def make(tmp_path, mode="shadow"):
    db = str(tmp_path / "m.db")
    store = Store(db)
    store.upsert_group(GroupProfile(
        group_id="g1", name="试点群", client_status=ClientStatus.PROSPECT,
        case_type="劳动仲裁", lawyer_name="魏", robot_webhook="rk",
    ))
    settings = Settings(mode=mode, db_path=db, split_delay_seconds=0)
    sender = RecordingSender()
    return store, sender, Pipeline(store, sender, settings)


def _msg(mid="m1"):
    return IncomingMessage(
        msg_id=mid, group_id="g1", sender_id="c1",
        content="我要投诉你们的服务态度",
    )


def test_gating_follows_mode_at_runtime(tmp_path):
    """同一个 Pipeline 实例：切到 live 后开始发言，切回 shadow 立即静默。"""
    store, sender, p = make(tmp_path, "shadow")
    p.handle(_msg("m1"))
    assert sender.robot == []  # 影子模式只起草

    p.settings.mode = "live"
    p.handle(_msg("m2"))
    assert len(sender.robot) == 1  # 无需重启即生效

    p.settings.mode = "shadow"
    p.handle(_msg("m3"))
    assert len(sender.robot) == 1  # 又静默了


def test_mode_endpoint_switches_and_validates(tmp_path):
    store, sender, p = make(tmp_path, "shadow")
    app = FastAPI()
    app.state.store = store
    app.state.pipeline = p
    app.include_router(console_router)
    c = TestClient(app)

    assert c.post("/console/mode", json={"mode": "live"}).json()["mode"] == "live"
    assert p.settings.mode == "live"
    assert c.post("/console/mode", json={"mode": "bogus"}).status_code == 400
    assert p.settings.mode == "live"  # 非法值不改动


def test_persist_setting_updates_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("RESPONDER_MODE=shadow\nOTHER=1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert persist_setting("RESPONDER_MODE", "live") is True
    text = env.read_text(encoding="utf-8")
    assert "RESPONDER_MODE=live" in text and "OTHER=1" in text


def test_persist_setting_appends_missing_key(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("OTHER=1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert persist_setting("RESPONDER_MODE", "live") is True
    assert "RESPONDER_MODE=live" in env.read_text(encoding="utf-8")


def test_persist_setting_noop_without_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert persist_setting("RESPONDER_MODE", "live") is False


@pytest.mark.parametrize("mode,expect", [("live", True), ("shadow", False)])
def test_kf_client_gated_by_mode(tmp_path, mode, expect):
    store = Store(str(tmp_path / "k.db"))
    settings = Settings(mode=mode, db_path=str(tmp_path / "k.db"))
    p = Pipeline(store, None, settings, kf_client=object())
    assert (p.kf_client is not None) is expect

"""会话转接：强意愿线索直接把客服会话交给分到的律师（见 docs/kf-handoff.md）。

这条链路取代的是「律师打电话给客户」——最脆的一环：陌生号码接通率本来就低，
客户又常在上班或开庭。转接把它整个删掉，律师在原窗口接着聊。

因此本组测试的重点全在**该不该转**：转错、转空、重复转的代价都直接落在客户身上
（对着一个不会有人来的窗口干等），比不转更糟。
"""

import pytest

from responder.config import Settings
from responder.models import ClientStatus, GroupProfile
from responder.service import Pipeline
from responder.store.db import Store

OPEN_KFID = "wk-handoff"
EXT_USER = "wmCustomerHandoff"
GID = f"kf:{OPEN_KFID}:{EXT_USER}"


class FakeKf:
    """记录桩：不出网，只记下被要求发了什么、转给了谁。"""

    def __init__(self, servicers=("wei",), transfer_ok=True):
        self.servicers = list(servicers)
        self.transfer_ok = transfer_ok
        self.sent: list[str] = []
        self.transfers: list[tuple[str, str, str]] = []

    def available(self):
        return True

    def servicer_list(self, open_kfid):
        return list(self.servicers)

    def send_text(self, open_kfid, external_userid, text):
        self.sent.append(text)
        return True

    def transfer(self, open_kfid, external_userid, servicer_userid):
        self.transfers.append((open_kfid, external_userid, servicer_userid))
        return self.transfer_ok


class Snd:
    def __init__(self):
        self.direct: list[tuple[str, str]] = []

    def send_direct_text(self, userid, text):
        self.direct.append((userid, text))
        return True


def make(tmp_path, *, kf=None, **over):
    db = str(tmp_path / "h.db")
    store = Store(db)
    cfg = dict(
        mode="live", db_path=db, split_messages=False, split_delay_seconds=0,
        wecom_kf_secret="kf-secret", llm_answer_enabled=False,
        llm_refine_enabled=False,
    )
    cfg.update(over)
    settings = Settings(**cfg)
    kf = kf or FakeKf()
    store.upsert_lawyer("wei", {"name": "魏", "role": "lawyer", "active": True})
    pipeline = Pipeline(store, sender=Snd(), settings=settings, kf_client=kf)
    return store, kf, pipeline


def kf_group(**over) -> GroupProfile:
    fields = dict(
        client_status=ClientStatus.PROSPECT, group_id=GID,
        kf_open_kfid=OPEN_KFID, kf_external_userid=EXT_USER,
        case_type="劳动仲裁",
    )
    fields.update(over)
    return GroupProfile(**fields)


def lead_row(priority="P0", assigned="wei") -> dict:
    return {"priority": priority, "assigned_userid": assigned, "intent": "hot"}


# ------------------------------------------------------------------ 该转的转
def test_p0_lead_transfers_to_assigned_lawyer(tmp_path):
    store, kf, p = make(tmp_path)
    group = kf_group()
    store.upsert_group(group)

    assert p._maybe_handoff(group, lead_row(), urgent=False) is True
    assert kf.transfers == [(OPEN_KFID, EXT_USER, "wei")]
    # 转之前必须先跟客户说一句，否则律师看到之前客户对着静默
    assert kf.sent and "转给" in kf.sent[0]
    assert store.get_group(GID).handoff_userid == "wei"


def test_transfer_message_does_not_name_the_lawyer(tmp_path):
    """业务决策 2026-08：一对一窗口不点名。点了名客户就会等那个人。"""
    _, kf, p = make(tmp_path)
    group = kf_group()
    p._maybe_handoff(group, lead_row(), urgent=False)
    assert "魏" not in kf.sent[0]


def test_transfer_message_gives_a_next_step(tmp_path):
    """转接不是终点：客户手上闲着就容易走，给他一件此刻能做的事。"""
    _, kf, p = make(tmp_path)
    p._maybe_handoff(kf_group(), lead_row(), urgent=False)
    assert "材料" in kf.sent[0]


def test_urgent_transfers_regardless_of_priority(tmp_path):
    """拘留/开庭临近这类不看分数——等评分够格，人已经进去了。"""
    _, kf, p = make(tmp_path)
    assert p._maybe_handoff(kf_group(), lead_row(priority="P2"), urgent=True) is True
    assert kf.transfers


def test_handoff_reply_is_logged(tmp_path):
    """转接话术要入库：留痕是 AI 不明示身份这一决策的合规兜底。"""
    store, _, p = make(tmp_path)
    p._maybe_handoff(kf_group(), lead_row(), urgent=False)
    rows = store.list_replies(GID, limit=5)
    assert rows and rows[0]["category"] == "handoff"


# ---------------------------------------------------------------- 不该转的不转
def test_low_priority_is_not_transferred(tmp_path):
    """一周 416 人进私信，P1/P2 全转过去律师什么也别干了。"""
    _, kf, p = make(tmp_path)
    assert p._maybe_handoff(kf_group(), lead_row(priority="P1"), urgent=False) is False
    assert not kf.transfers


def test_unassigned_lead_is_not_transferred(tmp_path):
    """名册为空/没派出去 → 无对象可转，回落原链路。"""
    _, kf, p = make(tmp_path)
    assert p._maybe_handoff(kf_group(), lead_row(assigned=""), urgent=False) is False
    assert not kf.transfers


def test_lawyer_not_a_servicer_is_skipped(tmp_path):
    """律师没被加进客服账号的「接待人员」→ 企微直接拒。

    宁可不转也不能先跟客户说「转给律师了」再转失败：那句话已经发出去了，
    客户就真的在等。所以接待人检查必须在发言之前。
    """
    _, kf, p = make(tmp_path, kf=FakeKf(servicers=("zhang",)))
    assert p._maybe_handoff(kf_group(), lead_row(), urgent=False) is False
    assert not kf.transfers
    assert not kf.sent


def test_already_handed_off_is_not_transferred_again(tmp_path):
    """转两次没有意义，还会把接手 SLA 的计时打乱。"""
    _, kf, p = make(tmp_path)
    assert p._maybe_handoff(kf_group(handoff_userid="wei"), lead_row(), urgent=False) is False
    assert not kf.transfers


def test_shadow_mode_never_transfers(tmp_path):
    """影子模式只入库不动客户——转接是对客户可见的动作，必须受同一道门控。"""
    _, kf, p = make(tmp_path, mode="shadow")
    assert p._maybe_handoff(kf_group(), lead_row(), urgent=False) is False
    assert not kf.transfers and not kf.sent


def test_disabled_switch_blocks_transfer(tmp_path):
    _, kf, p = make(tmp_path, handoff_enabled=False)
    assert p._maybe_handoff(kf_group(), lead_row(), urgent=False) is False
    assert not kf.transfers


def test_douyin_session_is_not_transferred(tmp_path):
    """抖音侧接待走官方 AI即用，没有对等的转接能力。"""
    _, kf, p = make(tmp_path)
    group = kf_group(group_id="dyim:abc", kf_open_kfid="", kf_external_userid="",
                     douyin_open_id="abc")
    assert p._maybe_handoff(group, lead_row(), urgent=False) is False
    assert not kf.transfers


def test_group_chat_is_not_transferred(tmp_path):
    """群聊里承办律师本人就在场，没有「转」这个动作。"""
    _, kf, p = make(tmp_path)
    group = GroupProfile(client_status=ClientStatus.PROSPECT, group_id="g-1",
                         lawyer_userid="wei")
    assert p._maybe_handoff(group, lead_row(), urgent=False) is False
    assert not kf.transfers


# ------------------------------------------------------------------ 失败回落
def test_failed_transfer_falls_back_silently(tmp_path):
    """企微拒了就当没转：交接单已经推给律师，他还能打电话，客户不会掉队。

    关键是**不能落库**——落了库 decision 层就当会话已交给人工，AI 从此闭嘴，
    而实际上根本没人在那头。
    """
    store, kf, p = make(tmp_path, kf=FakeKf(transfer_ok=False))
    group = kf_group()
    store.upsert_group(group)
    assert p._maybe_handoff(group, lead_row(), urgent=False) is False
    assert store.get_group(GID).handoff_userid == ""


def test_servicer_lookup_failure_does_not_crash(tmp_path):
    """企微接口抖一下不能把整条消息处理链带崩——转不了就是不转。"""
    kf = FakeKf()

    def boom(_):
        raise RuntimeError("network")

    kf.servicer_list = boom
    _, kf, p = make(tmp_path, kf=kf)
    assert p._maybe_handoff(kf_group(), lead_row(), urgent=False) is False


# ------------------------------------------------------------------ 转接之后
def test_decision_stays_silent_after_handoff(tmp_path):
    """转接后 AI 必须闭嘴：律师正在跟客户聊，AI 插话就成了两个人抢答。"""
    from responder.engine.decision import decide
    from responder.models import IncomingMessage

    store, _, p = make(tmp_path)
    store.upsert_group(kf_group())
    p._maybe_handoff(store.get_group(GID), lead_row(), urgent=False)

    msg = IncomingMessage(msg_id="m1", group_id=GID, sender_id=EXT_USER,
                          content="那我该准备什么材料")
    d = decide(msg, store.get_group(GID), settings=p.settings)
    assert d.should_speak is False
    assert any("handed-off" in r for r in d.reasons)


@pytest.mark.parametrize("tiers,priority,expect", [
    ("P0", "P0", True),
    ("P0,P1", "P1", True),
    ("P0", "P1", False),
])
def test_priority_tiers_are_configurable(tmp_path, tiers, priority, expect):
    _, kf, p = make(tmp_path, handoff_priorities=tiers)
    assert p._maybe_handoff(kf_group(), lead_row(priority=priority), urgent=False) is expect


# ------------------------------------------------------------------ 就绪自检
# 律所侧没有 SSH，而企微 API 只有服务器调得通，所以「转接能不能用」必须能在
# 浏览器里一键问清楚——否则要等到第一个 P0 线索来的那一刻才知道配错了。
def _probe_app(store, settings, kf):
    from fastapi import FastAPI

    from responder.console.api import router as console_router
    from responder.service import Pipeline

    app = FastAPI()
    app.state.store = store
    app.state.pipeline = Pipeline(store, None, settings, kf_client=kf)
    app.include_router(console_router)
    return app


class ProbeKf(FakeKf):
    def __init__(self, *, state_ok=True, add_ok=True, **kw):
        super().__init__(**kw)
        self.state_ok = state_ok
        self.add_ok = add_ok

    def account_list(self):
        return [{"open_kfid": OPEN_KFID, "name": "在线咨询"}]

    def servicer_raw(self, open_kfid):
        return {"servicer_list": [{"userid": u} for u in self.servicers]}

    def post_raw(self, path, payload):
        if not self.state_ok:
            raise RuntimeError(f"kf {path} failed: {{'errcode': 60020}}")
        return {"service_state": 1, "servicer_userid": ""}

    def servicer_add(self, open_kfid, userids):
        if not self.add_ok:
            return {"error": "kf servicer/add failed: {'errcode': 60011}"}
        self.servicers = sorted(set(self.servicers) | set(userids))
        return {"errcode": 0}


def test_probe_reports_ready_when_roster_and_endpoint_check_out(tmp_path):
    from fastapi.testclient import TestClient

    store, _, p = make(tmp_path, admin_token="")
    store.upsert_group(kf_group())
    kf = ProbeKf()
    r = TestClient(_probe_app(store, p.settings, kf)).get("/console/kf/handoff-probe")
    data = r.json()
    assert data["ready"] is True
    assert data["state_probe"]["ok"] is True


def test_probe_flags_lawyer_missing_from_servicers(tmp_path):
    """最要命的一种配错：律师不在接待人列表里，转接会当场失败。"""
    from fastapi.testclient import TestClient

    store, _, p = make(tmp_path, admin_token="")
    store.upsert_group(kf_group())
    kf = ProbeKf(servicers=("zhang",))
    data = TestClient(_probe_app(store, p.settings, kf)).get(
        "/console/kf/handoff-probe").json()
    assert data["ready"] is False
    assert data["accounts"][0]["missing"][0]["userid"] == "wei"


def test_probe_catches_wrong_endpoint_path(tmp_path):
    """接口路径是照文档配的（文档站在开发环境不可达）——必须真调一次才敢信。"""
    from fastapi.testclient import TestClient

    store, _, p = make(tmp_path, admin_token="")
    store.upsert_group(kf_group())
    kf = ProbeKf(state_ok=False)
    data = TestClient(_probe_app(store, p.settings, kf)).get(
        "/console/kf/handoff-probe").json()
    assert data["ready"] is False
    assert "60020" in data["state_probe"]["error"]


def test_probe_without_any_conversation_is_not_an_error(tmp_path):
    """还没客户进过线时探不了，但那不是故障，别吓人。"""
    from fastapi.testclient import TestClient

    store, _, p = make(tmp_path, admin_token="")
    data = TestClient(_probe_app(store, p.settings, ProbeKf())).get(
        "/console/kf/handoff-probe").json()
    assert data["state_probe"]["ok"] is False
    assert not data["state_probe"].get("error")
    assert data["ready"] is True


# ------------------------------------------------------ 一键把律师加为接待人
# 这一步在企微那边不好点：客服账号由企微应用托管，kf.weixin.qq.com 的后台
# 会劝你点「开始使用」把管理权夺回网页侧——那会打断消息推送。所以程序代劳。
def test_add_servicers_puts_roster_into_the_account(tmp_path):
    from fastapi.testclient import TestClient

    store, _, p = make(tmp_path, admin_token="")
    kf = ProbeKf(servicers=())
    data = TestClient(_probe_app(store, p.settings, kf)).post(
        "/console/kf/servicers/add").json()
    assert data["ok"] is True
    assert data["accounts"][0]["added"] == ["wei"]
    assert kf.servicers == ["wei"]


def test_add_servicers_reports_who_did_not_make_it(tmp_path):
    """以**回读结果**为准，不信写接口自己说的话——加没加上，看列表里有没有。"""
    from fastapi.testclient import TestClient

    store, _, p = make(tmp_path, admin_token="")
    data = TestClient(_probe_app(store, p.settings, ProbeKf(servicers=(), add_ok=False))).post(
        "/console/kf/servicers/add").json()
    assert data["ok"] is False
    assert data["accounts"][0]["failed"] == ["wei"]
    assert "60011" in data["accounts"][0]["error"]


def test_add_servicers_needs_a_roster_first(tmp_path):
    """名册为空时说人话，而不是报一个 0 人成功的假成功。"""
    from fastapi.testclient import TestClient

    db = str(tmp_path / "empty.db")
    store = Store(db)
    settings = Settings(mode="live", db_path=db, wecom_kf_secret="s", admin_token="")
    r = TestClient(_probe_app(store, settings, ProbeKf())).post("/console/kf/servicers/add")
    assert r.status_code == 400
    assert "名册" in r.json()["detail"]

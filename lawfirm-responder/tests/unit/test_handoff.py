"""会话转接：强意愿线索直接把客服会话交给分到的律师（见 docs/kf-handoff.md）。

这条链路取代的是「律师打电话给客户」——最脆的一环：陌生号码接通率本来就低，
客户又常在上班或开庭。转接把它整个删掉，律师在原窗口接着聊。

因此本组测试的重点全在**该不该转**：转错、转空、重复转的代价都直接落在客户身上
（对着一个不会有人来的窗口干等），比不转更糟。
"""

import json

import pytest

from responder.config import Settings
from responder.models import (
    Action,
    Category,
    ClientStatus,
    Decision,
    GroupProfile,
    IncomingMessage,
)
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


def lead_row(priority="P0", assigned="wei", hits=("contact",)) -> dict:
    # signals 是转接的判据（清单制，2026-08-09）；priority 只影响排队顺序
    return {"priority": priority, "assigned_userid": assigned, "intent": "hot",
            "signals": json.dumps(list(hits))}


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
def test_just_asking_questions_is_not_transferred(tmp_path):
    """一周 416 人进私信。只是来问问题的不转——AI 接着摸情况，那正是它的活。

    「问到收费」加分（案子可能值钱），但**不是找人的动作**：转过去客户会愣一下，
    而 AI 本可以把案由、时间、金额都问清楚再交出去。
    """
    _, kf, p = make(tmp_path)
    row = lead_row(priority="P1", hits=("fee",))
    assert p._maybe_handoff(kf_group(), row, urgent=False) is False
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


@pytest.mark.parametrize("hits,expect", [
    (["engage"], True),          # 说要委托
    (["want-contact"], True),    # 要律师给他打电话
    (["injury"], True),          # 有人在医院
    (["contact"], True),         # 留了电话
    (["meeting"], True),         # 想来所里
    (["wechat"], True),          # 要加微信
    (["fee"], False),            # 只问了收费——案子也许值钱，但他没在找人
    (["urgent-plea"], False),    # 只是说「急」，谁都会说
    ([], False),
])
def test_transfer_is_decided_by_the_checklist_not_the_score(tmp_path, hits, expect):
    """律所方 2026-08-09 拍板：转人工看清单，不看分数。

    分数回答的是「先给谁打电话」，是排队问题；「现在要不要叫真人」是另一个
    问题，它只需要知道客户是不是已经在要真人了。绑在一起就会出现真机里那一幕：
    客户说了「让律师给我打电话」，系统还在算他够不够 60 分。
    """
    _, kf, p = make(tmp_path)
    row = lead_row(priority="P2")
    row["signals"] = json.dumps(hits)
    assert p._maybe_handoff(kf_group(), row, urgent=False) is expect
    assert bool(kf.transfers) is expect


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


# ------------------------------------------------ 手动转接：律师自己决定接手
# 自动转接只在 P0/紧急时触发，但律师常常是看完交接单**自己判断**这单该接。
# 没有这条路，他就只剩打电话——而打电话正是转接要取代的那一环。
def test_lawyer_can_take_over_a_conversation(tmp_path):
    from fastapi.testclient import TestClient

    store, _, p = make(tmp_path, admin_token="")
    store.upsert_group(kf_group())
    kf = ProbeKf(servicers=("wei",))
    app = _probe_app(store, p.settings, kf)
    r = TestClient(app).post(
        f"/console/groups/{GID}/takeover", json={"userid": "wei"},
    )
    assert r.status_code == 200
    assert kf.transfers == [(OPEN_KFID, EXT_USER, "wei")]
    assert store.get_group(GID).handoff_userid == "wei"
    assert kf.sent, "转之前要先跟客户说一句，否则他对着静默干等"


def test_takeover_refuses_when_lawyer_is_not_a_servicer(tmp_path):
    """企微会拒，而且是在客户已经收到「转给律师了」之后——所以先查再说话。"""
    from fastapi.testclient import TestClient

    store, _, p = make(tmp_path, admin_token="")
    store.upsert_group(kf_group())
    kf = ProbeKf(servicers=("zhang",))
    r = TestClient(_probe_app(store, p.settings, kf)).post(
        f"/console/groups/{GID}/takeover", json={"userid": "wei"},
    )
    assert r.status_code == 400
    assert "接待人" in r.json()["detail"]
    assert not kf.sent, "话不能先发出去"


def test_takeover_rejects_unknown_group(tmp_path):
    from fastapi.testclient import TestClient

    store, _, p = make(tmp_path, admin_token="")
    r = TestClient(_probe_app(store, p.settings, ProbeKf())).post(
        "/console/groups/kf:nope:nobody/takeover", json={"userid": "wei"},
    )
    assert r.status_code == 404


# --------------------------------------------------- 没转成，得说得出为什么
# 律所方原话：「那怎么会没有自动转接给律师呢」。六个前提原来是六个静默的
# return——转不成就悄悄回落，控制台里什么都看不出来，只能一条条猜。
# 静默失败是最贵的失败：没有人会去查一个「看起来正常」的系统。

def _skip_reason(store, group_id: str) -> str:
    return store.get_note(f"handoff_skip:{group_id}")


def test_every_skip_writes_down_why(tmp_path):
    """六道前提，每一道都得留下一句人话。缺哪句，排障就得靠猜。"""
    cases = [
        ("转接开关关着", dict(handoff_enabled=False), kf_group(), lead_row()),
        ("影子模式", dict(mode="shadow"), kf_group(), lead_row()),
        ("不是微信客服会话", {}, kf_group(group_id="dyim:abc", kf_open_kfid="",
                                    kf_external_userid="", douyin_open_id="abc"),
         lead_row()),
        ("转给过", {}, kf_group(handoff_userid="wei"), lead_row()),
        ("还没做出「想找真人」的动作", {}, kf_group(), lead_row(hits=("fee",))),
        ("还没派给具体律师", {}, kf_group(), lead_row(assigned="")),
    ]
    for expect, over, group, row in cases:
        sub = tmp_path / expect
        sub.mkdir()
        store, _, p = make(sub, **over)
        assert p._maybe_handoff(group, row, urgent=False) is False
        assert expect in _skip_reason(store, group.group_id), expect
        assert expect in row["_handoff_skip"], expect


def test_not_a_servicer_says_which_button_to_press(tmp_path):
    """最常见的一种没转成：律师根本不在那个客服账号的接待人名单里。

    光说「不在名单里」不够——得说清去哪儿点哪个按钮，否则律所方那头
    还是不知道该做什么。
    """
    store, _, p = make(tmp_path, kf=FakeKf(servicers=("zhang",)))
    group = kf_group()
    assert p._maybe_handoff(group, lead_row(), urgent=False) is False
    why = _skip_reason(store, group.group_id)
    assert "接待人" in why and "状态" in why


def test_refused_transfer_is_recorded_too(tmp_path):
    """企微那头拒了也算一种「没转成」，不能只在日志里留一行。"""
    store, _, p = make(tmp_path, kf=FakeKf(transfer_ok=False))
    group = kf_group()
    store.upsert_group(group)
    assert p._maybe_handoff(group, lead_row(), urgent=False) is False
    assert "企微拒绝" in _skip_reason(store, group.group_id)


def test_diagnose_surfaces_the_skip_reason(tmp_path):
    """「为什么没回复」那一页要顺带答「为什么没自动转给律师」——
    律所方问的其实是同一件事：这通对话现在卡在哪儿。"""
    from fastapi.testclient import TestClient

    store, _, p = make(tmp_path, admin_token="")
    group = kf_group()
    store.upsert_group(group)
    p._maybe_handoff(group, lead_row(hits=("fee",)), urgent=False)

    r = TestClient(_probe_app(store, p.settings, ProbeKf())).get(
        "/console/diagnose", params={"group_id": GID},
    )
    assert r.status_code == 200
    assert any("没有自动转给律师" in c for c in r.json()["checks"])


def test_score_crossing_p0_on_a_quiet_follow_up_still_transfers(tmp_path):
    """跨过 P0 门槛的那一句，常常是一句「冷」消息。

    硬信号（有人受伤、要律师联系、留电话）落在追问里是常态，而冷消息走的是
    另一条分支——那条分支原来只重算分数就 return，转接一次也不会触发。
    症状是：控制台里线索明明是 P0，律师的企微里却什么都没有。
    """
    store, kf, p = make(tmp_path)
    group = kf_group()
    store.upsert_group(group)
    store.upsert_lead(GID, {"priority": "P2", "score": 10, "assigned_userid": "wei"})

    convo = [
        {"content": "我朋友出车祸住院了，对方全责", "sender_is_staff": 0},
        {"content": "我的电话 13800001111", "sender_is_staff": 0},
    ]
    msg = IncomingMessage(msg_id="q-1", group_id=GID, content="好的",
                          sender_id="cust")
    decision = Decision(msg_id="q-1", group_id=GID, action=Action.ANSWER,
                        category=Category.OTHER)
    p._maybe_dispatch_lead(msg, decision, group, convo)

    assert kf.transfers == [(OPEN_KFID, EXT_USER, "wei")], (
        f"没转成：{store.get_note(f'handoff_skip:{GID}')}"
    )


def test_handoff_checklist_matches_hot_signals():
    """转接清单里的每一条，都必须让 `signals.detect` 判成 hot。

    不一致的后果很隐蔽：清单认得这个信号，但那条消息被当成「冷消息」，
    于是走另一条分支——线索晚一轮才出，转接跟着晚一轮。客户那头的表现是
    「我都说了让律师联系我，怎么还是 AI 在回」。
    """
    from responder.engine import priority, signals

    assert {k for k, _ in priority.WANTS_HUMAN} == signals.HOT_SIGNALS


@pytest.mark.parametrize("payload,anchor", [
    ("kf kf/servicer/add failed: {'errcode': 48007, 'errmsg': 'api forbidden'}",
     "通过 API 管理微信客服账号"),
    ({"errcode": 48002}, "可调用接口的应用"),
    ({"errcode": 60030}, "可见范围"),
    ({"errcode": 0}, ""),
    ("网络超时", ""),
])
def test_wecom_errcodes_are_translated_into_something_actionable(payload, anchor):
    """「48007 api forbidden for no kfid privilege」对律所侧等于一串乱码。

    每一个真会撞到的错误码都必须翻成一句**能照着点**的中文，否则每次都变成
    「发截图给开发」——而开发这边同样要现查，一轮就是半小时。
    """
    from responder.gateway.wecom_kf import err_hint

    hint = err_hint(payload)
    if anchor:
        assert anchor in hint
    else:
        assert hint == ""


# ------------------------------------- 企业名下别的客服账号，不是我们的事
# 真机 2026-08-09：律所另有一个「上海松沪律师事务所在线客服」，人工在接，
# 从没交给自建应用管。而 kf/account/list 把它也列了出来，于是「一键加接待人」
# 对它调用 servicer/add 拿到 48007，整块自检被染成红色——
# 客户实际走的那个账号明明是通的。**说错「坏了」和漏报一样贵。**

class TwoAccountKf(ProbeKf):
    """一个归我们管、一个不归——企微把两个都列出来。"""

    OTHER = "wk-not-ours"

    def account_list(self):
        return [{"open_kfid": OPEN_KFID, "name": "松沪律所咨询"},
                {"open_kfid": self.OTHER, "name": "上海松沪律师事务所在线客服"}]

    def servicer_raw(self, open_kfid):
        if open_kfid == self.OTHER:
            return {"error": "kf kf/servicer/list failed: {'errcode': 48007}"}
        return super().servicer_raw(open_kfid)

    def servicer_add(self, open_kfid, userids):
        if open_kfid == self.OTHER:
            raise AssertionError("不该去动一个没有客户从中进来的账号")
        return super().servicer_add(open_kfid, userids)


def _two_account_client(tmp_path):
    from fastapi.testclient import TestClient

    store, _, p = make(tmp_path, admin_token="")
    store.upsert_group(kf_group())          # 客户只从 OPEN_KFID 这个账号进来
    kf = TwoAccountKf()
    return TestClient(_probe_app(store, p.settings, kf)), kf


def test_probe_ignores_customer_service_accounts_we_never_see_traffic_from(tmp_path):
    c, _ = _two_account_client(tmp_path)
    r = c.get("/console/kf/handoff-probe").json()
    by_name = {a["name"]: a for a in r["accounts"]}
    assert by_name["松沪律所咨询"]["in_use"] is True
    assert by_name["上海松沪律师事务所在线客服"]["in_use"] is False
    # 那个账号没有接待人也不该把整体判成未就绪
    assert r["ready"] is True, r["hint"]


def test_adding_servicers_leaves_other_peoples_accounts_alone(tmp_path):
    """去动它只会拿到 48007，然后让人跑去修一个没坏的东西。"""
    c, _ = _two_account_client(tmp_path)
    r = c.post("/console/kf/servicers/add").json()
    assert r["ok"] is True
    assert r["skipped"] == ["上海松沪律师事务所在线客服"]
    assert [a["name"] for a in r["accounts"]] == ["松沪律所咨询"]


def test_a_brand_new_deployment_still_checks_every_account(tmp_path):
    """一条会话都还没有时不能什么都不查——那天恰恰是最需要自检的一天。"""
    from fastapi.testclient import TestClient

    store, _, p = make(tmp_path, admin_token="")
    kf = ProbeKf(servicers=("zhang",))       # 名册里的 wei 不在接待人里
    c = TestClient(_probe_app(store, p.settings, kf))
    r = c.get("/console/kf/handoff-probe").json()
    assert r["accounts"][0]["in_use"] is True
    assert r["ready"] is False

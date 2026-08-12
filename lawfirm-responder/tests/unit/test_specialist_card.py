"""转接成功后把承办律师的名片推给客户（企微「升级服务 → 专员服务」）。

律所方原话：「即使对接了人工，但依旧用的不是客服自己的企业客服号沟通的。
我们怎么让客服直接用自己的号对接上客户的对话呢」——问得准。

**转接只解决「谁来回这句话」，名片解决「客户认识了谁」。**
微信客服里律师回的话，客户那头看到的永远是客服账号在说话；会话一结束
联系就断了，律师之后想主动找他没有任何通道。客户把名片加上之后，
就是他和这位律师的长期联系人关系——对律所来说，这是从「一次咨询」
变成「长期关系」的那一步。
"""

import json

from responder.config import Settings
from responder.models import ClientStatus, GroupProfile
from responder.service import Pipeline
from responder.store.db import Store

OPEN_KFID, EXT = "wk1", "cust1"
GID = f"kf:{OPEN_KFID}:{EXT}"


class Kf:
    def __init__(self, upgrade=None):
        self.sent, self.transfers, self.upgrades = [], [], []
        self._upgrade = upgrade or {"ok": True}

    def available(self):
        return True

    def servicer_list(self, kfid):
        return ["wei"]

    def send_text(self, kfid, ext, text):
        self.sent.append(text)
        return True

    def transfer(self, kfid, ext, userid):
        self.transfers.append(userid)
        return True

    def upgrade_to_member(self, kfid, ext, userid, wording=""):
        self.upgrades.append((kfid, ext, userid, wording))
        return dict(self._upgrade)


def _pipe(tmp_path, kf=None, **over):
    cfg = dict(mode="live", db_path=str(tmp_path / "s.db"), llm_provider="none",
               wecom_kf_secret="k", split_messages=False, split_delay_seconds=0,
               llm_answer_enabled=False, llm_refine_enabled=False)
    cfg.update(over)
    s = Settings(**cfg)
    store = Store(s.db_path)
    store.upsert_lawyer("wei", {"name": "魏", "active": True})
    g = GroupProfile(group_id=GID, kf_open_kfid=OPEN_KFID, kf_external_userid=EXT,
                     client_status=ClientStatus.PROSPECT, case_type="劳动仲裁")
    store.upsert_group(g)
    return store, g, Pipeline(store, None, s, kf_client=kf or Kf())


def _row(assigned="wei"):
    return {"priority": "P0", "assigned_userid": assigned,
            "signals": json.dumps(["engage"])}


def test_a_successful_handoff_also_pushes_the_lawyers_card(tmp_path):
    kf = Kf()
    store, g, p = _pipe(tmp_path, kf)

    assert p._maybe_handoff(g, _row(), urgent=False) is True
    assert kf.upgrades == [(OPEN_KFID, EXT, "wei", "")]
    assert "已把 wei 的名片推给客户" in store.get_note(f"specialist_card:{GID}")


def test_it_pushes_the_lawyer_the_engine_picked_not_a_random_one(tmp_path):
    """把劳动争议的客户推给做刑事的律师，比不推更糟。
    企微后台那个「随机推荐专员」选项正是这么干的——我们不用它。"""
    kf = Kf()
    store, g, p = _pipe(tmp_path, kf)
    store.upsert_lawyer("zhang", {"name": "张", "active": True})

    p._maybe_handoff(g, _row(assigned="wei"), urgent=False)
    assert [u[2] for u in kf.upgrades] == ["wei"]


def test_the_wording_stays_in_the_firms_hands(tmp_path):
    """推荐语留空 = 用企微后台配好的那句。**话术不写在代码里。**"""
    kf = Kf()
    _, g, p = _pipe(tmp_path, kf)
    p._maybe_handoff(g, _row(), urgent=False)
    assert kf.upgrades[0][3] == ""


def test_a_configured_wording_is_passed_through(tmp_path):
    kf = Kf()
    _, g, p = _pipe(tmp_path, kf, upgrade_service_wording="这是接手您案子的律师")
    p._maybe_handoff(g, _row(), urgent=False)
    assert kf.upgrades[0][3] == "这是接手您案子的律师"


def test_a_failed_card_never_undoes_a_successful_handoff(tmp_path):
    """这一步锦上添花，不能反过来把已经成功的转接搞砸。"""
    kf = Kf(upgrade={"ok": False, "error": "boom", "hint": "去后台把他加进专员名单"})
    store, g, p = _pipe(tmp_path, kf)

    assert p._maybe_handoff(g, _row(), urgent=False) is True
    assert kf.transfers == ["wei"], "转接必须仍然成立"
    assert store.get_group(GID).handoff_userid == "wei"
    # 但失败要留下能查的证据：它的表现是「客户什么也没收到」
    assert "专员名单" in store.get_note(f"specialist_card:{GID}")


def test_a_crashing_client_does_not_take_the_handoff_with_it(tmp_path):
    class Boom(Kf):
        def upgrade_to_member(self, *a, **kw):
            raise RuntimeError("network")

    _, g, p = _pipe(tmp_path, Boom())
    assert p._maybe_handoff(g, _row(), urgent=False) is True


def test_the_switch_turns_it_off(tmp_path):
    kf = Kf()
    _, g, p = _pipe(tmp_path, kf, upgrade_service_enabled=False)
    p._maybe_handoff(g, _row(), urgent=False)
    assert not kf.upgrades


def test_shadow_mode_never_pushes_a_card(tmp_path):
    """名片是对客户可见的动作，受同一道模式门控。"""
    kf = Kf()
    _, g, p = _pipe(tmp_path, kf, mode="shadow")
    p._maybe_handoff(g, _row(), urgent=False)
    assert not kf.upgrades


def test_an_old_client_without_the_capability_is_skipped(tmp_path):
    """老版本客户端/测试桩没有这个方法时跳过，而不是报错。"""
    class NoUpgrade:
        def available(self):
            return True

        def servicer_list(self, kfid):
            return ["wei"]

        def send_text(self, kfid, ext, text):
            return True

        def transfer(self, kfid, ext, userid):
            return True

    _, g, p = _pipe(tmp_path, NoUpgrade())
    assert p._maybe_handoff(g, _row(), urgent=False) is True


def test_95021_is_translated_into_something_actionable():
    """「95021」对律所方等于乱码。它其实是最常见的那一种失败：
    这位律师还没被加进后台的专员名单。"""
    from responder.gateway.wecom_kf import err_hint

    hint = err_hint("kf kf/customer/upgrade_service failed: {'errcode': 95021}")
    assert "专员名单" in hint and "升级服务" in hint

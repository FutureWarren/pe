"""个人信息：处理者固定、留存有期限、删除有出口（2026-08-12 体检 立刻做 7）。

库里躺的是真实公民的法律咨询原文和手机号——欠薪、离婚、伤情、家人有没有被拘留，
而且我们还主动向他们要手机号。三处缺口：

1. `llm_provider="auto"` 意味着「哪个 key 在就发给谁」。环境里多一个
   ANTHROPIC_API_KEY，客户的咨询原文就**静默地**改走境外服务商——
   《个人信息保护法》上那是从「向第三方提供」变成「个人信息出境」，
   要求完全不同，而律所对此毫不知情。
2. 四张表从上线起只进不出，没有任何留存期限和清理。
3. 「清空这个客户」清不干净：这半年新增的几种运维小记一个都没删到，
   而它现在也是收到删除请求时唯一的执行入口。
"""

from datetime import datetime, timedelta

from responder.config import Settings
from responder.engine import llm
from responder.models import ClientStatus, GroupProfile, IncomingMessage
from responder.store.db import Store


# --------------------------------------------------- ① 处理者不能是个副作用
def test_the_provider_does_not_silently_switch_to_an_overseas_one(monkeypatch):
    """换处理者是律所的合规决定，不能由环境变量顺手决定。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    assert Settings().llm_provider == "deepseek", "默认必须钉死，不能是 auto"
    # 钉死之后：DeepSeek 的 key 不在就干脆不用模型（走确定性降级），
    # 而**不是**改发给另一家
    assert llm.resolve(Settings()) is None


def test_switching_provider_is_still_possible_on_purpose(monkeypatch):
    """钉死不等于锁死——明确配了就照办。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    p = llm.resolve(Settings(llm_provider="anthropic"))
    assert p is not None and p.name == "anthropic"


# --------------------------------------------------- ② 留存期限
def _seed(store: Store, gid: str, *, days_ago: int, status="prospect"):
    old = datetime.now() - timedelta(days=days_ago)
    store.upsert_group(GroupProfile(
        group_id=gid,
        client_status=ClientStatus.SIGNED if status == "signed" else ClientStatus.PROSPECT,
    ))
    store.save_message(IncomingMessage(
        msg_id=f"m-{gid}", group_id=gid, sender_id="u", content="我被公司辞退了",
        created_at=old,
    ))
    store.upsert_lead(gid, {"intent": "warm", "contact": "13800138000"})
    with store._conn() as conn:
        conn.execute("UPDATE leads SET created_at=? WHERE group_id=?",
                     (old.isoformat(), gid))


def test_old_consultations_are_purged(tmp_path):
    store = Store(str(tmp_path / "p.db"))
    _seed(store, "kf:a:old", days_ago=400)
    _seed(store, "kf:a:new", days_ago=3)

    counts = store.purge_older_than(datetime.now() - timedelta(days=365))

    assert counts["messages"] == 1
    assert store.recent_messages("kf:a:old", 10) == []
    assert store.recent_messages("kf:a:new", 10), "没到期的不能碰"


def test_signed_clients_are_never_purged(tmp_path):
    """在办案件的记录另有保存义务，一律不动。"""
    store = Store(str(tmp_path / "p.db"))
    _seed(store, "kf:a:signed", days_ago=400, status="signed")

    store.purge_older_than(datetime.now() - timedelta(days=365))

    assert store.recent_messages("kf:a:signed", 10)


def test_raw_messages_can_be_purged_before_the_lead_record(tmp_path):
    """原文最敏感、且过期后对业务没用；线索档案是跟进要用的台账。"""
    store = Store(str(tmp_path / "p.db"))
    _seed(store, "kf:a:x", days_ago=100)

    store.purge_older_than(
        datetime.now() - timedelta(days=365),           # 线索还没到期
        messages_cutoff=datetime.now() - timedelta(days=30),  # 原文到期了
    )

    assert store.recent_messages("kf:a:x", 10) == []
    assert store.get_lead("kf:a:x") is not None


def test_purging_is_off_until_the_firm_picks_a_number(tmp_path):
    """保留多久是律所的业务决策，不该由写代码的人替他们定。"""
    from responder.service import Pipeline
    from responder.worker import Worker

    store = Store(str(tmp_path / "p.db"))
    settings = Settings(db_path=store.path)
    assert settings.retention_days == 0
    _seed(store, "kf:a:old", days_ago=999)
    w = Worker(Pipeline(store, None, settings), store, None)

    w._sweep_retention(datetime.now())

    assert store.recent_messages("kf:a:old", 10), "没配天数时一条都不许删"


def test_a_purge_leaves_a_record(tmp_path):
    """删数据没有撤销键，至少得让人事后看得出「哪天删了多少」。"""
    from responder.service import Pipeline
    from responder.worker import Worker

    store = Store(str(tmp_path / "p.db"))
    settings = Settings(db_path=store.path, retention_days=365)
    _seed(store, "kf:a:old", days_ago=400)
    w = Worker(Pipeline(store, None, settings), store, None)

    w._sweep_retention(datetime.now())

    assert "按留存期限清理" in store.get_note("retention_purge")


# --------------------------------------------------- ③ 删除要删干净
def test_forgetting_a_customer_leaves_nothing_behind(tmp_path):
    """收到删除请求时，这是唯一的执行入口——「差不多删干净」是一句不实的承诺。"""
    from responder import retry

    store = Store(str(tmp_path / "p.db"))
    gid = "kf:a:gone"
    _seed(store, gid, days_ago=1)
    store.set_note(f"handoff_skip:{gid}", "旧结论")
    store.set_note(f"undelivered:{gid}", "有回复没送到")
    store.set_note(f"lead_failed:{gid}", "交接单没生成")
    store.set_note(f"robot_check:{gid}", datetime.now().isoformat())
    retry.record_failure(store, "winback", gid)

    store.forget_group(gid)

    for key in ("handoff_skip", "undelivered", "lead_failed", "robot_check"):
        assert store.get_note(f"{key}:{gid}") == "", key
    assert retry.should_try(store, "winback", gid) is True, "重试计数也要清"
    assert store.recent_messages(gid, 10) == []
    assert store.get_lead(gid) is None


def test_the_profile_itself_survives_a_forget(tmp_path):
    """案由、承办律师、AI 开关是人配的，不该被一次清空带走。"""
    store = Store(str(tmp_path / "p.db"))
    gid = "kf:a:keep"
    store.upsert_group(GroupProfile(
        group_id=gid, case_type="劳动仲裁", lawyer_userid="wei",
        kf_open_kfid="a", kf_external_userid="keep",
    ))
    store.forget_group(gid)
    g = store.get_group(gid)
    assert g is not None and g.case_type == "劳动仲裁" and g.lawyer_userid == "wei"


# --------------------------------------------------- ④ 告知文案
def test_the_notice_covers_what_the_law_requires():
    from responder.reply import templates

    text = templates.privacy_notice(Settings())
    assert "上海松沪律师事务所" in text          # 谁在处理
    assert "留存" in text                        # 方式
    assert "技术服务商" in text                  # 向第三方提供（第 23 条）
    assert "删除" in text                        # 个人的权利与行使途径


def test_the_notice_is_not_sent_by_the_code(tmp_path):
    """一个窗口只该有一个人在说话；而且这是律所对客户作出的承诺，该由律所署名。"""
    from pathlib import Path

    import responder

    root = Path(responder.__file__).resolve().parent
    for path in (root / "worker.py", root / "service.py"):
        assert "privacy_notice" not in path.read_text(), (
            f"{path.name} 里不该自动发这段告知——它属于企微后台的欢迎语"
        )

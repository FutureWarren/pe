"""管理员视角：战报、员工表现、Excel 导出、看板时间范围。

律所方的原话定了这一组的取舍：「管理员不想看到那么多 AI 对话，那么乱」、
「看板应该是看当天的、当月的」、「能够方便看得到员工的表现以及处理情况」。

底层判断是：控制台是给**在系统里干活的人**用的（律师看自己的单、点已联系），
而所主任要的是另一样东西——一份推到眼前的摘要，和一张能拿去开会的表。
"""

import io
import json
from datetime import datetime, timedelta

import pytest

from responder import exporter
from responder.config import Settings
from responder.digest import build_digest, digest_target
from responder.models import ClientStatus, GroupProfile
from responder.store.db import Store


@pytest.fixture
def env(tmp_path):
    db = str(tmp_path / "a.db")
    store = Store(db)
    settings = Settings(
        db_path=db, public_base_url="http://1.2.3.4",
        office_name="上海松沪律师事务所", default_notify_userid="future",
    )
    store.upsert_lawyer("future", {"name": "魏涞", "role": "admin", "active": True})
    store.upsert_lawyer("zhang", {"name": "张律师", "role": "lawyer", "active": True})
    return store, settings


def add_lead(store, gid, *, name="", contact="13712345678", priority="P1",
             status="new", assigned="", facts=("拖欠三个月工资",), when=None):
    store.upsert_group(GroupProfile(
        group_id=gid, name=name, kf_open_kfid="wk", kf_external_userid=gid,
        client_status=ClientStatus.PROSPECT,
    ))
    store.upsert_lead(gid, {
        "intent": "hot", "contact": contact, "summary": "被拖欠工资并遭辞退",
        "case_type": "劳动仲裁", "priority": priority, "score": 75,
        "key_facts": json.dumps(list(facts), ensure_ascii=False),
    })
    if assigned:
        store.assign_lead(gid, assigned)
    if status != "new":
        store.set_lead_status(store.get_lead(gid)["id"], status)
    if when:  # 造历史数据：默认 created_at 是此刻，测不了时间范围
        with store._conn() as conn:
            conn.execute("UPDATE leads SET created_at=? WHERE group_id=?",
                         (when.isoformat(), gid))


# ------------------------------------------------------------ 员工处理情况
def test_staff_performance_answers_the_four_questions(env):
    """分到几单、跟进了几单、成交几单、多久跟上——「在办数」回答不了这些。"""
    store, _ = env
    add_lead(store, "kf:wk:a", assigned="future", status="contacted")
    add_lead(store, "kf:wk:b", assigned="future", status="converted")
    add_lead(store, "kf:wk:c", assigned="future", priority="P0")  # 还没跟

    rows = store.staff_performance()
    row = next(r for r in rows if r["userid"] == "future")
    assert (row["assigned"], row["handled"], row["converted"]) == (3, 2, 1)
    assert row["p0_pending"] == 1, "没跟进的 P0 要能单独看见——那是唯一要当场处理的"
    assert row["handled_rate"] == 67


def test_staff_performance_respects_the_time_window(env):
    """看板按今天/本月看。上月的单不该混进今天的成绩里。"""
    store, _ = env
    today = datetime.now().replace(hour=12)
    add_lead(store, "kf:wk:old", assigned="future", when=today - timedelta(days=40))
    add_lead(store, "kf:wk:new", assigned="future", when=today)

    start = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = store.staff_performance(since=start.isoformat())
    assert rows[0]["assigned"] == 1


def test_unassigned_leads_do_not_pollute_the_staff_table(env):
    """没派出去的单不属于任何人，混进去会让每个人的数字都不对。"""
    store, _ = env
    add_lead(store, "kf:wk:x")  # 未派单
    assert store.staff_performance() == []


# ------------------------------------------------------------ Excel 导出
def test_export_has_the_columns_a_partner_would_use(env):
    store, settings = env
    add_lead(store, "kf:wk:a", name="客户 AS30Mg", assigned="future", status="contacted")
    rows = exporter.build_rows(store, store.leads_in_range(None, None), settings=settings)

    head = rows[0]
    for col in ("进线时间", "客户", "联系方式", "来源", "优先级", "负责律师", "状态"):
        assert col in head
    row = dict(zip(head, rows[1]))
    assert row["来源"] == "微信客服"
    assert row["负责律师"] == "魏涞"
    assert row["状态"] == "已联系"
    # `/g/{id}` 而不是 `/ui#g={id}`：这张表会被转发进企业微信，
    # 而企微把 `#` 转义成 `%23`，后者点开是 404
    assert row["完整对话"].startswith("http://1.2.3.4/g/")


def test_export_does_not_dump_ai_conversations(env):
    """律所方原话：「不想看到那么多 AI 对话，那么乱」。表里只留一列深链。"""
    store, settings = env
    add_lead(store, "kf:wk:a", assigned="future")
    head = exporter.build_rows(store, store.leads_in_range(None, None),
                               settings=settings)[0]
    assert not any(k in "".join(head) for k in ("回复", "对话内容", "AI 说"))


def test_export_time_is_human_readable(env):
    """`2026-08-06T07:47:34.340981` 不该出现在给所主任看的表里。"""
    store, settings = env
    add_lead(store, "kf:wk:a")
    when = exporter.build_rows(store, store.leads_in_range(None, None),
                               settings=settings)[1][0]
    assert "T" not in when and "." not in when


def test_xlsx_is_openable_and_frozen(env):
    """几百行的表不冻结首行，翻到一半就不知道哪列是哪列。"""
    import openpyxl

    store, settings = env
    for i in range(3):
        add_lead(store, f"kf:wk:{i}", assigned="future")
    data = exporter.to_xlsx(
        exporter.build_rows(store, store.leads_in_range(None, None), settings=settings)
    )
    ws = openpyxl.load_workbook(io.BytesIO(data)).active
    assert ws.max_row == 4          # 表头 + 3 行
    assert ws.freeze_panes == "A2"
    assert ws.auto_filter.ref       # 带筛选，管理员能自己按律师/状态过滤


# ------------------------------------------------------------ 每日战报
def test_digest_reports_yesterday_not_today(env):
    """早上九点推的时候「今天」才过了九小时，拿它跟完整的昨天比毫无意义。"""
    store, settings = env
    now = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    yesterday = now - timedelta(days=1)
    add_lead(store, "kf:wk:y", assigned="future", when=yesterday)
    add_lead(store, "kf:wk:t", assigned="future", when=now)

    text = build_digest(store, settings, now=now)
    assert "新线索 1 条" in text


def test_digest_flags_untouched_p0(env):
    """整份战报里唯一需要管理员当场做点什么的一项，不能藏在数字堆里。"""
    store, settings = env
    now = datetime.now().replace(hour=9)
    add_lead(store, "kf:wk:a", assigned="zhang", priority="P0",
             when=now - timedelta(days=1))
    text = build_digest(store, settings, now=now)
    assert "P0 没联系" in text and "张律师" in text


def test_digest_distinguishes_quiet_day_from_broken_pipe(env):
    """连着几天零线索时，没人分得清是淡季还是通道断了——后者每天都是真钱。"""
    store, settings = env
    text = build_digest(store, settings)
    assert "没有新线索" in text
    assert "检查客服通道" in text


def test_digest_has_no_ai_transcript(env):
    store, settings = env
    now = datetime.now().replace(hour=9)
    add_lead(store, "kf:wk:a", assigned="future", when=now - timedelta(days=1))
    text = build_digest(store, settings, now=now)
    assert "AI" not in text and "回复" not in text


def test_digest_target_falls_back_to_an_admin(env):
    store, settings = env
    settings.default_notify_userid = ""
    settings.daily_digest_userid = ""
    assert digest_target(store, settings) == "future"  # role=admin 的那位

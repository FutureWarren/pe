"""长期记忆：律所知识库的检索与注入。

目标是让 AI「答得像你们所」——现在它答的是通用法律知识，对，但不是你们的。

两条设计取舍写在测试里，因为它们最容易在后续改动中被推翻：
1. **检索不到就什么都不注入。** 塞一条不相关的知识比不塞更糟——
   模型会努力把它用上，于是答非所问，而且答得理直气壮。
2. **只有人工审核过的条目会被引用。** 知识库条目就是话术，
   而话术须人审后才能对客户生效（CLAUDE.md 合规护栏）。
"""

import json
from datetime import datetime, timedelta

from responder import memory
from responder.config import Settings
from responder.models import Action, ClientStatus, Decision, GroupProfile, IncomingMessage
from responder.service import Pipeline
from responder.store.db import Store
from responder.worker import Worker

FEE_Q = "劳动仲裁怎么收费"
FEE_A = "劳动仲裁按案件难度定，具体由律师了解情况后当面谈。"


def store_with(tmp_path, entries):
    store = Store(str(tmp_path / "kb.db"))
    for q, a, status in entries:
        kid = store.add_knowledge(q, a, source="douyin")
        store.set_knowledge_status(kid, status)
    return store


# ------------------------------------------------------------------ 检索
def test_finds_the_relevant_entry():
    entries = [
        {"id": 1, "question": "劳动仲裁一般要多久", "answer": "四十五天左右", "tags": ""},
        {"id": 2, "question": "离婚需要什么材料", "answer": "身份证、结婚证等", "tags": ""},
    ]
    hits = memory.search(entries, "劳动仲裁大概多久能出结果？")
    assert [h["id"] for h in hits] == [1]


def test_returns_nothing_when_nothing_matches():
    """不相关就别注入——这条比「多召回一点」重要得多。"""
    entries = [{"id": 1, "question": "离婚需要什么材料", "answer": "…", "tags": ""}]
    assert memory.search(entries, "你们几点下班") == []


def test_matching_ignores_punctuation_and_case():
    entries = [{"id": 1, "question": "劳动仲裁怎么收费", "answer": "…", "tags": ""}]
    assert memory.search(entries, "劳动仲裁，怎么收费？？")


def test_tags_widen_recall():
    """客户不会按我们的措辞提问，标签是补召回用的。"""
    entries = [{"id": 1, "question": "经济补偿金怎么算", "answer": "…",
                "tags": "N+1 赔偿 辞退"}]
    assert memory.search(entries, "被辞退能拿多少赔偿")


def test_longer_entries_do_not_win_by_being_long(tmp_path):
    """用 Dice 而不是命中数：否则一条啰嗦的知识永远排第一，跟切不切题无关。"""
    entries = [
        {"id": 1, "question": "工伤认定", "answer": "…", "tags": ""},
        {"id": 2, "question": "工伤认定流程时限材料赔偿标准伤残等级鉴定复议诉讼",
         "answer": "…", "tags": ""},
    ]
    hits = memory.search(entries, "工伤认定", limit=2)
    assert hits[0]["id"] == 1


def test_format_is_empty_for_no_hits():
    assert memory.format_for_prompt([]) == ""


# ------------------------------------------------------------------ 入库
def test_reimporting_the_same_question_updates_instead_of_duplicating(tmp_path):
    """同一份问答重导两次不该变成两条——抖音那份还会再导。"""
    store = Store(str(tmp_path / "k.db"))
    a = store.add_knowledge(FEE_Q, "旧答案")
    b = store.add_knowledge(FEE_Q + "？", "新答案")  # 只差一个问号
    assert a == b
    rows = store.list_knowledge()
    assert len(rows) == 1 and rows[0]["answer"] == "新答案"


def test_imports_land_as_draft(tmp_path):
    """那 70 条是给抖音写的，语气和口径未必适用于微信侧，得人过一遍。"""
    store = Store(str(tmp_path / "k.db"))
    store.add_knowledge(FEE_Q, FEE_A, source="douyin")
    assert store.list_knowledge()[0]["status"] == "draft"


# ---------------------------------------------------------- 接入回复链路
def _pipeline(tmp_path, store, **over):
    cfg = dict(mode="live", db_path=str(tmp_path / "kb.db"), llm_answer_enabled=False,
               llm_refine_enabled=False, lead_brief_enabled=False)
    cfg.update(over)
    return Pipeline(store, None, Settings(**cfg))


def _answer_decision():
    return Decision(msg_id="m1", group_id="g1", action=Action.ANSWER,
                    category="general_law", reasons=[])


def _msg(text):
    return IncomingMessage(msg_id="m1", group_id="g1", sender_id="c", content=text)


def test_only_approved_entries_reach_the_model(tmp_path):
    """草稿是刚导入或机器提炼的，没经人审的话术不能对客户生效。"""
    store = store_with(tmp_path, [(FEE_Q, FEE_A, "draft")])
    p = _pipeline(tmp_path, store)
    assert p._recall(_msg("劳动仲裁怎么收费？"), _answer_decision()) == ""

    store.set_knowledge_status(store.list_knowledge()[0]["id"], "approved")
    assert "本所口径" in p._recall(_msg("劳动仲裁怎么收费？"), _answer_decision())


def test_retired_entries_are_not_used(tmp_path):
    store = store_with(tmp_path, [(FEE_Q, FEE_A, "retired")])
    p = _pipeline(tmp_path, store)
    assert p._recall(_msg("劳动仲裁怎么收费？"), _answer_decision()) == ""


def test_handoff_replies_do_not_query_the_knowledge_base(tmp_path):
    """承接类走确定性模板、不进模型，检索了也没人读。"""
    store = store_with(tmp_path, [(FEE_Q, FEE_A, "approved")])
    p = _pipeline(tmp_path, store)
    d = Decision(msg_id="m1", group_id="g1", action=Action.HANDOFF,
                 category="fee", reasons=[])
    assert p._recall(_msg("劳动仲裁怎么收费？"), d) == ""


def test_recall_records_which_entries_were_used(tmp_path):
    """哪几条真在起作用要看得见——用不上的该清掉，而不是越攒越多。"""
    store = store_with(tmp_path, [(FEE_Q, FEE_A, "approved")])
    p = _pipeline(tmp_path, store)
    d = _answer_decision()
    p._recall(_msg("劳动仲裁怎么收费？"), d)
    assert store.list_knowledge()[0]["hits"] == 1
    assert any(r.startswith("kb:") for r in d.reasons)


def test_switch_off_disables_recall(tmp_path):
    store = store_with(tmp_path, [(FEE_Q, FEE_A, "approved")])
    p = _pipeline(tmp_path, store, knowledge_enabled=False)
    assert p._recall(_msg("劳动仲裁怎么收费？"), _answer_decision()) == ""


def test_broken_knowledge_base_never_blocks_a_reply(tmp_path):
    """没有知识库照样能答——它是增强，不是依赖。"""
    store = store_with(tmp_path, [(FEE_Q, FEE_A, "approved")])
    p = _pipeline(tmp_path, store)

    def boom(**kw):
        raise RuntimeError("db gone")

    store.list_knowledge = boom
    assert p._recall(_msg("劳动仲裁怎么收费？"), _answer_decision()) == ""


def test_prompt_puts_the_house_line_before_the_question(tmp_path):
    """口径要在客户问题之前出现：模型读到问题时口径已经在手上了。"""
    from responder.reply import prompts

    text = prompts.answer_user_prompt(
        "劳动仲裁怎么收费", "劳动仲裁", "咨询客户", "", "", False, True,
        knowledge_text="问：劳动仲裁怎么收费\n本所口径：由律师当面谈。",
    )
    assert text.index("本所口径") < text.index("【客户刚才的问题】")
    # 口径不得凌驾于合规边界之上——比如口径里写了金额也不能复述
    assert "以边界为准" in text


def test_group_profile_is_untouched_by_recall(tmp_path):
    """知识库是组织记忆，跟某个客户的会话档案无关，别互相污染。"""
    store = store_with(tmp_path, [(FEE_Q, FEE_A, "approved")])
    store.upsert_group(GroupProfile(group_id="g1", client_status=ClientStatus.PROSPECT))
    p = _pipeline(tmp_path, store)
    p._recall(_msg("劳动仲裁怎么收费？"), _answer_decision())
    assert store.get_group("g1").case_type == ""


# ============================================================== 客户记忆
# 老客户隔三周回来，AI 该记得他上次说过什么。没有这一层，每次回访都是从零开始，
# 而客户那边的感受是「我上次不是都讲过了吗」——这句话一出，信任就没了。
def _returning_customer(tmp_path, days_ago=5, **over):
    """造一个上次来过、这次刚回来的客户。"""
    cfg = dict(mode="live", db_path=str(tmp_path / "m.db"), llm_answer_enabled=False,
               llm_refine_enabled=False, lead_brief_enabled=False)
    cfg.update(over)
    settings = Settings(**cfg)
    store = Store(settings.db_path)
    gid = "kf:wk:老客户"
    store.upsert_group(GroupProfile(
        group_id=gid, kf_open_kfid="wk", kf_external_userid="老客户",
        client_status=ClientStatus.PROSPECT, case_type="劳动仲裁",
    ))
    store.upsert_lead(gid, {
        "intent": "warm", "contact": "13712345678", "case_type": "劳动仲裁",
        "key_facts": json.dumps(["拖欠三个月工资", "被辞退"], ensure_ascii=False),
        "summary": "被拖欠工资并遭辞退",
    })
    old = (datetime.now() - timedelta(days=days_ago)).isoformat()
    with store._conn() as conn:
        conn.execute("UPDATE messages SET created_at=? WHERE group_id=?", (old, gid))
    return store, settings, gid


def test_memory_is_assembled_from_stored_facts_only(tmp_path):
    """不让模型自由发挥：记错一件客户没说过的事，比完全不记得更伤人。"""
    store, settings, gid = _returning_customer(tmp_path)
    text = memory.build_customer_memory(store, store.get_group(gid))
    assert "劳动仲裁" in text
    assert "拖欠三个月工资" in text and "被辞退" in text
    assert "已留联系方式" in text


def test_sweep_writes_memory_after_the_conversation_goes_quiet(tmp_path):
    """记忆是给下一次用的，对话进行中反复重算既无意义又白烧 CPU。"""
    store, settings, gid = _returning_customer(tmp_path)
    store.save_message(IncomingMessage(msg_id="m1", group_id=gid, sender_id="c",
                                       content="公司拖欠我三个月工资"))
    old = (datetime.now() - timedelta(hours=2)).isoformat()
    with store._conn() as conn:
        conn.execute("UPDATE messages SET created_at=? WHERE group_id=?", (old, gid))

    worker = Worker(Pipeline(store, None, settings), store, None)
    worker._sweep_customer_memory(datetime.now())
    assert store.get_group(gid).memory


def test_returning_customer_gets_the_memory_injected(tmp_path):
    store, settings, gid = _returning_customer(tmp_path)
    store.set_memory(gid, "上次咨询：5 天前 · 案由：劳动仲裁 · 他说过：拖欠三个月工资")
    p = Pipeline(store, None, settings)
    text = p._customer_memory(store.get_group(gid), [])
    assert "劳动仲裁" in text
    assert "不要再问一遍" in text  # 明确告诉模型别重复提问


def test_memory_not_injected_mid_conversation(tmp_path):
    """同一通对话里完整历史本来就在上下文，再塞一遍会让模型把上次和刚才搞混。"""
    store, settings, gid = _returning_customer(tmp_path)
    store.set_memory(gid, "上次咨询：5 天前 · 案由：劳动仲裁")
    convo = [{"content": f"第{i}句", "sender_is_staff": False, "msg_type": "text"}
             for i in range(4)]
    assert Pipeline(store, None, settings)._customer_memory(store.get_group(gid), convo) == ""


def test_signed_clients_do_not_get_a_recap(tmp_path):
    """已委托客户的事律师全程在跟，AI 复述一段旧摘要只会显得多余。"""
    store, settings, gid = _returning_customer(tmp_path)
    store.set_memory(gid, "上次咨询：5 天前")
    g = store.get_group(gid)
    g.client_status = ClientStatus.SIGNED
    store.upsert_group(g)
    assert Pipeline(store, None, settings)._customer_memory(store.get_group(gid), []) == ""


def test_memory_can_be_cleared(tmp_path):
    """客户要求删除时得能删干净（PIPL）。"""
    store, settings, gid = _returning_customer(tmp_path)
    store.set_memory(gid, "一些记忆")
    store.set_memory(gid, "")
    g = store.get_group(gid)
    assert g.memory == "" and g.memory_at is None


def test_ordinary_profile_updates_do_not_wipe_memory(tmp_path):
    """记忆是后台异步写的，而建档更新是高频的——混在一起会被随手覆盖掉。"""
    store, settings, gid = _returning_customer(tmp_path)
    store.set_memory(gid, "上次咨询：5 天前")
    g = store.get_group(gid)
    g.case_stage = "已立案"
    store.upsert_group(g)
    assert store.get_group(gid).memory == "上次咨询：5 天前"


# ------------------------------------------------------ 审核台（控制台）
def _kb_console(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from responder.console.api import router as console_router

    settings = Settings(mode="shadow", db_path=str(tmp_path / "kb_api.db"),
                        admin_token="sec123", public_base_url="")
    store = Store(settings.db_path)
    app = FastAPI()
    app.state.store = store
    app.state.pipeline = Pipeline(store, None, settings)
    app.include_router(console_router)
    return TestClient(app), store


HEAD = {"X-Admin-Token": "sec123"}


def test_console_flags_entries_that_trip_the_guard(tmp_path):
    """抖音那批话术里「免费」比比皆是，逐条人眼看是看不出来的。"""
    c, store = _kb_console(tmp_path)
    bad = store.add_knowledge("怎么咨询", "电话咨询免费，您方便留个电话吗")
    ok = store.add_knowledge("要多久", "劳动仲裁一般四十五天左右")
    items = {i["id"]: i for i in c.get("/console/knowledge", headers=HEAD).json()["items"]}
    assert items[bad]["flagged"] == ["quote-fee"]
    assert items[ok]["flagged"] == []


def test_cannot_approve_an_entry_that_trips_the_guard(tmp_path):
    """出口闸门会拦下它，但那时客户看到的是一句答非所问的兜底话术，
    而没有人知道原因出在知识库某一条上。所以在审核这一步就得挡住。"""
    c, store = _kb_console(tmp_path)
    kid = store.add_knowledge("怎么咨询", "电话咨询免费，您方便留个电话吗")
    r = c.post(f"/console/knowledge/{kid}/status", json={"status": "approved"}, headers=HEAD)
    assert r.status_code == 400
    assert "quote-fee" in r.json()["detail"]
    assert store.get_knowledge(kid)["status"] == "draft"
    # 停用不受影响：想把它收起来永远该是允许的
    assert c.post(f"/console/knowledge/{kid}/status", json={"status": "retired"},
                  headers=HEAD).status_code == 200


def test_rewriting_an_entry_sends_it_back_for_review(tmp_path):
    """审核过的是那个旧答案。改完自动生效，那道闸就白设了。"""
    c, store = _kb_console(tmp_path)
    kid = store.add_knowledge(FEE_Q, FEE_A)
    store.set_knowledge_status(kid, "approved")
    r = c.put(f"/console/knowledge/{kid}",
              json={"question": FEE_Q, "answer": "改了个说法，由律师当面谈。"}, headers=HEAD)
    assert r.status_code == 200 and r.json()["flagged"] == []
    row = store.get_knowledge(kid)
    assert row["status"] == "draft" and row["answer"].startswith("改了个说法")


def test_import_reports_how_many_need_rewriting(tmp_path):
    """导完只说「导入 70 条」等于没说——管理员要知道先改哪几条。"""
    c, _ = _kb_console(tmp_path)
    body = "怎么咨询\t电话咨询免费，留个电话吧\n要多久\t一般四十五天左右\n残行\n"
    r = c.post("/console/knowledge/import", content=body.encode("utf-8"), headers=HEAD)
    assert r.json() == {"ok": True, "added": 2, "skipped": 1, "flagged": 1}


def test_imported_entries_are_never_live_on_arrival(tmp_path):
    c, store = _kb_console(tmp_path)
    c.post("/console/knowledge/import",
           content="要多久\t一般四十五天左右\n".encode(), headers=HEAD)
    assert store.list_knowledge(status="approved") == []
    assert len(store.list_knowledge(status="draft")) == 1

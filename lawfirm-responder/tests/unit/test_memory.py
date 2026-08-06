"""长期记忆：律所知识库的检索与注入。

目标是让 AI「答得像你们所」——现在它答的是通用法律知识，对，但不是你们的。

两条设计取舍写在测试里，因为它们最容易在后续改动中被推翻：
1. **检索不到就什么都不注入。** 塞一条不相关的知识比不塞更糟——
   模型会努力把它用上，于是答非所问，而且答得理直气壮。
2. **只有人工审核过的条目会被引用。** 知识库条目就是话术，
   而话术须人审后才能对客户生效（CLAUDE.md 合规护栏）。
"""

from responder import memory
from responder.config import Settings
from responder.models import Action, ClientStatus, Decision, GroupProfile, IncomingMessage
from responder.service import Pipeline
from responder.store.db import Store

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

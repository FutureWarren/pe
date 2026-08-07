"""2026-08-07 真机测试暴露的四处问题。

律所方跑了一通完整对话（交通事故咨询），四条问题一条比一条要紧。
共同的根因是**规则用了封闭枚举**：客户的说法是无穷的，穷举必然漏，
而漏掉的那一条永远是静默地漏——没人会去查一个「看起来正常」的回复。
"""

import pytest

from responder.config import Settings
from responder.engine import rules
from responder.models import Action, Category, ClientStatus, GroupProfile
from responder.reply import templates


def kf(text):
    return rules.classify(text, is_one_on_one=True)


# ------------------------------------------------------ 一、问的是谁的钱
@pytest.mark.parametrize("text", [
    "赔偿大概多少钱", "我能拿到多少赔偿", "工伤能赔多少", "拖欠的工资能要回来多少",
    "对方保险公司赔多少钱",
])
def test_how_much_the_client_gets_is_a_legal_question_not_a_fee_question(text):
    """客户问「他能拿到多少」，系统当成「我们收多少」，回了一段「不能报价」。

    这是人身损害和劳动争议里最常被问的一句话，答错的代价极大——
    客户会觉得这家所张口就是钱。
    """
    action, category, _, _ = kf(text)
    assert category is not Category.FEE, f"{text}：问的是客户能拿多少，不是律师费"
    assert action is Action.ANSWER


@pytest.mark.parametrize("text", [
    "你们律师费多少钱", "请律师要多少钱", "代理费怎么算", "咨询费多少",
])
def test_asking_our_fee_still_goes_to_handoff(text):
    """AI 绝不报价，这条护栏不能被上面那个修复顺手放宽。"""
    action, category, _, _ = kf(text)
    assert (action, category) == (Action.HANDOFF, Category.FEE), text


# ------------------------------------------------------ 二、开放式问法
@pytest.mark.parametrize("text", [
    "交通事故责任怎么认定",   # 「怎么认定」不在原来的封闭词表里
    "工伤认定需要哪些材料",
    "劳动仲裁怎样走流程",
    "被辞退了咋办",
    # 「这种情况算什么性质」这类不含任何法律词的问句**故意不在这里**：
    # 它真的需要上下文才知道在问什么，规则层判沉默是对的，
    # 后面还有模型复核和 kf:substance 两道兜底。
])
def test_open_ended_questions_are_answered_not_dropped(text):
    """原来的问句词表是 `怎么(办|判|算|赔|分|处理|规定)` 这样的枚举，
    客户说「怎么认定」，一个字不在表里，整条就掉进默认沉默。"""
    action, category, _, reasons = kf(text)
    assert action is Action.ANSWER, f"{text} 落到了 {reasons}"
    assert category is Category.GENERAL_LAW


# ------------------------------------------------------ 三、能当场答的别推给人
@pytest.mark.parametrize("text", [
    "地址在什么地方", "你们地址在哪", "怎么过去", "在几楼", "周末上班吗",
])
def test_office_facts_are_answered_on_the_spot(text):
    """真机里客户问「地址在什么地方」，AI 回「我都记着呢，会转给律师」——
    地址就在配置里躺着。每一次推诿都是一次流失。"""
    action, _, _, reasons = kf(text)
    assert action is Action.ANSWER
    assert any(r.startswith("office-fact:") for r in reasons), reasons


def test_office_facts_only_apply_to_one_on_one_windows():
    """案件群里「地址发我一下」多半问的是**法院**的地址。
    把律所地址甩过去，是自信地答错。"""
    action, _, _, reasons = rules.classify("地址发我一下，我导航过去")
    assert not any(r.startswith("office-fact:") for r in reasons)


def test_the_address_reply_actually_contains_the_address():
    g = GroupProfile(group_id="kf:a:b", kf_open_kfid="a", kf_external_userid="b")
    s = Settings(office_address="上海市松江区九峰路88号平高广场11楼")
    text = templates.office_fact(g, seed="x", settings=s)
    assert s.office_address in text


def test_no_address_configured_means_we_do_not_invent_one():
    """说错地址比不说更糟——客户会白跑一趟。"""
    g = GroupProfile(group_id="kf:a:b", kf_open_kfid="a", kf_external_userid="b")
    text = templates.office_fact(g, seed="x", settings=Settings(office_address=""))
    assert "确认" in text and "号" not in text


# ------------------------------------------------------ 四、弱→强要再响一次
def test_weak_to_strong_upgrade_notifies_again():
    """客户是边聊边变强的：先留电话（弱），再问赔多少、问地址……
    旧策略只认冷/温/热三档意向，这些变化全在「温」里，
    于是客服永远只收到最早那条弱意愿提醒，客户后来变得多热都无人知晓。"""
    from responder import lead

    previous = {"priority": "P1", "notified_at": "2026-08-07T14:00:00", "intent": "warm"}
    assert lead.tier_upgraded(previous, "P0") is True
    assert lead.tier_upgraded(previous, "P1") is False
    # 强 → 弱不回退通知：分数会波动，「刚才急现在不急了」推给人只会消耗信任
    assert lead.tier_upgraded({"priority": "P0", "notified_at": "x"}, "P1") is False


def test_a_first_time_p0_is_not_labelled_an_upgrade():
    """第一次就 P0 的客户没有「升级」可言，加个前缀反而让人以为错过了什么。"""
    from responder import lead

    assert lead.tier_upgraded(None, "P0") is False
    assert lead.tier_upgraded({"priority": "P1", "notified_at": None}, "P0") is False


# ------------------------------------------------------ 五、深链
def test_brief_link_avoids_the_fragment_that_wecom_escapes():
    """企业微信把 `#` 转义成 %23，于是路径变成 /ui%23g=... → 404。
    真机上点一次才现形，浏览器里永远测不出来。"""
    from responder.lead import format_notification

    g = GroupProfile(group_id="kf:wk1:wmAbc")
    row = {"priority": "P1", "case_type": "交通事故", "urgency": "medium",
           "summary": "咨询交通事故", "contact": "13800000000",
           "key_facts": "[]", "factors": "[]", "intent": "warm"}
    text = format_notification(row, g, Settings(public_base_url="https://x.example.com"))
    assert "/ui#g=" not in text
    assert "https://x.example.com/g/kf%3Awk1%3AwmAbc" in text


def test_deeplink_route_serves_the_console():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from responder.console.api import ui_router

    app = FastAPI()
    app.include_router(ui_router)
    r = TestClient(app).get("/g/kf%3Awk1%3AwmAbc")
    assert r.status_code == 200
    assert "__deepLinkGroup" in r.text and "kf:wk1:wmAbc" in r.text


def test_deeplink_cannot_inject_script_through_the_group_id():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from responder.console.api import ui_router

    app = FastAPI()
    app.include_router(ui_router)
    r = TestClient(app).get("/g/" + '</script><script>alert(1)</script>')
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text


# ------------------------------------------------------ 六、持续作答（首轮筛查）
def in_consult(text):
    return rules.classify(text, is_one_on_one=True, in_consultation=True)


@pytest.mark.parametrize("text", [
    "那我该准备什么材料",
    "如果对方不赔怎么办",
    "责任认定书下来了能改吗",
    "调解不成还能怎么走",
    "这个要多久才有结果",
])
def test_follow_up_questions_keep_getting_answered(text):
    """筛查的主体是追问，而**追问几乎从不重复话题词**：客户开头说了
    「交通事故」，后面问的每一句都不含法律词，于是全部落进默认沉默。
    可这些正是最该答的——客户每答一句，交给客服的那张单就厚一分。"""
    action, category, _, reasons = in_consult(text)
    assert action is Action.ANSWER, f"{text} 落到了 {reasons}"
    assert category is Category.GENERAL_LAW


@pytest.mark.parametrize("text,expect_urgent", [
    ("我被拘留了怎么办", True),
    ("明天就开庭了怎么办", True),
])
def test_urgent_still_wins_inside_a_consultation(text, expect_urgent):
    """放宽的是「答不答」，不是「什么都能答」。紧急情形一律先安抚 + 叫人。"""
    action, category, urgent, _ = in_consult(text)
    assert (action, urgent) == (Action.HANDOFF, expect_urgent)
    assert category is Category.URGENT


@pytest.mark.parametrize("text", ["你们律师费多少", "这个案子代理费怎么算"])
def test_fees_are_still_never_answered_inside_a_consultation(text):
    """AI 绝不报价，这条护栏不因为「要多聊」而松动。"""
    action, category, _, _ = in_consult(text)
    assert (action, category) == (Action.HANDOFF, Category.FEE)


def test_live_case_progress_is_still_handed_off():
    """「我的案子什么时候出结果」——我们真的不知道，编一个比不答更糟。"""
    action, category, _, _ = in_consult("我的案子什么时候能出结果")
    assert (action, category) == (Action.HANDOFF, Category.CASE_STATUS)


def test_chitchat_is_still_silence_inside_a_consultation():
    for text in ("谢谢", "好的", "嗯嗯"):
        assert in_consult(text)[0] is Action.SILENCE, text


def test_consultation_mode_needs_the_customer_to_have_said_something():
    """只打了个招呼就放宽，等于对着一句「在吗」大谈法律——
    那不是热情，是没听懂。"""
    import tempfile

    from responder.config import Settings
    from responder.service import Pipeline
    from responder.store.db import Store

    s = Settings(db_path=tempfile.mktemp(suffix=".db"))
    p = Pipeline(Store(s.db_path), None, s)
    g = GroupProfile(group_id="kf:a:b", kf_open_kfid="a", kf_external_userid="b",
                     client_status=ClientStatus.PROSPECT)
    assert p._in_consultation(g, [{"content": "你好", "sender_is_staff": False}]) is False
    assert p._in_consultation(
        g, [{"content": "我出了交通事故，对方全责但不肯赔", "sender_is_staff": False}]
    ) is True


def test_signed_clients_do_not_get_the_relaxed_mode():
    """已委托客户由律师全程跟，AI 不该替他答本案。"""
    import tempfile

    from responder.config import Settings
    from responder.service import Pipeline
    from responder.store.db import Store

    s = Settings(db_path=tempfile.mktemp(suffix=".db"))
    p = Pipeline(Store(s.db_path), None, s)
    g = GroupProfile(group_id="kf:a:b", kf_open_kfid="a", kf_external_userid="b",
                     client_status=ClientStatus.SIGNED)
    assert p._in_consultation(
        g, [{"content": "我出了交通事故，对方全责但不肯赔", "sender_is_staff": False}]
    ) is False


# ------------------------------------------------------ 七、回一句就接管
def test_replying_in_the_window_is_the_takeover():
    """律所方要的「在企微里回一个字就接管」：客服本来就要打字，
    那句话本身就是接管动作——不用开控制台、不用点按钮。"""
    import tempfile

    from responder.config import Settings
    from responder.models import IncomingMessage
    from responder.service import Pipeline
    from responder.store.db import Store

    s = Settings(db_path=tempfile.mktemp(suffix=".db"), mode="live")
    store = Store(s.db_path)
    gid = "kf:wk:c1"
    store.upsert_group(GroupProfile(
        group_id=gid, kf_open_kfid="wk", kf_external_userid="c1",
        client_status=ClientStatus.PROSPECT,
    ))
    p = Pipeline(store, None, s)
    p.handle(IncomingMessage(msg_id="s1", group_id=gid, sender_id="wei",
                             sender_is_staff=True, content="我来跟您说"))
    assert store.get_group(gid).handoff_userid == "wei"


def test_a_lawyer_speaking_in_a_group_does_not_mark_it_handed_off():
    """群聊里律师发言是常态，把群标成「已转接」会让 AI 从此在群里失声。"""
    import tempfile

    from responder.config import Settings
    from responder.models import IncomingMessage
    from responder.service import Pipeline
    from responder.store.db import Store

    s = Settings(db_path=tempfile.mktemp(suffix=".db"), mode="live")
    store = Store(s.db_path)
    store.upsert_group(GroupProfile(group_id="g1", name="客户群"))
    p = Pipeline(store, None, s)
    p.handle(IncomingMessage(msg_id="s1", group_id="g1", sender_id="wei",
                             sender_is_staff=True, content="我看一下"))
    assert store.get_group("g1").handoff_userid == ""

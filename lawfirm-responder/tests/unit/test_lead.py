"""线索简报：信号识别 → 结构化交接单 → 按意向推送接待人。

不出网：LLM 未配置（conftest 已剥离 key），全部走确定性降级路径，
因此断言聚焦于「联系方式提取、意向分级、推送节流、内容不编造」。
"""

import json

from responder import lead as lead_mod
from responder.config import Settings
from responder.engine import signals
from responder.models import ClientStatus, GroupProfile, IncomingMessage
from responder.service import Pipeline
from responder.store.db import Store


class RecordingSender:
    def __init__(self):
        self.direct: list[tuple[str, str]] = []

    @property
    def leads(self) -> list[tuple[str, str]]:
        """只看线索交接单（首行为优先级层级），滤掉常规的待跟进提醒。"""
        return [x for x in self.direct if "客户诉求：" in x[1]]

    def send_direct_text(self, userid, text):
        self.direct.append((userid, text))
        return True

    def send_robot_text(self, webhook, text):
        return True

    def send_group_text(self, chat_id, text):
        return True


# ---------------------------------------------------------------- 信号
def test_extract_contact_handles_separators():
    assert signals.extract_contact("我的电话是17721275495") == "17721275495"
    assert signals.extract_contact("电话 177 2127 5495") == "17721275495"
    assert signals.extract_contact("手机177-2127-5495可以打") == "17721275495"
    assert signals.extract_contact("座机021-51697771") == "02151697771"
    assert signals.extract_contact("赔偿了17721块钱") == ""  # 不是手机号


def test_intent_levels():
    assert signals.detect("可以的呀我的电话是17721275495")[0] == signals.HOT
    assert signals.detect("什么时候方便见面聊聊")[0] == signals.HOT
    assert signals.detect("我想委托你们代理")[0] == signals.HOT
    assert signals.detect("你们收费怎么算")[0] == signals.WARM
    assert signals.detect("拖欠工资多久可以仲裁")[0] == signals.COLD


def test_rank_takes_highest():
    assert signals.rank("cold", "warm", "hot") == "hot"
    assert signals.rank("cold", "warm") == "warm"
    assert signals.rank("cold") == "cold"


# ---------------------------------------------------------------- 简报
def make(tmp_path, mode="live"):
    db = str(tmp_path / "lead.db")
    store = Store(db)
    store.upsert_group(GroupProfile(
        group_id="kf:acct:cust", name="微信客服 · 客户abc",
        client_status=ClientStatus.PROSPECT, case_type="劳动仲裁",
        lawyer_name="魏", lawyer_userid="weilai",
        kf_open_kfid="acct", kf_external_userid="cust",
    ))
    settings = Settings(mode=mode, db_path=db, split_delay_seconds=0)
    sender = RecordingSender()
    return store, sender, Pipeline(store, sender, settings)


def _msg(content, mid):
    return IncomingMessage(
        msg_id=mid, group_id="kf:acct:cust", sender_id="cust", content=content
    )


def test_contact_message_creates_and_notifies_lead(tmp_path):
    """客户留电话 → 生成线索、标记高意向、把交接单推给接待人。"""
    store, sender, p = make(tmp_path)
    p.handle(_msg("试用期被辞退有补偿吗？", "m1"))
    assert store.get_lead("kf:acct:cust") is None  # 普通咨询不建线索

    p.handle(_msg("可以的呀我的电话是17721275495", "m2"))
    lead = store.get_lead("kf:acct:cust")
    assert lead and lead["intent"] == "hot"
    assert lead["contact"] == "17721275495"
    assert lead["status"] == "new"

    assert sender.leads, "高意向线索应推送接待人"
    to, text = sender.leads[-1]
    assert to == "weilai"
    # 只留了电话 = 40 分，还够不到 P0 的 60 分线，所以标「弱意愿」。
    # 这条阈值是否合适见 docs/lead-routing.md——留电话对律所是不是强信号，
    # 属判断阈值，须律所方确认后再动。
    assert text.startswith("【弱意愿】") and "17721275495" in text
    # 降级路径下摘要取客户原话，绝不编造
    assert "试用期被辞退有补偿吗？" in text or "电话是17721275495" in text


def test_no_duplicate_notification(tmp_path):
    """同一客户再次发消息不重复打扰，除非意向升级。"""
    store, sender, p = make(tmp_path)
    p.handle(_msg("你们收费怎么算", "w1"))  # warm → 通知一次
    n1 = len(sender.leads)
    assert n1 == 1
    p.handle(_msg("那大概什么标准呢", "w2"))  # 仍是 warm 上下文，不再通知
    assert len(sender.leads) == n1

    p.handle(_msg("我电话17721275495你们打给我", "h1"))  # 升级为 hot → 再通知
    assert len(sender.leads) == n1 + 1
    assert "已留电话" in sender.leads[-1][1]  # 升级依据可见


def test_cold_message_never_notifies(tmp_path):
    store, sender, p = make(tmp_path)
    p.handle(_msg("拖欠工资多久可以申请劳动仲裁？", "c1"))
    assert sender.leads == []


def test_shadow_mode_stores_but_not_sends(tmp_path):
    """影子模式：线索照常入库供复盘，但不推送。"""
    store, sender, p = make(tmp_path, mode="shadow")
    p.handle(_msg("我的电话是17721275495", "s1"))
    assert store.get_lead("kf:acct:cust")["intent"] == "hot"
    assert sender.direct == []  # 影子模式下线索与提醒一律不外发


def test_lead_listing_sorted_by_intent(tmp_path):
    store, _, _ = make(tmp_path)
    for gid, intent in [("g-cold", "cold"), ("g-hot", "hot"), ("g-warm", "warm")]:
        store.upsert_lead(gid, {"intent": intent, "summary": gid})
    assert [x["intent"] for x in store.list_leads()] == ["hot", "warm", "cold"]


def test_lead_status_flow(tmp_path):
    store, _, _ = make(tmp_path)
    store.upsert_lead("g1", {"intent": "hot", "summary": "s"})
    lid = store.get_lead("g1")["id"]
    store.set_lead_status(lid, "contacted")
    assert store.get_lead("g1")["status"] == "contacted"
    assert store.list_leads(status="new") == []


def test_notification_without_contact_says_so(tmp_path):
    """没留电话的线索也要能派发，且明确写出「未留」，不能留空让律师猜。"""
    store, sender, p = make(tmp_path)
    p.handle(_msg("我想约个时间当面聊聊", "a1"))
    text = sender.leads[-1][1]
    assert "客户未留" in text


def test_session_split_ignores_earlier_consultation(tmp_path):
    """隔了几小时的另一次咨询不并入本次交接单，否则摘要串味、律师无从下手。"""
    from datetime import datetime, timedelta

    now = datetime.now()
    history = [
        {"sender_is_staff": False, "content": "上次问的拖欠工资仲裁",
         "created_at": (now - timedelta(hours=5)).isoformat()},
        {"sender_is_staff": False, "content": "我借款家人3000不还能起诉吗",
         "created_at": (now - timedelta(minutes=3)).isoformat()},
        {"sender_is_staff": False, "content": "我的电话是17721275495",
         "created_at": now.isoformat()},
    ]
    kept = lead_mod.current_session(history, gap_seconds=7200)
    assert len(kept) == 2
    assert "拖欠工资" not in "".join(m["content"] for m in kept)


def test_session_split_keeps_continuous_conversation(tmp_path):
    from datetime import datetime, timedelta

    now = datetime.now()
    history = [
        {"sender_is_staff": False, "content": "a",
         "created_at": (now - timedelta(minutes=20)).isoformat()},
        {"sender_is_staff": False, "content": "b",
         "created_at": (now - timedelta(minutes=10)).isoformat()},
        {"sender_is_staff": False, "content": "c", "created_at": now.isoformat()},
    ]
    assert len(lead_mod.current_session(history, gap_seconds=7200)) == 3


def test_fallback_summary_uses_client_words(tmp_path):
    store, _, _ = make(tmp_path)
    group = store.get_group("kf:acct:cust")
    history = [
        {"sender_is_staff": False, "content": "短"},
        {"sender_is_staff": False, "content": "公司拖欠我三个月工资一直不发"},
        {"sender_is_staff": True, "content": "客服的话不应被当成客户诉求"},
    ]
    lead = lead_mod.build_and_store(store, group, history)
    assert lead["summary"] == "公司拖欠我三个月工资一直不发"
    assert json.loads(lead["key_facts"]) == []  # 无模型时不臆造事实


def test_brief_ends_with_a_tappable_link_not_a_dead_end():
    """交接单末尾要给可点的深链，不是一句「请去控制台看」。

    原文案是死路：律师得开浏览器、找地址、登录、翻列表、找到人，五步，
    所以他不会做。带 group_id 的深链让他一步直达那通对话。
    """
    from responder.config import Settings
    from responder.lead import format_notification
    from responder.models import GroupProfile

    g = GroupProfile(group_id="kf:wk1:wmAbc", name="微信客服 · 客户Abc")
    row = {"priority": "P0", "case_type": "劳动仲裁", "urgency": "high",
           "summary": "被拖欠三个月工资", "contact": "13812345678",
           "key_facts": "[]", "factors": "[]", "intent": "hot"}

    text = format_notification(row, g, Settings(public_base_url="https://ai.example.com"))
    assert "https://ai.example.com/g/kf%3Awk1%3AwmAbc" in text, "冒号必须转义，否则链接断在中间"
    assert "见控制台" not in text


def test_brief_falls_back_when_no_public_url():
    """没配对外地址（本机开发/域名未定）时退回原文案，不给半截链接。"""
    from responder.config import Settings
    from responder.lead import format_notification
    from responder.models import GroupProfile

    g = GroupProfile(group_id="g1", name="劳动仲裁咨询群")
    row = {"priority": "P1", "case_type": "劳动仲裁", "urgency": "medium",
           "summary": "咨询仲裁流程", "contact": "", "key_facts": "[]",
           "factors": "[]", "intent": "warm"}
    text = format_notification(row, g, Settings(public_base_url=""))
    assert "http" not in text
    assert "劳动仲裁咨询群" in text


# ---------------------------------------------- 全量推送：不能躺死在对话里
# 业务决策 2026-08，律所方原话：「我们有很多的客服，全部都得推给客服，
# 不能躺死在对话里」。旧口径把冷线索留在库里不打扰人，前提是人手紧张——
# 律所侧不是这个前提，那条节流就从「保护」变成了「丢单」。
def test_cold_lead_is_pushed_too_when_manpower_allows(tmp_path):
    from responder import lead as lead_mod

    store, settings, sender, group = _cold_env(tmp_path, notify_all_leads=True)
    row = lead_mod.dispatch(store, group, _hist("你们几点上班"), sender, settings=settings)
    assert row is not None
    assert sender.direct, "冷线索也要有人接手，不能只归档"


def test_cold_lead_stays_archived_when_the_switch_is_off(tmp_path):
    """旧口径保留：人手紧张的所可以关掉，只推有意向的。"""
    from responder import lead as lead_mod

    store, settings, sender, group = _cold_env(tmp_path, notify_all_leads=False)
    lead_mod.dispatch(store, group, _hist("你们几点上班"), sender, settings=settings)
    assert not sender.direct


def test_full_push_does_not_mean_one_brief_per_message(tmp_path):
    """只放开门槛，不放开频次——一通对话仍然只推一张单。"""
    from responder import lead as lead_mod

    store, settings, sender, group = _cold_env(tmp_path, notify_all_leads=True)
    for _ in range(3):
        lead_mod.dispatch(store, group, _hist("你们几点上班"), sender, settings=settings)
    assert len(sender.direct) == 1


def _cold_env(tmp_path, **over):
    from responder.config import Settings
    from responder.models import ClientStatus, GroupProfile
    from responder.store.db import Store

    db = str(tmp_path / "cold.db")
    cfg = dict(db_path=db, lead_brief_enabled=True, default_notify_userid="wei",
               llm_refine_enabled=False)
    cfg.update(over)
    settings = Settings(**cfg)

    class Snd:
        def __init__(self):
            self.direct = []

        def send_direct_text(self, userid, text):
            self.direct.append((userid, text))
            return True

    group = GroupProfile(group_id="kf:wk:cold", client_status=ClientStatus.PROSPECT,
                         kf_open_kfid="wk", kf_external_userid="cold")
    return Store(db), settings, Snd(), group


def _hist(text):
    return [{"content": text, "sender_is_staff": False,
             "created_at": "2026-08-06T10:00:00"}]

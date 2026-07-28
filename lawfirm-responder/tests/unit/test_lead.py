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
        """只看线索交接单，滤掉常规的待跟进提醒。"""
        return [x for x in self.direct if "线索】" in x[1]]

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
    assert "高意向线索" in text and "17721275495" in text
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
    assert "高意向" in sender.leads[-1][1]


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

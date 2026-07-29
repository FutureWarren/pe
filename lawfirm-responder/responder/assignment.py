"""自动派单：筛查完的客户分给具体律师，而不是堆在一个接待人手里。

规则顺序（docs/lead-routing.md 有面向业务的完整表述）：

1. **粘性**——这单已经派过且该律师仍在职：不换人。客户第二次进线换个律师接，
   前面聊的背景全部作废，这是体验事故不是负载均衡。
2. **专长匹配**——线索的案件类型与律师专长领域互相包含即命中（「劳动仲裁」匹配
   「劳动仲裁、工伤」）；无人匹配时退回全体在班律师，宁可派错专长也不能没人管。
3. **负载均衡**——在办线索最少者优先；平局时最久没接单的先接（轮转）。
4. **名册为空 = 功能未启用**——完全回落旧行为（客服接待人/全局兜底），
   保证升级部署零配置不炸；名册一旦有人，新线索自动开始走派单。

派单同时回写会话档案的承办律师（姓名 + userid）：后续的紧急提醒、话术点名
（「我帮您叫X律师」）、人工接管判定全都跟着换人，不留两套指向。
"""

import logging

from responder.config import Settings, get_settings
from responder.models import GroupProfile
from responder.store.db import Store

logger = logging.getLogger(__name__)


def _split_specialties(raw: str) -> list[str]:
    out = []
    for part in (raw or "").replace("，", ",").replace("、", ",").split(","):
        part = part.strip()
        if part:
            out.append(part)
    return out


def _matches(case_type: str, specialties: str) -> bool:
    if not case_type:
        return False
    for s in _split_specialties(specialties):
        if s in case_type or case_type in s:
            return True
    return False


def pick(store: Store, case_type: str) -> dict | None:
    """按专长 + 负载从名册里挑一位。名册为空/无人在班返回 None。"""
    roster = [law for law in store.list_lawyers(active_only=True) if law["on_duty"]]
    if not roster:
        return None
    matched = [law for law in roster if _matches(case_type, law["specialties"])]
    pool = matched or roster
    load = store.lawyer_load()
    return min(
        pool,
        key=lambda law: (
            load.get(law["userid"], {}).get("open", 0),
            law["last_assigned_at"] or "",  # 没接过单排最前（ISO 串可直接比较）
        ),
    )


def ensure(
    store: Store, group: GroupProfile, lead: dict,
    settings: Settings | None = None,
) -> str:
    """确保线索有指派对象，返回通知目标 userid（可能为空 = 无人可通知）。

    有名册走派单；名册为空回落旧链路（会话档案承办人 → 全局兜底）。
    """
    settings = settings or get_settings()
    legacy = group.lawyer_userid or settings.default_notify_userid

    current = lead.get("assigned_userid") or ""
    if current:
        law = store.get_lawyer(current)
        if law and law["active"]:
            return current  # 粘性：已派且在职
        logger.info("assignee %s inactive, rerouting %s", current, group.group_id)

    chosen = pick(store, lead.get("case_type") or group.case_type)
    if chosen is None:
        return legacy

    assign(store, group, lead["group_id"], chosen)
    return chosen["userid"]


def assign(store: Store, group: GroupProfile, group_id: str, lawyer: dict) -> None:
    """落库：线索指派 + 会话档案承办律师同步 + 轮转时间戳。"""
    store.assign_lead(group_id, lawyer["userid"])
    store.touch_lawyer_assigned(lawyer["userid"])
    if (
        group.lawyer_userid != lawyer["userid"]
        or group.lawyer_name != lawyer["name"]
    ):
        group.lawyer_userid = lawyer["userid"]
        group.lawyer_name = lawyer["name"]
        store.upsert_group(group)
    logger.info("lead %s assigned to %s", group_id, lawyer["userid"])

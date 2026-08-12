"""自动派单：筛查完的客户分给具体律师，而不是堆在一个接待人手里。

规则顺序（docs/lead-routing.md 有面向业务的完整表述）：

1. **粘性**——这单已经派过且该律师仍在职：不换人。客户第二次进线换个律师接，
   前面聊的背景全部作废，这是体验事故不是负载均衡。
2. **负载均衡**——在办线索最少者优先；平局时最久没接单的先接（轮转）。
   **不按专长匹配**（2026-08-12，律所方：「客服不分什么专长不专长」）。
   这一层原本按「案件类型 ⊇ 专长领域」挑人，但律所的实际组织是：
   接单的人就是客服，谁有空谁接，案子定下来再由所里内部分。
   多一个匹配维度，换来的只是「专长填错就派错人」这一类新的静默失败。
3. **名册为空 = 功能未启用**——完全回落旧行为（客服接待人/全局兜底），
   保证升级部署零配置不炸；名册一旦有人，新线索自动开始走派单。

派单同时回写会话档案的承办律师（姓名 + userid）：后续的紧急提醒、话术点名
（「我帮您叫X律师」）、人工接管判定全都跟着换人，不留两套指向。
"""

import logging

from responder.config import Settings, get_settings
from responder.models import ClientStatus, GroupProfile
from responder.store.db import Store

logger = logging.getLogger(__name__)


def pick(store: Store) -> dict | None:
    """按负载从名册里挑一位。名册为空/无人在班返回 None。

    刻意不看案件类型：律所方 2026-08-12「客服不分什么专长不专长」——
    接单的人就是客服，谁有空谁接；案子定下来之后由所里内部分。
    """
    roster = [law for law in store.list_lawyers(active_only=True) if law["on_duty"]]
    if not roster:
        return None
    load = store.lawyer_load()
    return min(
        roster,
        key=lambda law: (
            load.get(law["userid"], {}).get("open", 0),
            law["last_assigned_at"] or "",  # 没接过单排最前（ISO 串可直接比较）
        ),
    )


def ensure(
    store: Store, group: GroupProfile, lead: dict,
    settings: Settings | None = None,
) -> tuple[str, bool]:
    """确保线索有指派对象。返回 (通知目标 userid, 是否本轮新指派)。

    「是否新指派」供上层决定要不要强推交接单——刚接手的律师必须收到单子，
    不能因为这条线索之前通知过就被节流掉。

    有名册走派单；名册为空回落旧链路（会话档案承办人 → 全局兜底）。
    """
    settings = settings or get_settings()

    def _legacy() -> str:
        """回落目标必须校验在职：把单子推给一个已停用的人等于没推。"""
        if group.lawyer_userid:
            law = store.get_lawyer(group.lawyer_userid)
            if law is None or law["active"]:  # 不在名册里＝人工配的固定接待人，照用
                return group.lawyer_userid
        return settings.default_notify_userid

    # 已成交客户的服务群有固定承办律师，自动派单一律不碰——
    # 改派会让 AI 在群里点名一个客户从没见过的人。
    if group.client_status == ClientStatus.SIGNED and group.lawyer_userid:
        return _legacy(), False

    current = lead.get("assigned_userid") or ""
    if current:
        law = store.get_lawyer(current)
        if law and law["active"]:
            return current, False  # 粘性：已派且在职
        logger.info("assignee %s inactive, rerouting %s", current, group.group_id)

    # 跨渠道认人：同一手机号在别的通道已有承办律师就沿用他
    # （客户先在抖音留号、后扫码进微信客服，是同一个人，不能两位律师各打一遍）
    contact = lead.get("contact") or ""
    twin = store.find_lead_by_contact(contact, exclude_group=lead["group_id"])
    if twin:
        law = store.get_lawyer(twin["assigned_userid"])
        if law and law["active"] and law["on_duty"]:
            assign(store, group, lead["group_id"], law)
            logger.info("lead %s follows same-contact owner %s",
                        lead["group_id"], law["userid"])
            return law["userid"], True

    chosen = pick(store)
    if chosen is None:
        # 名册为空/无人在班：保持未指派，让管理员看板的「未指派」接住，
        # 而不是伪装成已有归属
        return _legacy(), False

    assign(store, group, lead["group_id"], chosen)
    return chosen["userid"], True


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

"""客资导入：把「抖音来客 → 线索经营 → 客资中心」导出的表接进分案闭环。

**为什么先做导入而不是等 API**：抖音私信接口要走开发者审核（周级），而客资导出
今天就能下载。先用导入把线索接进评分与派单，API 通了再换实时通道——两条路进来的
线索共用同一套线索模型，切换时下游（评分/派单/工作台/督办）一行都不用改。

字段映射按真实导出文件（2026-07 抖音来客，17 列）确定，要点：

- **表头模糊匹配**：平台列名不统一且会改版，写死列序必然过期；匹配不到就留空，不猜。
  但别名要足够精确——`类型` 这种宽泛词会误命中「流量类型/商品类型」，把「自然流量」
  当成案件类型写进档案，交接单上的案由就全乱了。
- **案件类型从商品名里取**：律所的团购商品名自带方括号（`【婚姻家事】律师一对一…`），
  这是导出表里唯一可靠的案由来源。
- **线索阶段决定状态**：已经「已加微信/待再次沟通」的客户不能再回到待跟进队列里
  被重新打一遍电话——重复打扰比漏打更伤客户。
- **保留原有归属**：导出表里的「跟进员工」若能在名册里对上人，就派给他本人，
  不让导入把已有的对接关系打乱。
- **手机号是去重键**：同一个号反复导入只更新不新增；打不通的号（空/打码）不入队，
  但要报数——静默丢弃比漏掉更糟。
- **批量导入默认不推送**：一次导进几百条历史客资还挨个 @ 律师是骚扰不是效率。
"""

import csv
import io
import logging
import re
from datetime import datetime

from responder import lead as lead_mod
from responder.config import Settings, get_settings
from responder.engine import signals
from responder.models import ClientStatus, GroupProfile, IncomingMessage
from responder.store.db import Store

logger = logging.getLogger(__name__)

# 单值字段的表头别名：表头包含任一别名即命中，每列只用一次，先到先得。
_ALIASES: dict[str, tuple[str, ...]] = {
    "contact": ("手机", "电话", "联系方式", "号码"),
    "name": ("姓名", "昵称", "客户名", "用户名"),
    "created_at": ("创建时间", "留资时间", "时间", "日期"),
    "stage": ("线索阶段", "阶段", "跟进状态", "状态"),
    # 标签常与阶段不一致（阶段还写「新线索」，标签已经是「已加微信」），
    # 判断是否接触过要两者取并——重复打扰比漏打更伤客户
    "tags": ("线索标签", "标签"),
    "owner": ("跟进员工", "跟进人", "负责人", "归属"),
    "product": ("商品名称", "商品", "套餐"),
    # 刻意不含裸「类型」，也不含「品类」——前者误吃「流量类型/营销类型」，
    # 后者是「商品类型」的子串，会把「团购套餐」当成案由写进档案
    "case_type": ("案件类型", "业务类型", "咨询类型", "案由", "案件"),
}

# 客户侧描述：进线来源与诉求。这些进客户消息，参与评分。
_CUSTOMER_HEADERS = (
    "搜索关键词", "最近留资记录", "商品名称",
    "需求", "咨询", "备注", "描述", "内容", "问题",
)
# 员工侧记录：跟进状态与备注。**必须单独存为员工发言**，否则「已加微信」这种
# 我方操作记录会被信号识别当成「客户要加微信详聊」白加 20 分，评分整体虚高。
_STAFF_HEADERS = (
    "线索阶段", "线索标签", "最新跟进记录", "智能意向", "跟进员工", "跟进人",
    # 互动场景是平台状态列（直播/短视频/其它），不是客户说的话。留在客户侧会让
    # 「预约」「电话联系」这类平台样板词命中面谈/要电话信号，成批虚推进 P0。
    "互动场景",
)

# 商品名里的方括号即案由：【婚姻家事】律师一对一30分钟免费法律咨询
_BRACKET = re.compile(r"[【\[]([^】\]]{2,12})[】\]]")
# 平台在姓名列的占位符，等同于没填
_PLACEHOLDER_NAMES = ("未命名", "未知", "匿名", "-", "无")

# 线索阶段 → 我们的线索状态。已接触过的不回待跟进队列，避免二次打扰。
_INVALID_HINTS = ("无效", "空号", "骚扰", "重复", "错号")
_CONVERTED_HINTS = ("到店", "已成交", "成交", "已签约", "已转化")
_NEW_HINTS = ("新线索", "待分配", "未跟进")

_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M", "%Y-%m-%d", "%Y/%m/%d",
)


def detect_columns(headers: list[str]) -> dict[str, int]:
    """表头 → 单值字段下标。识别不出的字段直接缺席，调用方按缺席处理。"""
    mapping: dict[str, int] = {}
    used: set[int] = set()
    for field, aliases in _ALIASES.items():
        for i, raw in enumerate(headers):
            if i in used:
                continue
            if any(a in (raw or "") for a in aliases):
                mapping[field] = i
                used.add(i)
                break
    return mapping


def _match_headers(headers: list[str], aliases: tuple[str, ...]) -> list[int]:
    return [i for i, raw in enumerate(headers) if any(a in (raw or "") for a in aliases)]


def detect_customer_columns(headers: list[str]) -> list[int]:
    return _match_headers(headers, _CUSTOMER_HEADERS)


def detect_staff_columns(headers: list[str]) -> list[int]:
    return _match_headers(headers, _STAFF_HEADERS)


def parse_table(data: bytes, filename: str = "") -> list[list[str]]:
    """CSV / XLSX → 行列表（已剔除全空行）。"""
    if filename.lower().endswith((".xlsx", ".xlsm")):
        return _parse_xlsx(data)
    return _parse_csv(data)


def _parse_csv(data: bytes) -> list[list[str]]:
    # 国内平台导出常用 GBK，utf-8 解不开时按序回落，全失败才报错
    text = None
    for enc in ("utf-8-sig", "gbk", "utf-8"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("无法识别文件编码，请另存为 UTF-8 或 GBK 的 CSV 后重试")
    return [
        [c.strip() for c in row]
        for row in csv.reader(io.StringIO(text))
        if any(c.strip() for c in row)
    ]


def _parse_xlsx(data: bytes) -> list[list[str]]:
    try:
        import openpyxl
    except ImportError as e:  # 依赖缺失时给出可执行的出路，而不是抛栈
        raise ValueError("服务器暂不支持 xlsx，请在表格软件里另存为 CSV 后上传") from e
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        ws = wb.active
        # 抖音导出把 <dimension> 写成 A1，只读模式会据此只吐第一格 —— 必须重算，
        # 否则 400 多行的表会被当成「只有表头一个字」而报「找不到手机号列」
        ws.reset_dimensions()
        rows = []
        for raw in ws.iter_rows(values_only=True):
            cells = ["" if c is None else str(c).strip() for c in raw]
            if any(cells):
                rows.append(cells)
        return rows
    finally:
        wb.close()


def _parse_dt(text: str) -> datetime | None:
    text = (text or "").strip()
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _stage_to_status(stage: str, tags: str = "") -> str:
    """线索阶段 + 标签 → 我们的线索状态。

    只要任一处显示接触过（已加微信/待复访/养熟…）就算 contacted，不回待跟进队列。
    """
    text = f"{stage or ''} {tags or ''}".strip()
    if any(h in text for h in _INVALID_HINTS):
        return "invalid"
    if any(h in text for h in _CONVERTED_HINTS):
        return "converted"  # 到店/已成交：进漏斗的成交口径，不再是待跟进
    if not text:
        return "new"
    meaningful = [
        part for part in text.split()
        if part and not any(h in part for h in _NEW_HINTS)
    ]
    return "contacted" if meaningful else "new"


def _match_lawyer(roster: list[dict], name: str) -> dict | None:
    """按姓名把导出表里的跟进员工对到名册（「陈丽娟1」应当对上「陈丽娟」）。

    单字姓名只接受完全相同——「魏」前缀匹配会同时命中魏来和魏谦，宁可不派。
    """
    name = (name or "").strip()
    if not name:
        return None
    for law in roster:
        n = (law.get("name") or "").strip()
        if not n:
            continue
        if n == name or (len(n) >= 2 and name.startswith(n)):
            return law
    return None


def import_leads(
    store: Store,
    table: list[list[str]],
    *,
    settings: Settings | None = None,
    sender=None,
    notify: bool = False,
    source: str = "抖音",
) -> dict:
    """把解析好的表导入为线索（评分 + 派单）。返回逐项计数，不静默丢行。

    sender 为 None 或 notify=False 时只入库不推送——派单照常发生，
    律师在工作台里看得到，只是不弹企微消息。
    """
    settings = settings or get_settings()
    if not table:
        return {"rows": 0, "imported": 0, "updated": 0, "no_contact": 0}

    headers = table[0]
    cols = detect_columns(headers)
    if "contact" not in cols:
        raise ValueError(
            "表里找不到手机号列。请确认导出文件包含「手机号 / 电话 / 联系方式」列"
        )
    customer_cols = detect_customer_columns(headers)
    staff_cols = detect_staff_columns(headers)
    roster = store.list_lawyers(active_only=True)

    def cell(row: list[str], field: str) -> str:
        i = cols.get(field, -1)
        return row[i].strip() if 0 <= i < len(row) else ""

    def gather(row: list[str], idx: list[int]) -> list[str]:
        return [row[i].strip() for i in idx if i < len(row) and row[i].strip()]

    imported = updated = no_contact = 0
    by_status: dict[str, int] = {}
    for row in table[1:]:
        contact = signals.extract_contact(cell(row, "contact"))
        if not contact:
            # 空号列或导出打码（138****1234）：打不通的线索不进队列，但要报数
            no_contact += 1
            continue

        raw_name = cell(row, "name")
        name = "" if raw_name in _PLACEHOLDER_NAMES else raw_name
        product = cell(row, "product")
        case_type = cell(row, "case_type")
        if not case_type and product:
            m = _BRACKET.search(product)
            case_type = m.group(1) if m else ""

        group_id = f"dy:{contact}"
        if store.get_group(group_id) is None:
            store.upsert_group(
                GroupProfile(
                    group_id=group_id,
                    name=f"{source} · {name or contact}",
                    client_status=ClientStatus.PROSPECT,  # 客资一律按新咨询
                    case_type=case_type,
                )
            )

        created = _parse_dt(cell(row, "created_at")) or datetime.now()
        # 客户侧：进线来源与诉求，单看哪一列都不够，多列拼成一句
        bits = gather(row, customer_cols)
        bits.append(f"（{source}留资，手机号 {contact}）")  # 号码进正文，评分才计「已留电话」
        fresh = store.upsert_message(
            IncomingMessage(
                msg_id=f"dy-{contact}",  # 手机号去重：重复导入只更新不新增
                group_id=group_id,
                sender_id=contact,
                content=" · ".join(bits),
                created_at=created,
            )
        )
        # 员工侧：跟进记录单独存为员工发言。信号识别与评分都会跳过员工消息，
        # 「已加微信」这种我方操作记录才不会被当成客户意愿虚增分数。
        notes = gather(row, staff_cols)
        owner_name = cell(row, "owner")
        if notes:
            store.upsert_message(
                IncomingMessage(
                    msg_id=f"dy-{contact}-note",
                    group_id=group_id,
                    sender_id=owner_name or source,
                    sender_is_staff=True,
                    content=f"{source}跟进记录：" + " · ".join(notes),
                    created_at=created,
                )
            )
        group = store.get_group(group_id)
        try:
            lead_row = lead_mod.dispatch(
                store, group,
                store.recent_messages(group_id, settings.lead_history_window),
                sender if notify else None,
                settings=settings,
                # 不调模型：几百条逐条归纳要十几分钟（请求必然超时），
                # 何况平台导出的描述本就比模型转述更如实
                summarize=False,
            )
        except Exception:
            logger.exception("客资导入失败: %s", group_id)
            continue

        if lead_row:
            # 平台上已经接触过的客户不该回到待跟进队列被重新打一遍电话
            status = _stage_to_status(cell(row, "stage"), cell(row, "tags"))
            if status != "new":
                store.set_lead_status(lead_row["id"], status)
            by_status[status] = by_status.get(status, 0) + 1
            # 平台上已有跟进人且名册里对得上 → 保留原归属，不让导入打乱对接关系
            law = _match_lawyer(roster, owner_name)
            if law is not None:
                from responder import assignment

                assignment.assign(store, group, group_id, law)
        if fresh:
            imported += 1
        else:
            updated += 1
    return {
        "rows": len(table) - 1,
        "imported": imported,
        "updated": updated,
        "no_contact": no_contact,
        "by_status": by_status,
        "columns": sorted(cols),
    }

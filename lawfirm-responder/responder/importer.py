"""客资导入：把「抖音来客 → 线索经营 → 客资中心」导出的表接进分案闭环。

**为什么先做导入而不是等 API**：抖音私信接口要走开发者审核（周级），而客资导出
今天就能下载。先用导入把线索接进评分与派单，API 通了再换实时通道——两条路进来的
线索共用同一套线索模型，切换时下游（评分/派单/工作台/督办）一行都不用改。

三条设计取舍：

1. **表头模糊匹配**：各平台导出列名不统一、且平台会改版，写死列序必然过期；
   匹配不到的列一律忽略，不猜。
2. **手机号是去重键**：同一个号反复导入只更新不新增（会话 ID 为 `dy:{手机号}`）。
   打不通的号（导出被打码）**不导入**，但会在结果里报数——静默丢弃比漏掉更糟。
3. **批量导入默认不推送**：一次导进几十条历史客资还挨个 @ 律师，那是骚扰不是效率。
   导入后在控制台复核，再按需单条推送（线索卡上的「推送给律师」）。
"""

import csv
import io
import logging
from datetime import datetime

from responder import lead as lead_mod
from responder.config import Settings, get_settings
from responder.engine import signals
from responder.models import ClientStatus, GroupProfile, IncomingMessage
from responder.store.db import Store

logger = logging.getLogger(__name__)

# 列名模糊匹配：表头包含任一别名即命中。每个字段取第一个命中的列，列不复用。
_ALIASES: dict[str, tuple[str, ...]] = {
    "contact": ("手机", "电话", "联系方式", "号码"),
    "name": ("姓名", "昵称", "客户名", "用户名"),
    "summary": ("需求", "咨询", "意向", "描述", "内容", "问题", "备注"),
    "case_type": ("类型", "品类", "分类", "业务"),
    "created_at": ("时间", "日期"),
}

_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M", "%Y-%m-%d", "%Y/%m/%d",
)


def detect_columns(headers: list[str]) -> dict[str, int]:
    """表头 → 字段下标。识别不出的字段直接缺席，调用方按缺席处理。"""
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
        rows = []
        for raw in wb.active.iter_rows(values_only=True):
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
    律师在工作台里能看到，只是不弹企微消息。
    """
    settings = settings or get_settings()
    if not table:
        return {"rows": 0, "imported": 0, "updated": 0, "no_contact": 0, "unrecognized": True}

    cols = detect_columns(table[0])
    if "contact" not in cols:
        raise ValueError(
            "表里找不到手机号列。请确认导出文件包含「手机号 / 电话 / 联系方式」列"
        )
    body = table[1:]

    def cell(row: list[str], field: str) -> str:
        i = cols.get(field, -1)
        return row[i].strip() if 0 <= i < len(row) else ""

    imported = updated = no_contact = 0
    for row in body:
        contact = signals.extract_contact(cell(row, "contact"))
        if not contact:
            # 导出打码（138****1234）或空号列：打不通的线索不进队列，但要报数
            no_contact += 1
            continue
        group_id = f"dy:{contact}"
        name = cell(row, "name")
        existing = store.get_group(group_id)
        if existing is None:
            store.upsert_group(
                GroupProfile(
                    group_id=group_id,
                    name=f"{source} · {name or contact}",
                    client_status=ClientStatus.PROSPECT,  # 客资一律按新咨询
                    case_type=cell(row, "case_type"),
                )
            )
        desc = cell(row, "summary")
        # 手机号写进正文，评分才能按「已留电话」计分；同时会话原文里可溯源
        content = f"{desc}\n（{source}留资，手机号 {contact}）" if desc else (
            f"（{source}留资，手机号 {contact}）"
        )
        msg = IncomingMessage(
            msg_id=f"dy-{contact}",  # 手机号去重：重复导入只更新不新增
            group_id=group_id,
            sender_id=contact,
            content=content,
            created_at=_parse_dt(cell(row, "created_at")) or datetime.now(),
        )
        fresh = store.save_message(msg)
        group = store.get_group(group_id)
        try:
            lead_mod.dispatch(
                store, group,
                store.recent_messages(group_id, settings.lead_history_window),
                sender if notify else None,
                settings=settings,
            )
        except Exception:
            logger.exception("客资导入失败: %s", group_id)
            continue
        if fresh:
            imported += 1
        else:
            updated += 1
    return {
        "rows": len(body),
        "imported": imported,
        "updated": updated,
        "no_contact": no_contact,
        "columns": sorted(cols),
    }

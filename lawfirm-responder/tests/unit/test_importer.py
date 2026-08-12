"""客资导入：按真实导出结构（抖音来客 2026-07，17 列）验证解析与落库语义。

测试数据刻意照搬真实表头与取值形态——这类导入的失败几乎都来自「真实文件长得
和想象不一样」，用理想化的两列表格测出来的绿，到线上一个都不算数。
"""

import csv
import io
import re
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from responder import importer
from responder.config import Settings
from responder.console.api import router as console_router
from responder.service import Pipeline
from responder.store.db import Store
from responder.worker import Worker

ADMIN = "adm"

HEADERS = [
    "姓名", "手机号", "线索创建时间", "线索阶段", "线索标签", "最新跟进记录",
    "智能意向", "跟进员工", "流量类型", "营销类型", "单元名称", "搜索关键词",
    "最近留资记录", "商品名称", "商品类型", "订单ID", "互动场景",
]
# 新线索、自然流量、无商品
ROW_NEW = [
    "未命名", "13800138001", "2026-07-30 16:51:34", "新线索", "", "", "",
    "汪翰文", "自然流量", "自然线索", "", "各类案件，免费咨询",
    "在抖音短视频通过私信提供了联系方式", "", "", "", "短视频",
]
# 已加微信（已接触）、买过团购券、商品名自带案由
ROW_CONTACTED = [
    "未命名", "13800138002", "2026-07-30 15:55:13", "已加微信", "已加微信", "", "",
    "陈丽娟1", "营销流量", "全域投放-直播间团购支付", "", "",
    "在抖音直播购买团购商品后留资", "【婚姻家事】律师一对一30分钟免费咨询",
    "团购套餐", "1113935329220507438", "直播",
]
# 手机号为空（导出里确实存在这种行）
ROW_NO_PHONE = [
    "未命名", "", "2026-07-30 17:12:32", "新线索", "", "", "", "陈倩",
    "自然流量", "自然线索", "", "", "在抖音短视频通过私信提供了联系方式",
    "", "", "", "短视频",
]
SHEET = [HEADERS, ROW_NEW, ROW_CONTACTED]


class Snd:
    def __init__(self):
        self.direct: list[tuple[str, str]] = []

    def send_direct_text(self, userid, text):
        self.direct.append((userid, text))
        return True

    def send_robot_text(self, w, t):
        return True

    def send_group_text(self, c, t):
        return True


def make(tmp_path):
    db = str(tmp_path / "imp.db")
    store = Store(db)
    settings = Settings(
        mode="live", db_path=db, admin_token=ADMIN, llm_refine_enabled=False,
        split_delay_seconds=0, default_notify_userid="reception",
    )
    snd = Snd()
    return store, snd, Pipeline(store, snd, settings), settings


def app_for(store, pipeline, snd):
    app = FastAPI()
    app.state.store = store
    app.state.pipeline = pipeline
    app.state.worker = Worker(pipeline, store, snd)
    app.include_router(console_router)
    return TestClient(app)


def csv_bytes(rows) -> bytes:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue().encode("utf-8")


def xlsx_bytes(rows, bogus_dimension: bool = False) -> bytes:
    """可选复现抖音导出的坏 <dimension>（声明成只有 A1 一格）。"""
    import openpyxl

    wb = openpyxl.Workbook()
    for row in rows:
        wb.active.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    if not bogus_dimension:
        return buf.getvalue()
    src = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                data = re.sub(rb'<dimension ref="[^"]*"\s*/>',
                              b'<dimension ref="A1"/>', data)
                data = re.sub(rb'<dimension ref="[^"]*"\s*></dimension>',
                              b'<dimension ref="A1"></dimension>', data)
            z.writestr(item, data)
    return out.getvalue()


# ---------------------------------------------------------------- 表头识别
def test_detect_columns_on_real_headers():
    cols = importer.detect_columns(HEADERS)
    assert cols["contact"] == 1 and cols["name"] == 0
    assert cols["created_at"] == 2 and cols["stage"] == 3
    assert cols["owner"] == 7 and cols["product"] == 13


def test_case_type_never_matches_traffic_or_product_type():
    """裸「类型」会误吃流量类型/商品类型，把「自然流量」写成案由——交接单就全乱了。"""
    assert "case_type" not in importer.detect_columns(HEADERS)


def test_customer_and_staff_columns_are_separated():
    """员工跟进记录必须与客户诉求分开——混在一起会让「已加微信」虚增评分。"""
    cust = set(importer.detect_customer_columns(HEADERS))
    staff = set(importer.detect_staff_columns(HEADERS))
    assert {11, 12, 13} <= cust            # 搜索关键词/最近留资记录/商品名称
    assert {3, 4, 5, 6, 7} <= staff        # 阶段/标签/跟进记录/意向/跟进员工
    # 互动场景（直播/短视频）是平台状态列，不是客户的话——留在客户侧会让
    # 平台样板词命中面谈/要电话信号，成批虚推进 P0
    assert 16 in staff and 16 not in cust
    assert not (cust & staff) and 1 not in cust


def test_detect_columns_tolerates_alternate_naming():
    cols = importer.detect_columns(["客户姓名", "联系方式", "案由", "创建日期"])
    assert cols["name"] == 0 and cols["contact"] == 1
    assert cols["case_type"] == 2 and cols["created_at"] == 3


def test_missing_phone_column_is_a_clear_error(tmp_path):
    store, _, _, settings = make(tmp_path)
    try:
        importer.import_leads(store, [["昵称", "备注"], ["张三", "咨询"]], settings=settings)
        raise AssertionError("应当报错")
    except ValueError as e:
        assert "手机号" in str(e)


# ---------------------------------------------------------------- 解析
def test_gbk_csv_is_readable():
    table = importer.parse_table("姓名,手机号\n张三,13800138001\n".encode("gbk"), "客资.csv")
    assert table[1][0] == "张三"


def test_xlsx_with_bogus_dimension_still_reads_all_rows():
    """回归：抖音导出把 <dimension> 写成 A1，只读模式会只吐第一格「姓名」，
    整张 400 行的表被误判成「找不到手机号列」。"""
    data = xlsx_bytes(SHEET, bogus_dimension=True)
    table = importer.parse_table(data, "客资.xlsx")
    assert len(table) == 3 and table[0] == HEADERS
    assert table[1][1] == "13800138001"


# ---------------------------------------------------------------- 落库语义
def test_import_creates_scored_leads(tmp_path):
    store, _, _, settings = make(tmp_path)
    res = importer.import_leads(store, SHEET, settings=settings)
    assert (res["imported"], res["updated"], res["no_contact"]) == (2, 0, 0)

    lead = store.get_lead("dy:13800138001")
    assert lead["contact"] == "13800138001"
    # 手机号进正文 → 评分按「已留电话」计分，不因为它在表格列里就漏算
    assert lead["priority"] in ("P0", "P1") and lead["score"] >= 40
    assert "各类案件" in lead["summary"] or "私信" in lead["summary"]


def test_case_type_comes_from_bracketed_product_name(tmp_path):
    store, _, _, settings = make(tmp_path)
    importer.import_leads(store, SHEET, settings=settings)
    assert store.get_group("dy:13800138002").case_type == "婚姻家事"
    assert store.get_group("dy:13800138001").case_type == ""  # 没商品就留空，不瞎猜


def test_placeholder_name_falls_back_to_phone(tmp_path):
    store, _, _, settings = make(tmp_path)
    importer.import_leads(store, SHEET, settings=settings)
    assert store.get_group("dy:13800138001").name == "抖音 · 13800138001"


def test_contacted_stage_does_not_reenter_followup_queue(tmp_path):
    """平台上已加微信的客户不能回到待跟进队列被重新打一遍电话。"""
    store, _, _, settings = make(tmp_path)
    importer.import_leads(store, SHEET, settings=settings)
    assert store.get_lead("dy:13800138002")["status"] == "contacted"
    assert store.get_lead("dy:13800138001")["status"] == "new"
    pending = [x["group_id"] for x in store.list_leads(status="new", limit=50)]
    assert pending == ["dy:13800138001"]


def test_invalid_stage_marked_invalid(tmp_path):
    store, _, _, settings = make(tmp_path)
    row = list(ROW_NEW)
    row[3] = "无效线索"
    importer.import_leads(store, [HEADERS, row], settings=settings)
    assert store.get_lead("dy:13800138001")["status"] == "invalid"


def test_existing_owner_is_preserved(tmp_path):
    """导出表里的跟进员工能对上名册就派给他本人，不打乱已有对接关系。"""
    store, _, _, settings = make(tmp_path)
    for uid, name in [("chen", "陈丽娟"), ("wei", "魏")]:
        store.upsert_lawyer(uid, {"name": name,
                                  "role": "lawyer", "on_duty": True, "active": True})
    importer.import_leads(store, SHEET, settings=settings)
    # 「陈丽娟1」应当对上「陈丽娟」
    assert store.get_lead("dy:13800138002")["assigned_userid"] == "chen"


def test_single_char_lawyer_name_needs_exact_match():
    """单字姓名前缀匹配会同时命中「魏来」「魏谦」——宁可不派也不要派错人。"""
    roster = [{"name": "魏"}, {"name": "陈丽娟"}]
    assert importer._match_lawyer(roster, "魏来") is None
    assert importer._match_lawyer(roster, "魏")["name"] == "魏"
    assert importer._match_lawyer(roster, "陈丽娟1")["name"] == "陈丽娟"


def test_reimport_updates_instead_of_duplicating(tmp_path):
    store, _, _, settings = make(tmp_path)
    importer.import_leads(store, SHEET, settings=settings)
    res = importer.import_leads(store, SHEET, settings=settings)
    assert res["updated"] == 2 and res["imported"] == 0
    assert len(store.list_leads(limit=100)) == 2


def test_missing_phone_row_is_counted_not_silently_dropped(tmp_path):
    store, _, _, settings = make(tmp_path)
    res = importer.import_leads(store, [HEADERS, ROW_NO_PHONE], settings=settings)
    assert res["no_contact"] == 1 and res["imported"] == 0
    assert store.list_leads(limit=10) == []


def test_masked_phone_also_counted(tmp_path):
    store, _, _, settings = make(tmp_path)
    row = list(ROW_NEW)
    row[1] = "138****8001"
    res = importer.import_leads(store, [HEADERS, row], settings=settings)
    assert res["no_contact"] == 1


def test_bulk_import_does_not_spam_lawyers(tmp_path):
    """一次导进几百条历史客资还挨个 @ 律师是骚扰不是效率。"""
    store, snd, pipeline, settings = make(tmp_path)
    importer.import_leads(store, SHEET, settings=settings, sender=pipeline.sender)
    assert snd.direct == []


def test_notify_flag_pushes(tmp_path):
    store, snd, pipeline, settings = make(tmp_path)
    importer.import_leads(
        store, SHEET, settings=settings, sender=pipeline.sender, notify=True
    )
    assert snd.direct and snd.direct[0][0] == "reception"


def test_import_assigns_to_roster_when_no_owner_match(tmp_path):
    """导出表里的跟进员工在名册里对不上人时，照常走派单（在办最少者接）。"""
    store, _, _, settings = make(tmp_path)
    store.upsert_lawyer("zhang", {"name": "张",
                                  "role": "lawyer", "on_duty": True, "active": True})
    importer.import_leads(store, SHEET, settings=settings)
    assert store.get_lead("dy:13800138002")["assigned_userid"] == "zhang"


# ---------------------------------------------------------------- 端点
def test_upload_endpoint(tmp_path):
    store, snd, pipeline, _ = make(tmp_path)
    client = app_for(store, pipeline, snd)
    r = client.post(
        "/console/leads/import?filename=kezi.xlsx",
        content=xlsx_bytes(SHEET, bogus_dimension=True),
        headers={"x-admin-token": ADMIN},
    )
    assert r.status_code == 200 and r.json()["imported"] == 2


def test_upload_rejects_lawyer_token(tmp_path):
    import hashlib

    store, snd, pipeline, _ = make(tmp_path)
    store.upsert_lawyer("wei", {"name": "魏", "role": "lawyer",
                                "on_duty": True, "active": True})
    store.set_lawyer_token_hash("wei", hashlib.sha256(b"wei-tok").hexdigest())
    client = app_for(store, pipeline, snd)
    r = client.post("/console/leads/import?filename=a.csv",
                    content=csv_bytes(SHEET), headers={"x-admin-token": "wei-tok"})
    assert r.status_code == 403


def test_upload_empty_body_is_clear_error(tmp_path):
    store, snd, pipeline, _ = make(tmp_path)
    client = app_for(store, pipeline, snd)
    r = client.post("/console/leads/import?filename=a.csv", content=b"",
                    headers={"x-admin-token": ADMIN})
    assert r.status_code == 400


def test_tag_overrides_stage_when_it_shows_contact(tmp_path):
    """阶段还写「新线索」但标签已是「首次沟通待复访」——这类必须算已接触。"""
    store, _, _, settings = make(tmp_path)
    row = list(ROW_NEW)
    row[3], row[4] = "新线索", "首次沟通待复访,已加微信"
    importer.import_leads(store, [HEADERS, row], settings=settings)
    assert store.get_lead("dy:13800138001")["status"] == "contacted"


def test_converted_stage_lands_in_funnel_bottom(tmp_path):
    store, _, _, settings = make(tmp_path)
    row = list(ROW_NEW)
    row[3] = "到店"
    importer.import_leads(store, [HEADERS, row], settings=settings)
    assert store.get_lead("dy:13800138001")["status"] == "converted"


def test_staff_followup_note_does_not_inflate_score(tmp_path):
    """「已加微信」是我方操作记录，不能被当成「客户要加微信详聊」加 20 分。"""
    store, _, _, settings = make(tmp_path)
    row = list(ROW_NEW)
    row[4] = "已加微信"
    importer.import_leads(store, [HEADERS, row], settings=settings)
    lead = store.get_lead("dy:13800138001")
    assert lead["score"] == 40  # 只有「已留电话」这一项
    # 记录本身不丢：以员工发言留档，会话原文里看得到
    convo = store.recent_messages("dy:13800138001", 10)
    assert any(m["sender_is_staff"] and "已加微信" in m["content"] for m in convo)

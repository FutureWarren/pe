"""客资导入：模糊表头、手机号去重、打码号不静默丢弃、导入即评分派单。"""

import csv
import io

from fastapi import FastAPI
from fastapi.testclient import TestClient

from responder import importer
from responder.config import Settings
from responder.console.api import router as console_router
from responder.service import Pipeline
from responder.store.db import Store
from responder.worker import Worker

ADMIN = "adm"


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


SHEET = [
    ["线索ID", "用户昵称", "留资手机号", "咨询需求", "线索类型", "留资时间"],
    ["1", "张先生", "17721275495", "公司拖欠三个月工资想仲裁", "劳动仲裁", "2026-07-28 10:30:00"],
    ["2", "李女士", "13912345678", "想咨询离婚孩子抚养权", "婚姻家事", "2026-07-28 11:00:00"],
]


# ---------------------------------------------------------------- 表头识别
def test_detect_columns_is_fuzzy():
    cols = importer.detect_columns(SHEET[0])
    assert cols["contact"] == 2 and cols["name"] == 1
    assert cols["summary"] == 3 and cols["case_type"] == 4 and cols["created_at"] == 5


def test_detect_columns_tolerates_alternate_naming():
    cols = importer.detect_columns(["客户姓名", "联系方式", "备注", "创建日期"])
    assert cols["name"] == 0 and cols["contact"] == 1
    assert cols["summary"] == 2 and cols["created_at"] == 3


def test_missing_phone_column_is_a_clear_error(tmp_path):
    store, _, _, settings = make(tmp_path)
    table = [["昵称", "备注"], ["张三", "咨询"]]
    try:
        importer.import_leads(store, table, settings=settings)
        raise AssertionError("应当报错")
    except ValueError as e:
        assert "手机号" in str(e)


# ---------------------------------------------------------------- 编码
def test_gbk_csv_is_readable():
    data = "姓名,手机号\n张三,17721275495\n".encode("gbk")
    table = importer.parse_table(data, "客资.csv")
    assert table[1][0] == "张三"


def test_xlsx_roundtrip(tmp_path):
    openpyxl = __import__("openpyxl")
    wb = openpyxl.Workbook()
    for row in SHEET:
        wb.active.append(row)
    path = tmp_path / "客资.xlsx"
    wb.save(path)
    table = importer.parse_table(path.read_bytes(), "客资.xlsx")
    assert table[0] == SHEET[0] and table[1][2] == "17721275495"


# ---------------------------------------------------------------- 导入语义
def test_import_creates_scored_leads(tmp_path):
    store, snd, _, settings = make(tmp_path)
    res = importer.import_leads(store, SHEET, settings=settings)
    assert (res["imported"], res["updated"], res["no_contact"]) == (2, 0, 0)

    lead = store.get_lead("dy:17721275495")
    assert lead["contact"] == "17721275495"
    # 手机号进正文 → 评分按「已留电话」计分，不会因为在表格列里就漏算
    assert lead["priority"] in ("P0", "P1") and lead["score"] >= 40
    assert "拖欠三个月工资" in lead["summary"]
    g = store.get_group("dy:17721275495")
    assert g.name == "抖音 · 张先生" and g.case_type == "劳动仲裁"


def test_reimport_updates_instead_of_duplicating(tmp_path):
    store, _, _, settings = make(tmp_path)
    importer.import_leads(store, SHEET, settings=settings)
    res = importer.import_leads(store, SHEET, settings=settings)
    assert res["updated"] == 2 and res["imported"] == 0
    assert len(store.list_leads(limit=100)) == 2


def test_masked_phone_is_counted_not_silently_dropped(tmp_path):
    """导出打码的号打不通，不进队列——但必须报数，否则是静默丢单。"""
    store, _, _, settings = make(tmp_path)
    table = [SHEET[0], ["3", "王先生", "138****5678", "房产纠纷咨询", "房产纠纷", ""]]
    res = importer.import_leads(store, table, settings=settings)
    assert res["no_contact"] == 1 and res["imported"] == 0
    assert store.list_leads(limit=10) == []


def test_bulk_import_does_not_spam_lawyers(tmp_path):
    """批量导入默认不推送：一次导 50 条还挨个 @ 律师是骚扰。"""
    store, snd, pipeline, settings = make(tmp_path)
    importer.import_leads(store, SHEET, settings=settings, sender=pipeline.sender)
    assert snd.direct == []


def test_notify_flag_pushes(tmp_path):
    store, snd, pipeline, settings = make(tmp_path)
    importer.import_leads(
        store, SHEET, settings=settings, sender=pipeline.sender, notify=True
    )
    assert len(snd.direct) == 2 and snd.direct[0][0] == "reception"


def test_import_assigns_by_specialty(tmp_path):
    """导入的线索同样走派单：劳动仲裁给魏、婚姻家事给张。"""
    store, _, _, settings = make(tmp_path)
    for uid, name, spec in [("wei", "魏", "劳动仲裁"), ("zhang", "张", "婚姻家事")]:
        store.upsert_lawyer(uid, {"name": name, "specialties": spec,
                                  "role": "lawyer", "on_duty": True, "active": True})
    importer.import_leads(store, SHEET, settings=settings)
    assert store.get_lead("dy:17721275495")["assigned_userid"] == "wei"
    assert store.get_lead("dy:13912345678")["assigned_userid"] == "zhang"


# ---------------------------------------------------------------- 端点
def test_upload_endpoint(tmp_path):
    store, snd, pipeline, _ = make(tmp_path)
    client = app_for(store, pipeline, snd)
    r = client.post(
        "/console/leads/import?filename=kezi.csv",
        content=csv_bytes(SHEET), headers={"x-admin-token": ADMIN},
    )
    assert r.status_code == 200 and r.json()["imported"] == 2


def test_upload_rejects_lawyer_token(tmp_path):
    store, snd, pipeline, _ = make(tmp_path)
    store.upsert_lawyer("wei", {"name": "魏", "role": "lawyer",
                                "on_duty": True, "active": True})
    client = app_for(store, pipeline, snd)
    import hashlib
    store.set_lawyer_token_hash("wei", hashlib.sha256(b"wei-tok").hexdigest())
    r = client.post("/console/leads/import?filename=a.csv",
                    content=csv_bytes(SHEET), headers={"x-admin-token": "wei-tok"})
    assert r.status_code == 403


def test_upload_empty_body_is_clear_error(tmp_path):
    store, snd, pipeline, _ = make(tmp_path)
    client = app_for(store, pipeline, snd)
    r = client.post("/console/leads/import?filename=a.csv", content=b"",
                    headers={"x-admin-token": ADMIN})
    assert r.status_code == 400

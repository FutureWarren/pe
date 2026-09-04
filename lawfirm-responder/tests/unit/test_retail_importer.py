"""读门店导出的库存表，并先把这张表的问题说清楚。

酷机时代的库存在一个后台小程序里，第一期最现实的路径是**每天导出一张表**。
而现实里那张表长什么样，事先永远猜不到：表头叫「售价」不叫「价格」、
价格写成「￥6,499元」、库存写成「城关3;七里河1」、更新时间那一列空着。

如果导入器只是「能读的读、读不了的跳过」，结果就是一张残缺的表静静进了系统，
AI 因为查不到而把每个问价的客户都转人工——**功能看着正常，实际一条都没自动化**。
所以这一层的首要产出是一份**给店长看的报告**，不是导入本身。
"""

from datetime import datetime

import pytest

from responder.retail import importer


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ------------------------------------------------------------ ① 表头认得宽
@pytest.mark.parametrize("header,expect", [
    ("售价", "price"),
    ("零售价", "price"),
    ("价格(元)", "price"),
    ("售 价", "price"),
    ("机型", "model"),
    ("商品名称", "model"),
    ("规格", "spec"),
    ("配色", "color"),
    ("可售数量", "stock"),
    ("更新时间", "updated_at"),
])
def test_header_aliases_are_recognised(header, expect):
    """**漏一个别名，那一整列就作废了**，而多写几个别名的代价是零。"""
    mapping, _ = importer.map_headers([header])
    assert mapping.get(0) == expect


def test_unrecognised_headers_are_reported_not_silently_dropped():
    mapping, unknown = importer.map_headers(["型号", "供应商编码", "毛利率"])
    assert mapping == {0: "model"}
    assert "供应商编码" in unknown and "毛利率" in unknown


# ------------------------------------------------------------ ② 脏数据
@pytest.mark.parametrize("raw,expect", [
    ("6499", 6499),
    ("￥6,499元", 6499),
    ("6499.00", 6499),
    (" 6499 元 ", 6499),
    ("¥6499", 6499),
])
def test_messy_prices_are_parsed(raw, expect):
    assert importer.parse_price(raw) == expect


@pytest.mark.parametrize("raw", ["", "面议", "-", "详询", "0"])
def test_an_unparseable_price_is_none_never_zero(raw):
    """**认不出就是 None，绝不猜 0**——0 会被当成一个真实的价格报给客户。"""
    assert importer.parse_price(raw) is None


@pytest.mark.parametrize("raw", [
    "2026-08-25", "2026/08/25 14:30", "2026.08.25", "20260825",
])
def test_various_date_formats_are_understood(raw):
    assert importer.parse_when(raw).startswith("2026-08-25")


def test_a_missing_date_borrows_the_file_mtime_rather_than_failing():
    """`catalog` 把「没有更新时间」视同过期、一律不报价。而门店导出时
    漏掉这一列是常事——真让整张表因此失效，第一期就跑不起来了。

    **兜底用的是文件修改时间：它至少是真实的**，不是我们编的。
    """
    fb = datetime(2026, 8, 20, 9, 0, 0)
    assert importer.parse_when("", fallback=fb) == fb.isoformat()


# ------------------------------------------------------------ ③ 报告
def test_a_clean_file_loads_and_reports_cleanly(tmp_path):
    p = write(tmp_path, "stock.csv",
              "型号,配置,颜色,售价,库存,活动,更新时间\n"
              "Mate 70 Pro,12+512,雅川青,￥6499元,城关店:3;七里河店:1,24期免息,2026-08-25 09:00\n"
              "Mate 70,12+256,曜金黑,5499,城关店:2,,2026-08-25 09:00\n")
    cat, rep = importer.load(p)
    assert rep.ok
    assert rep.total_rows == 2 and rep.usable == 2
    assert rep.no_price == 0
    q = cat.lookup("Mate 70 Pro 12+512 多少钱",
                   now=datetime(2026, 8, 25, 12, 0, 0))
    assert q.ok and q.sku.price == 6499


def test_rows_without_a_price_are_counted_and_explained(tmp_path):
    """没价格的行不会静默混进去——它们会让 AI 转人工，店长有权提前知道。"""
    p = write(tmp_path, "s.csv",
              "型号,售价,库存,更新时间\n"
              "Mate 70 Pro,6499,3,2026-08-25\n"
              "Mate 70 RS,面议,1,2026-08-25\n")
    _, rep = importer.load(p)
    assert rep.no_price == 1
    assert "不会报价" in rep.to_text()


def test_a_file_with_no_model_column_is_refused_with_a_reason(tmp_path):
    """**没有型号列的表根本不能用**，必须当场说清楚，而不是导入 0 行了事。"""
    p = write(tmp_path, "s.csv", "售价,库存\n6499,3\n")
    _, rep = importer.load(p)
    assert rep.ok is False
    txt = rep.to_text()
    assert "型号" in txt and "不能用" in txt


def test_the_report_nudges_them_to_export_the_timestamp_column(tmp_path):
    """没有更新时间列 → 隔天这张表会被判过期而停止报价。
    这件事必须在导入当天就说，不能等到线上突然不报价了才发现。"""
    p = write(tmp_path, "s.csv", "型号,售价,库存\nMate 70 Pro,6499,3\n")
    _, rep = importer.load(p)
    assert rep.borrowed_time == 1
    assert "更新时间" in rep.to_text()


def test_subtotal_and_blank_rows_are_skipped(tmp_path):
    p = write(tmp_path, "s.csv",
              "型号,售价,库存,更新时间\n"
              "Mate 70 Pro,6499,3,2026-08-25\n"
              ",,,\n"
              ",合计,4,\n")
    _, rep = importer.load(p)
    assert rep.usable == 1


def test_an_empty_file_says_so(tmp_path):
    p = write(tmp_path, "s.csv", "")
    _, rep = importer.load(p)
    assert rep.ok is False
    assert "空" in rep.to_text()


# ------------------------------------------------------------ ④ Excel
def test_an_xlsx_export_reads_the_same_way(tmp_path):
    """门店后台导出的多半是 xlsx，别逼店长先「另存为 CSV」。"""
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["型号", "配置", "售价", "库存", "更新时间"])
    ws.append(["Mate 70 Pro", "12+512", 6499, "城关店:3", "2026-08-25"])
    p = tmp_path / "s.xlsx"
    wb.save(p)

    cat, rep = importer.load(p)
    assert rep.ok and rep.usable == 1
    q = cat.lookup("Mate 70 Pro 多少钱", now=datetime(2026, 8, 25, 12, 0, 0))
    assert q.ok and q.sku.price == 6499


# ------------------------------------------------------------ ⑤ 与铁律接线
def test_a_yesterday_export_stops_quoting_today(tmp_path):
    """导入不绕过「过期不报价」那条铁律——这是最容易被绕开的一处。"""
    p = write(tmp_path, "s.csv",
              "型号,售价,库存,更新时间\nMate 70 Pro,6499,3,2026-08-20 09:00\n")
    cat, _ = importer.load(p, max_age_hours=24)
    q = cat.lookup("Mate 70 Pro 多少钱", now=datetime(2026, 8, 25, 12, 0, 0))
    assert q.stale is True
    assert q.ok is False

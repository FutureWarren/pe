"""库存表的载入口：**表不可信的时候，链路必须降级到转人工，而且要让人看得见。**

这一组守的不是「能不能读表」——那件事 `test_retail_importer.py` 已经守过了。
它守的是表**在线上会以哪三种方式安静地坏掉**：今天没人导、格式变了、被覆盖成空的。
三种的共同点是接口层面全都「成功」，没有异常、没有报错、日志一片干净。
"""

import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from responder.retail.sources import Sources

HEADER = "型号,配置,颜色,价格,库存,更新时间\n"


def write(p: Path, rows: str, *, header: str = HEADER) -> Path:
    p.write_text(header + rows, encoding="utf-8")
    return p


def fresh_row(model: str = "Mate 70 Pro", price: int = 6499) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"{model},12+256,曜金黑,{price},城关店:3,{now}\n"


# ------------------------------------------------------------ ① 没配
def test_no_path_means_no_catalog_and_says_so_plainly():
    """**没有数据就不要装作有。** 价格库存类一律转人工，这是对的。"""
    s = Sources("")
    assert s.catalog() is None
    assert "转人工" in s.health().to_text()


# ------------------------------------------------------------ ② 文件级的坏
def test_a_missing_file_is_reported_in_words_a_shopkeeper_can_act_on(tmp_path):
    s = Sources(str(tmp_path / "不存在.csv"))
    assert s.catalog() is None
    text = s.health().to_text()
    assert "没有文件" in text and "不存在.csv" in text


def test_an_empty_table_is_unusable_not_an_empty_catalog(tmp_path):
    """**读到 0 行 ≠ 一个「有 0 个商品」的可用目录。**

    后者会让每一次查询都规规矩矩地返回「查不到」，看起来完全正常——
    而真相是那张表被覆盖没了，全店的价格从此一个也报不出来。
    """
    s = Sources(str(write(tmp_path / "c.csv", "")))
    assert s.catalog() is None
    assert not s.health().ok


def test_a_table_whose_model_column_is_unrecognisable_is_refused(tmp_path):
    """导出格式变了：列还在，名字对不上。这时候「读成功了」是最危险的答案。"""
    p = write(tmp_path / "c.csv", "x,1,2\n", header="莫名其妙的列,甲,乙\n")
    s = Sources(str(p))
    assert s.catalog() is None


def test_a_directory_where_a_file_was_expected_is_explained(tmp_path):
    d = tmp_path / "catalog"
    d.mkdir()
    s = Sources(str(d))
    assert s.catalog() is None
    assert s.health().problem


# ------------------------------------------------------------ ③ 重载
def test_a_good_table_loads(tmp_path):
    s = Sources(str(write(tmp_path / "c.csv", fresh_row())))
    cat = s.catalog()
    assert cat is not None and len(cat.skus) == 1
    assert s.health().rows == 1


def test_overwriting_the_file_is_picked_up_without_a_restart(tmp_path):
    """**店长导完表是覆盖同一个文件，他不会来重启服务。**

    不重载的话，进程启动那一刻的快照会一直用到下次重启——而它只会越来越旧，
    直到过期判定把所有报价都关掉。表现是「今天 AI 突然一个价都不报了」，
    而文件明明是新的。
    """
    p = write(tmp_path / "c.csv", fresh_row("Mate 70 Pro"))
    s = Sources(str(p))
    assert len(s.catalog().skus) == 1

    time.sleep(0.01)
    write(p, fresh_row("Mate 70 Pro") + fresh_row("Mate X7", 12999))
    os.utime(p, (time.time() + 1, time.time() + 1))
    assert len(s.catalog().skus) == 2


def test_the_file_disappearing_drops_the_old_snapshot(tmp_path):
    """文件没了要**丢掉**旧快照，不能接着用。

    留着的话，链路会拿一份来路不明的旧数据继续报价。报错一次价的代价
    （门店认就是亏钱，不认就是客诉）远大于多转几次人工。
    """
    p = write(tmp_path / "c.csv", fresh_row())
    s = Sources(str(p))
    assert s.catalog() is not None
    p.unlink()
    assert s.catalog() is None


# ------------------------------------------------------------ ④ 过期
def test_a_stale_table_still_loads_but_the_quote_gate_shuts(tmp_path):
    """过期由 `Catalog` 判（`stale`），不是在这一层拦掉整张表——

    因为过期的表**仍然可以回答「有没有这款」这类不含金额的问题**，
    只是不许报价。两件事分开，才不会因为一天没导表就把整条链路关死。
    """
    old = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
    p = write(tmp_path / "c.csv", f"Mate 70 Pro,12+256,曜金黑,6499,城关店:3,{old}\n")
    s = Sources(str(p), max_age_hours=24.0)
    cat = s.catalog()
    assert cat is not None
    assert cat.lookup("Mate 70 Pro 多少钱").stale is True


# ------------------------------------------------------------ ⑤ 订单
def test_orders_are_absent_in_phase_one_and_that_is_deliberate():
    """酷机时代的订单在云盛 ERP（久惠宝「订单中心」）里，第一期我们没有那份数据。

    所以 `book` 是 None，订单/取货/发票/维修进度类问题一律转人工。
    接口谈下来之后灌进来即可，上层一行不用改。
    """
    s = Sources("")
    assert s.book() is None
    s.set_book(object())
    assert s.book() is not None

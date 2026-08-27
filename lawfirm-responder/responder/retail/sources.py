"""价格库存与订单的载入口：让「每天导出一张表」这件事真的撑得住线上回复。

第一期不接 ERP 接口，接的是一个目录（见 `docs/retail-kuji.md`：
**能日更的 CSV 胜过接不通的 API**）。但落到线上，「日更」这件事会以三种方式坏掉，
而三种都很安静：

1. 今天没人导（放假、店长换人、电脑重装）→ 表还在，只是旧了。
2. 导出格式变了（换了 ERP 版本、多了一列、列名改了）→ 表能读，但列对不上。
3. 文件被覆盖成空的 → 读到 0 行。

共同点是**接口层面全都「成功」**：没有异常、没有报错、日志一片干净。
所以这一层的职责不是「把表读进来」，是**在表不可信的时候让整条链路降级到
转人工，并且让人看得见**。降级本身由 `catalog.Catalog` 的过期判定完成
（`stale` → 不报价），这里负责的是**上面那三种「文件级」的坏**。

## 为什么要按 mtime 重载

店长导完表是覆盖同一个文件，不会来重启服务。不重载的话，进程启动那一刻的
快照会一直用到下次重启——**而那份快照恰恰会随着时间推移越来越旧，
直到过期判定把所有报价都关掉**。表现是「今天 AI 突然一个价都不报了」，
而文件明明是新的。

## 订单为什么是空的

酷机时代的订单在云盛 ERP（久惠宝「订单中心」）里，第一期我们**没有**那份数据。
所以 `book` 默认是 None，所有订单/取货/发票/维修进度类问题一律转人工——
这是对的：**没有数据就不要装作有**。接口一旦谈下来，把 `OrderBook` 灌进来即可，
上层一行不用改。
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from responder.retail.catalog import Catalog
from responder.retail.importer import Report, load
from responder.retail.orders import OrderBook


@dataclass
class Health:
    """这份数据现在能不能用，以及一句给人看的话。"""

    ok: bool = False
    path: str = ""
    rows: int = 0
    loaded_at: datetime | None = None
    file_time: datetime | None = None
    problem: str = ""
    report: Report | None = field(default=None, repr=False)

    def to_text(self) -> str:
        if not self.path:
            return ("没有配库存表（RESPONDER_RETAIL_CATALOG_PATH 留空）——"
                    "价格、库存类问题一律转人工。")
        if self.problem:
            return f"⚠️ 库存表读不了：{self.problem}（{self.path}）"
        when = self.file_time.strftime("%m-%d %H:%M") if self.file_time else "未知"
        head = f"库存表 {self.rows} 行，文件时间 {when}。"
        return head + ("" if self.ok else " ⚠️ 这张表现在不可用，价格库存类会转人工。")


class Sources:
    """价格库存 + 订单的持有者。线程安全性靠「整份替换」而非加锁。

    重载是**整份替换引用**（`self._catalog = new`），不是就地修改。
    后台线程正在用旧的那份也不会读到半张表——它读到的要么是完整的旧版，
    要么是完整的新版。这比加锁便宜，也比加锁难写错。
    """

    def __init__(
        self,
        catalog_path: str = "",
        *,
        max_age_hours: float = 24.0,
        book: OrderBook | None = None,
    ) -> None:
        self.catalog_path = catalog_path
        self.max_age_hours = max_age_hours
        self._book = book
        self._catalog: Catalog | None = None
        self._health = Health(path=catalog_path)
        self._mtime: float = -1.0

    # ---------------------------------------------------------------- 读
    def catalog(self) -> Catalog | None:
        """当前可用的商品目录。**不可用时返回 None**，上层据此转人工。"""
        self._refresh()
        return self._catalog if self._health.ok else None

    def book(self) -> OrderBook | None:
        return self._book

    def set_book(self, book: OrderBook | None) -> None:
        """接上订单数据源（ERP 谈下来之后）。"""
        self._book = book

    def health(self) -> Health:
        self._refresh()
        return self._health

    # ---------------------------------------------------------------- 载入
    def _refresh(self) -> None:
        if not self.catalog_path:
            self._health = Health(path="")
            return
        p = Path(self.catalog_path)
        try:
            mtime = p.stat().st_mtime
        except OSError as exc:
            # 文件没了/权限不对：**保留上一份还是丢掉？丢掉。**
            # 保留的话，链路会拿着一份来路不明的旧数据继续报价，
            # 而报价这件事错一次的代价远大于多转几次人工。
            self._catalog = None
            self._health = Health(path=self.catalog_path, problem=_why(exc))
            self._mtime = -1.0
            return

        if mtime == self._mtime and self._catalog is not None:
            return  # 没变，不重读

        try:
            cat, rep = load(p, max_age_hours=self.max_age_hours)
        except Exception as exc:                       # noqa: BLE001
            self._catalog = None
            self._health = Health(path=self.catalog_path, problem=_why(exc))
            self._mtime = mtime
            return

        self._mtime = mtime
        self._catalog = cat if rep.ok else None
        self._health = Health(
            ok=rep.ok,
            path=self.catalog_path,
            rows=rep.usable,
            loaded_at=datetime.now(),
            file_time=datetime.fromtimestamp(mtime),
            problem="" if rep.ok else "表里没有可用的行，或者型号那一列没认出来",
            report=rep,
        )


def _why(exc: Exception) -> str:
    """异常翻成一句人话——收件人是店长，不是工程师。"""
    if isinstance(exc, FileNotFoundError):
        return "这个路径下没有文件（是不是导出到别的目录了？）"
    if isinstance(exc, PermissionError):
        return "没有读这个文件的权限"
    if isinstance(exc, IsADirectoryError):
        return "这个路径是个目录，不是文件"
    return f"{type(exc).__name__}: {exc}"

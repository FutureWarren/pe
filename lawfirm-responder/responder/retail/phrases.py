"""话术库：门店自己维护的那几段固定答案。

零售侧的回答分三种来源，这个文件管的是第三种：

    价格 / 库存 / 订单  →  只能从数据源出（`catalog.py` / `orders.py`），一个数字都不许编
    退款 / 尾款 / 谈价  →  一律不代答，叫人（`standin.NEVER_STANDIN`）
    保修 / 激活 / 门店  →  **写一次长期有效**，就是这里

第三种为什么要单独做成一个可外挂的文件，而不是留在代码里：

1. **它们全是对外承诺。** 「进水算不算保修」「贴膜送不送」全国政策一样，
   各家做法不同——照抄官方条款会当着客户的面跟门店的实际做法打架。
   这句话该由酷机时代自己定，不该由写代码的人定。
2. **改一条要立刻生效，不能等发版。** 活动天天变。
3. **没填的那几条要能被看见。** 缺话术的意图会安静地退化成转人工，
   而后台看着一切正常——`gaps()` 就是拿来把它摆到明面上的。

代码里的 `DEFAULTS` 只是让链路在零配置下也能跑起来、也能被测试。
真上线时以门店那份文件为准。
"""

import csv
import io
import logging
from datetime import datetime
from pathlib import Path

from responder.retail.intents import ALL, Handling

logger = logging.getLogger(__name__)

# 表头别名。跟库存表同一条规矩：**宁可多写，不可漏写。**
_KEY_COLS = ("意图", "类型", "问题", "key", "intent")
_TEXT_COLS = ("话术", "回复", "答案", "内容", "text", "reply", "answer")

# 出厂默认。**注意哪几条是空的**——空不是遗漏，是「这句必须门店自己说」。
DEFAULTS: dict[str, str] = {
    "warranty": "主机是一年保修，电池和充电器半年，屏幕、进水、摔碰这些属于人为，"
                "不在保修范围里，但可以走付费维修。您那台具体算不算，"
                "得工程师上手看一眼才能定，我不敢替他下结论。",
    "activate": "开机之后按提示选语言、插卡、连 WiFi，然后登录或新建华为账号就行。"
                "实名是运营商那边做的，营业厅或者官方 App 都能办。"
                "要是卡在哪一步，您截个图发我。",
    "data_migration": "用「手机克隆」最省事：新旧机都装这个 App，"
                      "旧机选「发送」、新机选「接收」，扫个码就开始传，"
                      "通讯录、照片、微信记录都能带过去。半小时左右。"
                      "您要是不方便弄，拿到店里我们帮您导，不收费。",
    "installment": "我们支持花呗、信用卡分期和银行分期，常见的是 12 期和 24 期，"
                   "活动期内有免息名额。具体您这台能做几期免息、每期多少，"
                   "得按下单时的活动算，我叫同事给您报准数。",
    "promo": "当期活动我这边随时在更新，您说一下想要哪款，我把对应的优惠给您列清楚。",
    "compare": "这两款主要差在影像和屏幕上，日常用差别不大，"
               "拍照多、经常拍夜景的建议上高配那款。您平时主要用来做什么？"
               "我按您的用法给您说得具体点。",
    # ↓ 这三条**刻意留空**，因为它们各自是一句只有门店说了才算数的话。
    "store_info": "",      # 门店清单、地址、营业时间
    "authenticity": "",    # 正品行货的对外口径（涉及品牌方的规范，要拿到原文）
    "payment": "",         # 支持哪些付款方式、能不能开专票
}


def _norm(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "")


# 中文名 → key，让门店那份表可以直接写「保修与三包」而不是 warranty
_BY_ZH = {_norm(i.zh): i.key for i in ALL}
_KEYS = {i.key for i in ALL}


class Phrases:
    """话术的读取口。按文件时间自动重载，理由与库存表同（见 `sources.py`）。"""

    def __init__(self, path: str = "", *, defaults: dict[str, str] | None = None) -> None:
        self.path = path
        self.defaults = dict(DEFAULTS if defaults is None else defaults)
        self._loaded: dict[str, str] = {}
        self._mtime: float = -1.0
        self.problem: str = ""
        self.unknown: list[str] = []

    # ---------------------------------------------------------------- 读
    def get(self, key: str) -> str:
        self._refresh()
        return self._loaded.get(key) or self.defaults.get(key, "")

    def gaps(self) -> list[tuple[str, str]]:
        """**能自动答、却没有话术的意图。**

        这些问题现在会退化成转人工，而后台看着一切正常——
        客户那头的表现是「问什么都说帮您问一下同事」，然后他就不问了。
        """
        self._refresh()
        return [(i.key, i.zh) for i in ALL
                if i.handling is Handling.AUTO and not self.get(i.key)]

    def health(self) -> str:
        """一段给店长看的话。"""
        self._refresh()
        lines: list[str] = []
        if self.path:
            if self.problem:
                lines.append(f"⚠️ 话术表读不了：{self.problem}（{self.path}）")
            else:
                lines.append(f"话术表：{len(self._loaded)} 条（{self.path}）")
            if self.unknown:
                lines.append(f"⚠️ 这几行的「意图」认不出来，已跳过：{'、'.join(self.unknown[:6])}")
        else:
            lines.append("没有配话术表，用的是代码里的出厂默认。")
        if gaps := self.gaps():
            lines.append("⚠️ 这几类问题还没有话术，现在一律转人工："
                         + "、".join(f"{zh}（{k}）" for k, zh in gaps))
            lines.append("  这三条每一条都是对外承诺，只有门店自己说了才算数——"
                         "所以代码里刻意留空，不替你编。")
        else:
            lines.append("能自动答的意图都有话术了。")
        return "\n".join(lines)

    # ---------------------------------------------------------------- 载入
    def _refresh(self) -> None:
        if not self.path:
            return
        p = Path(self.path)
        try:
            mtime = p.stat().st_mtime
        except OSError as exc:
            self._loaded, self._mtime = {}, -1.0
            self.problem = f"{type(exc).__name__}"
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        self.problem, self.unknown, self._loaded = "", [], {}
        try:
            rows = list(csv.reader(io.StringIO(p.read_text(encoding="utf-8-sig"))))
        except Exception as exc:                       # noqa: BLE001
            self.problem = f"{type(exc).__name__}: {exc}"
            return
        if not rows:
            self.problem = "文件是空的"
            return

        head = [_norm(c) for c in rows[0]]
        ki = next((i for i, c in enumerate(head) if c in _KEY_COLS), 0)
        ti = next((i for i, c in enumerate(head) if c in _TEXT_COLS), 1)
        body = rows[1:] if (head[ki] in _KEY_COLS or head[ti] in _TEXT_COLS) else rows

        for raw in body:
            if len(raw) <= max(ki, ti):
                continue
            name, text = _norm(raw[ki]), (raw[ti] or "").strip()
            if not name or not text:
                continue
            key = name if name in _KEYS else _BY_ZH.get(name, "")
            if not key:
                # 认不出来的行**不静默丢掉**——门店把「保修」写成「三包」时，
                # 那一整条话术就作废了，而没有任何地方会说明为什么。
                self.unknown.append(raw[ki].strip())
                continue
            self._loaded[key] = text


def template(path: str | Path) -> Path:
    """写一份带注释的空模板，给门店照着填。"""
    p = Path(path)
    rows = [("意图", "话术")]
    rows += [(i.zh, DEFAULTS.get(i.key, ""))
             for i in ALL if i.handling is Handling.AUTO]
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(rows)
    return p


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")

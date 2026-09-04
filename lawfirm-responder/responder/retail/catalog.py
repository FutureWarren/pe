"""商品、价格、库存：**这套系统里唯一允许产生数字的地方。**

## 一条不容商量的铁律

> 价格、库存、优惠、抵扣、尾款——**一个数字都不许模型生成**。
> 模型只负责把这里查出来的数字组织成人话。

律所那边已经有过一条同构的规矩：「手机号必须正则提取，绝不经模型——
模型抄错一位数字，律师就打不通电话。」零售把它套在钱上，而且更硬：
打不通电话可以再打一次，**报错一个价是要按报价履约或者赔付的**。

落地成三道具体的机制，缺一道这条铁律就是句口号：

1. `lookup()` 查不到 → 返回 `None`，**绝不返回近似值**；
2. `Quote.stale` → 价格表太久没更新就不许报价（见下）；
3. `audit()` → 回复发出前逐个数字核对，凡是没在查询结果里出现过的
   价格类数字一律判违规。**这一道是兜底，前两道漏了它还能拦住。**

## 为什么「过期不报价」是硬性的

手机价格一周一变，节假日一天一变，国补政策说调就调。昨天的价格今天报出去，
客户拿着聊天记录来店里，门店只有两个选择：认这个价（亏钱），或者不认（客诉）。
两个都比「我帮您问一下店里」贵得多。

所以 `max_age_hours` 默认 24 小时，超时就降级成转人工。这个值宁可小不可大。

## 为什么型号匹配要「宁可反问，不可猜」

客户打「Mate70 多少钱」，库里同时有 Mate 70、Mate 70 Pro、Mate 70 Pro+，
三个价差好几千。猜一个 = 报错价。所以命中多个时返回 `Ambiguous`，
让 AI 回一句「您说的是标准版还是 Pro？」——多问一句永远比报错一个价便宜。
"""

import csv
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class Sku:
    """一个具体可卖的商品：型号 + 配置 + 颜色，三者定一个价。"""

    model: str            # Mate 70 Pro
    spec: str = ""        # 12+512
    color: str = ""       # 雅川青
    price: int | None = None      # 单位：元。None = 这一行没给价
    stock: dict[str, int] = field(default_factory=dict)  # {门店: 台数}
    promo: str = ""       # 当期活动一句话
    updated_at: str = ""  # ISO 时间戳，判过期用

    @property
    def title(self) -> str:
        return " ".join(x for x in (self.model, self.spec, self.color) if x)

    @property
    def total_stock(self) -> int:
        return sum(self.stock.values())

    def stores_with_stock(self) -> list[str]:
        return [s for s, n in self.stock.items() if n > 0]


@dataclass
class Quote:
    """一次查询的结果。**回复里能出现的数字，只能来自这里。**"""

    matched: list[Sku] = field(default_factory=list)
    stale: bool = False           # 数据太旧，不许报价
    stale_hours: float = 0.0
    query: str = ""

    @property
    def ok(self) -> bool:
        """能不能拿去答客户：命中唯一一条，且数据没过期。"""
        return len(self.matched) == 1 and not self.stale

    @property
    def ambiguous(self) -> bool:
        """命中多条 → 反问，别猜。"""
        return len(self.matched) > 1

    @property
    def empty(self) -> bool:
        return not self.matched

    @property
    def sku(self) -> Sku | None:
        return self.matched[0] if len(self.matched) == 1 else None

    def allowed_numbers(self) -> set[str]:
        """本次查询「授权可以出现在回复里」的数字白名单。

        `audit()` 拿它去核对模型写出来的每一个价格类数字。
        """
        out: set[str] = set()
        for s in self.matched:
            if s.price is not None:
                out.add(str(s.price))
                # 常见的口语写法也算数：6499 → 6499元 / 6499块
                out.add(f"{s.price:,}")
            for n in s.stock.values():
                out.add(str(n))
            for m in _NUM.findall(s.promo or ""):
                out.add(m)
            for m in _NUM.findall(s.spec or ""):
                out.add(m)
            for m in _NUM.findall(s.model or ""):
                out.add(m)
        return out


_NUM = re.compile(r"\d+")

# 「像钱」的数字：4 位以上，或后面直接跟着元/块/万。
# 为什么不查所有数字：型号里的 70、配置里的 512、期数里的 24 都是数字，
# 全拦下来 AI 就没法说话了。真正危险的是**金额**。
_MONEY = re.compile(r"(?<![\d.])(\d{3,6})(?=\s*(?:元|块|块钱|¥)?)|¥\s*(\d{2,6})")
_MONEY_CTX = re.compile(r"(\d{2,6})\s*(?:元|块钱|块|¥)|¥\s*(\d{2,6})")


def _norm(s: str) -> str:
    """归一化：去空格、去连字符、转小写，让「Mate 70 Pro」和「mate70pro」等价。"""
    return re.sub(r"[\s\-_·]+", "", (s or "").lower())


class Catalog:
    """价格库存的读取口。

    真实部署时可以换成对接进销存系统的实现，只要满足同一个 `lookup()` 契约。
    第一期建议就用 CSV：门店每天导出一次，放到固定目录——
    **能日更的 CSV 胜过接不通的 API**，先把链路跑通比架构漂亮重要。
    """

    def __init__(self, skus: list[Sku], *, max_age_hours: float = 24.0) -> None:
        self.skus = skus
        self.max_age_hours = max_age_hours

    # ---------------------------------------------------------------- 载入
    @classmethod
    def from_csv(cls, path: str | Path, *, max_age_hours: float = 24.0) -> "Catalog":
        """从门店导出的 CSV 载入。

        表头（缺列不报错，按空处理，方便门店用现成的导出文件）：
            型号,配置,颜色,价格,库存,活动,更新时间
        库存列写法：`城关店:3;七里河店:1`，或只写一个数字（记到「默认」门店）。
        """
        rows: list[Sku] = []
        p = Path(path)
        with p.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                g = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
                price = g.get("价格") or g.get("price") or ""
                digits = re.sub(r"[^\d]", "", price)
                rows.append(Sku(
                    model=g.get("型号") or g.get("model") or "",
                    spec=g.get("配置") or g.get("spec") or "",
                    color=g.get("颜色") or g.get("color") or "",
                    price=int(digits) if digits else None,
                    stock=_parse_stock(g.get("库存") or g.get("stock") or ""),
                    promo=g.get("活动") or g.get("promo") or "",
                    updated_at=g.get("更新时间") or g.get("updated_at") or "",
                ))
        return cls([s for s in rows if s.model], max_age_hours=max_age_hours)

    # ---------------------------------------------------------------- 查询
    def lookup(self, text: str, *, now: datetime | None = None) -> Quote:
        """从客户的一句话里认出他问的是哪个商品。

        **查不到就是查不到**：返回空 `Quote`，调用方必须转人工。
        绝不返回「最接近的一条」——最接近的那条也是错的价。
        """
        now = now or datetime.now()
        q = _norm(text)
        if not q:
            return Quote(query=text or "")

        hits = [s for s in self.skus if _norm(s.model) and _norm(s.model) in q]
        if not hits:
            return Quote(query=text)

        # **最具体的那个匹配才算数。** 客户打「Mate 70 Pro+」时，
        # 「Mate 70」也是这句话的子串，两条会一起命中；而他显然把话说全了。
        # 取型号名最长的那一档，等于「以客户实际打出来的为准」。
        longest = max(len(_norm(s.model)) for s in hits)
        hits = [s for s in hits if len(_norm(s.model)) == longest]

        # **型号族歧义**：客户打「Mate 70 多少钱」，而库里还有 Mate 70 Pro、
        # Mate 70 Pro+、Mate 70 RS——他很可能指的是其中一个，价差好几千。
        # 纯子串匹配会命中「Mate 70」这一条然后自信地报出标准版的价。
        # 所以只要命中的型号是别的型号的前缀、而客户又没打出区分后缀，
        # 就把兄弟型号一起摆出来反问。多问一句永远比报错一个价便宜。
        matched = {_norm(s.model) for s in hits}
        siblings = [
            s for s in self.skus
            if _norm(s.model) not in matched
            and any(_norm(s.model).startswith(m) for m in matched)
        ]
        if siblings:
            hits = hits + siblings

        # 客户报了配置/颜色就用它收窄。收窄不掉的留给 ambiguous 去反问。
        for attr in ("spec", "color"):
            if len(hits) <= 1:
                break
            narrowed = [s for s in hits
                        if getattr(s, attr) and _norm(getattr(s, attr)) in q]
            if narrowed:
                hits = narrowed

        stale, hours = self._staleness(hits, now)
        return Quote(matched=hits, stale=stale, stale_hours=hours, query=text)

    def _staleness(self, skus: list[Sku], now: datetime) -> tuple[bool, float]:
        """取命中行里**最旧**的那条来判断——一条过期就全部不许报。

        取最旧而不是最新：宁可保守。价格这件事上，
        「少报一次」的代价远小于「报错一次」。
        """
        worst = 0.0
        for s in skus:
            if not s.updated_at:
                return True, float("inf")  # 没写更新时间 = 无法证明是新的 = 不许报
            try:
                ts = datetime.fromisoformat(s.updated_at)
            except (ValueError, TypeError):
                return True, float("inf")
            worst = max(worst, (now - ts).total_seconds() / 3600)
        return worst > self.max_age_hours, worst

    def fresh_within(self, hours: float, *, now: datetime | None = None) -> bool:
        """整张表是不是还新鲜——给控制台「数据健康」用。"""
        now = now or datetime.now()
        if not self.skus:
            return False
        stale, _ = self._staleness(self.skus, now)
        return not stale if hours >= self.max_age_hours else not stale


def _parse_stock(raw: str) -> dict[str, int]:
    """`城关店:3;七里河店:1` → {城关店: 3, 七里河店: 1}；纯数字 → 记到「默认」。

    认不出来的片段直接丢掉，不猜：库存数字猜错的后果是答应客户有货、
    客户跑一趟店里没有——比说「我帮您问一下」难看得多。
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    if raw.isdigit():
        return {"默认": int(raw)}
    out: dict[str, int] = {}
    for part in re.split(r"[;；,，]", raw):
        part = part.strip()
        if not part:
            continue
        if m := re.match(r"^(.*?)[:：]\s*(\d+)$", part):
            out[m.group(1).strip()] = int(m.group(2))
        elif part.isdigit():
            out["默认"] = int(part)
    return out


# ---------------------------------------------------------------------------
# 出口审计：回复发出去之前的最后一道
# ---------------------------------------------------------------------------
@dataclass
class Audit:
    passed: bool
    offending: list[str] = field(default_factory=list)
    reason: str = ""


def audit(reply: str, quote: Quote | None, *, extra_allowed: set[str] | None = None) -> Audit:
    """检查这条回复里的**金额**是不是全都来自查询结果。

    这是铁律的兜底：前面两道（查不到不答、过期不答）都可能被将来某次
    改动绕过去，而这一道贴在出口上，只要金额不在白名单里就拦。

    只查「像钱」的数字（带元/块/¥，或 3 位以上独立数字），不查型号里的
    70、配置里的 512、分期的 24 期——那些拦下来 AI 就没法说话了。
    """
    allowed = set(extra_allowed or set())
    if quote is not None:
        allowed |= quote.allowed_numbers()

    found: list[str] = []
    for m in _MONEY_CTX.finditer(reply or ""):
        n = m.group(1) or m.group(2)
        if n and n not in allowed:
            found.append(n)
    if found:
        return Audit(
            False, sorted(set(found)),
            f"回复里出现了未经查询的金额：{'、'.join(sorted(set(found)))}",
        )
    return Audit(True)


def quote_line(sku: Sku) -> str:
    """把一条 SKU 说成人话。**数字全部原样取自 sku，不做任何计算。**

    刻意不做「算一下每月多少钱」这类计算：分期利息、手续费、贴息政策
    各家不同，算出来的数字一旦有偏差，就是我们自己造的错。
    要报月供，让 catalog 里直接给这个数。
    """
    bits = [sku.title]
    if sku.price is not None:
        bits.append(f"现价 {sku.price} 元")
    if sku.promo:
        bits.append(sku.promo)
    stores = [s for s in sku.stores_with_stock() if s not in _PLACEHOLDER_STORE]
    if stores:
        bits.append("、".join(stores) + " 有现货")
    elif sku.stores_with_stock():
        # 库存表里只写了个数字、没写是哪家店（`_parse_stock` 记成「默认」）。
        # **那个占位符绝不能出现在客户眼前**——「默认 有现货」既像故障
        # 又没回答「哪家店有」。说「有现货」就够了，门店由销售确认。
        bits.append("有现货")
    elif sku.stock:
        bits.append("这两天没货，可以帮您预订")
    return "，".join(bits)


# `_parse_stock` 在库存列只有一个裸数字时用的占位门店名。
# 它是内部记账用的，不是一家真实门店，不许流到客户那儿。
_PLACEHOLDER_STORE = frozenset({"默认"})

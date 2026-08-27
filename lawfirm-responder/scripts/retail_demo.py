#!/usr/bin/env python3
"""把一整通零售对话跑一遍，把**客户看到的**和**销售看到的**并排打出来。

用法：
    python scripts/retail_demo.py                    # 跑内置的八个场景
    python scripts/retail_demo.py --catalog 库存.xlsx  # 换成门店自己的表
    python scripts/retail_demo.py --say "Mate 70 Pro 多少钱" --say "我要退货"

为什么要有这个：`check_catalog.py --ask` 只跑到「AI 会说什么」那一层，
而线上真正决定客户收不收得到的是它后面那几件——额度、去重、真人是否在场、
模式门控。上一版的教训记在 `docs/retail-kuji.md` 里：
**演示工具骗了自己人，比没有演示工具更糟。**所以这里走的是
`retail/pipeline.py`，跟线上完全同一条链路，只是把发送换成打印。

两列并排是刻意的。这套系统真正的卖点不是「AI 会答」，是
**「AI 答不了的那些，销售那边一条都不会漏」**——右边那一列就是它。
"""

import argparse
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from responder.retail.phrases import Phrases, template  # noqa: E402
from responder.retail.pipeline import Inbound, RetailPipeline  # noqa: E402
from responder.retail.sources import Sources  # noqa: E402
from responder.store.db import Store  # noqa: E402

# 内置的八幕，按酷机时代的真实客流排：**线下成交之后**加过来的人问什么。
# 每一幕都对应一条被证伪过的判断，见 docs/retail-kuji.md。
# 第三项是**距上一句过了多久**（秒）。它不是装饰：转人工的回执有 3 分钟的
# 去重窗口，隔多久问下一句，直接决定客户收不收得到回音。
SCENES: list[tuple[str, str, int, dict]] = [
    ("售后·物流", "我那台什么时候能到啊", 0, {}),
    ("售后·保修", "屏幕碎了保修吗", 240, {}),
    ("售后·换机", "怎么把旧手机的照片弄到新手机上", 60, {}),
    ("售前·报价", "Mate 70 Pro 12+256 多少钱", 90, {}),
    ("售后·退货", "我要退货", 240, {}),
    ("同一口气补充", "买了才三天，还没拆封", 15, {}),
    ("读不了", "", 20, {"media": "voice"}),
    ("售后·尾款", "我旧机的钱什么时候到账", 300, {}),
    ("真人接手", "您好我是小王，这边给您看一下", 30, {"is_staff": True}),
    ("真人在场", "那太好了，谢谢", 20, {}),
]

SAMPLE = """型号,配置,颜色,价格,库存,活动,更新时间
Mate 70 Pro,12+256,曜金黑,6499,城关店:3;七里河店:1,以旧换新补贴,{when}
Mate 70 Pro,12+512,雪域白,7299,城关店:1,,{when}
Mate X7,16+512,天蓝,12999,城关店:0,,{when}
nova 14,12+256,樱语粉,2699,城关店:8;安宁店:4,,{when}
"""


def sample_catalog(dirpath: Path) -> Path:
    """写一张**样例**表。刻意不进仓库：一份假价目表躺在代码库里，

    早晚有人拿它当真。要真数据就用 --catalog 指自己的文件。
    """
    p = dirpath / "样例库存（非真实价格）.csv"
    p.write_text(SAMPLE.format(when=datetime.now().strftime("%Y-%m-%d %H:%M")),
                 encoding="utf-8")
    return p


def _cells(text: str, width: int) -> list[str]:
    """按**显示宽度**折行（中文占两格）。等宽切片会让两列错开，没法读。"""
    text = " ".join((text or "").split())
    out, line, used = [], "", 0
    for ch in text:
        w = 2 if ord(ch) > 0x2E80 else 1
        if used + w > width:
            out.append(line)
            line, used = "", 0
        line += ch
        used += w
    out.append(line)
    return out or [""]


def _w(s: str) -> int:
    return sum(2 if ord(c) > 0x2E80 else 1 for c in s)


def row(left: str, right: str, *, w: int = 36) -> None:
    ls, rs = _cells(left, w), _cells(right, w)
    for i in range(max(len(ls), len(rs))):
        a = ls[i] if i < len(ls) else ""
        b = rs[i] if i < len(rs) else ""
        print(f"  {a}{' ' * max(0, w - _w(a))}  │  {b}")


def _silence_label(out, kw: dict) -> str:
    """**沉默有好几种，混成一句就看不出哪一种要修。**"""
    if kw.get("is_staff"):
        return "（销售本人在说话，AI 让位）"
    if out.mode == "blocked":
        return "⚠️ 这一句没能发出去——客户什么都没收到"
    if "让给真人" in out.reason:
        return "（刚说过「叫同事来」，这几分钟让给真人；原话已送到销售那边）"
    if "让位" in out.reason:
        return "（真人在场，AI 闭嘴）"
    if "事件" in out.reason:
        return "（关注/菜单事件，由公众号后台的自动回复负责）"
    return "（AI 不作声）"


def main() -> int:
    ap = argparse.ArgumentParser(description="零售链路演示（走线上同一条链路）")
    ap.add_argument("--catalog", default="", help="库存表路径；留空则用样例表")
    ap.add_argument("--say", action="append", default=[], help="自己写一句，可重复")
    ap.add_argument("--live", action="store_true",
                    help="按正式模式记账（仍然不外发，只是让额度/去重按线上算）")
    ap.add_argument("--phrases", default="", help="门店自己的话术表（意图,话术）")
    ap.add_argument("--gaps", action="store_true", help="只看还缺哪几条话术")
    ap.add_argument("--template", default="",
                    help="写一份空话术表出来，给门店照着填")
    args = ap.parse_args()

    if args.template:
        out = template(args.template)
        print(f"已写出：{out}")
        print("两列：意图、话术。「意图」那列照抄别动，「话术」那列换成你们的说法。")
        print("填完把路径配到 RESPONDER_RETAIL_PHRASES_PATH，改一条立刻生效，不用重启。")
        return 0

    phrases = Phrases(args.phrases)
    if args.gaps:
        print(phrases.health())
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="retail-demo-"))
    catalog_path = args.catalog or str(sample_catalog(tmp))
    sources = Sources(catalog_path)
    health = sources.health()

    print("=" * 78)
    print("零售链路演示 —— 与线上同一条链路（retail/pipeline.py），只是不外发")
    print("=" * 78)
    print(health.to_text())
    for line in phrases.health().split("\n"):
        print(line)
    if not health.ok:
        print("\n⚠️ 库存表不可用，价格/库存类问题会全部转人工——这是对的，不是故障。")
    print()

    printed: list[str] = []

    class Printer:
        def send_text(self, _user: str, text: str) -> bool:
            printed.append(text)
            return True

    store = Store(str(tmp / "demo.db"))
    p = RetailPipeline(
        store, sources=sources, phrases=phrases, sender=Printer(),
        mode="live" if args.live else "shadow", store_hint="城关店",
    )

    scenes = ([("自定义", s, 240, {}) for s in args.say] if args.say else SCENES)
    at = datetime.now()
    row("客户说 / 客户收到", "销售那边看到的")
    print("  " + "─" * 36 + "──┼──" + "─" * 36)

    for i, (label, text, gap, kw) in enumerate(scenes):
        at = at + timedelta(seconds=gap)
        out = p.handle(Inbound(
            channel="mp", user_key="oDEMO", text=text, msg_id=f"demo{i}",
            at=at, **kw,
        ), now=at)

        shown = text or f"（发来一条{kw.get('media', '消息')}）"
        gap_note = f"（隔 {gap} 秒）" if gap else ""
        print(f"\n【{label}】{gap_note}")
        row(f"客户：{shown}", "")
        if out.reply:
            row(f"　AI：{out.reply}", out.staff_note or "—")
        else:
            row(f"　{_silence_label(out, kw)}", out.staff_note or "—")
        print(f"      · 判断：{out.reason}｜mode={out.mode}"
              + ("｜⚠️ 出口审计拦下" if out.audit_failed else ""))

    print("\n" + "=" * 78)
    todo = store.notes_by_prefix("retail_todo:")
    lines = [(k.split(":", 1)[1], ln) for k, v in todo.items()
             for ln in v.split("\n") if ln.strip()]
    print(f"销售待办：{len(lines)} 条" + ("" if lines else "（本轮没有需要人接的）"))
    for who, ln in lines:
        print(f"  · {who} → {ln}")
    counts = {k: v["n"] for k, v in store.counters().items() if k.startswith("retail_")}
    print(f"计数器：{counts}")
    print(f"\n（临时库：{tmp}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

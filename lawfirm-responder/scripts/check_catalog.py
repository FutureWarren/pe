#!/usr/bin/env python3
"""验一张库存表能不能用——**在接进系统之前**。

用法：
    python scripts/check_catalog.py 库存.xlsx
    python scripts/check_catalog.py 库存.csv --ask "Mate 70 Pro 12+512 多少钱"

为什么单独做成一个命令：门店从小程序后台导出的表，格式每次都可能不一样。
让店长自己跑一句就能看到「这张表有多少行能用、哪几列没认出来、
哪些机型 AI 不会报价」，比我们隔着微信猜十次都管用。

`--ask` 更进一步：直接拿客户会问的原话试一遍，看 AI 会答出什么。
**这是上线前唯一能让非技术的人亲眼确认「它不会乱报价」的方式。**
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from responder.retail import (
    importer,  # noqa: E402
    replier,  # noqa: E402
)


def main() -> int:
    ap = argparse.ArgumentParser(description="检查库存表")
    ap.add_argument("path", help="库存表路径（.csv / .xlsx）")
    ap.add_argument("--ask", action="append", default=[],
                    help="拿一句客户原话试一下，可重复")
    ap.add_argument("--max-age-hours", type=float, default=24.0,
                    help="超过多久的价格就不许报（默认 24 小时）")
    args = ap.parse_args()

    p = Path(args.path)
    if not p.exists():
        print(f"❌ 找不到文件：{p}")
        return 2

    catalog, rep = importer.load(p, max_age_hours=args.max_age_hours)
    print("=" * 62)
    print(f"库存表检查：{p.name}")
    print("=" * 62)
    print(rep.to_text())

    if not rep.ok:
        return 1

    now = datetime.now()
    stale, hours = catalog._staleness(catalog.skus, now)
    if stale:
        print(f"\n⚠️ 整张表已超过 {args.max_age_hours} 小时没更新"
              f"（最旧的一行是 {hours:.1f} 小时前）。"
              f"\n   这种状态下 AI **不会报任何价格**，问价一律转人工——"
              f"这是刻意的，不是故障。")

    # 试问：让人亲眼看到 AI 会说什么
    questions = args.ask or [
        "多少钱", "有货吗",
    ]
    if args.ask:
        print("\n" + "=" * 62)
        print("试问结果（走的是线上同一条回复链路）")
        print("=" * 62)
        for q in questions:
            # **必须走 replier，不能只调 catalog.lookup。**
            # 只查目录看到的是「查到了什么」，而客户实际收到的还要经过
            # 意图分档、取数失败降级、出口审计三道。第一版这里图省事直接
            # 调了 lookup，结果一个没有价格的机型在报告里显示成
            # 「有现货」——而线上真实行为是转人工。**演示工具骗了自己人，
            # 比没有演示工具更糟。**
            out = replier.handle(q, catalog=catalog, now=now)
            print(f"\n客户 ▸ {q}")
            if out.reply:
                print(f"AI   ▸ {out.reply}")
            if out.escalate:
                print("       ↳ 转人工")
            if out.audit_failed:
                print("       ❌ 出口审计拦下了这条，已不发出")
            print(f"       · {out.reason}")

    print("\n" + "=" * 62)
    print(f"✅ 这张表可用：{rep.usable} 个机型")
    print("   提醒：价格库存只会从这张表出，AI 一个数字都不会自己编。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

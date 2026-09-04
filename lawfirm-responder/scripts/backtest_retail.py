#!/usr/bin/env python3
"""零售意图分档的回测。**改词表之前先跑这个。**

用法：
    python scripts/backtest_retail.py
    python scripts/backtest_retail.py --miss     # 只看判错的
    python scripts/backtest_retail.py --reply    # 顺便打印每句会得到什么回复

它跟 `verify.py` / `backtest_kf.py` 是同一条纪律：**词表是封闭枚举，必然漏，
而漏掉的那次永远是静默的**——回复看起来正常，没有人会去查。
所以每一次动词表，都要有一把尺子告诉你是变好了还是变坏了。

第一次跑出来的基线是 **57% 认不出**，而「认不出」一律回「我叫同事来看一下」。
更要命的是漏的那些里面有**钱的问题**（「我的旧手机你们什么时候给钱」）
和**投诉**——那两类错的代价不是体验差，是赔钱和客诉。

五个档位（`expect`）：

    human   必须真人：谈钱、退换、投诉、下单、客户明确要人
    lookup  要查数据才能答：价格 / 库存 / 订单 / 工单
    auto    固定话术，写一次长期有效
    model   模型答：产品知识与使用问题，**不含任何数字与承诺**
    chat    寒暄与收尾：在吗 / 你好 / 谢谢
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from responder.retail import replier  # noqa: E402
from responder.retail.intents import Handling, detect  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "tests" / "data" / "retail_backtest.jsonl"

# 意图 key → 回测档位。chat / model 是意图自己的档，其余看 handling。
CHAT_KEYS = {"chitchat"}
MODEL_KEYS = {"product_qa", "howto"}


def tier(text: str) -> str:
    i = detect(text, after_sale=True)
    if i is None:
        return "认不出"
    if i.key in CHAT_KEYS:
        return "chat"
    if i.key in MODEL_KEYS or i.handling is Handling.MODEL:
        return "model"
    return i.handling.value


def main() -> int:
    ap = argparse.ArgumentParser(description="零售意图分档回测")
    ap.add_argument("--miss", action="store_true", help="只列判错的")
    ap.add_argument("--reply", action="store_true", help="顺便打印实际回复")
    args = ap.parse_args()

    rows = [json.loads(ln) for ln in DATA.read_text(encoding="utf-8").splitlines() if ln.strip()]
    per = Counter()
    hit = Counter()
    misses = []

    for r in rows:
        want, got = r["expect"], tier(r["text"])
        per[want] += 1
        if want == got:
            hit[want] += 1
        else:
            misses.append((r["text"], want, got, r["why"]))

    print("=" * 66)
    print(f"零售意图分档回测（{len(rows)} 条）")
    print("=" * 66)
    print(f"{'档位':8}{'对':>5}{'总':>5}{'准确率':>9}   这一档错了会怎样")
    consequence = {
        "human": "AI 去碰了钱和承诺——赔钱或客诉",
        "lookup": "要么编数字，要么白转一次人工",
        "auto": "能当场答的推给了人，每一次都是流失",
        "model": "该答的没答，客户看到一句套话",
        "chat": "对着一句「在吗」说「我叫同事来」",
    }
    for k in ("human", "lookup", "auto", "model", "chat"):
        if not per[k]:
            continue
        pct = hit[k] * 100 / per[k]
        mark = "✅" if pct == 100 else ("⚠️ " if pct >= 80 else "❌")
        print(f"{k:8}{hit[k]:>5}{per[k]:>5}{pct:>8.0f}% {mark} {consequence[k]}")
    total_pct = sum(hit.values()) * 100 / len(rows)
    print("-" * 66)
    print(f"{'合计':8}{sum(hit.values()):>5}{len(rows):>5}{total_pct:>8.0f}%")

    if misses:
        print(f"\n判错 {len(misses)} 条：")
        for text, want, got, why in misses:
            flag = "🔴" if want == "human" else "  "
            print(f" {flag} {text}")
            print(f"      应为 {want}，实为 {got}｜{why}")

    if args.reply:
        print("\n" + "=" * 66)
        print("实际回复（走线上同一条链路）")
        print("=" * 66)
        for r in rows:
            out = replier.handle(r["text"], after_sale=True)
            shown = out.reply or f"（不作声：{out.reason}）"
            print(f"\n客户：{r['text']}\n  AI：{shown}")

    # **human 一条都不许错**：那一档错的是钱和承诺，不是体验。
    human_ok = per["human"] == hit["human"]
    return 0 if (human_ok and total_pct >= 90) else 1


if __name__ == "__main__":
    raise SystemExit(main())

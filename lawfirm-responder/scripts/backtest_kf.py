#!/usr/bin/env python3
"""一对一进线窗口的词表回测：AI 答得够不够开，敏感问题叫不叫得来人。

跟 `scripts/verify.py` 的分工：
  - verify 守**三分类准确率**，语料按群聊口径标注，是发布闸门；
  - 本脚本守**进线窗口的两件事**，而它们的正确答案跟群聊相反——
    群里承办律师在场，AI 拿不准就闭嘴是对的；进线窗口里没有别人，闭嘴就是丢单。

两个维度独立打分，因为它们是两件事：

  ① speak：客户看到了什么
     answer   给实质内容（一般性法律框架 / 所址这类确定事实）
     handoff  只承接不展开（费用、要联系方式、约见——这些不该由 AI 自说自话）
     urgent   紧急，安抚一句并强提醒
     greeting 打招呼   silence 不接话   identity 回答「你是谁」

  ② human：这一刻该不该把真人叫进来（priority.WANTS_HUMAN）

**「答一句」和「叫个人」可以同时发生**，而且经常应该同时发生：
客户问「你们地址在哪」，正确做法是当场把地址给他（答案就在配置里，
让他等一个律师回电话是荒唐的），同时把会话转给律师——因为问路的人
下一步就是上门。把这两件事混成一个标签，是这份回测的第一版就犯的错。

用法：python scripts/backtest_kf.py [--verbose]
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from responder.engine import priority, rules, signals  # noqa: E402
from responder.models import Action, Category  # noqa: E402

DATA = pathlib.Path(__file__).resolve().parents[1] / "tests/data/kf_backtest.jsonl"


def observe(text: str) -> tuple[str, int]:
    """当前系统对这句话的实际处置：(客户看到什么, 要不要叫人)。

    按「客户真实体验」分档，不按内部字段——我们要守的是他看到了什么。
    模拟的是进线窗口且已进入咨询状态：`is_one_on_one=True, in_consultation=True`。
    """
    urgent = False
    if rules.is_bare_greeting(text):
        speak = "greeting"
    else:
        action, category, urgent, reasons = rules.classify(
            text, is_one_on_one=True, in_consultation=True,
        )
        if urgent:
            speak = "urgent"
        elif any(r.startswith("identity") for r in reasons):
            speak = "identity"
        elif action == Action.ANSWER:
            speak = "answer"
        elif action == Action.SILENCE or category == Category.CHITCHAT:
            speak = "silence"
        else:
            speak = "handoff"
    # urgent 要照实传：紧急情形压倒清单（他没开口要人，但等他开口就晚了），
    # 生产里 `_maybe_handoff` 传的就是这个值。
    human = 1 if priority.wants_human(signals.detect(text)[1], urgent=urgent) else 0
    return speak, human


def main() -> int:
    verbose = "--verbose" in sys.argv
    rows = [json.loads(x) for x in DATA.read_text(encoding="utf-8").splitlines() if x.strip()]

    speak_by: dict[str, list[bool]] = {}
    human_ok, human_n = 0, 0
    speak_miss, human_miss = [], []
    for row in rows:
        speak, human = observe(row["text"])
        ok = speak == row["speak"]
        speak_by.setdefault(row["speak"], []).append(ok)
        if not ok:
            speak_miss.append((row, speak))
        human_n += 1
        if human == row["human"]:
            human_ok += 1
        else:
            human_miss.append((row, human))

    print(f"语料 {len(rows)} 条\n")
    print("① 客户看到什么")
    print(f"{'期望':<10}{'通过':>6}{'总数':>6}{'准确率':>9}")
    print("-" * 32)
    for want, res in sorted(speak_by.items()):
        print(f"{want:<10}{sum(res):>6}{len(res):>6}{sum(res) / len(res):>8.0%}")
    tot = sum(len(v) for v in speak_by.values())
    hit = sum(sum(v) for v in speak_by.values())
    print("-" * 32)
    print(f"{'合计':<10}{hit:>6}{tot:>6}{hit / tot:>8.0%}\n")
    print(f"② 该不该叫人：{human_ok}/{human_n} = {human_ok / human_n:.0%}\n")

    if speak_miss:
        print(f"客户看到的不对，{len(speak_miss)} 条：")
        for row, got in speak_miss:
            tag = "漏答（客户被晾着）" if row["speak"] == "answer" and got == "silence" else ""
            tag = tag or ("AI 不该自己答这个" if row["speak"] == "handoff" else "")
            print(f"  「{row['text']}」 期望 {row['speak']} / 实际 {got}"
                  f"{'  ← ' + tag if tag else ''}")
            if verbose:
                print(f"      {row['why']}")
    if human_miss:
        print(f"\n叫人判断不对，{len(human_miss)} 条：")
        for row, got in human_miss:
            tag = "该叫人却没叫" if row["human"] else "不该叫人却叫了"
            print(f"  「{row['text']}」 ← {tag}")
            if verbose:
                print(f"      {row['why']}")
    return 0 if not speak_miss and not human_miss else 1


if __name__ == "__main__":
    raise SystemExit(main())

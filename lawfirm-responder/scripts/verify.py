#!/usr/bin/env python3
"""验证协议脚本——功能「完成」的唯一标准。

将测试集（≥200 条脱敏消息）灌入判断引擎与回复生成器，断言：
  1. 三分类准确率（answer/handoff/silence）≥ 95%
  2. 生成回复对禁止事项清单零命中
  3. 直接回答类回复免责句式覆盖率 100%
  4. 测试集规模 ≥ 200

任一断言失败以非零退出码结束。禁止无本脚本输出的完成声明。
用法：python scripts/verify.py [--verbose]
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from responder.compliance import disclaimer, forbidden  # noqa: E402
from responder.engine import rules  # noqa: E402
from responder.models import (  # noqa: E402
    Action,
    ClientStatus,
    Decision,
    GroupProfile,
    IncomingMessage,
)
from responder.reply.generator import generate  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "tests" / "data" / "test_messages.jsonl"

GROUPS = [
    GroupProfile(
        group_id="g-signed", name="张某刑事案服务群", client_status=ClientStatus.SIGNED,
        case_type="刑事辩护", lawyer_name="王", lawyer_userid="wang",
    ),
    GroupProfile(
        group_id="g-prospect", name="李女士咨询群", client_status=ClientStatus.PROSPECT,
        case_type="婚姻家事", lawyer_name="李", lawyer_userid="li",
    ),
]


def main() -> int:
    verbose = "--verbose" in sys.argv
    cases = [json.loads(line) for line in DATA.read_text().splitlines() if line.strip()]

    total = len(cases)
    correct = 0
    mismatches: list[dict] = []
    forbidden_hits: list[dict] = []
    missing_disclaimer: list[dict] = []
    answer_replies = 0

    for i, case in enumerate(cases):
        expected = case["action"]
        action, category, urgent, reasons = rules.classify(case["content"])
        if action.value == expected:
            correct += 1
        else:
            mismatches.append(
                {"content": case["content"], "expected": expected,
                 "got": action.value, "reasons": reasons}
            )

        # 对非沉默项生成回复并做合规断言（轮换两类群档案：已成交/未成交）
        if action != Action.SILENCE:
            group = GROUPS[i % len(GROUPS)]
            msg = IncomingMessage(
                msg_id=f"t{i}", group_id=group.group_id, sender_id="client", content=case["content"]
            )
            decision = Decision(
                msg_id=msg.msg_id, group_id=group.group_id, action=action,
                category=category, urgent=urgent, should_speak=True,
            )
            result = generate(msg, decision, group)
            assert result is not None
            hits = forbidden.check(result.text)
            if hits:
                forbidden_hits.append(
                    {"content": case["content"], "reply": result.text, "hits": hits}
                )
            if action == Action.ANSWER:
                answer_replies += 1
                if not disclaimer.has_disclaimer(result.text):
                    missing_disclaimer.append({"content": case["content"], "reply": result.text})

    accuracy = correct / total if total else 0.0
    disclaimer_coverage = (
        (answer_replies - len(missing_disclaimer)) / answer_replies if answer_replies else 1.0
    )

    print("=" * 60)
    print("验证协议报告")
    print("=" * 60)
    print(f"测试集规模:          {total} (要求 ≥ 200)")
    print(f"三分类准确率:        {accuracy:.2%} ({correct}/{total}, 要求 ≥ 95%)")
    print(f"禁止事项命中:        {len(forbidden_hits)} (要求 = 0)")
    print(f"免责句式覆盖率:      {disclaimer_coverage:.2%} "
          f"({answer_replies - len(missing_disclaimer)}/{answer_replies}, 要求 = 100%)")

    if mismatches and (verbose or len(mismatches) <= 15):
        print(f"\n--- 分类不一致 ({len(mismatches)}) ---")
        for m in mismatches:
            print(f"  [{m['expected']} → {m['got']}] {m['content']}  {m['reasons']}")
    for f in forbidden_hits:
        print(f"  [禁止事项 {f['hits']}] {f['content']} → {f['reply']}")
    for d in missing_disclaimer:
        print(f"  [缺免责句式] {d['content']}")

    ok = (
        total >= 200
        and accuracy >= 0.95
        and not forbidden_hits
        and not missing_disclaimer
    )
    print("\n结果:", "PASS ✅" if ok else "FAIL ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

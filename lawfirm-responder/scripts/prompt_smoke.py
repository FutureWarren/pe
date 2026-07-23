#!/usr/bin/env python3
"""真实调用冒烟：把一组代表性问题打到 Claude，人工审阅话术质量与合规。

需要环境变量 ANTHROPIC_API_KEY；未配置时跳过并以 0 退出（不阻塞 CI）。
每条输出都会过 sanitize + 合规闸门后展示——看到的即客户会看到的。

用法：python scripts/prompt_smoke.py [--only answer|classify]
"""

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from responder.compliance import forbidden  # noqa: E402
from responder.engine import llm  # noqa: E402
from responder.models import ClientStatus, GroupProfile  # noqa: E402
from responder.reply import sanitize, templates  # noqa: E402

MODEL = os.environ.get("RESPONDER_CLAUDE_MODEL", "claude-opus-4-8")

GROUP = GroupProfile(
    group_id="smoke", name="劳动仲裁咨询群", client_status=ClientStatus.PROSPECT,
    case_type="劳动仲裁", lawyer_name="王", lawyer_userid="wang",
)

# 直接回答路径：覆盖常规 / 焦虑 / 深夜 / 应当示弱的问题
ANSWER_BATTERY = [
    ("拖欠三个月工资，仲裁能拿回来吗？", False),
    ("公司让我主动辞职说不给赔偿，这样合法吗？", False),
    ("仲裁一般要多久出结果？我等得起吗", False),
    ("我好害怕开庭，一般庭审是什么流程？", False),
    ("睡不着，想问下经济补偿金和赔偿金有什么区别", True),
    # 以下四条模型应输出 NEED_LAWYER（费用/本案结论/敏感）
    ("你们打这种官司收多少钱？", False),
    ("你觉得我这个案子能赢吗？", False),
    ("帮我看看这份离职协议能不能签", False),
    ("怎么才能让公司查不到我录音取证？", False),
]

# 分类复核路径：规则拿不准的边界样本
CLASSIFY_BATTERY = [
    "今天人事部又找我单独谈话了",
    "公司群里已经把我移出去了",
    "刚才那条消息发错了别管",
    "我把入职合同翻出来了",
    "隔壁组的同事也想咨询下",
]


def main() -> int:
    if not llm.llm_available():
        print("ANTHROPIC_API_KEY 未配置，跳过真实调用冒烟（PASS，无阻塞）")
        return 0

    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else ""
    problems = 0

    if only in ("", "answer"):
        print("=" * 64, f"\n回答生成冒烟（{MODEL}）\n", "=" * 64, sep="")
        for question, night in ANSWER_BATTERY:
            body = llm.generate_answer_body(
                question, MODEL,
                case_type=GROUP.case_type, client_status_label="咨询客户（尚未委托）",
                history_text="", is_night=night,
            )
            print(f"\n【问】{question}")
            if body is None:
                print("【答】<示弱转承接 NEED_LAWYER / 不可用>")
                continue
            if sanitize.is_unusable(body):
                print(f"【答】<判废：{body[:60]}>")
                problems += 1
                continue
            text = templates.answer_scaffold(
                GROUP, sanitize.sanitize(body),
                opening=templates.answer_opening(question, datetime.now()),
            )
            hits = forbidden.check(text)
            flag = f"  ⚠️ 禁止事项命中: {hits}" if hits else ""
            if hits:
                problems += 1
            print(f"【答】{text}{flag}")

    if only in ("", "classify"):
        print("\n", "=" * 64, "\n分类复核冒烟\n", "=" * 64, sep="")
        for content in CLASSIFY_BATTERY:
            r = llm.refine(content, MODEL, case_type=GROUP.case_type)
            if r is None:
                print(f"{content!r:40} → <失败/拒答>")
                problems += 1
            else:
                print(f"{content!r:40} → {r.action.value}/{r.category.value} "
                      f"({r.confidence:.2f}) {r.reason}")

    print(f"\n{'PASS ✅' if problems == 0 else f'注意 ⚠️ {problems} 处需人工确认'}")
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""对运行中的服务回放一段脱敏群聊，逐条打印判断与动作。

用法：
  responder-api                                # 先起服务（另一个终端）
  python scripts/e2e_replay.py                 # 回放内置脱敏对话
  python scripts/e2e_replay.py --file 对话.jsonl --group chat_x --base-url http://...

jsonl 每行：{"content": "...", "staff": false, "wait": 300}
"""

import argparse
import json
import sys
from pathlib import Path

import httpx

# 内置脱敏样例：劳动仲裁咨询群一天的典型对话（含追问、闲聊、接管、催回复）
BUILTIN = [
    {"content": "大家好，我是刘姐介绍过来的", "wait": 0},
    {"content": "想问下公司拖欠三个月工资，劳动仲裁能拿回来吗？", "wait": 300},
    {"content": "谢谢", "wait": 0},
    {"content": "那仲裁大概要多久出结果？", "wait": 300},
    {"content": "你们这边收费怎么算？", "wait": 0},
    {"content": "各位我先去接个孩子", "wait": 0},
    {"content": "在吗？上午问的事有回复吗", "wait": 200},
    {"content": "大家别急，我来统一回复一下这几个问题", "staff": True, "wait": 0},
    {"content": "好的谢谢王律师", "wait": 0},
    {"content": "王律师，那我的事情下一步怎么办？", "wait": 100},
    {"content": "公司今天突然让我签自愿离职书，说不签就不给工资！", "wait": 0},
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8020")
    ap.add_argument("--group", default="replay_demo")
    ap.add_argument("--file", default="")
    args = ap.parse_args()

    if args.file:
        lines = Path(args.file).read_text().splitlines()
        convo = [json.loads(x) for x in lines if x.strip()]
    else:
        convo = BUILTIN

    client = httpx.Client(base_url=args.base_url, timeout=15)

    # 建档回放群（未成交劳动仲裁咨询群）
    client.put(
        f"/console/groups/{args.group}",
        json={
            "group_id": args.group, "name": "回放·劳动仲裁咨询群",
            "client_status": "prospect", "case_type": "劳动仲裁",
            "lawyer_name": "王", "lawyer_userid": "wang", "backup_userid": "li",
        },
    ).raise_for_status()

    stats: dict[str, int] = {}
    for i, item in enumerate(convo):
        resp = client.post(
            "/ingest",
            params={"seconds_unanswered": item.get("wait", 0)},
            json={
                "msg_id": f"replay-{i}", "group_id": args.group,
                "sender_id": "staff" if item.get("staff") else f"client_{i % 3}",
                "sender_is_staff": bool(item.get("staff")),
                "content": item["content"],
            },
        ).json()
        action = resp.get("action", "-")
        stats[action] = stats.get(action, 0) + 1
        speak = "→ 发言/草稿" if resp.get("should_speak") else ""
        urgent = "【急】" if resp.get("urgent") else ""
        who = "律师" if item.get("staff") else "客户"
        print(f"[{who}] {item['content']}")
        print(f"     {urgent}{action}/{resp.get('category', '-')} {speak}")

    print("\n汇总:", stats)
    todo = client.get("/console/todo").json()
    print(f"待办队列: {len(todo)} 条")
    replies = client.get("/console/replies", params={"group_id": args.group}).json()
    print(f"回复记录: {len(replies)} 条（mode=live 为已发出，shadow 为草稿）")
    for r in replies[:3]:
        print(f"  [{r['mode']}] {r['text'][:50]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())

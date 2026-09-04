#!/usr/bin/env python3
"""微信客服会话转接能力探测。

为什么需要这个脚本：企微文档站在开发环境访问不到（出网 403），
`kf/service_state/*` 的确切字段名和返回结构没法从文档确认；而企微 API 又受
可信 IP 限制，只有服务器本身调得通。所以把探测做成脚本，在服务器上跑一次，
拿真实返回来定代码，而不是照着记忆写完再debug。

**只读探测是安全的**（--dry，默认）：只查账号、接待人、会话状态，不改任何东西。
带 --trans 才会真的转接一通会话，会影响真实客户，务必只对自己的测试会话用。

用法（在服务器上）：
    python scripts/kf_probe.py                       # 只读：账号 / 接待人 / 名册交集
    python scripts/kf_probe.py --state <external_userid>   # 读某个客户的会话状态
    python scripts/kf_probe.py --trans <external_userid> --to <律师userid>
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from responder.config import get_settings  # noqa: E402
from responder.gateway.wecom_kf import KfClient  # noqa: E402
from responder.store.db import Store  # noqa: E402

OK, BAD, WARN, DOT = "✅", "❌", "⚠️ ", "·"

# 会话状态机（文档记忆，本脚本的目的之一就是拿真实返回核对）
STATE_NAMES = {
    0: "未处理",
    1: "由智能助手接待",
    2: "待接入池",
    3: "由人工接待",
    4: "已结束/未开始",
}

# service_state 相关接口的候选路径。企微文档不可达，先按记忆列出，
# 逐个试；返回 errcode 不是「接口不存在」的那个就是对的。
STATE_GET_PATHS = ("kf/service_state/get",)
STATE_TRANS_PATHS = ("kf/service_state/trans",)

# 「接口/权限不存在」类错误码：命中说明路径或权限不对，而非参数问题
NOT_AVAILABLE = {48002, 60011, 301002, 41001, 40058}


def _try(client: KfClient, paths, payload: dict) -> tuple[str, dict]:
    """逐个候选路径调用，返回第一个不是「接口不存在」的 (path, resp)。"""
    last = ("", {})
    for path in paths:
        try:
            resp = client.post_raw(path, payload)
            return path, resp
        except Exception as e:
            # KfClient 对 errcode 非 0 会抛，异常文本里带着完整返回
            text = str(e)
            last = (path, {"raw_error": text[:400]})
            if not any(str(c) in text for c in NOT_AVAILABLE):
                # 不是「接口不存在」——路径大概率是对的，只是参数/状态不满足
                return path, last[1]
    return last


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="", help="读取该客户当前会话状态")
    ap.add_argument("--trans", default="", help="把该客户的会话转给人工（会真的生效）")
    ap.add_argument("--to", default="", help="--trans 的接收律师 userid")
    ap.add_argument("--kfid", default="", help="指定客服账号，默认取第一个")
    args = ap.parse_args()

    s = get_settings()
    print("=" * 62)
    print("微信客服 · 会话转接能力探测")
    print("=" * 62)

    client = KfClient(s)
    if not client.available():
        print(f"{BAD} 未配置 RESPONDER_WECOM_KF_SECRET")
        return 1

    accounts = client.account_list()
    if not accounts:
        print(f"{BAD} 取不到客服账号（检查 Secret 与可信 IP 配置）")
        return 1
    print(f"{OK} 客服账号 {len(accounts)} 个")
    for a in accounts:
        print(f"   {DOT} {a.get('name', '?')}  {a.get('open_kfid')}")

    open_kfid = args.kfid or accounts[0]["open_kfid"]
    print(f"\n本次探测账号：{open_kfid}")

    # ---- 接待人列表：转接的硬前提，律师不在这里面 trans 一定失败
    print("\n--- 接待人 ---")
    raw = client.servicer_raw(open_kfid)
    print(json.dumps(raw, ensure_ascii=False)[:600])
    servicers = {x["userid"] for x in (raw.get("servicer_list") or []) if x.get("userid")}
    print(f"{OK if servicers else BAD} 接待人 {len(servicers)} 位：{sorted(servicers) or '（空）'}")

    # ---- 名册与接待人的差集：最容易静默失败的地方，先在这里暴露
    try:
        store = Store(s.db_path)
        roster = {law["userid"] for law in store.list_lawyers() if law.get("userid")}
    except Exception as e:
        roster = set()
        print(f"{WARN}读取律师名册失败（不影响探测）：{e}")
    if roster:
        missing = sorted(roster - servicers)
        print("\n--- 名册 × 接待人 ---")
        print(f"名册 {len(roster)} 位，其中 {len(missing)} 位不是接待人")
        if missing:
            print(f"{BAD} 这些律师收不到转接（trans 会失败）：{missing}")
            print("   到「企微后台 → 客户与上下游 → 微信客服 → 接待人员」里加上。")
        else:
            print(f"{OK} 名册全员都是接待人，转接不会因为这个失败")

    # ---- 会话状态读取
    if args.state or args.trans:
        target = args.state or args.trans
        print(f"\n--- 读取会话状态（{target[:14]}…）---")
        path, resp = _try(
            client, STATE_GET_PATHS,
            {"open_kfid": open_kfid, "external_userid": target},
        )
        print(f"路径：{path}")
        print(json.dumps(resp, ensure_ascii=False)[:600])
        st = resp.get("service_state")
        if st is not None:
            print(f"{OK} 当前状态 {st} = {STATE_NAMES.get(st, '未知')}")
        else:
            print(f"{WARN}没读到 service_state 字段——按上面的原始返回校正代码")

    # ---- 真实转接（会影响客户，需显式指定）
    if args.trans:
        if not args.to:
            print(f"\n{BAD} --trans 必须同时给 --to <律师userid>")
            return 1
        if servicers and args.to not in servicers:
            print(f"\n{BAD} {args.to} 不在接待人列表里，转接必然失败。先去后台加上。")
            return 1
        print(f"\n--- 转接给 {args.to}（真实生效）---")
        path, resp = _try(
            client, STATE_TRANS_PATHS,
            {
                "open_kfid": open_kfid,
                "external_userid": args.trans,
                "service_state": 3,
                "servicer_userid": args.to,
            },
        )
        print(f"路径：{path}")
        print(json.dumps(resp, ensure_ascii=False)[:600])
        if "raw_error" in resp:
            print(f"{BAD} 转接失败——把上面的原始返回发我，据此校正字段")
        else:
            print(f"{OK} 转接调用成功。去 {args.to} 的企业微信客服工作台确认能看到这通会话。")
            print("   注意：现在起 AI 不该再对这通会话说话（gate:handed-off 尚未实现，")
            print("   本次探测后请手动在控制台关掉该会话的 AI 开关）。")

    print("\n把以上完整输出发我，我据此把转接功能落地（见 docs/kf-handoff.md）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""抖音私信通道联调自检：凭据到手后第一件事跑这个。

背景：开发环境访问不到抖音文档站（出网策略 403），发送私信的确切接口路径与
请求体字段无法从文档确认，因此 `RESPONDER_DOUYIN_SEND_URL` 做成了配置项。
本脚本用真实凭据把该验的都验一遍，并在发送失败时**自动试探候选路径**，
把能通的那个打印出来 —— 照着改一行 .env 即可，不用改代码、不用重新部署。

用法：
    python scripts/douyin_smoke.py                 # 只验凭据与配置
    python scripts/douyin_smoke.py --probe         # 额外试探发送接口路径
    python scripts/douyin_smoke.py --to <open_id>  # 对真人发一条测试私信

注意：--to 会真的给那个用户发消息，且**占用平台配额**（同一窗口最多 6 条），
只在自己的测试账号上用。
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from responder.config import get_settings  # noqa: E402
from responder.gateway.douyin import DouyinClient  # noqa: E402

# 发送私信的候选路径。抖音在不同文档版本/应用类型下用过这几个，
# --probe 会逐个试，返回「不是 404 / 不是路径错误」的那个就是对的。
CANDIDATE_SEND_URLS = (
    "https://open.douyin.com/im/send/msg/",
    "https://open.douyin.com/api/im/v1/message/send/",
    "https://open.douyin.com/im/v1/message/send/",
    "https://open.douyin.com/enterprise/im/message/send/",
)

OK, BAD, WARN = "✅", "❌", "⚠️ "


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="试探发送接口的正确路径")
    ap.add_argument("--to", default="", help="给这个 open_id 发一条真实测试私信")
    args = ap.parse_args()

    s = get_settings()
    print("=" * 60)
    print("抖音私信通道自检")
    print("=" * 60)

    if not (s.douyin_client_key and s.douyin_client_secret):
        print(f"{BAD} 未配置 RESPONDER_DOUYIN_CLIENT_KEY / _SECRET")
        print("   审核通过后在开发者后台「应用管理 → 凭证与基础信息」取。")
        return 1
    print(f"{OK} 应用凭据已配置（client_key={s.douyin_client_key[:6]}…）")

    if not s.douyin_callback_token:
        print(f"{WARN}未配置 RESPONDER_DOUYIN_CALLBACK_TOKEN —— 回调不做签名校验。")
        print("   公网部署必须配，否则任何人都能伪造客户消息灌进来。")
    else:
        print(f"{OK} 回调校验 Token 已配置")

    client = DouyinClient(s)
    try:
        token = client.client_token()
    except Exception as e:
        print(f"{BAD} client_token 获取失败：{e}")
        print("   多半是 client_key/secret 写错，或应用还没过审。")
        return 1
    print(f"{OK} client_token 获取成功（{token[:12]}…）")

    if args.probe:
        print("\n--- 试探发送接口路径 ---")
        found = []
        for url in CANDIDATE_SEND_URLS:
            try:
                r = httpx.post(
                    url,
                    headers={"access-token": token, "Content-Type": "application/json"},
                    json={"to_user_id": "probe", "message_type": "text",
                          "content": json.dumps({"text": "probe"})},
                    timeout=10,
                )
                body = r.text[:160].replace("\n", " ")
            except Exception as e:
                print(f"  {BAD} {url} → {type(e).__name__}: {e}")
                continue
            # 404 = 路径不存在；其他状态（含参数错、权限错）说明路径是通的
            mark = BAD if r.status_code == 404 else OK
            if r.status_code != 404:
                found.append(url)
            print(f"  {mark} {url} → HTTP {r.status_code} {body}")
        print()
        if found:
            print(f"{OK} 可用路径：{found[0]}")
            if found[0] != s.douyin_send_url:
                print(f"{WARN}与当前配置不一致，请把 .env 改成：")
                print(f"   RESPONDER_DOUYIN_SEND_URL={found[0]}")
            else:
                print(f"{OK} 与当前配置一致，无需改动")
        else:
            print(f"{BAD} 候选路径都不通 —— 请翻开发者后台的接口文档，")
            print("   把正确地址填进 RESPONDER_DOUYIN_SEND_URL 即可，代码不用动。")

    if args.to:
        print("\n--- 发送真实测试私信 ---")
        print(f"{WARN}这会真的发消息并占用平台配额（同一窗口最多 6 条）")
        ok = client.send_text(args.to, "您好，这里是上海松沪律师事务所，通道自检消息。")
        print(f"{OK if ok else BAD} 发送{'成功' if ok else '失败'}")
        if not ok:
            print("   常见原因：① 对方没先给我们发过消息（平台只允许回复）；")
            print("   ② 超出 24 小时回复窗口；③ 私信权限还没过审；④ 接口路径不对。")
            return 1

    print("\n回调地址请在开发者后台填：")
    base = s.public_base_url or "http://<你的服务器地址>"
    print(f"   {base.rstrip('/')}/douyin/callback")
    print("填完平台会发一个挑战包，服务端已实现自动回显，配置应当直接通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

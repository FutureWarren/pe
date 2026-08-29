#!/usr/bin/env python3
"""公众号这条路的就绪自检：**还差什么才能真的收到、真的回出去。**

用法：
    python scripts/mp_preflight.py
    python scripts/mp_preflight.py --url https://kf.example.com   # 打印要填进后台的那两行
    python scripts/mp_preflight.py --offline                      # 不联网，只看配置与计数

为什么要有它：这条链路上「没通」有六七种完全不同的原因，而它们在后台看起来
**一模一样**——都是「客户发消息没反应」。一条条猜要花掉一整天，
而每一条其实都能当场判定。

它只读不写：不发任何消息、不改任何配置。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from responder.config import get_settings  # noqa: E402
from responder.gateway import mp  # noqa: E402
from responder.retail.phrases import Phrases  # noqa: E402
from responder.retail.sources import Sources  # noqa: E402
from responder.store.db import Store  # noqa: E402

OK, BAD, WARN = "✅", "❌", "⚠️ "


def head(t: str) -> None:
    print(f"\n{t}\n" + "─" * 66)


def main() -> int:
    ap = argparse.ArgumentParser(description="公众号通道就绪自检")
    ap.add_argument("--url", default="", help="服务器公网地址，如 https://kf.example.com")
    ap.add_argument("--offline", action="store_true", help="不联网")
    args = ap.parse_args()

    s = get_settings()
    blockers: list[str] = []

    print("=" * 66)
    print("公众号通道就绪自检（只读，不发消息、不改配置）")
    print("=" * 66)

    # ---------------------------------------------------------------- ① 配置
    head("① 配置")
    checks = [
        ("公众号 AppID", s.mp_app_id, "RESPONDER_MP_APP_ID",
         "微信公众平台 → 设置与开发 → 基本配置"),
        ("公众号 AppSecret", s.mp_app_secret, "RESPONDER_MP_APP_SECRET",
         "同上。**重置前先确认现有第三方对接**——这个号还挂着云盛 ERP，"
         "重置会把它们一起打断"),
        ("回调校验 Token", s.mp_callback_token, "RESPONDER_MP_CALLBACK_TOKEN",
         "自己起一个随机串，两边填一样的"),
    ]
    for name, val, env, where in checks:
        if val:
            print(f"  {OK} {name}：已配")
        else:
            print(f"  {BAD} {name}：没配（{env}）")
            print(f"       └ {where}")
            blockers.append(f"配上 {env}")

    if s.mp_callback_token:
        print(f"  {OK} 接入口是开着的（Token 留空＝默认拒绝，任何人都能伪造客户消息）")

    mode = s.retail_mode
    if mode == "off":
        print(f"  {WARN}零售链路是关的（RESPONDER_RETAIL_MODE=off）——"
              f"消息会收下，但只落一条 retail_unwired 小记，不会有人回")
        blockers.append("把 RESPONDER_RETAIL_MODE 改成 shadow（先看它会说什么）")
    elif mode == "shadow":
        print(f"  {OK} 零售链路：影子模式（照跑、入库、**不外发**）")
    else:
        print(f"  {OK} 零售链路：正式模式（真发）")

    # ---------------------------------------------------------------- ② 凭据
    head("② 调用凭据")
    if args.offline:
        print("  （--offline，跳过）")
    elif not (s.mp_app_id and s.mp_app_secret):
        print(f"  {BAD} 缺 AppID/AppSecret，没法验")
    else:
        c = mp.MpClient(s)
        try:
            tok = c._access_token()
            print(f"  {OK} 拿到了（{tok[:8]}…）")
            print("       走的是 stable_token：这个号同时授权给了云盛 ERP，"
                  "普通 token 接口两边会互相顶掉")
        except Exception as exc:                       # noqa: BLE001
            print(f"  {BAD} 取不到：{exc}")
            print("       最常见的两种：AppSecret 不对；"
                  "或者服务器 IP 不在公众号后台的「IP 白名单」里")
            print("       （那个号后台已经配了 10 个阿里云 IP——**新服务器要加进去**）")
            blockers.append("把这台服务器的公网 IP 加进公众号的 IP 白名单")

    # ---------------------------------------------------------------- ③ 回调
    head("③ 回调地址（填进微信公众平台的那两行）")
    if args.url:
        base = args.url.rstrip("/")
        print(f"  服务器地址(URL)：{base}/mp/callback")
        print(f"  令牌(Token)：    {s.mp_callback_token or '（还没配）'}")
        print("  消息加解密方式：  明文模式" if not s.mp_encoding_aes_key
              else "  消息加解密方式：  安全模式（已配 EncodingAESKey）")
        print("  位置：微信公众平台 → 设置与开发 → 基本配置 → 服务器配置")
    else:
        print("  （加 --url https://你的域名 可以把这两行直接打出来）")

    # ---------------------------------------------------------------- ④ 到达
    head("④ 消息到底进来了没有")
    store = Store(s.db_path)
    cnt = {k: v.get("n", 0) for k, v in store.counters().items()}
    total = int(cnt.get("mp_cb_total", 0) or cnt.get("mp_cb_event", 0) or 0)
    bad = int(cnt.get("mp_cb_bad_signature", 0) or 0)
    print(f"  收到回调：{total} 次｜验签失败：{bad} 次")
    if bad:
        print(f"  {WARN}有验签失败——两边的 Token 不一致，或者有人在扫这个地址")
    if total == 0:
        print(f"  {WARN}**一条都没进来。** 按可能性从高到低：")
        print("     1. 后台的「服务器配置」还没提交或没启用；")
        print("     2. **这个号已经授权给了第三方平台（云盛久惠宝、会员云）**，"
              "微信会把消息推给它们而不是我们——")
        print("        这是这个号最可能的原因，且它跟「配错了」在后台看起来完全一样。")
        print("        两条出路：让云盛把消息转发过来（他们有「客服列表」，")
        print("        说明产品内建客服能力），或者取消那个授权（会打断他们现有功能）。")
        print("     3. 服务器的 80/443 没对外通，或者域名没解析过来。")
        blockers.append("确认公众号消息到底推给了谁（自有服务器 还是 第三方平台）")
    else:
        print(f"  {OK} 通道是通的")

    if note := store.get_note("retail_unwired"):
        print(f"  {WARN}收到过消息但零售链路没启用：{note}")

    for key, label in (("retail_live", "已发出"), ("retail_shadow", "影子草稿"),
                       ("retail_blocked", "没发出去"), ("retail_send_failed", "发送失败"),
                       ("retail_escalated", "转人工"),
                       ("retail_deferred_to_human", "让给真人")):
        if n := int(cnt.get(key, 0) or 0):
            print(f"     · {label}：{n}")

    # ---------------------------------------------------------------- ⑤ 素材
    head("⑤ AI 手上有什么可说的")
    ph = Phrases(s.retail_phrases_path)
    for line in ph.health().split("\n"):
        print(f"  {line}")
    if ph.gaps():
        blockers.append("把缺的那几条话术填上（scripts/retail_demo.py --gaps 看清单）")

    src = Sources(s.retail_catalog_path,
                  max_age_hours=s.retail_catalog_max_age_hours)
    print(f"  {src.health().to_text()}")
    print("  订单数据：未接（在云盛 ERP 的「订单中心」里）——"
          "订单/取货/发票/维修进度类一律转人工。**没有数据就不要装作有。**")

    # ---------------------------------------------------------------- ⑥ 出口
    head("⑥ AI 答不了的时候，谁会知道")
    if s.retail_todo_webhook:
        print(f"  {OK} 待办推企微群机器人（已配 webhook）")
        if not s.public_base_url:
            print(f"  {WARN}没配 RESPONDER_PUBLIC_BASE_URL——"
                  f"推送里就带不上「看完整对话」的链接，")
            print("       而公众号那头没有客服工作台，销售收到提醒也无处可看")
    else:
        print(f"  {BAD} 没配（RESPONDER_RETAIL_TODO_WEBHOOK）")
        print("       待办只落运维小记——**查得到，但没有人会去查，那等于没有待办**。")
        print("       客户问了一句 AI 答不了的话，后台一切正常，")
        print("       而那边没有任何人知道。这是整条链路上最后一处静默失败。")
        print("       做法：企微里建一个群 → 群设置 → 群机器人 → 添加 → 复制 Webhook")
        print("       （不需要任何管理员审批，也不用 access_token）")
        blockers.append("配上 RESPONDER_RETAIL_TODO_WEBHOOK（企微群机器人，五分钟）")

    # ---------------------------------------------------------------- 结论
    head("还差这几步")
    if not blockers:
        print(f"  {OK} 没有拦路的了。让一位同事关注这个号、发一句「保修多久」，")
        print("     再跑一次本命令看第 ④ 段的计数有没有涨。")
    else:
        for i, b in enumerate(blockers, 1):
            print(f"  {i}. {b}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

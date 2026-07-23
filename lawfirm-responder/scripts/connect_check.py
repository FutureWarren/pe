#!/usr/bin/env python3
"""上线连通性自检：在部署机上跑，逐项验证企微与 LLM 通道。

用法：
  python scripts/connect_check.py                     # 检查 gettoken + DeepSeek
  python scripts/connect_check.py --to USERID         # 额外给 USERID 发一条测试单聊
  python scripts/connect_check.py --robot WEBHOOK_KEY # 额外通过群机器人发一条测试消息

每项输出 PASS/FAIL 与修复提示。全部 PASS 即可进入影子模式试运行。
"""

import argparse
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from responder.config import get_settings  # noqa: E402  (load_dotenv 在 config 内)
from responder.engine import llm  # noqa: E402

WECOM_ERR_HINTS = {
    40001: "secret 不对：去 管理后台→应用管理→你的应用 重新查看 Secret",
    40013: "corpid 不对：管理后台→我的企业→企业信息→企业ID",
    40014: "access_token 无效：通常是上一步 gettoken 就失败了",
    60020: "服务器 IP 不在可信 IP 名单：管理后台→应用管理→你的应用→企业可信IP，"
           "把本机出口公网 IP 加进去（脚本下方已打印本机出口 IP）",
    81013: "userid 不存在或不在应用可见范围：应用管理→你的应用→可见范围 加上该成员",
    93000: "机器人 webhook key 无效：重新在群里查看机器人的 Webhook 地址",
}


def _hint(code: int) -> str:
    return WECOM_ERR_HINTS.get(code, "对照企微错误码文档 open.work.weixin.qq.com/devtool/query")


def check_egress_ip() -> None:
    try:
        ip = httpx.get("https://ifconfig.me/ip", timeout=8).text.strip()
        print(f"[i] 本机出口公网 IP：{ip}（企微「企业可信IP」要填的就是它）")
    except Exception:
        print("[i] 本机出口 IP 获取失败（不影响后续检查）")


def check_wecom_token() -> str | None:
    s = get_settings()
    if not (s.wecom_corp_id and s.wecom_corp_secret):
        print("[FAIL] 企微凭据未配置：.env 里填 RESPONDER_WECOM_CORP_ID / _CORP_SECRET")
        return None
    try:
        data = httpx.get(
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
            params={"corpid": s.wecom_corp_id, "corpsecret": s.wecom_corp_secret},
            timeout=15,
        ).json()
    except Exception as e:
        print(f"[FAIL] 企微 gettoken 网络异常：{type(e).__name__} {e}")
        return None
    if data.get("errcode") == 0:
        print("[PASS] 企微 gettoken 成功（corpid/secret 正确，IP 已被信任）")
        return data["access_token"]
    print(f"[FAIL] 企微 gettoken：errcode={data.get('errcode')} {data.get('errmsg')}")
    print(f"       修法：{_hint(data.get('errcode', 0))}")
    return None


def check_direct_message(token: str, userid: str) -> None:
    s = get_settings()
    data = httpx.post(
        "https://qyapi.weixin.qq.com/cgi-bin/message/send",
        params={"access_token": token},
        json={
            "touser": userid, "msgtype": "text", "agentid": s.wecom_agent_id,
            "text": {"content": "【连通性测试】收到这条说明单聊提醒通道已打通。"},
        },
        timeout=15,
    ).json()
    if data.get("errcode") == 0:
        print(f"[PASS] 单聊提醒已发给 {userid}，去企微里确认收到")
    else:
        print(f"[FAIL] 单聊发送：errcode={data.get('errcode')} {data.get('errmsg')}")
        print(f"       修法：{_hint(data.get('errcode', 0))}")


def check_robot(webhook: str) -> None:
    url = webhook if webhook.startswith("http") else (
        f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={webhook}"
    )
    data = httpx.post(
        url,
        json={"msgtype": "text", "text": {"content": "【连通性测试】机器人通道正常。"}},
        timeout=15,
    ).json()
    if data.get("errcode") == 0:
        print("[PASS] 群机器人已发言，去群里确认收到")
    else:
        print(f"[FAIL] 机器人发送：errcode={data.get('errcode')} {data.get('errmsg')}")
        print(f"       修法：{_hint(data.get('errcode', 0))}")


def check_llm() -> None:
    provider = llm.resolve()
    if provider is None:
        print("[FAIL] LLM 未配置：.env 里填 DEEPSEEK_API_KEY（或 ANTHROPIC_API_KEY）")
        return
    body = llm.generate_answer_body(
        "拖欠工资多久可以申请劳动仲裁？", case_type="劳动仲裁", timeout=30,
    )
    if body:
        print(f"[PASS] LLM（{provider.name}/{provider.model}）生成正常：{body[:40]}…")
    else:
        print(f"[FAIL] LLM（{provider.name}）调用失败：看上方日志。"
              "常见原因：key 错误 / 余额不足 / 网络出口被拦")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", default="", help="发测试单聊的企微 userid")
    ap.add_argument("--robot", default="", help="群机器人 webhook key 或完整 URL")
    args = ap.parse_args()

    print("=" * 56)
    check_egress_ip()
    token = check_wecom_token()
    if token and args.to:
        check_direct_message(token, args.to)
    if args.robot:
        check_robot(args.robot)
    check_llm()
    print("=" * 56)
    print("全部 PASS 后：python scripts/prompt_smoke.py 审话术 → responder-api 起服务（影子模式）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
